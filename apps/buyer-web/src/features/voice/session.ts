/**
 * The session: one socket, one microphone, one playback queue, and the rules between them.
 *
 * Framework-free on purpose. `useVoiceSession` is a thin React wrapper over this class,
 * and everything that is hard to get right -- the order of a barge-in, which generation a
 * late audio chunk belongs to, when `playback_ended` may be sent -- lives here where it can
 * be driven by a test with no DOM, no sound card and no socket.
 *
 * The three rules this file exists to hold:
 *
 *  - **Never withhold microphone frames** (19.6). Not while the assistant speaks, and not
 *    while the buyer is off the talk button. Silence is substituted for the audio, and a
 *    frame of exactly the same length still goes out, because a recognizer stream that
 *    stops receiving audio times out and takes the next utterance down with it.
 *  - **Local action first, server reconciliation second** (19.7), and only ever for
 *    something reversible. Playback is flushed before `barge_in` is sent. No money action
 *    is ever taken optimistically anywhere near this file -- there is no money action in
 *    this file at all.
 *  - **A transcript is intent evidence, never authority evidence** (19.11). This session
 *    can send exactly four things: audio, text, a barge-in and a playback report. Nothing
 *    it can send approves, pays, refunds or cancels.
 */
import { browserMic, silenceFrame, type MicFrame, type MicOptions, type MicSource } from "./capture";
import { PlaybackQueue, webAudioOutput, type AudioOutput } from "./playback";
import {
  dismissDegradation,
  initialTranscriptState,
  reduceTranscript,
  type VoiceTranscriptState,
} from "./transcript";
import {
  encodeClientFrame,
  parseServerFrame,
  TextInputSchema,
  type ClientFrame,
  type SpeechChunkHeader,
} from "./wire";

/* ----------------------------------------------------------------------------- ports */

export interface VoiceSocket {
  send(data: string | ArrayBuffer): void;
  close(): void;
}

export interface VoiceSocketHandlers {
  onOpen(): void;
  onText(data: string): void;
  onBinary(data: ArrayBuffer): void;
  onClose(): void;
}

export type SocketFactory = (url: string, handlers: VoiceSocketHandlers) => VoiceSocket;

/** Audio in and out, sharing one clock. */
export interface AudioIO {
  output: AudioOutput;
  startMic(options: MicOptions): Promise<MicSource>;
  close(): void;
}

export type AudioIOFactory = () => Promise<AudioIO>;

/* ----------------------------------------------------------------------------- state */

export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export type MicState = "off" | "starting" | "live" | "denied" | "failed";

/**
 * Something this CLIENT noticed, as opposed to a `degradation` frame from the server.
 *
 * Kept in the same shape and rendered in the same place, because from where the buyer is
 * standing "the connection dropped" and "speech recognition dropped" are the same event:
 * the thing stopped working and they need to know what still does. Silent degradation is
 * a defect whichever side of the socket noticed it (19.12).
 */
export type ClientNoticeKind =
  | "connection_lost"
  | "microphone_denied"
  | "microphone_failed"
  | "frame_unreadable"
  | "speech_chunk_mismatch";

export interface ClientNotice {
  kind: ClientNoticeKind;
  detail: string;
}

export interface VoiceSessionState {
  connection: ConnectionState;
  /** How many consecutive reconnects have been attempted. Reset by a successful open. */
  reconnectAttempts: number;
  transcript: VoiceTranscriptState;
  mic: MicState;
  /** Which capture path is running, for the diagnostics line. */
  micPath: "audioworklet" | "scriptprocessor" | null;
  /** True while the buyer holds the talk control: real audio is on the wire. */
  transmitting: boolean;
  /** Smoothed level 0..1, for the meter. Zero whenever nothing real is being sent. */
  micLevel: number;
  notice: ClientNotice | null;
}

const initialSessionState: VoiceSessionState = {
  connection: "idle",
  reconnectAttempts: 0,
  transcript: initialTranscriptState,
  mic: "off",
  micPath: null,
  transmitting: false,
  micLevel: 0,
  notice: null,
};

/* ------------------------------------------------------------------------- the session */

export interface VoiceSessionOptions {
  url: string;
  connect?: SocketFactory;
  openAudio?: AudioIOFactory;
}

/** 0.5 s doubling to a 10 s ceiling: no hot loop, and no minute-long silence either. */
const RECONNECT_BACKOFF_START_MS = 500;
const RECONNECT_BACKOFF_MAX_MS = 10_000;
/** The meter is smoothed so it reads as a level rather than as a strobe. */
const LEVEL_SMOOTHING = 0.4;

export class VoiceSession {
  private readonly url: string;
  private readonly connect: SocketFactory;
  private readonly openAudio: AudioIOFactory;

  private state: VoiceSessionState = initialSessionState;
  private readonly listeners = new Set<() => void>();

  private socket: VoiceSocket | null = null;
  private audio: AudioIO | null = null;
  private audioPromise: Promise<AudioIO | null> | null = null;
  private mic: MicSource | null = null;
  private playback: PlaybackQueue | null = null;

  private stopped = true;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private micStarting = false;

  /** The header waiting for its binary frame. The pairing is positional, not keyed. */
  private pendingChunk: SpeechChunkHeader | null = null;

  /** Barge-in accounting: seconds of sustained level above the threshold. */
  private bargeHeldS = 0;
  private bargeSent = false;

  constructor(options: VoiceSessionOptions) {
    this.url = options.url;
    this.connect = options.connect ?? browserSocket;
    this.openAudio = options.openAudio ?? browserAudioIO;
  }

  /* ------------------------------------------------------------------ store interface */

  getState = (): VoiceSessionState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private patch(change: Partial<VoiceSessionState>): void {
    this.state = { ...this.state, ...change };
    for (const listener of this.listeners) listener();
  }

  /* --------------------------------------------------------------------- lifecycle */

  /**
   * Open the socket and the audio device.
   *
   * The `AudioContext` is created here rather than when the microphone starts, because
   * `start` is called from a click and a context created outside a user gesture stays
   * suspended. The microphone itself waits for `session_ready`: its frame size and sample
   * rate are the server's to state, and guessing them is how a client ends up sending
   * 48 kHz audio to a 16 kHz recognizer and blaming the model.
   */
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.patch({ connection: "connecting", notice: null });
    this.openSocket();
    void this.ensureAudio();
  }

  stop(): void {
    this.stopped = true;
    this.clearReconnect();
    this.teardownAudio();
    this.audioPromise = null;
    this.socket?.close();
    this.socket = null;
    this.pendingChunk = null;
    this.patch({ connection: "closed", transmitting: false, mic: "off", micLevel: 0 });
  }

  /**
   * Push-to-talk.
   *
   * Releasing the control does not stop the capture and does not stop sending: it swaps
   * the buyer's audio for digital silence of the same length. Same rule as the server's
   * echo gate, same reason (19.6) -- a recognizer that stops receiving audio times out,
   * and the next thing the buyer says lands on a dead stream.
   */
  setTransmitting(on: boolean): void {
    if (this.state.transmitting === on) return;
    this.bargeHeldS = 0;
    this.patch({ transmitting: on, micLevel: on ? this.state.micLevel : 0 });
  }

  /**
   * Typed input, which is always available -- including while speech recognition is down
   * (19.12). Returns false for text the contract would refuse.
   */
  sendText(text: string): boolean {
    const parsed = TextInputSchema.safeParse({ type: "text_input", text: text.trim() });
    if (!parsed.success) return false;
    return this.send(parsed.data);
  }

  dismissDegradation(id: string): void {
    const next = dismissDegradation(this.state.transcript, id);
    if (next !== this.state.transcript) this.patch({ transcript: next });
  }

  dismissNotice(): void {
    if (this.state.notice) this.patch({ notice: null });
  }

  /* ------------------------------------------------------------------------- socket */

  private openSocket(): void {
    this.socket = this.connect(this.url, {
      onOpen: () => {
        this.patch({ connection: "open", reconnectAttempts: 0, notice: null });
      },
      onText: (data) => this.handleText(data),
      onBinary: (data) => this.handleBinary(data),
      onClose: () => this.handleClose(),
    });
  }

  private handleClose(): void {
    this.socket = null;
    this.pendingChunk = null;
    if (this.stopped) return;
    // The speakers are not fed by a socket that is gone; stop them rather than letting a
    // half-played sentence hang on while the panel says it is reconnecting.
    this.playback?.flush();
    const attempts = this.state.reconnectAttempts + 1;
    const delay = Math.min(
      RECONNECT_BACKOFF_START_MS * 2 ** (attempts - 1),
      RECONNECT_BACKOFF_MAX_MS,
    );
    this.patch({
      connection: "reconnecting",
      reconnectAttempts: attempts,
      notice: {
        kind: "connection_lost",
        detail: `Reconnecting in ${(delay / 1000).toFixed(1)}s (attempt ${attempts}).`,
      },
    });
    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.stopped) return;
      this.patch({ connection: "connecting" });
      this.openSocket();
    }, delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private send(frame: ClientFrame): boolean {
    if (!this.socket) return false;
    this.socket.send(encodeClientFrame(frame));
    return true;
  }

  /* -------------------------------------------------------------------- server frames */

  private handleText(raw: string): void {
    const parsed = parseServerFrame(raw);
    if (!parsed.ok) {
      // One unreadable frame is not a reason to tear down a working conversation, but it
      // is a reason to say so: a silently ignored frame is a silently missing reply.
      this.patch({ notice: { kind: "frame_unreadable", detail: parsed.reason } });
      return;
    }
    const frame = parsed.frame;
    const transcript = reduceTranscript(this.state.transcript, frame);
    if (transcript !== this.state.transcript) this.patch({ transcript });

    switch (frame.type) {
      case "session_ready":
        // Including the `session_ready` that follows a reconnect: generations are numbered
        // per session and start again from zero.
        this.playback?.reset();
        void this.startMic();
        break;
      case "speech_start":
        this.bargeSent = false;
        this.bargeHeldS = 0;
        this.playback?.begin(frame.speech_generation);
        break;
      case "speech_chunk":
        this.pendingChunk = frame;
        break;
      case "speech_end":
        this.playback?.serverFinished(frame.speech_generation);
        break;
      case "interrupted":
        this.playback?.adopt(frame.speech_generation);
        break;
      default:
        break;
    }
  }

  /**
   * The binary frame that follows a `speech_chunk` header, and only that.
   *
   * Audio with no header before it is discarded rather than guessed at. There is no field
   * in the bytes that says which generation or which rate they are, so a chunk that
   * arrived out of order is unplayable by definition -- and playing it anyway is how a
   * cancelled sentence reaches the buyer's ears after they interrupted it.
   */
  private handleBinary(data: ArrayBuffer): void {
    const header = this.pendingChunk;
    this.pendingChunk = null;
    if (!header || !this.playback) return;
    this.playback.enqueue(header, data);
  }

  /* ----------------------------------------------------------------------- audio in */

  /**
   * The audio device, opened at most once.
   *
   * The promise is memoised rather than the result, because `start` opens the device and
   * `session_ready` asks for it again a round trip later -- and a second `AudioContext`
   * would give playback and capture two different clocks, which is exactly the class of
   * bug the scheduling in `playback.ts` exists to avoid.
   */
  private ensureAudio(): Promise<AudioIO | null> {
    if (this.audio) return Promise.resolve(this.audio);
    this.audioPromise ??= this.openAudio().then(
      (audio) => {
        if (this.stopped) {
          audio.close();
          return null;
        }
        this.audio = audio;
        return audio;
      },
      (error: unknown) => {
        this.patch({
          mic: "failed",
          notice: { kind: "microphone_failed", detail: describe(error) },
        });
        return null;
      },
    );
    return this.audioPromise;
  }

  private async startMic(): Promise<void> {
    if (this.mic || this.micStarting || this.stopped) return;
    const ready = this.state.transcript.ready;
    if (!ready) return;

    this.micStarting = true;
    this.patch({ mic: "starting" });
    try {
      const audio = await this.ensureAudio();
      if (!audio || this.stopped) return;

      this.playback ??= new PlaybackQueue({
        output: audio.output,
        leadS: ready.playback_lead_s,
        onGenerationEnded: (speechGeneration) => {
          // The one frame the server cannot infer: the echo tail starts from HERE, when
          // this client's speakers actually fell quiet (19.6).
          this.send({ type: "playback_ended", speech_generation: speechGeneration });
        },
        onMalformedChunk: (chunk, actualBytes) => {
          this.patch({
            notice: {
              kind: "speech_chunk_mismatch",
              detail: `chunk ${chunk.seq} announced ${chunk.byte_length} bytes and carried ${actualBytes}`,
            },
          });
        },
      });

      const mic = await audio.startMic({
        sampleRateHz: ready.input.sample_rate_hz,
        frameMs: ready.mic_frame_ms,
        onFrame: (frame) => this.handleMicFrame(frame),
      });
      if (this.stopped) {
        mic.stop();
        return;
      }
      this.mic = mic;
      this.patch({ mic: "live", micPath: mic.path });
    } catch (error) {
      const denied = error instanceof DOMException && error.name === "NotAllowedError";
      this.patch({
        mic: denied ? "denied" : "failed",
        notice: {
          kind: denied ? "microphone_denied" : "microphone_failed",
          detail: describe(error),
        },
      });
    } finally {
      this.micStarting = false;
    }
  }

  private handleMicFrame(frame: MicFrame): void {
    const transmitting = this.state.transmitting;
    // ALWAYS a frame, of exactly the same length, whether or not it carries the buyer.
    const payload = transmitting ? frame.pcm : silenceFrame(frame.pcm.byteLength);
    this.socket?.send(payload);

    const level = transmitting
      ? this.state.micLevel + (frame.rms - this.state.micLevel) * LEVEL_SMOOTHING
      : 0;
    if (Math.abs(level - this.state.micLevel) > 0.005) this.patch({ micLevel: level });

    this.detectBargeIn(frame, transmitting);
  }

  /**
   * Barge-in, on the client, before the server hears about it (19.7, field guide 5.2).
   *
   * Waiting for a round trip means the assistant talks over the buyer for a full RTT, and
   * an assistant that keeps talking while you interrupt it is the single most disliked
   * thing a voice product does. The sustain window is what separates an interruption from
   * a cough or a chair; both thresholds come from `session_ready`, never from this file.
   */
  private detectBargeIn(frame: MicFrame, transmitting: boolean): void {
    const ready = this.state.transcript.ready;
    if (!ready) return;
    const speaking = this.state.transcript.speaking || (this.playback?.queuedSeconds ?? 0) > 0;

    if (speaking && transmitting && frame.rms > ready.barge_in_level_rms) {
      this.bargeHeldS += frame.durationS;
      if (this.bargeHeldS > ready.barge_in_sustain_s && !this.bargeSent) {
        this.bargeSent = true;
        this.playback?.flush(); // stop OUR audio first
        this.send({ type: "barge_in" }); // and only then tell the server
      }
      return;
    }
    this.bargeHeldS = 0;
  }

  private teardownAudio(): void {
    this.mic?.stop();
    this.mic = null;
    this.playback?.close();
    this.playback = null;
    this.audio?.close();
    this.audio = null;
  }
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

/* --------------------------------------------------------------- browser implementations */

/**
 * The default socket.
 *
 * Same-origin `wss://` by default: the page's Content-Security-Policy names
 * `connect-src 'self'`, and `'self'` covers the WebSocket schemes of the same origin and
 * nothing else. A voice gateway on another host needs that policy widened, deliberately,
 * in `lib/security/csp.ts` -- not a client that quietly points somewhere the policy would
 * refuse.
 */
/**
 * The voice gateway's own origin in local development, or "" in a deployment.
 *
 * The gateway is a separate ASGI process on :8100. `csp.ts` names `ws://127.0.0.1:8100`
 * and its http origin in `connect-src` when `NODE_ENV !== "production"` and omits them
 * otherwise, so the browser can reach it directly while developing and cannot in a
 * deployment -- where the expectation is a reverse proxy in front of the app's own origin.
 *
 * `NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN` overrides both, for a deployment that puts the
 * gateway somewhere else and widens its own policy to match. It is read through
 * `process.env` rather than a runtime lookup because Next inlines `NEXT_PUBLIC_*` at build
 * time, which is also why it cannot be changed without a rebuild.
 */
export function voiceGatewayOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
  if (configured) return configured.replace(/\/+$/, "");
  return process.env.NODE_ENV === "production" ? "" : "http://127.0.0.1:8100";
}

/** Where to mint a voice ticket. Same origin in a deployment, the gateway in development. */
export function defaultTicketUrl(path = "/v1/voice/tickets"): string {
  const origin = voiceGatewayOrigin();
  if (origin) return `${origin}${path}`;
  if (typeof window === "undefined") return "/api/voice/tickets";
  return `${window.location.origin}/api/voice/tickets`;
}

/**
 * Where to open the microphone stream.
 *
 * With a gateway origin configured this addresses it directly and `path` is the gateway's
 * own route. Without one it is same-origin, which is what a reverse-proxied deployment
 * wants and what `connect-src 'self'` permits.
 */
export function defaultVoiceUrl(path = "/api/voice/stream"): string {
  const origin = voiceGatewayOrigin();
  if (origin) return `${origin.replace(/^http/, "ws")}/v1/voice/stream`;
  if (typeof window === "undefined") return path;
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${path}`;
}

export const browserSocket: SocketFactory = (url, handlers) => {
  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  socket.onopen = () => handlers.onOpen();
  socket.onmessage = (event: MessageEvent<string | ArrayBuffer>) => {
    if (typeof event.data === "string") handlers.onText(event.data);
    else handlers.onBinary(event.data);
  };
  socket.onclose = () => handlers.onClose();
  // An error is always followed by a close on a WebSocket, so the reconnect is driven from
  // there alone and this handler exists only to stop the event reaching the console as an
  // unhandled one.
  socket.onerror = () => {};
  return {
    send(data) {
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(data);
    },
    close() {
      socket.onclose = null;
      socket.close();
    },
  };
};

export const browserAudioIO: AudioIOFactory = async () => {
  const context = new AudioContext();
  // Created inside the click that started the session, so this resolves immediately.
  if (context.state === "suspended") await context.resume();
  let closed = false;
  return {
    output: webAudioOutput(context),
    startMic: (options) => browserMic(context, options),
    close() {
      // Idempotent: closing an `AudioContext` twice rejects, and teardown runs from both
      // an explicit stop and an unmount.
      if (closed) return;
      closed = true;
      void context.close();
    },
  };
};

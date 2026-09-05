/**
 * Test doubles for the two things a voice session cannot have in a test: a socket and a
 * sound card.
 *
 * Both record into ONE ordered log. That shared log is the point: the property that
 * matters about a barge-in is not that playback stopped and not that a frame was sent, it
 * is that they happened in that order (19.7). Two separate spies could both be satisfied by
 * an implementation that got the order exactly backwards.
 */
import type { AudioIO, SocketFactory, VoiceSocket, VoiceSocketHandlers } from "../session";
import type { AudioOutput, ScheduledSource } from "../playback";
import type { MicFrame, MicOptions, MicSource } from "../capture";
import type { ServerFrame, SessionReady } from "../wire";

/** One thing that happened, in the order it happened. */
export type Event =
  | { at: "socket"; kind: "text"; body: string }
  | { at: "socket"; kind: "binary"; bytes: number; silent: boolean }
  | { at: "socket"; kind: "close" }
  | { at: "audio"; kind: "schedule"; sampleRateHz: number; startAt: number; bytes: number }
  | { at: "audio"; kind: "stop" }
  | { at: "audio"; kind: "close" };

export class Recorder {
  readonly events: Event[] = [];

  push(event: Event): void {
    this.events.push(event);
  }

  /** Just the shapes, for an ordering assertion that reads like a sentence. */
  get shape(): string[] {
    return this.events.map((event) => {
      if (event.at === "socket" && event.kind === "text") {
        const parsed = JSON.parse(event.body) as { type: string };
        return `send:${parsed.type}`;
      }
      if (event.at === "socket" && event.kind === "binary") {
        return event.silent ? "send:silence" : "send:audio";
      }
      return `${event.at}:${event.kind}`;
    });
  }

  /** Every client frame sent as text, parsed. */
  sentFrames(): Array<Record<string, unknown>> {
    return this.events
      .filter((event): event is Extract<Event, { kind: "text" }> => event.kind === "text")
      .map((event) => JSON.parse(event.body) as Record<string, unknown>);
  }

  clear(): void {
    this.events.length = 0;
  }
}

function allZero(buffer: ArrayBuffer): boolean {
  const bytes = new Uint8Array(buffer);
  for (const byte of bytes) if (byte !== 0) return false;
  return true;
}

export interface FakeSocket extends VoiceSocket {
  handlers: VoiceSocketHandlers;
  /** Deliver a server frame as the socket would. */
  deliver(frame: ServerFrame): void;
  deliverRaw(text: string): void;
  deliverBinary(bytes: number): void;
  open(): void;
  serverClose(): void;
  closed: boolean;
}

export function fakeSocketFactory(recorder: Recorder): {
  connect: SocketFactory;
  /** Every socket the session has opened, in order. Reconnects append. */
  sockets: FakeSocket[];
  latest(): FakeSocket;
} {
  const sockets: FakeSocket[] = [];
  const connect: SocketFactory = (_url, handlers) => {
    const socket: FakeSocket = {
      handlers,
      closed: false,
      send(data) {
        if (typeof data === "string") {
          recorder.push({ at: "socket", kind: "text", body: data });
        } else {
          recorder.push({
            at: "socket",
            kind: "binary",
            bytes: data.byteLength,
            silent: allZero(data),
          });
        }
      },
      close() {
        this.closed = true;
        recorder.push({ at: "socket", kind: "close" });
      },
      open() {
        handlers.onOpen();
      },
      deliver(frame) {
        handlers.onText(JSON.stringify(frame));
      },
      deliverRaw(text) {
        handlers.onText(text);
      },
      deliverBinary(bytes) {
        handlers.onBinary(new ArrayBuffer(bytes));
      },
      serverClose() {
        handlers.onClose();
      },
    };
    sockets.push(socket);
    return socket;
  };
  return { connect, sockets, latest: () => sockets[sockets.length - 1] };
}

export interface FakeAudio {
  io: AudioIO;
  /** Advance the device clock, in seconds. */
  advance(seconds: number): void;
  /** Feed one microphone frame into the session, as the capture path would. */
  frame(options?: Partial<MicFrame>): void;
  /** Finish the nth still-playing chunk, as the device would when it drains. */
  finishOldestChunk(): void;
  scheduledChunks(): number;
  micStarted: boolean;
}

export function fakeAudio(recorder: Recorder): FakeAudio {
  let clock = 0;
  let onFrame: ((frame: MicFrame) => void) | null = null;
  const pending: Array<{ ended: () => void; stopped: boolean }> = [];

  const output: AudioOutput = {
    now: () => clock,
    schedule(pcm, sampleRateHz, at, onEnded) {
      recorder.push({
        at: "audio",
        kind: "schedule",
        sampleRateHz,
        startAt: at,
        bytes: pcm.byteLength,
      });
      const entry = { ended: onEnded, stopped: false };
      pending.push(entry);
      const source: ScheduledSource = {
        stop() {
          entry.stopped = true;
          recorder.push({ at: "audio", kind: "stop" });
        },
      };
      return source;
    },
    close() {
      recorder.push({ at: "audio", kind: "close" });
    },
  };

  const api: FakeAudio = {
    micStarted: false,
    io: {
      output,
      async startMic(options: MicOptions): Promise<MicSource> {
        onFrame = options.onFrame;
        api.micStarted = true;
        return { path: "audioworklet", stop: () => {} };
      },
      close() {},
    },
    advance(seconds) {
      clock += seconds;
    },
    frame(overrides = {}) {
      // Not silent by default: the recorder tells real audio from substituted silence by
      // looking at the bytes, which is also how a server would.
      const pcm = new ArrayBuffer(3200);
      new Uint8Array(pcm).fill(7);
      const frame: MicFrame = { pcm, rms: 0.01, durationS: 0.1, ...overrides };
      onFrame?.(frame);
    },
    finishOldestChunk() {
      const entry = pending.find((candidate) => !candidate.stopped);
      if (!entry) return;
      entry.stopped = true;
      entry.ended();
    },
    scheduledChunks() {
      return pending.length;
    },
  };
  return api;
}

/** A `session_ready` with the constants of `voice_runtime.constants`. */
export function sessionReady(overrides: Partial<SessionReady> = {}): SessionReady {
  return {
    type: "session_ready",
    session_id: "vs_test",
    input: { sample_rate_hz: 16_000, encoding: "pcm16le", channels: 1 },
    output: { sample_rate_hz: 24_000, encoding: "pcm16le", channels: 1 },
    mic_frame_ms: 100,
    echo_tail_s: 0.6,
    barge_in_level_rms: 0.08,
    barge_in_sustain_s: 0.3,
    playback_lead_s: 0.03,
    voice_is_authority: false,
    ...overrides,
  };
}

/** Let every already-resolved promise settle. */
export function settle(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

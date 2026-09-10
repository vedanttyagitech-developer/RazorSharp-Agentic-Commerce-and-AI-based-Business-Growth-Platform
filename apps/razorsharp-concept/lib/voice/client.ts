'use client';
// The voice socket, as the gateway defines it.
//
// One rule shapes this whole file: **a `speech_chunk` header announces exactly one binary
// frame, and it is the next one.** Text and binary arrive on the same socket, so the header
// is held until its audio lands. Anything that reordered them -- a queue, a promise, a
// setState between the two -- would pair a clause with somebody else's audio.
//
// The other rule is about authority. `session_ready` carries `voice_is_authority: false`,
// and this client honours it by never acting on speech: a transcript is text to show, a
// spoken yes is reported to the server which decides what it meant. Nothing here approves,
// pays or cancels anything.

import {conversationTransition, initialConversation, type ConversationEvent, type ConversationState} from './conversation';
import {VOICE_PROTOCOL_VERSION} from './wire';
import type {TurnClosed, TurnOpened, TurnReasoning} from './wire';
import { Microphone, SpeechPlayer } from './audio';
import { ensureBuyerSession } from '../commerce';

export type VoiceTicket = {
  ticket: string;
  expires_in_s: number;
  session_id: string;
  speech_available: boolean;
  socket_url: string;
};

type AudioContract = {
  sample_rate_hz: number;
  encoding: string;
  channels: number;
};

export type SessionReady = {
  type: 'session_ready';
  protocol_version: number;
  session_id: string;
  input: AudioContract;
  output: AudioContract;
  mic_frame_ms: number;
  echo_tail_s: number;
  voice_is_authority: false;
};

/** One product a reply put on the page. `unit_price` is the API's own money object. */
export type VoiceItem = {
  sku: string;
  name: string;
  unit_price: { minor: number; currency: string; display: string } | null;
  stock_units?: number;
  available?: boolean;
};

/**
 * The product a reply put forward, and whether the buyer asked for it to be ADDED.
 *
 * `offer` is set both when the buyer asked to add something and when the assistant merely
 * showed them a product -- a spoken "yes" refers to it either way. `isProposal` is the
 * difference, and acting without it means filling the basket every time somebody asks to
 * look at something.
 */
export type VoiceOffer = {
  sku: string;
  name: string;
  quantity: number;
  isProposal: boolean;
  cartId?: string | null;
  absoluteQuantity?: number | null;
  blockedBy?: string | null;
  binding?: {
    basket_content_hash: string | null;
    unit_price_minor: number;
    catalogue_revision: number;
  } | null;
};

/** What the surface is told. Deliberately narrower than the wire: the UI shows, it decides nothing. */
export type VoiceEvents = {
  onConversation?: (state:ConversationState)=>void;
  onReady?: (ready: SessionReady) => void;
  /** Interim text. Replace what is held; never treat as intent. */
  onPartial?: (text: string) => void;
  /** Final text for the turn. `stale` means it aged out and the agent never saw it. */
  onFinal?: (text: string, stale: boolean) => void;
  onReply?: (text: string) => void;
  /** The products this reply put on the page. Empty clears the shelf; the page redraws. */
  onItems?: (items: VoiceItem[]) => void;
  /**
   * The product put forward. Act on it only when `isProposal` -- that is the buyer's own
   * instruction to add it. Adding to a basket is not consent to buy, and nothing here
   * approves, pays or cancels: `session_ready` says `voice_is_authority: false`.
   */
  onOffer?: (offer: VoiceOffer) => void;
  onSpeaking?: (speaking: boolean) => void;
  onAudioStarted?: () => void;
  /** A degraded path, named. The gateway states that typing still works and money did not move. */
  onDegraded?: (kind: string, message: string) => void;
  /**
   * The socket is open but the microphone is not. Typing works; speaking does not.
   *
   * A separate event from `onError`, because it is not one: the buyer declined a
   * permission, or has no device. The session is usable and the surface should say which
   * half of it is missing rather than reporting a failure.
   */
  onMicUnavailable?: (message: string) => void;
  onError?: (message: string) => void;
  onClosed?: (reason: string, retryable?: boolean) => void;
};

const IDLE = 'idle';

/**
 * A wire field, if it is a string, and the fallback otherwise.
 *
 * Not `String(value)`: a field that arrived as an object would become the literal text
 * "[object Object]" and be shown to the buyer as a transcript or spoken aloud as a reply.
 * A frame that does not carry the field it declares is a frame this client cannot act on,
 * and the honest reading of it is the fallback.
 */
function asText(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

/** Sustained post-echo-cancellation speech, not a click or a single loud sample. */
export class BargeInDetector {
  private noise = 0.004;
  private frames: ArrayBuffer[] = [];
  observe(pcm: ArrayBuffer, speaking: boolean, sampleRate: number): ArrayBuffer[] | null {
    if (!pcm.byteLength || pcm.byteLength % 2) return null;
    const samples = new Int16Array(pcm);
    let energy = 0;
    for (const sample of samples) energy += (sample / 32768) ** 2;
    const rms = Math.sqrt(energy / samples.length);
    if (!speaking) {
      this.frames = [];
      // Learn the quiet floor, not the buyer's speaking volume.
      if (rms < 0.02) this.noise = 0.95 * this.noise + 0.05 * rms;
      return null;
    }
    if (rms < Math.max(0.025, this.noise * 4)) {
      this.frames = [];
      return null;
    }
    this.frames.push(pcm.slice(0));
    const duration = this.frames.reduce((total, frame) => total + frame.byteLength / 2, 0) / sampleRate;
    if (duration < 0.3) return null;
    const onset = this.frames;
    this.frames = [];
    return onset;
  }
}

export class VoiceClient {
  private socket: WebSocket | null = null;
  private mic = new Microphone();
  private player: SpeechPlayer | null = null;
  private primedOutput: AudioContext | null = null;
  private pendingChunk: { seq: number; byte_length: number; utterance_id: number } | null = null;
  private generation = 0;
  private outputs = new Map<number, {generation:number; sent:boolean; pending:number; audible:boolean; drained:boolean}>();
  private ready: SessionReady | null = null;
  private closing = false;
  private openingAbort = new AbortController();
  private pendingInterrupts = 0;
  private speechActive = false;
  private bargeDetector = new BargeInDetector();
  /** True once the microphone is actually capturing. False means this is a typing session. */
  listening = false;

  conversation = initialConversation();
  private transition(event:ConversationEvent):void {
    this.conversation=conversationTransition(this.conversation,event);
    this.events.onConversation?.(this.conversation);
  }
  constructor(private readonly events: VoiceEvents = {}) {}

  /** Mint a ticket through this app's own server, which holds the buyer's credential. */
  static async ticket(signal?: AbortSignal): Promise<VoiceTicket> {
    if (typeof window !== 'undefined') await ensureBuyerSession();
    let response = await fetch('/api/voice/ticket', { method: 'POST', signal });
    // The buyer may start speaking before catalogue loading creates the cookie, or
    // return with an expired session. The commerce bridge remains the sole mint owner.
    if (response.status === 409 || response.status === 401) {
      const session = await fetch('/api/commerce/carts/current', { signal });
      if (!session.ok)
        throw new Error(
          'Shopping session unavailable. Reconnect before sending a request.',
        );
      response = await fetch('/api/voice/ticket', { method: 'POST', signal });
    }
    const body = (await response.json()) as Record<string, unknown>;
    if (!response.ok)
      throw new Error(
        typeof body.detail === 'string'
          ? body.detail
          : 'The voice gateway refused a ticket.',
      );
    return body as unknown as VoiceTicket;
  }

  async open(signal?: AbortSignal): Promise<void> {
    if (this.closing)
      throw new DOMException('Voice session closed', 'AbortError');
    // Unlock output during the mic-button gesture, before ticket/network awaits consume
    // transient user activation. The handshake supplies the PCM rate later.
    if (typeof AudioContext !== 'undefined') {
      this.primedOutput = new AudioContext();
      void this.primedOutput.resume().catch(() => {
        if (!this.closing) this.events.onError?.('Sound could not start. Reconnect voice using the microphone button.');
      });
    }
    this.transition({type:'connect'});
    const abort = () => void this.close();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    let ticket: VoiceTicket;
    try {
      ticket = await VoiceClient.ticket(this.openingAbort.signal);
    } finally {
      signal?.removeEventListener('abort', abort);
    }
    if (this.closing)
      throw new DOMException('Voice session closed', 'AbortError');
    const url = `${ticket.socket_url}?ticket=${encodeURIComponent(ticket.ticket)}`;
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onmessage = (event) =>
      void this.receive(event).catch((error) => {
        if (this.closing) return;
        this.events.onError?.(
          `Voice playback or protocol error: ${(error as Error).message}. Reconnect or type instead.`,
        );
        this.events.onClosed?.('audio or protocol failure');
        void this.close();
      });
    socket.onerror = () =>
      this.events.onError?.('The voice connection failed.');
    socket.onclose = (event) => {
      if (!this.closing)
        this.events.onClosed?.(event.reason || 'the connection closed', [1001, 1006, 1011, 1012, 1013].includes(event.code));
      void this.close();
    };

    await new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timeout);
        socket.removeEventListener('error', failed);
        socket.removeEventListener('close', failed);
        this.openingAbort.signal.removeEventListener('abort', failed);
      };
      socket.onopen = () => {
        cleanup();
        resolve();
      };
      const failed = () => {
        cleanup();
        reject(new Error('The voice gateway could not be reached.'));
        void this.close();
      };
      const timeout = setTimeout(() => {
        failed();
        void this.close();
      }, 15_000);
      socket.addEventListener('error', failed, { once: true });
      socket.addEventListener('close', failed, { once: true });
      this.openingAbort.signal.addEventListener('abort', failed, {
        once: true,
      });
    });
  }

  private recognitionReady = false;
  private async receive(event: MessageEvent): Promise<void> {
    if (this.closing) return;
    // Binary: the audio the previous header announced, and nothing else ever.
    if (event.data instanceof ArrayBuffer) {
      const header = this.pendingChunk;
      this.pendingChunk = null;
      if (!header || !this.player) return;
      if (
        event.data.byteLength !== header.byte_length ||
        event.data.byteLength % 2
      )
        throw new Error(
          'Audio frame does not match the announced PCM16 length',
        );
      const output = this.outputs.get(header.utterance_id);
      if (!output || output.generation !== this.generation || this.pendingInterrupts) return;
      output.pending++;
      try {
        await this.player.play(event.data, header.utterance_id, () => {
          if (this.closing || this.outputs.get(header.utterance_id) !== output || output.audible) return;
          output.audible = true;
          this.speechActive = true;
          this.transition({type:'audio_started',utterance_id:header.utterance_id});
          this.events.onSpeaking?.(true);
          this.events.onAudioStarted?.();
          this.send({type:'playback_started',utterance_id:header.utterance_id,speech_generation:output.generation});
        });
      } finally { output.pending--; this.notifyPlaybackEnded(header.utterance_id); }
      return;
    }

    const frame = JSON.parse(String(event.data)) as Record<string, unknown>;
    switch (frame.type) {
      case 'recognition_state': {
        this.recognitionReady = frame.state === 'ready';
        this.transition({type:'capture',active:this.listening && this.recognitionReady});
        break;
      }
      case 'session_ready': {
        if (this.ready) throw new Error('Duplicate voice session handshake');
        const ready = frame as unknown as SessionReady;
        if (
          ready.protocol_version !== VOICE_PROTOCOL_VERSION ||
          ready.voice_is_authority !== false ||
          ready.input.channels !== 1 ||
          ready.output.channels !== 1
        )
          throw new Error('Unsupported voice session contract');
        this.ready = ready;
        this.transition({type:'ready'});
        this.player = new SpeechPlayer(ready.output.sample_rate_hz, (id) =>
          this.playbackEnded(id), this.primedOutput,
        );
        this.primedOutput = null;
        this.events.onReady?.(ready);
        if (this.closing) return;
        // A microphone that will not open costs the buyer speech and nothing else. The
        // socket stays open, typed turns keep working, and replies are still spoken back
        // -- which is specification 19.12's rule applied to the one input this client
        // does not control. Letting this throw took the whole session down and left a
        // surface that had gone quiet for a reason it never explained.
        try {
          await this.mic.start(
            {
              sampleRateHz: ready.input.sample_rate_hz,
              frameMs: ready.mic_frame_ms,
            },
            (pcm) => this.sendAudio(pcm),
            (message) => {
              this.listening = false;
              this.transition({type:"capture",active:false});
              this.events.onMicUnavailable?.(message);
            },
          );
          if (this.closing) {
            await this.mic.stop();
            return;
          }
          this.listening = true;
          this.transition({type:'capture',active:this.recognitionReady});
        } catch (cause) {
          if (this.closing) return;
          this.events.onMicUnavailable?.(
            cause instanceof DOMException && cause.name === 'NotAllowedError'
              ? 'Microphone access was declined, so I cannot listen. Type instead and I will still answer aloud.'
              : 'No microphone is available, so I cannot listen. Type instead and I will still answer aloud.',
          );
        }
        break;
      }
      case 'turn_opened':
      case 'turn_reasoning':
      case 'turn_closed':
        if(!Number.isSafeInteger(frame.intent_id)||Number(frame.intent_id)<1)throw Error('Invalid intent identity');
        if(frame.type==='turn_closed')this.transition({type:frame.type,intent_id:Number(frame.intent_id),outcome:(frame as unknown as TurnClosed).outcome});
        else this.transition({type:frame.type,intent_id:Number(frame.intent_id)} as TurnOpened|TurnReasoning);
        break;
      case 'transcript_partial':
        this.events.onPartial?.(asText(frame.text));
        break;
      case 'transcript_final':
        this.events.onFinal?.(asText(frame.text), Boolean(frame.stale));
        break;
      case 'agent_reply': {
        if (
          this.pendingInterrupts ||
          Number(frame.speech_generation ?? this.generation) < this.generation
        )
          break;
        this.events.onReply?.(asText(frame.text));
        this.events.onItems?.(
          Array.isArray(frame.items) ? (frame.items as VoiceItem[]) : [],
        );
        const offer = frame.offer as Record<string, unknown> | null | undefined;
        if (offer && typeof offer.sku === 'string')
          this.events.onOffer?.({
            sku: offer.sku,
            name: asText(offer.name),
            quantity: Number(offer.quantity ?? 1),
            // Absent reads as "shown, not asked for" -- the safe direction.
            isProposal: frame.offer_is_proposal === true,
            cartId: offer.cart_id as string | null,
            absoluteQuantity: offer.absolute_quantity as number | null,
            blockedBy: offer.blocked_by as string | null,
            binding: offer.binding as VoiceOffer['binding'],
          });
        break;
      }
      case 'speech_start': {
        if (this.pendingInterrupts || Number(frame.speech_generation) < this.generation) break;
        const id = Number(frame.utterance_id);
        if (!Number.isSafeInteger(id) || id < 1 || this.outputs.has(id)) throw Error('Invalid utterance identity');
        this.generation = Number(frame.speech_generation);
        this.outputs.set(id,{generation:this.generation,sent:false,pending:0,audible:false,drained:true});
        this.transition({type:'audio_queued',utterance_id:id});
        break;
      }
      case 'speech_chunk': {
        const id = Number(frame.utterance_id), output = this.outputs.get(id);
        if (this.pendingInterrupts || !output || Number(frame.speech_generation) !== output.generation) break;
        output.drained = false;
        this.pendingChunk = {seq:Number(frame.seq),byte_length:Number(frame.byte_length),utterance_id:id};
        break;
      }
      case 'speech_end': {
        const id = Number(frame.utterance_id), output = this.outputs.get(id);
        if (!output || Number(frame.speech_generation) !== output.generation) break;
        output.sent = true;
        this.notifyPlaybackEnded(id);
        break;
      }
      case 'interrupted':
        this.speechActive = false;
        this.pendingInterrupts = Math.max(0, this.pendingInterrupts - 1);
        this.generation = Number(frame.speech_generation ?? this.generation);
        this.player?.flush();
        this.pendingChunk = null;
        this.outputs.clear();
        this.transition({type:'interrupt'});
        this.events.onSpeaking?.(false);
        break;
      case 'degradation':
        this.events.onDegraded?.(
          asText(frame.kind, IDLE),
          asText(frame.message),
        );
        break;
      case 'error':
        this.events.onError?.(
          asText(frame.message, 'The voice session failed.'),
        );
        break;
      default:
        // Consent and card frames belong to the approval surface, which reads them from
        // the trusted screen rather than from speech. Ignored here on purpose.
        break;
    }
  }

  private sendAudio(pcm: ArrayBuffer): void {
    if (this.socket?.readyState === WebSocket.OPEN && this.recognitionReady) {
      // Do not deliver old microphone audio as a fresh request after a network stall.
      if (this.socket.bufferedAmount > 64_000) {
        this.events.onClosed?.(
          'the network is too slow for live audio; reconnect when it recovers',
        );
        void this.close();
        return;
      }
      const onset = this.bargeDetector.observe(
        pcm, this.speechActive && !this.pendingInterrupts,
        this.ready?.input.sample_rate_hz ?? 16000,
      );
      if (onset) {
        this.bargeIn();
        // The server opens its echo gate on barge_in. Replay just the detected onset
        // so it hears the first word rather than only the rest of the correction.
        for (const frame of onset) this.socket.send(frame);
      } else this.socket.send(pcm);
    }
  }

  private send(frame: Record<string, unknown>): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(frame));
    return true;
  }

  private playbackEnded(id?: number): void {
    if (id === undefined) return;
    const output = this.outputs.get(id);
    if (output) output.drained = true;
    this.notifyPlaybackEnded(id);
  }

  private notifyPlaybackEnded(id: number): void {
    const output = this.outputs.get(id);
    if (!output || !output.sent || !output.drained || output.pending) return;
    this.outputs.delete(id);
    this.transition({type:'audio_finished',utterance_id:id});
    this.speechActive = [...this.outputs.values()].some(row => row.audible);
    this.events.onSpeaking?.(this.speechActive);
    if (output.audible) this.send({type:'playback_ended',utterance_id:id,speech_generation:output.generation});
  }

  /** Typed input. Always available, including while recognition is degraded (19.12). */
  checkoutGuidance(
    checkoutId: string | null | undefined,
    stage: string = 'review',
    version?: number,
  ): void {
    if(checkoutId===undefined){this.send({type:'screen_context',scope:'checkout'});return}
    this.bargeIn();
    this.send({
      type: 'checkout_guidance',
      checkout_id: checkoutId,
      stage,
      version,
    });
  }

  cartUpdated(cartId: string, eventId: string): boolean {
    return this.send({ type: 'cart_updated', cart_id: cartId, event_id: eventId });
  }

  text(value: string): boolean {
    return this.send({ type: 'text_input', text: value });
  }

  /** The buyer started talking over the reply: flush locally first, then tell the server. */
  bargeIn(reason: "speak" | "cancel" = "speak"): void {
    this.speechActive = false;
    this.pendingChunk = null;
    this.outputs.clear();
    this.transition({type:"interrupt"});
    this.player?.flush();
    if (this.send({ type: 'barge_in', reason })) this.pendingInterrupts++;
  }

  get speechContract(): SessionReady | null {
    return this.ready;
  }

  async close(): Promise<void> {
    if (this.closing) return;
    this.closing = true;
    this.transition({type:"close"});
    this.outputs.clear();
    this.openingAbort.abort();
    this.listening = false;
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.pendingChunk = null;
    const player = this.player;
    this.player = null;
    const output = this.primedOutput;
    this.primedOutput = null;
    await Promise.all([this.mic.stop(), player?.close(), output?.close().catch(() => undefined)]);
  }
}

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

import { Microphone, SpeechPlayer } from './audio';

export type VoiceTicket = {
  ticket: string;
  expires_in_s: number;
  session_id: string;
  speech_available: boolean;
  socket_url: string;
};

type AudioContract = { sample_rate_hz: number; encoding: string; channels: number };

export type SessionReady = {
  type: 'session_ready';
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
  binding?: {basket_content_hash:string|null;unit_price_minor:number;catalogue_revision:number} | null;
};

/** What the surface is told. Deliberately narrower than the wire: the UI shows, it decides nothing. */
export type VoiceEvents = {
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
  onClosed?: (reason: string) => void;
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

export class VoiceClient {
  private socket: WebSocket | null = null;
  private mic = new Microphone();
  private player: SpeechPlayer | null = null;
  private pendingChunk: { seq: number; byte_length: number } | null = null;
  private generation = 0;
  private speechFinished = false;
  private playbackDrained = true;
  private notifiedGeneration = -1;
  private ready: SessionReady | null = null;
  private closing = false;
  /** True once the microphone is actually capturing. False means this is a typing session. */
  listening = false;

  constructor(private readonly events: VoiceEvents = {}) {}

  /** Mint a ticket through this app's own server, which holds the buyer's credential. */
  static async ticket(signal?: AbortSignal): Promise<VoiceTicket> {
    let response = await fetch('/api/voice/ticket', { method: 'POST', signal });
    // The buyer may start speaking before catalogue loading creates the cookie, or
    // return with an expired session. The commerce bridge remains the sole mint owner.
    if (response.status === 409 || response.status === 401) {
      const session = await fetch('/api/commerce/carts/current', { signal });
      if (!session.ok) throw new Error('Shopping session unavailable. Reconnect before sending a request.');
      response = await fetch('/api/voice/ticket', { method: 'POST', signal });
    }
    const body = (await response.json()) as Record<string, unknown>;
    if (!response.ok)
      throw new Error(
        typeof body.detail === 'string' ? body.detail : 'The voice gateway refused a ticket.',
      );
    return body as unknown as VoiceTicket;
  }

  async open(signal?: AbortSignal): Promise<void> {
    const ticket = await VoiceClient.ticket(signal);
    const url = `${ticket.socket_url}?ticket=${encodeURIComponent(ticket.ticket)}`;
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onmessage = (event) => void this.receive(event);
    socket.onerror = () => this.events.onError?.('The voice connection failed.');
    socket.onclose = (event) => {
      void this.mic.stop();
      void this.player?.close();
      this.player = null;
      if (!this.closing) this.events.onClosed?.(event.reason || 'the connection closed');
    };

    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve();
      const failed = () => reject(new Error('The voice gateway could not be reached.'));
      socket.addEventListener('error', failed, { once: true });
    });
  }

  private async receive(event: MessageEvent): Promise<void> {
    // Binary: the audio the previous header announced, and nothing else ever.
    if (event.data instanceof ArrayBuffer) {
      const header = this.pendingChunk;
      this.pendingChunk = null;
      if (!header || !this.player) return;
      await this.player.play(event.data);
      return;
    }

    const frame = JSON.parse(String(event.data)) as Record<string, unknown>;
    switch (frame.type) {
      case 'session_ready': {
        const ready = frame as unknown as SessionReady;
        this.ready = ready;
        this.player = new SpeechPlayer(ready.output.sample_rate_hz, () => this.playbackEnded());
        this.events.onReady?.(ready);
        // A microphone that will not open costs the buyer speech and nothing else. The
        // socket stays open, typed turns keep working, and replies are still spoken back
        // -- which is specification 19.12's rule applied to the one input this client
        // does not control. Letting this throw took the whole session down and left a
        // surface that had gone quiet for a reason it never explained.
        try {
          await this.mic.start(
            { sampleRateHz: ready.input.sample_rate_hz, frameMs: ready.mic_frame_ms },
            (pcm) => this.sendAudio(pcm),
          );
          this.listening = true;
        } catch (cause) {
          this.events.onMicUnavailable?.(
            cause instanceof DOMException && cause.name === 'NotAllowedError'
              ? 'Microphone access was declined, so I cannot listen. Type instead and I will still answer aloud.'
              : 'No microphone is available, so I cannot listen. Type instead and I will still answer aloud.',
          );
        }
        break;
      }
      case 'transcript_partial':
        this.events.onPartial?.(asText(frame.text));
        break;
      case 'transcript_final':
        this.events.onFinal?.(asText(frame.text), Boolean(frame.stale));
        break;
      case 'agent_reply': {
        this.events.onReply?.(asText(frame.text));
        this.events.onItems?.(Array.isArray(frame.items) ? frame.items as VoiceItem[] : []);
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
            binding: offer.binding as VoiceOffer["binding"],
          });
        break;
      }
      case 'speech_start':
        this.speechFinished = false;
        this.playbackDrained = true;
        this.generation = Number(frame.speech_generation ?? 0);
        this.events.onSpeaking?.(true);
        break;
      case 'speech_chunk':
        this.playbackDrained = false;
        this.pendingChunk = {
          seq: Number(frame.seq ?? 0),
          byte_length: Number(frame.byte_length ?? 0),
        };
        break;
      case 'speech_end':
        this.speechFinished = true;
        this.notifyPlaybackEnded();
        break;
      case 'interrupted':
        this.generation = Number(frame.speech_generation ?? this.generation);
        this.player?.flush();
        this.events.onSpeaking?.(false);
        break;
      case 'degradation':
        this.events.onDegraded?.(asText(frame.kind, IDLE), asText(frame.message));
        break;
      case 'error':
        this.events.onError?.(asText(frame.message, 'The voice session failed.'));
        break;
      default:
        // Consent and card frames belong to the approval surface, which reads them from
        // the trusted screen rather than from speech. Ignored here on purpose.
        break;
    }
  }

  private sendAudio(pcm: ArrayBuffer): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(pcm);
  }

  private send(frame: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(frame));
  }

  private playbackEnded(): void {
    this.playbackDrained = true;
    this.notifyPlaybackEnded();
  }

  private notifyPlaybackEnded(): void {
    // A network gap between audio chunks is not the end of an utterance.
    if (!this.speechFinished || !this.playbackDrained || this.notifiedGeneration === this.generation) return;
    this.notifiedGeneration = this.generation;
    this.events.onSpeaking?.(false);
    this.send({ type: 'playback_ended', speech_generation: this.generation });
  }

  /** Typed input. Always available, including while recognition is degraded (19.12). */
  checkoutGuidance(checkoutId: string|null, stage: string = 'review', version?:number): void {
    this.bargeIn();
    this.send({type:'checkout_guidance',checkout_id:checkoutId,stage,version});
  }

  text(value: string): void {
    this.send({ type: 'text_input', text: value });
  }

  /** The buyer started talking over the reply: flush locally first, then tell the server. */
  bargeIn(): void {
    this.player?.flush();
    this.send({ type: 'barge_in' });
  }

  get speechContract(): SessionReady | null {
    return this.ready;
  }

  async close(): Promise<void> {
    this.closing = true;
    this.listening = false;
    await this.mic.stop();
    await this.player?.close();
    this.player = null;
    this.socket?.close();
    this.socket = null;
  }
}

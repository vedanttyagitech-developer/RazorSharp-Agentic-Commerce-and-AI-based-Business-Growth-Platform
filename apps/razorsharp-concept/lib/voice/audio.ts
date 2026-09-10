'use client';
// Microphone in, speech out. Everything that touches the Web Audio API lives here.
//
// The gateway's `session_ready` frame states both contracts and this module honours them
// rather than assuming them: 16 kHz PCM16 mono in 100 ms frames going up, 24 kHz PCM16
// coming down. Two separate AudioContexts, because they run at different rates and a single
// context would have to resample one of them.

export type MicContract = { sampleRateHz: number; frameMs: number };

/** A live microphone, cut into frames and handed to `onFrame`. */
export class Microphone {
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private generation = 0;

  async start(
    contract: MicContract,
    onFrame: (pcm: ArrayBuffer) => void,
    onUnavailable?: (message: string) => void,
  ): Promise<void> {
    const generation = this.generation + 1;
    await this.stop();
    if (generation !== this.generation)
      throw new DOMException('Microphone start cancelled', 'AbortError');
    // Ask for the rate the contract names. Browsers usually oblige; the worklet resamples
    // when one does not, so the wire never carries a rate the server was not told about.
    const context = new AudioContext({ sampleRate: contract.sampleRateHz });
    this.context = context;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          // The gateway runs its own echo gate, and these help it rather than replacing it:
          // what the browser cancels never reaches the recogniser at all.
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (generation !== this.generation) {
        stream.getTracks().forEach((track) => track.stop());
        throw new DOMException('Microphone start cancelled', 'AbortError');
      }
      this.stream = stream;
      const unavailable = (message: string) => {
        if (generation !== this.generation) return;
        // A disconnected device must not leave the surface claiming it is listening.
        void this.stop();
        onUnavailable?.(message);
      };
      for (const track of stream.getTracks()) {
        track.onended = () => unavailable(
          'The microphone disconnected or access ended. Reconnect voice after checking your input device. Typing still works.',
        );
      }
      await context.audioWorklet.addModule('/voice-mic-worklet.js');
      if (generation !== this.generation)
        throw new DOMException('Microphone start cancelled', 'AbortError');
      const source = context.createMediaStreamSource(this.stream);
      const node = new AudioWorkletNode(context, 'mic-frames', {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: {
          targetRate: contract.sampleRateHz,
          frameSamples: Math.round(
            (contract.sampleRateHz * contract.frameMs) / 1000,
          ),
        },
      });
      node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
        if (generation === this.generation) onFrame(event.data);
      };
      source.connect(node);
      this.node = node;
      node.onprocessorerror = () => unavailable(
        'Microphone audio processing stopped. Reconnect voice or type your request.',
      );
      if (context.state === 'suspended') await context.resume();
      if (generation !== this.generation)
        throw new DOMException('Microphone start cancelled', 'AbortError');
    } catch (error) {
      if (generation === this.generation) await this.stop();
      throw error;
    }
  }

  /** Stop capturing and release the device, so the browser's recording indicator clears. */
  async stop(): Promise<void> {
    this.generation++;
    const node = this.node,
      stream = this.stream,
      context = this.context;
    this.node = null;
    this.stream = null;
    this.context = null;
    node?.port.close();
    node?.disconnect();
    stream?.getTracks().forEach((track) => track.stop());
    await context?.close().catch(() => undefined);
  }
}

/**
 * Speech playback, scheduled back to back.
 *
 * Chunks arrive faster than they play, so each is scheduled at the end of the last rather
 * than when it lands: playing them on arrival leaves audible seams between clauses. The
 * `onIdle` callback is what the client turns into `playback_ended`, which starts the
 * gateway's echo tail -- the server never assumes it, so it must be reported honestly.
 */
export class SpeechPlayer {
  private context: AudioContext | null = null;
  private playAt = 0;
  private live = new Set<AudioBufferSourceNode>();
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private generation = 0;
  private closed = false;
  private startTimers = new Set<ReturnType<typeof setTimeout>>();
  private pendingByUtterance = new Map<number, number>();

  constructor(
    private readonly sampleRateHz: number,
    private readonly onIdle: (utteranceId?: number) => void,
    context: AudioContext | null = null,
  ) { this.context = context; }

  private ensure(): AudioContext {
    if (!this.context)
      this.context = new AudioContext({ sampleRate: this.sampleRateHz });
    return this.context;
  }

  async play(pcm: ArrayBuffer, utteranceId = 0, onStarted?: () => void): Promise<void> {
    if (this.closed) return;
    if (!pcm.byteLength || pcm.byteLength % 2)
      throw new Error('Invalid PCM16 audio frame');
    const generation = this.generation;
    const context = this.ensure();
    if (context.state === 'suspended') {
      let timeout: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          context.resume(),
          new Promise<never>((_, reject) => {
            timeout = setTimeout(() => reject(new Error('Audio playback is blocked. Press the mic button to reconnect and enable sound.')), 4000);
          }),
        ]);
      } finally { clearTimeout(timeout); }
    }
    if (this.closed || generation !== this.generation) return;

    const samples = new Int16Array(pcm);
    const buffer = context.createBuffer(1, samples.length, this.sampleRateHz);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i += 1)
      channel[i] = samples[i] / 0x8000;

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    const startAt = Math.max(context.currentTime, this.playAt);
    source.start(startAt);
    this.playAt = startAt + buffer.duration;
    this.live.add(source);
    this.pendingByUtterance.set(utteranceId, (this.pendingByUtterance.get(utteranceId) ?? 0) + 1);
    const started = () => {
      if (!this.closed && generation === this.generation && context.state === 'running') onStarted?.();
    };
    if (startAt <= context.currentTime) started();
    else {
      const timer = setTimeout(() => { this.startTimers.delete(timer); started(); }, Math.ceil((startAt-context.currentTime)*1000));
      this.startTimers.add(timer);
    }
    source.onended = () => {
      if (generation !== this.generation || this.closed) return;
      this.live.delete(source);
      const remaining = (this.pendingByUtterance.get(utteranceId) ?? 1) - 1;
      if (remaining <= 0) { this.pendingByUtterance.delete(utteranceId); this.onIdle(utteranceId); }
      else this.pendingByUtterance.set(utteranceId, remaining);
    };
  }

  /**
   * Stop immediately and drop what is queued. This is barge-in: the buyer started talking,
   * and the client flushes first so the interruption is heard as instant -- the server
   * reconciles afterwards (19.7).
   */
  flush(): void {
    this.generation++;
    this.startTimers.forEach(clearTimeout);
    this.startTimers.clear();
    this.pendingByUtterance.clear();
    this.live.forEach((source) => {
      try {
        source.stop();
      } catch {
        // Already ended between the check and the call; nothing to stop.
      }
    });
    this.live.clear();
    this.playAt = 0;
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = null;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.flush();
    const context = this.context;
    this.context = null;
    await context?.close().catch(() => undefined);
  }
}

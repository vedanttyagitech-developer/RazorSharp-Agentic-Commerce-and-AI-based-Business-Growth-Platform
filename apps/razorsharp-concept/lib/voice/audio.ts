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

  async start(contract: MicContract, onFrame: (pcm: ArrayBuffer) => void): Promise<void> {
    // Ask for the rate the contract names. Browsers usually oblige; the worklet resamples
    // when one does not, so the wire never carries a rate the server was not told about.
    const context = new AudioContext({ sampleRate: contract.sampleRateHz });
    this.context = context;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // The gateway runs its own echo gate, and these help it rather than replacing it:
        // what the browser cancels never reaches the recogniser at all.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    await context.audioWorklet.addModule('/voice-mic-worklet.js');
    const source = context.createMediaStreamSource(this.stream);
    const node = new AudioWorkletNode(context, 'mic-frames', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      processorOptions: {
        targetRate: contract.sampleRateHz,
        frameSamples: Math.round((contract.sampleRateHz * contract.frameMs) / 1000),
      },
    });
    node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => onFrame(event.data);
    source.connect(node);
    this.node = node;
    if (context.state === 'suspended') await context.resume();
  }

  /** Stop capturing and release the device, so the browser's recording indicator clears. */
  async stop(): Promise<void> {
    this.node?.port.close();
    this.node?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    await this.context?.close().catch(() => undefined);
    this.node = null;
    this.stream = null;
    this.context = null;
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

  constructor(
    private readonly sampleRateHz: number,
    private readonly onIdle: () => void,
  ) {}

  private ensure(): AudioContext {
    if (!this.context) this.context = new AudioContext({ sampleRate: this.sampleRateHz });
    return this.context;
  }

  async play(pcm: ArrayBuffer): Promise<void> {
    const context = this.ensure();
    if (context.state === 'suspended') await context.resume();

    const samples = new Int16Array(pcm);
    const buffer = context.createBuffer(1, samples.length, this.sampleRateHz);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i += 1) channel[i] = samples[i] / 0x8000;

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    const startAt = Math.max(context.currentTime, this.playAt);
    source.start(startAt);
    this.playAt = startAt + buffer.duration;
    this.live.add(source);
    source.onended = () => {
      this.live.delete(source);
      this.scheduleIdle();
    };
  }

  /** Report idle once the queue has actually drained, not on every chunk boundary. */
  private scheduleIdle(): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => {
      if (this.live.size === 0) this.onIdle();
    }, 60);
  }

  /**
   * Stop immediately and drop what is queued. This is barge-in: the buyer started talking,
   * and the client flushes first so the interruption is heard as instant -- the server
   * reconciles afterwards (19.7).
   */
  flush(): void {
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
  }

  async close(): Promise<void> {
    this.flush();
    await this.context?.close().catch(() => undefined);
    this.context = null;
  }
}

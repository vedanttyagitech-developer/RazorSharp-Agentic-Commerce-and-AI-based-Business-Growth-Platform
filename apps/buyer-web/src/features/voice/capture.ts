/**
 * The microphone: device audio in, 16 kHz PCM16 little-endian mono out, ~100 ms at a time.
 *
 * The recognizer's contract is a MIME string, `audio/pcm;rate=16000`, and the rate travels
 * in it rather than in a config field. There is no negotiation and no fallback: audio that
 * is not exactly this format is transcribed as gibberish or not at all, so this module's
 * whole job is to produce exactly it from whatever rate the device happens to run at.
 *
 * Three constraints are set on `getUserMedia` and all three matter. Echo cancellation,
 * noise suppression and gain control are what make the barge-in threshold meaningful --
 * `barge_in_level_rms` is measured on AEC-processed audio, so a raw microphone would cross
 * it constantly. What browser AEC does NOT do is cancel Web Audio playback (field guide
 * 1.2): the assistant's own voice still returns through the microphone, which is why the
 * server keeps an echo gate and why this client reports when its speakers fall quiet.
 *
 * An AudioWorklet is preferred and a ScriptProcessor is the fallback. The worklet module
 * is built here as a blob rather than served as a file, because this feature owns no
 * static assets; if the page's Content-Security-Policy refuses the blob the fallback picks
 * it up, which is why the fallback exists rather than being dead code kept for old
 * browsers.
 */

/** One microphone frame, in the format the recognizer contract names. */
export interface MicFrame {
  /** PCM16 little-endian mono at the session's input rate. */
  pcm: ArrayBuffer;
  /** Root-mean-square level of this frame, 0..1, on AEC-processed audio. */
  rms: number;
  /** Wall duration of the frame, for sustain accounting in the barge-in detector. */
  durationS: number;
}

export interface MicOptions {
  /** `session_ready.input.sample_rate_hz`. Never assumed to be 16 kHz by this module. */
  sampleRateHz: number;
  /** `session_ready.mic_frame_ms`. */
  frameMs: number;
  onFrame: (frame: MicFrame) => void;
}

export interface MicSource {
  stop(): void;
  /** Which path is actually running, for the diagnostics line in the panel. */
  readonly path: "audioworklet" | "scriptprocessor";
}

export type MicFactory = (options: MicOptions) => Promise<MicSource>;

const BYTES_PER_SAMPLE = 2;
/** Render quanta are 128 samples; batching to this many keeps the message rate sane. */
const WORKLET_BLOCK = 2048;
const SCRIPT_PROCESSOR_BLOCK = 4096;

/**
 * Linear-interpolating resampler with state carried across blocks.
 *
 * The device rate is rarely an integer multiple of the target: 48000 divides by three,
 * 44100 does not. Resampling each block independently drops or duplicates a sample at
 * every boundary, which is audible to a recognizer as a click roughly forty times a second
 * and measurably worsens transcription. So the fractional read position and the last
 * sample of the previous block both survive into the next call.
 */
class Resampler {
  /** Fractional read index, relative to the start of the block being pushed. */
  private position = 0;
  /** Final sample of the previous block, for interpolating across the seam. */
  private previous = 0;
  private readonly ratio: number;

  constructor(inputRateHz: number, outputRateHz: number) {
    this.ratio = inputRateHz / outputRateHz;
  }

  push(block: Float32Array, emit: (sample: number) => void): void {
    if (block.length === 0) return;
    let position = this.position;
    while (position < block.length) {
      const base = Math.floor(position);
      if (base + 1 >= block.length) break; // the interpolation partner is in the next block
      const left = base < 0 ? this.previous : block[base];
      const right = block[base + 1];
      emit(left + (right - left) * (position - base));
      position += this.ratio;
    }
    this.position = position - block.length;
    this.previous = block[block.length - 1];
  }
}

/**
 * Accumulates resampled float samples into fixed-size PCM16 frames.
 *
 * The RMS is computed over exactly the samples of the frame it is reported with, so the
 * barge-in detector gets one level reading per frame duration and its sustain arithmetic
 * is over a known interval rather than over however much audio the device happened to
 * deliver in one callback.
 */
class FrameBuilder {
  private readonly bytes: ArrayBuffer;
  private readonly view: DataView;
  private filled = 0;
  private energy = 0;

  constructor(
    private readonly frameSamples: number,
    private readonly durationS: number,
    private readonly onFrame: (frame: MicFrame) => void,
  ) {
    this.bytes = new ArrayBuffer(frameSamples * BYTES_PER_SAMPLE);
    this.view = new DataView(this.bytes);
  }

  add(sample: number): void {
    const clamped = Math.max(-1, Math.min(1, sample));
    this.view.setInt16(this.filled * BYTES_PER_SAMPLE, Math.round(clamped * 32767), true);
    this.energy += clamped * clamped;
    this.filled += 1;
    if (this.filled < this.frameSamples) return;

    const rms = Math.sqrt(this.energy / this.frameSamples);
    this.filled = 0;
    this.energy = 0;
    this.onFrame({ pcm: this.bytes.slice(0), rms, durationS: this.durationS });
  }
}

/** Float blocks at the device rate in, PCM16 frames at the contract rate out. */
export interface Downsampler {
  push(block: Float32Array): void;
}

/**
 * The whole format conversion, in one object with no browser in it.
 *
 * Both capture paths -- the worklet and the fallback -- hand their float blocks to this,
 * so there is exactly one implementation of "16 kHz PCM16 little-endian mono in ~100 ms
 * frames" and it can be checked sample by sample in a test. A format that is subtly wrong
 * does not fail loudly: it transcribes as nonsense, and every hour spent on it is spent
 * looking at the model.
 */
export function pcm16Downsampler(options: {
  inputRateHz: number;
  outputRateHz: number;
  frameMs: number;
  onFrame: (frame: MicFrame) => void;
}): Downsampler {
  const frameSamples = Math.round((options.outputRateHz * options.frameMs) / 1000);
  const builder = new FrameBuilder(frameSamples, options.frameMs / 1000, options.onFrame);
  const resampler = new Resampler(options.inputRateHz, options.outputRateHz);
  return {
    push(block) {
      resampler.push(block, (sample) => builder.add(sample));
    },
  };
}

/**
 * The worklet, as source text.
 *
 * It does nothing but batch and forward. All the format work happens on the main thread:
 * a bug in a worklet is invisible -- no stack in the console, no breakpoint -- and the
 * cost of the resampler running on the main thread is a few microseconds per block.
 */
const WORKLET_SOURCE = `
class VoiceMicTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(${WORKLET_BLOCK});
    this.filled = 0;
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let index = 0; index < channel.length; index += 1) {
      this.buffer[this.filled] = channel[index];
      this.filled += 1;
      if (this.filled === this.buffer.length) {
        this.port.postMessage(this.buffer.slice(0));
        this.filled = 0;
      }
    }
    return true;
  }
}
registerProcessor("voice-mic-tap", VoiceMicTap);
`;

/** The constraints, in one place, because all three are load-bearing. */
export const MIC_CONSTRAINTS: MediaTrackConstraints = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};

/**
 * Open the microphone against a caller-owned `AudioContext`.
 *
 * The context is not created here and is not closed by `stop()`: the session owns it, and
 * playback shares it so that both sides of the conversation are on one clock.
 */
export async function browserMic(
  context: AudioContext,
  options: MicOptions,
): Promise<MicSource> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS });

  const downsampler = pcm16Downsampler({
    inputRateHz: context.sampleRate,
    outputRateHz: options.sampleRateHz,
    frameMs: options.frameMs,
    onFrame: options.onFrame,
  });
  const consume = (block: Float32Array) => downsampler.push(block);

  const source = context.createMediaStreamSource(stream);
  let blobUrl: string | null = null;
  let worklet: AudioWorkletNode | null = null;
  let processor: ScriptProcessorNode | null = null;
  let sink: GainNode | null = null;

  const stopTracks = () => {
    for (const track of stream.getTracks()) track.stop();
  };

  try {
    blobUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "text/javascript" }));
    await context.audioWorklet.addModule(blobUrl);
    worklet = new AudioWorkletNode(context, "voice-mic-tap");
    worklet.port.onmessage = (event: MessageEvent<Float32Array>) => consume(event.data);
    source.connect(worklet);
  } catch {
    // A CSP that refuses blob workers, or a browser without AudioWorklet. The deprecated
    // path still delivers the same frames; it just does it on the main thread.
    if (worklet) worklet.disconnect();
    worklet = null;
    processor = context.createScriptProcessor(SCRIPT_PROCESSOR_BLOCK, 1, 1);
    processor.onaudioprocess = (event) => consume(event.inputBuffer.getChannelData(0));
    // A ScriptProcessor only runs while it is connected to the destination, and connecting
    // the microphone to the speakers is a feedback loop. A muted gain node in between is
    // the standard way to keep the graph alive without playing the buyer back to
    // themselves.
    sink = context.createGain();
    sink.gain.value = 0;
    source.connect(processor);
    processor.connect(sink);
    sink.connect(context.destination);
  } finally {
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  }

  return {
    path: worklet ? "audioworklet" : "scriptprocessor",
    stop() {
      if (worklet) {
        worklet.port.onmessage = null;
        worklet.disconnect();
      }
      if (processor) {
        processor.onaudioprocess = null;
        processor.disconnect();
      }
      sink?.disconnect();
      source.disconnect();
      stopTracks();
    },
  };
}

/** A frame of digital silence, the same length as a real one. */
export function silenceFrame(byteLength: number): ArrayBuffer {
  return new ArrayBuffer(byteLength);
}

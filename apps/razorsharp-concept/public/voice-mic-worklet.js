// The microphone, cut into the frames the gateway expects.
//
// `SessionReady` states the contract: PCM16LE, mono, 16 kHz, 100 ms per frame. This runs on
// the audio thread because the main thread is not allowed to be late -- a frame delayed by a
// React render is a gap in what the recogniser hears, and the recogniser cannot tell a gap
// from a pause.
//
// Resampling is linear and deliberate. The AudioContext is asked for 16 kHz and usually
// gives it; where the browser insists on its own rate this keeps the contract rather than
// sending audio at a rate the server was never told about, which is heard as a chipmunk or
// a drawl and is diagnosed as a broken recogniser.
class MicFrameProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const { targetRate = 16000, frameSamples = 1600 } = options.processorOptions || {};
    this.targetRate = targetRate;
    this.frameSamples = frameSamples;
    this.ratio = sampleRate / targetRate;
    this.pending = new Float32Array(0);
    this.carry = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    // Resample first, so the buffer below is always counted in target-rate samples.
    let samples;
    if (this.ratio === 1) {
      samples = channel;
    } else {
      const out = new Float32Array(Math.floor((channel.length - this.carry) / this.ratio) + 1);
      let n = 0;
      for (let at = this.carry; at < channel.length; at += this.ratio) {
        const i = Math.floor(at);
        const frac = at - i;
        const a = channel[i];
        const b = i + 1 < channel.length ? channel[i + 1] : a;
        out[n++] = a + (b - a) * frac;
      }
      // Carry the fractional position across the block so the stream does not drift.
      this.carry = this.carry + n * this.ratio - channel.length;
      samples = out.subarray(0, n);
    }

    const merged = new Float32Array(this.pending.length + samples.length);
    merged.set(this.pending);
    merged.set(samples, this.pending.length);

    let offset = 0;
    while (merged.length - offset >= this.frameSamples) {
      const frame = new Int16Array(this.frameSamples);
      for (let i = 0; i < this.frameSamples; i += 1) {
        // Clamp before scaling: a sample outside [-1, 1] wraps rather than clips when it
        // is cast, and a wrap is a loud click the recogniser hears as a consonant.
        const s = Math.max(-1, Math.min(1, merged[offset + i]));
        frame[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(frame.buffer, [frame.buffer]);
      offset += this.frameSamples;
    }
    this.pending = merged.slice(offset);
    return true;
  }
}

registerProcessor('mic-frames', MicFrameProcessor);

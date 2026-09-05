/**
 * The microphone format, checked sample by sample.
 *
 * The recognizer takes `audio/pcm;rate=16000` and nothing else. There is no negotiation,
 * no error and no fallback: audio in the wrong format is transcribed as nonsense, and the
 * time that costs is spent looking at the model rather than at the twenty lines that
 * produced the bytes.
 */
import { describe, expect, it } from "vitest";

import { pcm16Downsampler } from "../capture";
import type { MicFrame } from "../capture";

function collect(inputRateHz: number, blocks: readonly Float32Array[]): MicFrame[] {
  const frames: MicFrame[] = [];
  const downsampler = pcm16Downsampler({
    inputRateHz,
    outputRateHz: 16_000,
    frameMs: 100,
    onFrame: (frame) => frames.push(frame),
  });
  for (const block of blocks) downsampler.push(block);
  return frames;
}

function constantBlock(length: number, value: number): Float32Array {
  return new Float32Array(length).fill(value);
}

describe("pcm16Downsampler", () => {
  it("turns 100 ms of 48 kHz audio into one 16 kHz PCM16 frame", () => {
    const frames = collect(48_000, [constantBlock(4800, 0.5)]);
    expect(frames).toHaveLength(1);
    // 1600 samples of PCM16 mono: two bytes each.
    expect(frames[0].pcm.byteLength).toBe(3200);
    expect(frames[0].durationS).toBeCloseTo(0.1, 6);
  });

  it("writes little-endian samples", () => {
    const frames = collect(16_000, [constantBlock(1700, 1)]);
    const view = new DataView(frames[0].pcm);
    // Full scale is 32767 = 0x7FFF, which is 0xFF 0x7F little-endian.
    expect(view.getUint8(0)).toBe(0xff);
    expect(view.getUint8(1)).toBe(0x7f);
    expect(view.getInt16(0, true)).toBe(32_767);
  });

  it("clamps rather than wrapping when the device overshoots full scale", () => {
    const frames = collect(16_000, [constantBlock(1700, 1.9)]);
    const view = new DataView(frames[0].pcm);
    // Wrapping would put a loud positive sample out as a loud NEGATIVE one: a click on
    // every peak, and a recognizer that hears clipping as consonants.
    expect(view.getInt16(0, true)).toBe(32_767);
  });

  it("reports the level of the frame it came with", () => {
    const quiet = collect(16_000, [constantBlock(1700, 0.02)]);
    const loud = collect(16_000, [constantBlock(1700, 0.5)]);
    expect(quiet[0].rms).toBeCloseTo(0.02, 3);
    expect(loud[0].rms).toBeCloseTo(0.5, 3);
    // The default barge-in threshold sits between the two, which is the point of it.
    expect(quiet[0].rms).toBeLessThan(0.08);
    expect(loud[0].rms).toBeGreaterThan(0.08);
  });

  it("keeps the rate over a non-integer ratio, without drifting across blocks", () => {
    // 44.1 kHz does not divide by 16 kHz. Resampling each block independently drops or
    // duplicates a sample at every seam; carrying the fractional position does not.
    const blocks = Array.from({ length: 10 }, () => constantBlock(4410, 0.25));
    const frames = collect(44_100, blocks);
    // One second of input is 16000 output samples, which is ten 100 ms frames.
    expect(frames).toHaveLength(10);
    for (const frame of frames) expect(frame.pcm.byteLength).toBe(3200);
  });

  it("does not care how the device chops its callbacks up", () => {
    // 128-sample render quanta, the smallest a worklet ever sees.
    const blocks = Array.from({ length: 40 }, () => constantBlock(128, 0.3));
    const frames = collect(48_000, blocks);
    // 40 * 128 input samples at 48 kHz is 5120 / 3 output samples: one whole frame, and
    // the remainder held for the next callback rather than emitted short.
    expect(frames).toHaveLength(1);
    expect(frames[0].pcm.byteLength).toBe(3200);
  });

  it("hands out a fresh buffer per frame", () => {
    const frames = collect(16_000, [constantBlock(3400, 0.4)]);
    expect(frames).toHaveLength(2);
    expect(frames[0].pcm).not.toBe(frames[1].pcm);
  });
});

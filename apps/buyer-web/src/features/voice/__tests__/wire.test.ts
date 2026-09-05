/**
 * The merge rule, and the contract it is written into.
 *
 * The first block is the regression test for `TumjoMainejoMaineTuMeriTuMainu` -- a real
 * transcript from a real build, produced by a client that appended what a streaming
 * recognizer sent instead of replacing it.
 */
import { describe, expect, it } from "vitest";

import { applyStreamText, parseServerFrame } from "../wire";

describe("applyStreamText", () => {
  it("replaces the held text rather than appending to it", () => {
    expect(applyStreamText("Tu", "Tu Meri")).toBe("Tu Meri");
    expect(applyStreamText("Tu Meri", "Tu Mainu")).toBe("Tu Mainu");
  });

  it("does not concatenate a revised hypothesis", () => {
    // The exact sequence that produced the corrupted transcript: each frame is the WHOLE
    // current hypothesis, and the recognizer revised itself twice.
    const frames = ["Tum", "Tumjo", "Tumjo maine", "Jo maine book", "Jo maine book kiya"];
    const settled = frames.reduce((held, incoming) => applyStreamText(held, incoming), "");
    expect(settled).toBe("Jo maine book kiya");
    expect(settled).not.toContain("TumjoMaine");
  });

  it("keeps the held turn when an empty frame arrives", () => {
    expect(applyStreamText("Jo maine book kiya", "")).toBe("Jo maine book kiya");
  });

  it("is empty only when nothing has ever been held", () => {
    expect(applyStreamText("", "")).toBe("");
  });
});

describe("parseServerFrame", () => {
  it("parses a transcript partial and carries its semantics fields", () => {
    const parsed = parseServerFrame(
      JSON.stringify({
        type: "transcript_partial",
        text: "two kilos of onions",
        turn_id: 4,
        stt_generation: 1,
        age_ms: 120,
        cumulative: true,
        revisable: true,
        empty_means: "keep_held",
      }),
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.frame.type).toBe("transcript_partial");
    if (parsed.frame.type !== "transcript_partial") return;
    expect(parsed.frame.cumulative).toBe(true);
    expect(parsed.frame.revisable).toBe(true);
    expect(parsed.frame.empty_means).toBe("keep_held");
  });

  it("refuses a session_ready that omits a tunable rather than guessing at one", () => {
    const parsed = parseServerFrame(
      JSON.stringify({
        type: "session_ready",
        session_id: "vs_1",
        input: { sample_rate_hz: 16000 },
        output: { sample_rate_hz: 24000 },
        mic_frame_ms: 100,
        echo_tail_s: 0.6,
        barge_in_sustain_s: 0.3,
        playback_lead_s: 0.03,
      }),
    );
    expect(parsed.ok).toBe(false);
    if (parsed.ok) return;
    expect(parsed.reason).toContain("barge_in_level_rms");
  });

  it("keeps the input and output rates apart", () => {
    const parsed = parseServerFrame(
      JSON.stringify({
        type: "session_ready",
        session_id: "vs_1",
        input: { sample_rate_hz: 16000, encoding: "pcm16le", channels: 1 },
        output: { sample_rate_hz: 24000, encoding: "pcm16le", channels: 1 },
        mic_frame_ms: 100,
        echo_tail_s: 0.6,
        barge_in_level_rms: 0.08,
        barge_in_sustain_s: 0.3,
        playback_lead_s: 0.03,
        voice_is_authority: false,
      }),
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok || parsed.frame.type !== "session_ready") return;
    expect(parsed.frame.input.sample_rate_hz).toBe(16_000);
    expect(parsed.frame.output.sample_rate_hz).toBe(24_000);
    expect(parsed.frame.voice_is_authority).toBe(false);
  });

  it("reports an unreadable frame instead of throwing", () => {
    expect(parseServerFrame("{not json").ok).toBe(false);
    expect(parseServerFrame(JSON.stringify({ type: "who_knows" })).ok).toBe(false);
  });
});

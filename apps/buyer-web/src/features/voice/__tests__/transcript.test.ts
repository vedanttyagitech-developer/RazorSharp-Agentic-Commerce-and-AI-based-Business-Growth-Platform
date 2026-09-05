/**
 * The reducer: what a stream of frames does to the conversation on screen.
 *
 * Everything here is the cumulative rule (19.5) and the rule that an interim is never
 * intent. Both are protocol-level invariants, so they are tested at the level of frames
 * rather than through a rendered component.
 */
import { describe, expect, it } from "vitest";

import { initialTranscriptState, reduceTranscript, type VoiceTranscriptState } from "../transcript";
import type { ServerFrame } from "../wire";

function partial(text: string, turnId = 1): ServerFrame {
  return {
    type: "transcript_partial",
    text,
    turn_id: turnId,
    stt_generation: 1,
    age_ms: 40,
    cumulative: true,
    revisable: true,
    empty_means: "keep_held",
  };
}

function final(text: string, turnId = 1, stale = false): ServerFrame {
  return {
    type: "transcript_final",
    text,
    turn_id: turnId,
    stt_generation: 1,
    age_ms: stale ? 6_000 : 300,
    stale,
    source: "voice",
    cumulative: true,
    revisable: false,
    empty_means: "keep_held",
  };
}

function fold(frames: readonly ServerFrame[]): VoiceTranscriptState {
  return frames.reduce(reduceTranscript, initialTranscriptState);
}

describe("interim transcripts", () => {
  it("replaces held text rather than appending it", () => {
    const state = fold([partial("Tu"), partial("Tu Meri"), partial("Tu Mainu")]);
    expect(state.held?.text).toBe("Tu Mainu");
  });

  it("does not concatenate across a revision", () => {
    const state = fold([
      partial("Tum"),
      partial("Tumjo"),
      partial("Tumjo maine"),
      partial("Jo maine book kiya"),
    ]);
    expect(state.held?.text).toBe("Jo maine book kiya");
    expect(state.held?.text).not.toContain("Tumjo maine");
  });

  it("keeps the held turn when an empty interim arrives", () => {
    const state = fold([partial("two kilos of onions"), partial("")]);
    expect(state.held?.text).toBe("two kilos of onions");
  });

  it("holds interim text apart from the settled turns, so it is never intent", () => {
    const state = fold([partial("add milk")]);
    expect(state.entries).toHaveLength(0);
    expect(state.held).toEqual({ turnId: 1, text: "add milk" });
  });
});

describe("final transcripts", () => {
  it("closes the turn and settles it", () => {
    const state = fold([partial("two kilos of"), final("two kilos of onions")]);
    expect(state.held).toBeNull();
    expect(state.entries).toHaveLength(1);
    const entry = state.entries[0];
    expect(entry.kind).toBe("buyer");
    expect(entry.text).toBe("two kilos of onions");
  });

  it("keeps the held text when the final itself is empty", () => {
    const state = fold([partial("two kilos of onions"), final("")]);
    expect(state.held).toBeNull();
    expect(state.entries[0].text).toBe("two kilos of onions");
  });

  it("deduplicates an identical consecutive final by content and turn id", () => {
    const state = fold([final("add milk"), final("add milk")]);
    expect(state.entries).toHaveLength(1);
  });

  it("keeps the same words spoken again as a different turn", () => {
    const state = fold([final("add milk", 1), final("add milk", 2)]);
    expect(state.entries).toHaveLength(2);
  });

  it("shows a stale final as heard, and does not treat it as intent", () => {
    const state = fold([final("add milk", 1, true)]);
    const entry = state.entries[0];
    expect(entry.kind).toBe("buyer");
    if (entry.kind !== "buyer") return;
    // Shown -- the buyer said it -- and marked as what it is: never given to the agent.
    expect(entry.text).toBe("add milk");
    expect(entry.stale).toBe(true);
  });

  it("abandons an interim from a turn that never settled", () => {
    const state = fold([partial("two kilos of", 1), partial("actually three", 2)]);
    expect(state.held).toEqual({ turnId: 2, text: "actually three" });
    expect(state.entries).toHaveLength(0);
  });
});

describe("assistant replies", () => {
  it("lands as text with no audio anywhere in sight", () => {
    const state = reduceTranscript(initialTranscriptState, {
      type: "agent_reply",
      text: "Your current total is Rs 395, including a Rs 25 delivery fee.",
      deterministic: true,
      locale: "en-IN",
      turn_id: 1,
      speech_generation: 1,
      template_id: "quote.total",
      template_version: 3,
      fields: { total: "₹395.00", delivery_fee: "₹25.00" },
    });
    const entry = state.entries[0];
    expect(entry.kind).toBe("assistant");
    if (entry.kind !== "assistant") return;
    expect(entry.deterministic).toBe(true);
    expect(entry.templateId).toBe("quote.total");
    expect(entry.templateVersion).toBe(3);
    expect(entry.fields).toEqual({ total: "₹395.00", delivery_fee: "₹25.00" });
    // Not speaking yet: the words exist before any speech frame has been seen.
    expect(state.speaking).toBe(false);
  });

  it("keeps the products a reply named on the entry that named them", () => {
    const state = reduceTranscript(initialTranscriptState, {
      type: "agent_reply",
      text: "Amul Taaza is cheapest today, then the Mother Dairy.",
      deterministic: false,
      locale: "en-IN",
      turn_id: 2,
      speech_generation: 1,
      template_id: null,
      template_version: null,
      fields: null,
      items: [
        { sku: "AMUL-DAIRY-001", name: "Amul Taaza Toned Milk 500 ml", unit_price: { minor: 2800, currency: "INR", display: "28.00" }, stock_units: 30, available: true },
        { sku: "MOTH-DAIRY-004", name: "Mother Dairy Toned Milk 500 ml", unit_price: null, stock_units: null, available: false },
      ],
    });
    const entry = state.entries[0];
    expect(entry.kind).toBe("assistant");
    if (entry.kind !== "assistant") return;
    expect(entry.items).toHaveLength(2);
    expect(entry.items?.[0]?.sku).toBe("AMUL-DAIRY-001");
    // Carried through unchanged, including the absent price and the sold-out flag: the
    // shelf renders what the gateway read, and decides nothing for itself.
    expect(entry.items?.[1]?.unit_price).toBeNull();
    expect(entry.items?.[1]?.available).toBe(false);
  });

  it("tells an empty shelf apart from no shelf at all", () => {
    // A conversational reply that named no product sends `[]`, which is a shelf with
    // nothing on it. A deterministic money utterance sends no `items` field at all, which
    // is "this reply is not about products" -- collapsing the two would let a spoken price
    // wipe the products the buyer was just offered.
    const emptied = reduceTranscript(initialTranscriptState, {
      type: "agent_reply",
      text: "I could not find that one.",
      deterministic: false,
      locale: "en-IN",
      turn_id: 3,
      speech_generation: 1,
      template_id: null,
      template_version: null,
      fields: null,
      items: [],
    });
    const emptyEntry = emptied.entries[0];
    if (emptyEntry.kind !== "assistant") throw new Error("expected an assistant entry");
    expect(emptyEntry.items).toEqual([]);

    const money = reduceTranscript(initialTranscriptState, {
      type: "agent_reply",
      text: "Your total is ₹395.00.",
      deterministic: true,
      locale: "en-IN",
      turn_id: 4,
      speech_generation: 1,
      template_id: "quote.total",
      template_version: 3,
      fields: { total: "₹395.00" },
    });
    const moneyEntry = money.entries[0];
    if (moneyEntry.kind !== "assistant") throw new Error("expected an assistant entry");
    expect(moneyEntry.items).toBeNull();
  });
});

describe("degradation", () => {
  it("keeps one notice per kind, with the latest message", () => {
    const state = fold([
      { type: "degradation", kind: "stt_connection_lost", message: "attempt 1", text_input_available: true, transaction_state_changed: false },
      { type: "degradation", kind: "stt_connection_lost", message: "attempt 2", text_input_available: true, transaction_state_changed: false },
      { type: "degradation", kind: "tts_failed", message: "chirp 503", text_input_available: true, transaction_state_changed: false },
    ]);
    expect(state.degradations).toHaveLength(2);
    expect(state.degradations[0].message).toBe("attempt 2");
  });
});

describe("speech lifecycle", () => {
  it("tracks the generation the server is speaking", () => {
    const state = fold([
      { type: "speech_start", speech_generation: 2 },
      { type: "speech_chunk", seq: 0, speech_generation: 2, text: "hello", sample_rate_hz: 24_000, byte_length: 3200, deterministic: false, encoding: "pcm16le" },
    ]);
    expect(state.speaking).toBe(true);
    expect(state.speechGeneration).toBe(2);
  });

  it("stops speaking on speech_end and on an interruption", () => {
    const ended = fold([
      { type: "speech_start", speech_generation: 2 },
      { type: "speech_end", speech_generation: 2, chunks: 4, cancelled: false },
    ]);
    expect(ended.speaking).toBe(false);

    const interrupted = fold([
      { type: "speech_start", speech_generation: 2 },
      { type: "interrupted", speech_generation: 3 },
    ]);
    expect(interrupted.speaking).toBe(false);
    expect(interrupted.speechGeneration).toBe(3);
  });
});

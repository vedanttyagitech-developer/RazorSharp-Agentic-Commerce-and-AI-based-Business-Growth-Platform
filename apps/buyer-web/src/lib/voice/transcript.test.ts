import { describe, expect, it } from "vitest";

import { EMPTY_TRANSCRIPT, applyStreamText, reduceTranscript } from "./transcript";

describe("transcript wire contract (spec 19.5)", () => {
  it("replaces rather than appends, and an empty frame never clears", () => {
    expect(applyStreamText("", "mujhe")).toBe("mujhe");
    expect(applyStreamText("mujhe", "mujhe doodh")).toBe("mujhe doodh");
    expect(applyStreamText("mujhe doodh", "")).toBe("mujhe doodh");
    expect(applyStreamText("mujhe doodh", null)).toBe("mujhe doodh");
  });

  it("only finals enter intent processing and identical consecutive finals are deduplicated", () => {
    let state = reduceTranscript(EMPTY_TRANSCRIPT, { turn_id: "t1", interim_input_transcription: { text: "Tum jo" } });
    state = reduceTranscript(state, { turn_id: "t1", interim_input_transcription: { text: "Tum jo maine" } });
    expect(state.input[0].interim).toBe("Tum jo maine");
    expect(state.confirmed_intents).toHaveLength(0);

    state = reduceTranscript(state, { turn_id: "t1", input_transcription: { text: "Tum jo maine tu meri" } });
    expect(state.input[0].closed).toBe(true);
    expect(state.input[0].final).toBe("Tum jo maine tu meri");
    expect(state.confirmed_intents).toEqual([{ turn_id: "t1", text: "Tum jo maine tu meri" }]);

    state = reduceTranscript(state, { turn_id: "t1", input_transcription: { text: "Tum jo maine tu meri" } });
    expect(state.confirmed_intents).toHaveLength(1);
  });
});

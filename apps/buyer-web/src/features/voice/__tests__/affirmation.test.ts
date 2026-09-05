import { describe, expect, it } from "vitest";

import { isAffirmative } from "../transcript";

describe("a spoken yes", () => {
  it("is a short whole-utterance yes in English, Hindi or Hinglish", () => {
    for (const said of ["yes", "Yes.", "yeah", "okay", "haan", "Ji haan", "theek hai", "add it", "हाँ", "ठीक है"]) {
      expect(isAffirmative(said), said).toBe(true);
    }
  });

  it("is not a sentence that merely contains a yes, nor a no", () => {
    for (const said of ["yes but make it two litres please", "no", "nahi", "yes no", "I need milk", ""]) {
      expect(isAffirmative(said), said).toBe(false);
    }
  });
});

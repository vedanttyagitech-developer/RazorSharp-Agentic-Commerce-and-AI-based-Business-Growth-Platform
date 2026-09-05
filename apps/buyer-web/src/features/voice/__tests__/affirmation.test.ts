import { describe, expect, it } from "vitest";

import { isAffirmative, spokenQuantity } from "../transcript";

describe("a spoken yes", () => {
  it("is a short whole-utterance yes in English, Hindi or Hinglish", () => {
    for (const said of ["yes", "Yes.", "yeah", "okay", "haan", "Ji haan", "theek hai", "add it", "हाँ", "ठीक है", "yes add that", "haan add karo", "okay please", "yes that one please", "add it please", "add two of those", "le lo", "daal do"]) {
      expect(isAffirmative(said), said).toBe(true);
    }
  });

  it("is not a sentence that merely contains a yes, nor a no", () => {
    for (const said of ["yes but make it two litres please and something", "no", "nahi", "yes no", "I need milk", "", "no thanks", "yes but not that one"]) {
      expect(isAffirmative(said), said).toBe(false);
    }
  });
});

describe("the count a spoken yes carries", () => {
  it("reads a number word or a digit, in English, Hindi or Hinglish", () => {
    expect(spokenQuantity("add two of those")).toBe(2);
    expect(spokenQuantity("do packet le lo")).toBe(2);
    expect(spokenQuantity("yes, 3 please")).toBe(3);
    expect(spokenQuantity("तीन डाल दो")).toBe(3);
  });

  it("is null when no count was said", () => {
    expect(spokenQuantity("yes add that")).toBeNull();
    expect(spokenQuantity("haan")).toBeNull();
  });
});

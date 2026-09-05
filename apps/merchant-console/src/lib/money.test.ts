/**
 * The money formatter, tested for the one thing a console cannot get wrong.
 *
 * `null` and `0` are different facts. `null` is the API saying it does not know — no
 * order row, nothing captured, no difference statable — and `0` is the API saying the
 * amount is nothing. An operator who reads "₹0.00" where the platform actually said
 * "I have no capture evidence" believes a settlement happened for nothing, and an
 * operator who reads "—" where the platform said zero believes the read failed. Both
 * mistakes are one collapsed branch away, so the distinction is asserted here directly
 * rather than left to a reviewer's eye.
 *
 * The rest of this file pins the rendering rules the columns depend on: two decimal
 * places always, Indian digit grouping, a true minus sign, and `display` from the server
 * preferred over anything this module could derive.
 */
import { describe, expect, it } from "vitest";

import {
  deltaMinor,
  formatCount,
  formatDelta,
  formatMinor,
  formatMinorOrDash,
  formatMoney,
  type Money,
} from "./money";

describe("formatMinor", () => {
  it("renders integer paise as rupees with two decimal places", () => {
    expect(formatMinor(3150)).toBe("₹31.50");
    expect(formatMinor(315)).toBe("₹3.15");
    expect(formatMinor(31500)).toBe("₹315.00");
  });

  // A price that renders as ₹31.5 beside ₹31.50 breaks the alignment of the column, and a
  // trailing paise digit that silently disappears is a price an operator misreads by 10x.
  it("keeps the second decimal place when the paise part is a round ten", () => {
    expect(formatMinor(3100)).toBe("₹31.00");
    expect(formatMinor(3110)).toBe("₹31.10");
    expect(formatMinor(5)).toBe("₹0.05");
  });

  it("groups the rupee part the way an Indian reader scans it", () => {
    expect(formatMinor(12345678)).toBe("₹1,23,456.78");
    expect(formatMinor(100000)).toBe("₹1,000.00");
  });

  it("uses a true minus sign rather than a hyphen", () => {
    expect(formatMinor(-7700)).toBe("−₹77.00");
    expect(formatMinor(-7700).startsWith("-")).toBe(false);
  });

  it("prefixes an unknown currency with its code rather than guessing a symbol", () => {
    expect(formatMinor(3150, "USD")).toBe("$31.50");
    expect(formatMinor(3150, "AED")).toBe("AED 31.50");
  });

  // NaN reaches this function only if something upstream did arithmetic it should not
  // have. It must never render as a number.
  it("renders a non-finite amount as an em dash, never as a figure", () => {
    expect(formatMinor(Number.NaN)).toBe("—");
    expect(formatMinor(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("formatMinorOrDash: absent is not zero", () => {
  it("renders a null amount as an em dash", () => {
    expect(formatMinorOrDash(null)).toBe("—");
  });

  it("renders an absent amount as an em dash", () => {
    expect(formatMinorOrDash(undefined)).toBe("—");
  });

  it("renders a zero amount as zero rupees", () => {
    expect(formatMinorOrDash(0)).toBe("₹0.00");
  });

  // The assertion the whole module exists for: the two must not collapse into one string.
  it("never renders absent and zero as the same thing", () => {
    expect(formatMinorOrDash(null)).not.toBe(formatMinorOrDash(0));
    expect(formatMinorOrDash(undefined)).not.toBe(formatMinorOrDash(0));
  });

  it("carries the currency through to a present amount", () => {
    expect(formatMinorOrDash(3150, "USD")).toBe("$31.50");
  });
});

describe("formatMoney", () => {
  const money: Money = { minor: 3150, currency: "INR", display: "31.50" };

  it("prefers the server's own display string over anything derived here", () => {
    expect(formatMoney(money)).toBe("₹31.50");
    // A display string that disagrees with `minor` is the server's to reconcile, not this
    // module's to overrule: whatever it sent is what the operator is shown.
    expect(formatMoney({ minor: 3150, currency: "INR", display: "99.99" })).toBe("₹99.99");
  });

  it("renders an absent MoneyOut as an em dash", () => {
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
  });

  it("distinguishes an absent MoneyOut from a zero one", () => {
    const zero: Money = { minor: 0, currency: "INR", display: "0.00" };
    expect(formatMoney(zero)).toBe("₹0.00");
    expect(formatMoney(null)).not.toBe(formatMoney(zero));
  });
});

describe("deltaMinor and formatDelta", () => {
  it("subtracts two integers the server sent, keeping the sign", () => {
    expect(deltaMinor(57995, 68195)).toBe(10200);
    expect(deltaMinor(68195, 57995)).toBe(-10200);
    expect(deltaMinor(57995, 57995)).toBe(0);
  });

  // "₹102.00 more" and "₹102.00 less" are opposite facts about who a refusal protected.
  it("signs a movement so the two directions cannot be confused", () => {
    expect(formatDelta(10200)).toBe("+₹102.00");
    expect(formatDelta(-10200)).toBe("−₹102.00");
    expect(formatDelta(10200)).not.toBe(formatDelta(-10200));
  });

  it("renders no movement without a sign", () => {
    expect(formatDelta(0)).toBe("₹0.00");
  });
});

describe("formatCount", () => {
  it("groups a whole count and renders zero as zero", () => {
    expect(formatCount(247)).toBe("247");
    expect(formatCount(12345)).toBe("12,345");
    expect(formatCount(0)).toBe("0");
  });

  it("renders a non-finite count as an em dash", () => {
    expect(formatCount(Number.NaN)).toBe("—");
  });
});

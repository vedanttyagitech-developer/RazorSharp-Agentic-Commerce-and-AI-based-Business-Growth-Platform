/**
 * Money is the one module where a wrong answer is silently spendable.
 *
 * Every other bug on this storefront shows itself: a broken read renders a failure, a
 * broken layout is visible. A rounding error in here renders a plausible number that a
 * buyer then approves, and the kernel admits a different one. So these tests check two
 * separate things — that the formatting is exactly right, and that the module has not
 * grown any arithmetic beyond the one subtraction it is allowed.
 *
 * The second half is a source-level test rather than a behavioural one on purpose. No
 * assertion about outputs can catch "somebody added a `subtotal` helper that multiplies
 * a unit price by a quantity", because such a helper would be correct in isolation and
 * wrong in principle: the total a buyer approves has to be the total the server hashed.
 * Reading the file and refusing new operators is the only test that fails on that.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { deltaMinor, formatDelta, formatMinor, formatMoney, type Money } from "./money";

describe("formatMinor", () => {
  it("renders integer paise as rupees and paise, always to two places", () => {
    expect(formatMinor(3150)).toBe("₹31.50");
    // 8550 and 9224 are the two totals the live kernel produced for the captured
    // refusal, so these are the exact strings the refusal screen has to draw.
    expect(formatMinor(8550)).toBe("₹85.50");
    expect(formatMinor(9224)).toBe("₹92.24");
  });

  it("keeps the trailing zeroes that make a column of prices read as one kind of number", () => {
    expect(formatMinor(3150)).toBe("₹31.50");
    expect(formatMinor(3100)).toBe("₹31.00");
    expect(formatMinor(3105)).toBe("₹31.05");
  });

  it("groups in the Indian system, not in thousands", () => {
    expect(formatMinor(12345678)).toBe("₹1,23,456.78");
    expect(formatMinor(100000000)).toBe("₹10,00,000.00");
  });

  it("drops the paise under `whole` only when the amount is exact rupees", () => {
    expect(formatMinor(2800, "INR", { whole: true })).toBe("₹28");
    expect(formatMinor(25500, "INR", { whole: true })).toBe("₹255");
    expect(formatMinor(9224, "INR", { whole: true })).toBe("₹92.24");
  });

  it("carries the sign, because a credit and a charge are opposite facts", () => {
    expect(formatMinor(-674)).toBe("-₹6.74");
    expect(formatMinor(674)).toBe("₹6.74");
  });

  it("renders zero as zero rather than as an absence", () => {
    expect(formatMinor(0)).toBe("₹0.00");
    expect(formatMinor(0, "INR", { whole: true })).toBe("₹0");
  });

  it("names a currency it has no symbol for instead of guessing one", () => {
    expect(formatMinor(674, "GBP")).toBe("GBP 6.74");
    expect(formatMinor(674, "USD")).toBe("$6.74");
    expect(formatMinor(674, "EUR")).toBe("€6.74");
  });

  it("renders an em dash for an amount that is not a number", () => {
    expect(formatMinor(Number.NaN)).toBe("—");
    expect(formatMinor(Number.POSITIVE_INFINITY)).toBe("—");
    // Not "₹0.00": a figure the platform cannot state and a figure of nothing owed are
    // different claims, and the second wearing the first's face is the failure mode.
    expect(formatMinor(Number.NaN)).not.toBe("₹0.00");
  });
});

describe("formatMoney", () => {
  /** The `unit_price` of AMUL-DAIRY-001, captured from the live API on 2026-09-05. */
  const milk: Money = { minor: 2800, currency: "INR", display: "28.00" };

  it("renders an em dash for an amount the API sent as null or did not send", () => {
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
  });

  it("renders the minor units, so it groups exactly as every other amount does", () => {
    expect(formatMoney(milk)).toBe("₹28.00");
    // The iPhone, captured from the live catalogue. `display` is "126899.00": the server
    // does not group, and this is the amount an approval screen puts under the buyer's
    // thumb, so it must read the way the product grid beside it reads.
    expect(formatMoney({ minor: 12689900, currency: "INR", display: "126899.00" })).toBe(
      "₹1,26,899.00",
    );
    // The basket total that sits directly under "Items subtotal ₹1,233.00".
    expect(formatMoney({ minor: 137445, currency: "INR", display: "1374.45" })).toBe("₹1,374.45");
  });

  it("takes the amount from `minor`, not from the display string beside it", () => {
    // `display` is kept for audit and is no longer read here. A disagreeing pair renders
    // the integer, which is the field the kernel hashes and the buyer approves.
    expect(formatMoney({ minor: 100, currency: "INR", display: "1,00,000.00" })).toBe("₹1.00");
  });

  it("takes the compact form the grid asks for", () => {
    expect(formatMoney(milk, { whole: true })).toBe("₹28");
    expect(formatMoney({ minor: 9224, currency: "INR", display: "92.24" }, { whole: true })).toBe(
      "₹92.24",
    );
  });

  it("names an unknown currency instead of dropping it", () => {
    expect(formatMoney({ minor: 674, currency: "GBP", display: "6.74" })).toBe("GBP 6.74");
  });
});

describe("deltaMinor", () => {
  it("subtracts the approved amount from the current one, both sent by the server", () => {
    // The captured refusal: approved 8550, current 9224.
    expect(deltaMinor(8550, 9224)).toBe(674);
  });

  it("keeps the sign, because more and less protect the buyer differently", () => {
    expect(deltaMinor(9224, 8550)).toBe(-674);
    expect(deltaMinor(8550, 8550)).toBe(0);
  });
});

describe("formatDelta", () => {
  it("signs a difference with a true minus sign, not a hyphen", () => {
    expect(formatDelta(674)).toBe("+₹6.74");
    expect(formatDelta(-674)).toBe("−₹6.74");
    expect(formatDelta(-674).startsWith("-")).toBe(false);
  });

  it("renders no sign at all on zero", () => {
    expect(formatDelta(0)).toBe("₹0.00");
  });

  it("carries the currency through", () => {
    expect(formatDelta(674, "GBP")).toBe("+GBP 6.74");
  });
});

/* ------------------------------------------------------ the arithmetic lock */

/**
 * The module's source with comments and string/template literals removed.
 *
 * Literals go because a hyphen inside `"-"` and a slash inside a JSDoc block are not
 * arithmetic, and a test that cannot tell the difference would either pass on a real
 * mistake or fail on a comment.
 */
function strippedSource(): string {
  // `import.meta.dirname` rather than `new URL("./money.ts", import.meta.url)`: the
  // bundler rewrites that second form into an asset URL before this code ever runs.
  const source = readFileSync(join(import.meta.dirname, "money.ts"), "utf8");

  let out = "";
  let index = 0;
  while (index < source.length) {
    const two = source.slice(index, index + 2);
    if (two === "/*") {
      const end = source.indexOf("*/", index + 2);
      index = end === -1 ? source.length : end + 2;
      continue;
    }
    if (two === "//") {
      const end = source.indexOf("\n", index);
      index = end === -1 ? source.length : end;
      continue;
    }
    const char = source[index];
    if (char === '"' || char === "'" || char === "`") {
      index += 1;
      while (index < source.length && source[index] !== char) {
        index += source[index] === "\\" ? 2 : 1;
      }
      index += 1;
      // A template literal's `${...}` holes are swallowed with it. Nothing in this module
      // computes inside one, and this test asserts below that nothing ever will.
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

describe("the module performs no arithmetic beyond splitting one amount and one subtraction", () => {
  const code = strippedSource();

  it("subtracts in exactly one place, and it is `current - approved`", () => {
    const subtractions = code.match(/[A-Za-z0-9_)\]]\s*-\s*[A-Za-z0-9_(]/g) ?? [];
    expect(subtractions).toHaveLength(1);
    expect(code).toContain("current - approved");
  });

  it("never adds two values together", () => {
    // String building happens inside template literals, which are stripped above, so any
    // surviving `+` would be a real addition of two operands.
    expect(code).not.toMatch(/[A-Za-z0-9_)\]]\s*\+\s*[A-Za-z0-9_(]/);
  });

  it("never multiplies", () => {
    expect(code).not.toContain("*");
  });

  it("divides only by one hundred, to split a single amount into rupees and paise", () => {
    const divisions = code.match(/\/[^/]/g) ?? [];
    expect(divisions).toHaveLength(1);
    expect(code).toMatch(/Math\.abs\(minor\)\s*\/\s*100/);
    expect(code).toMatch(/Math\.abs\(minor\)\s*%\s*100/);
  });

  it("never rounds, never parses a decimal string back into a number", () => {
    for (const forbidden of [
      "Math.round",
      "Math.floor",
      "Math.ceil",
      "toFixed",
      "parseFloat",
      "parseInt",
      "Number(",
      "reduce(",
    ]) {
      expect(code).not.toContain(forbidden);
    }
  });

  it("keeps the truncation it does use on the absolute value of one amount", () => {
    // `Math.trunc` is allowed and load-bearing: it splits `8550` into `85` and `50`
    // without ever seeing a fractional value, which is what makes the split exact.
    expect(code).toMatch(/Math\.trunc\(Math\.abs\(minor\)/);
  });
});

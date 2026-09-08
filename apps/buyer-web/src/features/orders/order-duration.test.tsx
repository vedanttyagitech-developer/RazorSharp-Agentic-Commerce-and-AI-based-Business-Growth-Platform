/**
 * How a sale's length is put into words, and the one case that must stay silent.
 *
 * The number itself is the database's: both ends stamped and subtracted by the clock that
 * stamped them. All this formatter chooses is wording, so these tests are about the two
 * ways wording can lie -- rounding away the part that matters, and printing a figure for
 * a sale nobody measured.
 */
import { describe, expect, it } from "vitest";

import { formatDuration } from "./capture-evidence";

describe("formatDuration", () => {
  it("says nothing at all when the sale was not measured", () => {
    // The important case. A sale whose opening version could not be read has no span, and
    // the server sends null to say so. Anything printed here -- "0s", "--", "unknown" --
    // is a claim: "0s" in particular would advertise the fastest sale in the shop.
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
  });

  it("refuses a figure it cannot trust rather than rendering it", () => {
    expect(formatDuration(Number.NaN)).toBeNull();
    expect(formatDuration(-1)).toBeNull();
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("keeps seconds beside minutes, because that is the difference worth seeing", () => {
    // "2m" for both of these would hide the thing the number exists to show: whether the
    // buyer decided at once or thought about it.
    expect(formatDuration(63)).toBe("1m 3s");
    expect(formatDuration(118)).toBe("1m 58s");
  });

  it("reads a sale that took no time at all as zero, not as nothing", () => {
    // Distinct from the null case on purpose. Zero here is a measurement: the order was
    // written in the same second the version was frozen. Not measured and measured-as-fast
    // are different facts and the screen must be able to tell them apart.
    expect(formatDuration(0)).toBe("0s");
  });

  it("drops an empty remainder rather than padding it", () => {
    expect(formatDuration(120)).toBe("2m");
    expect(formatDuration(7200)).toBe("2h");
  });

  it("counts a long sale in hours and minutes", () => {
    expect(formatDuration(47)).toBe("47s");
    expect(formatDuration(3599)).toBe("59m 59s");
    expect(formatDuration(8040)).toBe("2h 14m");
  });

  it("floors a fractional second rather than showing one", () => {
    expect(formatDuration(47.9)).toBe("47s");
  });
});

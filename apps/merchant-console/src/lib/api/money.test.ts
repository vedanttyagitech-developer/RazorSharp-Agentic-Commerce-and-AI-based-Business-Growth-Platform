import { describe, expect, it } from "vitest";
import { formatBasisPoints, formatIndianNumber, formatPaise, formatPercentage } from "./money";

describe("formatPaise", () => {
  it("formats standard integer paise correctly into Indian Rupees", () => {
    expect(formatPaise(0)).toBe("₹0.00");
    expect(formatPaise(50)).toBe("₹0.50");
    expect(formatPaise(2800)).toBe("₹28.00");
    expect(formatPaise(68195)).toBe("₹681.95");
  });

  it("handles negative values cleanly", () => {
    expect(formatPaise(-2500)).toBe("-₹25.00");
  });

  it("formats large numbers using Indian numbering grouping (lakhs & crores)", () => {
    expect(formatPaise(24892000)).toBe("₹2,48,920.00");
    expect(formatPaise(1000000000)).toBe("₹1,00,00,000.00");
  });

  it("throws a descriptive error when encountering floating point numbers", () => {
    expect(() => formatPaise(28.5)).toThrowError(/Money minor units must be an integer/);
    expect(() => formatPaise(100.001)).toThrowError(/Money minor units must be an integer/);
  });

  it("returns placeholder dash for null or undefined", () => {
    expect(formatPaise(null)).toBe("—");
    expect(formatPaise(undefined)).toBe("—");
  });
});

describe("formatIndianNumber", () => {
  it("groups numbers according to Indian numbering system", () => {
    expect(formatIndianNumber(500)).toBe("500");
    expect(formatIndianNumber(1000)).toBe("1,000");
    expect(formatIndianNumber(100000)).toBe("1,00,000");
    expect(formatIndianNumber(10000000)).toBe("1,00,00,000");
  });
});

describe("formatBasisPoints", () => {
  it("converts integer basis points to formatted percentage", () => {
    expect(formatBasisPoints(0)).toBe("0.00%");
    expect(formatBasisPoints(500)).toBe("5.00%");
    expect(formatBasisPoints(1800)).toBe("18.00%");
  });

  it("throws error for non-integer basis points", () => {
    expect(() => formatBasisPoints(18.5)).toThrowError(/Basis points must be an integer/);
  });
});

describe("formatPercentage", () => {
  it("formats numeric ratios to percentage strings", () => {
    expect(formatPercentage(0.985)).toBe("98.5%");
    expect(formatPercentage(0.012)).toBe("1.2%");
    expect(formatPercentage(NaN)).toBe("0.0%");
  });
});

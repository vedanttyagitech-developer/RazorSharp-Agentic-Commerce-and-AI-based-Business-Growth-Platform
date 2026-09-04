/**
 * Money formatting utilities for integer minor units (paise).
 *
 * This codebase enforces integer paise representation everywhere.
 * Floating point numbers are strictly forbidden in monetary values.
 */

export function formatPaise(minor: number | null | undefined, currency = "INR"): string {
  if (minor === null || minor === undefined) {
    return "—";
  }

  if (!Number.isInteger(minor)) {
    throw new Error(
      `Money minor units must be an integer, got non-integer value: ${minor}. Floats in monetary calculations are forbidden.`
    );
  }

  const sign = minor < 0 ? "-" : "";
  const absoluteMinor = Math.abs(minor);
  const rupees = Math.floor(absoluteMinor / 100);
  const paise = absoluteMinor % 100;
  const paiseStr = paise.toString().padStart(2, "0");

  // Format with Indian number numbering system (lakhs, crores)
  const rupeesStr = formatIndianNumber(rupees);

  if (currency === "INR") {
    return `${sign}₹${rupeesStr}.${paiseStr}`;
  }
  return `${sign}${currency} ${rupeesStr}.${paiseStr}`;
}

export function formatIndianNumber(num: number): string {
  const str = num.toString();
  if (str.length <= 3) return str;
  const lastThree = str.substring(str.length - 3);
  const otherNumbers = str.substring(0, str.length - 3);
  const formattedOthers = otherNumbers.replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return `${formattedOthers},${lastThree}`;
}

export function formatBasisPoints(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return "0.00%";
  if (!Number.isInteger(bps)) {
    throw new Error(`Basis points must be an integer, got: ${bps}`);
  }
  const pct = (bps / 100).toFixed(2);
  return `${pct}%`;
}

export function formatPercentage(ratio: number): string {
  if (isNaN(ratio) || !isFinite(ratio)) return "0.0%";
  return `${(ratio * 100).toFixed(1)}%`;
}

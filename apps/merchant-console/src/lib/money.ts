/**
 * Money is an integer number of paise. Nothing here adds, multiplies or rounds it.
 *
 * The console's job is to render an amount the server already computed. Retained-revenue
 * arithmetic in particular is done by `proof_chain.retained_revenue` against committed
 * rows and arrives as finished integers; recomputing a difference here would produce a
 * number that agrees with the API right up until the day it does not, and an evidence
 * page whose figures were derived in a browser is not evidence.
 *
 * The one subtraction this module offers, `deltaMinor`, exists for the delta rows the
 * kernel sends as an approved/current pair with no difference field of its own.
 *
 * `Number` is safe for these values: paise totals stay far below 2^53.
 */

/** An amount as the API sends it. `display` is the server's own exact decimal string. */
export interface Money {
  minor: number;
  currency: string;
  display: string;
}

const SYMBOLS: Readonly<Record<string, string>> = { INR: "₹", USD: "$", EUR: "€" };

/**
 * Render integer minor units for display: `3150` -> `₹31.50`.
 *
 * Two decimal places always. A console reads as a column of figures, and a price that
 * renders as `₹31.5` beside `₹31.50` breaks the alignment that makes the column
 * scannable in the first place.
 */
export function formatMinor(minor: number, currency = "INR"): string {
  if (!Number.isFinite(minor)) return "—";
  const symbol = SYMBOLS[currency] ?? `${currency} `;
  const negative = minor < 0;
  const units = Math.trunc(Math.abs(minor) / 100);
  const paise = Math.abs(minor) % 100;
  return `${negative ? "−" : ""}${symbol}${units.toLocaleString("en-IN")}.${String(paise).padStart(2, "0")}`;
}

/** Render a nullable amount. `null` is "the API said this is not known", not zero. */
export function formatMinorOrDash(minor: number | null | undefined, currency = "INR"): string {
  return minor === null || minor === undefined ? "—" : formatMinor(minor, currency);
}

/** Render a `MoneyOut` from the API. Prefers the server's own `display` string. */
export function formatMoney(money: Money | null | undefined): string {
  if (!money) return "—";
  const symbol = SYMBOLS[money.currency] ?? `${money.currency} `;
  return `${symbol}${money.display}`;
}

/**
 * The signed difference between two amounts, for a refusal's delta rows.
 *
 * A subtraction of two integers the server sent, never a re-derivation of either. The
 * sign is kept because "₹77.00 more" and "₹77.00 less" are opposite facts about who the
 * refusal protected.
 */
export function deltaMinor(approved: number, current: number): number {
  return current - approved;
}

/** `+₹77.00` / `−₹77.00`, with a true minus sign rather than a hyphen. */
export function formatDelta(minor: number, currency = "INR"): string {
  if (minor === 0) return formatMinor(0, currency);
  const sign = minor > 0 ? "+" : "−";
  return `${sign}${formatMinor(Math.abs(minor), currency)}`;
}

/** A whole count with Indian grouping, for queue depths and catalogue sizes. */
export function formatCount(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString("en-IN") : "—";
}

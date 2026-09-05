/**
 * Money is an integer number of paise. Nothing here adds, multiplies or rounds it.
 *
 * The browser's job is to render an amount the server already computed. Every total,
 * tax, delivery fee and discount on this storefront arrives from the quote engine as
 * `*_minor` and is displayed as-is, because a total computed here could disagree with
 * the one hashed into an approval, and a buyer approving one number while the kernel
 * admits another is the exact failure this project exists to prevent.
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
 * Two decimal places always, because a price that renders as `₹31.5` next to `₹31.50`
 * reads as a different kind of number. Pass `whole` for the compact form the product
 * grid uses, which drops `.00` only when the amount is exact rupees.
 */
export function formatMinor(
  minor: number,
  currency = "INR",
  options: { whole?: boolean } = {},
): string {
  if (!Number.isFinite(minor)) return "—";
  const symbol = SYMBOLS[currency] ?? `${currency} `;
  const negative = minor < 0;
  const units = Math.trunc(Math.abs(minor) / 100);
  const paise = Math.abs(minor) % 100;
  const grouped = units.toLocaleString("en-IN");
  const body = options.whole && paise === 0 ? grouped : `${grouped}.${String(paise).padStart(2, "0")}`;
  return `${negative ? "-" : ""}${symbol}${body}`;
}

/** Render a `MoneyOut` from the API. Prefers the server's own `display` string. */
export function formatMoney(money: Money | null | undefined, options: { whole?: boolean } = {}): string {
  if (!money) return "—";
  if (options.whole) return formatMinor(money.minor, money.currency, options);
  const symbol = SYMBOLS[money.currency] ?? `${money.currency} `;
  return `${symbol}${money.display}`;
}

/**
 * The signed difference between two amounts, for a refusal's delta rows.
 *
 * A subtraction of two integers the server sent, never a re-derivation of either. The
 * sign is kept because "₹12.00 more" and "₹12.00 less" are opposite facts about who the
 * refusal protected.
 */
export function deltaMinor(approved: number, current: number): number {
  return current - approved;
}

/** `+₹12.00` / `−₹12.00`, with a true minus sign rather than a hyphen. */
export function formatDelta(minor: number, currency = "INR"): string {
  if (minor === 0) return formatMinor(0, currency);
  const sign = minor > 0 ? "+" : "−";
  return `${sign}${formatMinor(Math.abs(minor), currency)}`;
}

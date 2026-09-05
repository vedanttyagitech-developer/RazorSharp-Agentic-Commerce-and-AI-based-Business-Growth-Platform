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

/**
 * An amount as the API sends it.
 *
 * `minor` is the amount. `display` is the server's own exact decimal string, kept because
 * an audit trail wants the merchant's rendering verbatim -- but it is never what a buyer
 * reads on a screen, because the server renders it ungrouped and this storefront groups
 * by lakh. Two renderings of one amount on one screen is a hesitation, so `display` stays
 * out of the way and every visible figure comes from `minor`.
 */
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

/**
 * Render a `MoneyOut` from the API, from its integer minor units.
 *
 * This used to print `money.display` instead, which is the server's decimal string with
 * no digit grouping in it. `money` is carried on exactly the largest figures on this
 * storefront -- the basket total, the amount on an approval, the button that approves it,
 * an order's amount -- so those were the only amounts rendered `₹126899.00` while every
 * `minor` amount beside them, including the grid card for the same product and the
 * version trail under the same approval, read `₹1,26,899.00`. An Indian buyer reads the
 * grouped form at a glance and has to count the digits of the other one, and the two
 * appeared together on one screen.
 *
 * Delegating loses nothing: `minor` and `display` are the same amount, the server sends
 * both, and `display` is still on the object for anything that wants the merchant's own
 * string. It is not a second opinion about the value, only about the spacing.
 */
export function formatMoney(money: Money | null | undefined, options: { whole?: boolean } = {}): string {
  if (!money) return "—";
  return formatMinor(money.minor, money.currency, options);
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

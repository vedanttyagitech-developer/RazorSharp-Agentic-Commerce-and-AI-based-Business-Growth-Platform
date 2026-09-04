/**
 * Formatting only. The storefront never computes a total, tax or gap; it renders the
 * integer minor units the fee engine returned (spec 24.1, merchant_sim.fees).
 */
const EXPONENTS: Record<string, number> = { INR: 2, USD: 2, EUR: 2, GBP: 2, SGD: 2, AED: 2, JPY: 0 };

export function minorExponent(currency: string): number {
  return EXPONENTS[currency] ?? 2;
}

export function formatMinor(minor: number, currency: string, locale = "en-IN"): string {
  const exponent = minorExponent(currency);
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  const scale = 10 ** exponent;
  const whole = Math.trunc(abs / scale);
  const fraction = abs % scale;
  const major = exponent === 0 ? whole : Number(`${whole}.${String(fraction).padStart(exponent, "0")}`);
  try {
    return (
      sign +
      new Intl.NumberFormat(locale, {
        style: "currency",
        currency,
        minimumFractionDigits: exponent,
        maximumFractionDigits: exponent,
      }).format(major)
    );
  } catch {
    return `${sign}${currency} ${whole}${exponent ? "." + String(fraction).padStart(exponent, "0") : ""}`;
  }
}

export function formatBasisPoints(bp: number): string {
  const percent = bp / 100;
  return `${Number.isInteger(percent) ? percent : percent.toFixed(2)}%`;
}

export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString("en-IN", { hour12: false }) + " UTC" + offsetLabel(date);
}

function offsetLabel(date: Date): string {
  const minutes = -date.getTimezoneOffset();
  if (minutes === 0) return "";
  const sign = minutes > 0 ? "+" : "-";
  const abs = Math.abs(minutes);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

import type { Quote, Unavailability } from "@/lib/api/types";
import { formatBasisPoints, formatMinor } from "@/lib/money";

import { Alert } from "./ui";

/**
 * Deterministic quote rendering. Every number comes from the fee engine's Quote; the
 * component formats and never adds. Tax is shown per line because that is how it was
 * rounded (half-up per line) and how a partial refund will later be computed.
 */
export function QuoteBreakdown({ quote, unavailable = [], stale = false }: { quote: Quote | null; unavailable?: Unavailability[]; stale?: boolean }) {
  if (unavailable.length > 0) {
    return (
      <Alert tone="warning" title="Some lines cannot be priced" role="alert">
        <ul className="list-disc pl-5">
          {unavailable.map((line) => (
            <li key={line.sku}>
              <span className="font-mono">{line.sku}</span>: requested {line.requested}, {line.listed ? `${line.available_units} available (sold out)` : "not listed (delisted)"}.
            </li>
          ))}
        </ul>
        <p className="mt-1">The basket is refused whole (STALE_CHECKOUT) rather than priced partially.</p>
      </Alert>
    );
  }
  if (!quote) return <p className="text-sm text-muted">Your basket is empty.</p>;

  return (
    <div className="space-y-3">
      {stale ? <Alert tone="warning" title="Quote re-priced">Merchant state moved since this basket was last quoted; the numbers below are current.</Alert> : null}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Quote breakdown by line</caption>
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th scope="col" className="py-1 pr-2">Item</th>
              <th scope="col" className="py-1 pr-2 text-right">Qty × unit</th>
              <th scope="col" className="py-1 pr-2 text-right">Subtotal</th>
              <th scope="col" className="py-1 text-right">Tax</th>
            </tr>
          </thead>
          <tbody>
            {quote.lines.map((line) => (
              <tr key={line.sku} className="border-b border-line/60">
                <td className="py-1.5 pr-2">
                  {line.name}
                  <span className="block font-mono text-xs text-muted">{line.sku}</span>
                </td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{line.quantity} × {formatMinor(line.unit_price_minor, quote.currency)}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{formatMinor(line.subtotal_minor, quote.currency)}</td>
                <td className="py-1.5 text-right tabular-nums">
                  {formatMinor(line.tax_minor, quote.currency)}
                  <span className="block text-xs text-muted">GST {formatBasisPoints(line.tax_bp)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="ml-auto grid max-w-sm grid-cols-[1fr_auto] gap-x-6 gap-y-1 text-sm">
        <dt className="text-muted">Items subtotal</dt>
        <dd className="text-right tabular-nums">{formatMinor(quote.items_subtotal_minor, quote.currency)}</dd>
        <dt className="text-muted">Items tax</dt>
        <dd className="text-right tabular-nums">{formatMinor(quote.items_tax_minor, quote.currency)}</dd>
        <dt className="text-muted">Delivery fee</dt>
        <dd className="text-right tabular-nums">{quote.free_delivery_applied ? <span>{formatMinor(0, quote.currency)} <span className="text-xs">(free delivery applied)</span></span> : formatMinor(quote.delivery_fee_minor, quote.currency)}</dd>
        <dt className="text-muted">Delivery tax</dt>
        <dd className="text-right tabular-nums">{formatMinor(quote.delivery_tax_minor, quote.currency)}</dd>
        <dt className="border-t border-line pt-1 font-semibold">Total</dt>
        <dd className="border-t border-line pt-1 text-right font-semibold tabular-nums">{formatMinor(quote.total_minor, quote.currency)}</dd>
      </dl>
      <p className="text-sm" role="status">
        {quote.free_delivery_applied
          ? "Free delivery applied: the pre-tax item subtotal meets the merchant threshold."
          : `Add ${formatMinor(quote.gap_to_free_delivery_minor, quote.currency)} more (pre-tax) for free delivery.`}
      </p>
      <p className="text-xs text-muted">
        Priced at catalogue revision <span className="font-mono">{quote.catalogue_revision}</span> from <span className="font-mono">{quote.source}</span>. Content hash <span className="font-mono">{quote.content_hash.slice(0, 12)}…</span>
      </p>
    </div>
  );
}

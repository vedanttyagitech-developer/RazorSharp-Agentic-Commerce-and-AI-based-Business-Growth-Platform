/**
 * The money panel: every figure is a field of the quote, rendered as it arrived.
 *
 * Nothing here is derived. The subtotal is not the sum of the lines above it as far as
 * this component is concerned, the gap to free delivery is not the difference between a
 * threshold and a total, and the total is not the sum of the rows. All five are integers
 * the fee engine computed, and the storefront's only job is to display them in the order
 * a buyer reads them. A figure computed here could disagree with the one hashed into the
 * approval, and the buyer would be consenting to a number no system ever held.
 *
 * The fingerprint at the foot is that hash, truncated. It is shown because it is the
 * thing an approval binds to: when the kernel later refuses a stale approval, the buyer
 * has already seen the identity of what they were quoted.
 */
"use client";

import type { ReactNode } from "react";

import { Amount, cx } from "@/components/ui";
import type { Quote } from "@/lib/api/types";
import { formatMinor } from "@/lib/money";

function Row({
  label,
  children,
  muted,
}: {
  label: string;
  children: ReactNode;
  muted?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <dt className={cx("text-[13px]", muted ? "text-[var(--ink-4)]" : "text-[var(--ink-3)]")}>{label}</dt>
      <dd className={cx("text-[13px] font-medium", muted ? "text-[var(--ink-4)]" : "text-[var(--ink-2)]")}>
        {children}
      </dd>
    </div>
  );
}

export function QuoteSummary({ quote, repricing = false }: { quote: Quote; repricing?: boolean }) {
  const gap = quote.gap_to_free_delivery_minor;
  const showNudge = gap !== null && gap > 0;

  return (
    <section
      aria-label="Bill details"
      aria-busy={repricing || undefined}
      className={cx(
        "rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-white transition-opacity",
        repricing && "opacity-60",
      )}
      style={{ boxShadow: "var(--card-shadow)" }}
    >
      <h2 className="border-b border-[var(--header-line)] px-4 py-3 text-[14px] font-bold text-[var(--ink)]">
        Bill details
      </h2>

      {showNudge ? (
        <p
          aria-live="polite"
          className="flex items-center gap-2 border-b border-[var(--header-line)] bg-blue-50 px-4 py-2.5 text-[12px] font-semibold text-[var(--blue)]"
        >
          <svg viewBox="0 0 16 16" aria-hidden className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M1.5 6.5h13M3 6.5V12a1 1 0 001 1h8a1 1 0 001-1V6.5M1.5 4.5h13v2h-13z" />
          </svg>
          {/* The figure is the server's `gap_to_free_delivery_minor`, not a difference computed here. */}
          Add {formatMinor(gap, quote.currency)} more for free delivery
        </p>
      ) : null}

      <dl className="px-4 py-3">
        <Row label="Items subtotal">
          <Amount minor={quote.items_subtotal_minor} currency={quote.currency} />
        </Row>
        <Row label="Tax on items" muted>
          <Amount minor={quote.items_tax_minor} currency={quote.currency} />
        </Row>
        <Row label="Delivery fee">
          {quote.free_delivery_applied ? (
            <span className="font-semibold text-[var(--green)]">Free</span>
          ) : (
            <Amount minor={quote.delivery_fee_minor} currency={quote.currency} />
          )}
        </Row>
        <Row label="Tax on delivery" muted>
          <Amount minor={quote.delivery_tax_minor} currency={quote.currency} />
        </Row>

        <div className="mt-2 flex items-baseline justify-between gap-4 border-t border-[var(--header-line)] pt-3">
          <dt className="text-[14px] font-bold text-[var(--ink)]">Total</dt>
          <dd className="text-[16px] font-extrabold text-[var(--ink)]">
            <Amount money={quote.total} />
          </dd>
        </div>
      </dl>

      <footer className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-[var(--header-line)] px-4 py-2.5">
        <p className="text-[11px] text-[var(--ink-5)]">
          quote fingerprint{" "}
          <span className="font-mono text-[var(--ink-4)]" title={quote.content_hash}>
            {quote.content_hash.slice(0, 12)}…
          </span>
        </p>
        <p className="text-[11px] text-[var(--ink-5)]">
          priced by {quote.source} at catalogue revision <span className="tnum">{quote.catalogue_revision}</span>
        </p>
      </footer>
    </section>
  );
}

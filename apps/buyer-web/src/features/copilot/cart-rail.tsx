/**
 * The cart, standing beside the conversation rather than hidden behind an icon.
 *
 * The copilot's old cart was a one-line strip under the transcript saying how many items
 * there were. That is enough to prove a write landed and nothing else: a buyer who had
 * been talking for a minute could not see what they had agreed to, what each thing cost,
 * or how far off free delivery was, and the assistant's "added it" was the only evidence
 * either way. Several times during development the strip said one thing and the server
 * said another, and there was nothing on screen to catch it.
 *
 * So the cart is a column of its own, permanently open, showing every line with its
 * photograph, its price and its own quantity control. Nothing here is computed: the
 * quantities, the amounts, the delivery gap and the total all come off the quote the
 * server returned, because a storefront that adds two numbers together can disagree with
 * the merchant about money, and nobody notices until the payment.
 *
 * Once a checkout is open the same column keeps the ledger instead. The lines the buyer is
 * being asked to pay for do not disappear behind the approval card -- they are the
 * approval card's subject, and an unpaid order that is invisible until you scroll is how a
 * buyer ends up approving a total they cannot account for.
 */

/* eslint-disable @next/next/no-img-element */

"use client";

import { useMemo } from "react";

import { Amount, cx } from "@/components/ui";
import type { Cart, Checkout, Quote } from "@/lib/api/types";
import { PLACEHOLDER_IMAGE, primaryImage } from "@/lib/product-images";

/** A line as this column draws it: the quote's figures, never the caller's arithmetic. */
interface RailLine {
  sku: string;
  name: string;
  quantity: number;
  unitPriceMinor: number;
  subtotalMinor: number;
}

function linesOf(quote: Quote | null): RailLine[] {
  if (!quote) return [];
  return quote.lines.map((line): RailLine => ({
    sku: line.sku,
    name: line.name,
    quantity: line.quantity,
    unitPriceMinor: line.unit_price_minor,
    subtotalMinor: line.subtotal_minor,
  }));
}

function Photo({ sku, name }: { sku: string; name: string }) {
  return (
    <img
      src={primaryImage(sku)}
      alt=""
      aria-hidden="true"
      width={56}
      height={56}
      loading="lazy"
      decoding="async"
      className="size-14 shrink-0 rounded-xl border border-white/10 bg-white/[0.04] object-cover"
      onError={(event) => {
        // The merchant's catalogue outruns the photographs; a missing file is not a
        // missing product, and a broken-image glyph beside a real line reads as breakage.
        const element = event.currentTarget;
        if (element.src !== PLACEHOLDER_IMAGE) element.src = PLACEHOLDER_IMAGE;
      }}
      title={name}
    />
  );
}

function Stepper({
  quantity,
  busy,
  onChange,
}: {
  quantity: number;
  busy: boolean;
  onChange: (next: number) => void;
}) {
  return (
    <div className="inline-flex items-center gap-1 rounded-full border border-white/12 bg-white/[0.04] p-0.5">
      <button
        type="button"
        disabled={busy}
        onClick={() => onChange(quantity - 1)}
        aria-label={quantity === 1 ? "Remove from cart" : "One fewer"}
        className="flex size-6 items-center justify-center rounded-full text-slate-300 transition-colors hover:bg-white/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
      >
        <svg viewBox="0 0 16 16" className="size-3" aria-hidden="true">
          <path d="M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      </button>
      <span
        className="min-w-5 text-center font-mono text-xs tabular-nums text-white"
        aria-label={`Quantity ${quantity}`}
      >
        {quantity}
      </span>
      <button
        type="button"
        disabled={busy}
        onClick={() => onChange(quantity + 1)}
        aria-label="One more"
        className="flex size-6 items-center justify-center rounded-full text-slate-300 transition-colors hover:bg-white/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
      >
        <svg viewBox="0 0 16 16" className="size-3" aria-hidden="true">
          <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  );
}

/**
 * How close the cart is to free delivery, when the merchant says there is a threshold.
 *
 * `gap_to_free_delivery_minor` and `free_delivery_applied` are both the quote's own
 * fields. The bar's width is the only number this component works out, and it is a
 * proportion of pixels rather than of money, so it can be wrong about the drawing and
 * never about the amount.
 */
function DeliveryProgress({
  gapMinor,
  applied,
  currency,
  totalMinor,
}: {
  gapMinor: number | null;
  applied: boolean;
  currency: string;
  totalMinor: number;
}) {
  if (applied) {
    return (
      <p className="flex items-center gap-1.5 text-xs font-medium text-emerald-300">
        <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true" fill="none">
          <path
            d="M3 8.5l3 3 7-7"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Delivery is free on this order.
      </p>
    );
  }
  if (gapMinor === null || gapMinor <= 0) return null;
  const thresholdMinor = totalMinor + gapMinor;
  const filled = thresholdMinor > 0 ? Math.min(100, (totalMinor / thresholdMinor) * 100) : 0;
  return (
    <div className="space-y-1.5">
      <p className="text-xs text-slate-300">
        <span className="font-semibold text-white">
          <Amount minor={gapMinor} currency={currency} />
        </span>{" "}
        away from free delivery
      </p>
      <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
        <div
          className="h-full rounded-full bg-gradient-to-r from-amber-400 to-emerald-400 transition-[width] duration-500"
          style={{ width: `${filled}%` }}
        />
      </div>
    </div>
  );
}

function Row({
  label,
  minor,
  currency,
  strong = false,
}: {
  label: string;
  minor: number;
  currency: string;
  strong?: boolean;
}) {
  return (
    <div
      className={cx(
        "flex items-baseline justify-between gap-3",
        strong ? "text-sm text-white" : "text-xs text-slate-400",
      )}
    >
      <span className={strong ? "font-semibold" : ""}>{label}</span>
      <span className={cx("font-mono tabular-nums", strong && "text-base font-semibold")}>
        <Amount minor={minor} currency={currency} />
      </span>
    </div>
  );
}

export function CartRail({
  cart,
  checkout,
  card,
  busySku,
  writing,
  onSetQuantity,
  onCheckout,
  className,
}: {
  cart: Cart | null;
  /**
   * The open checkout, when there is one. Its presence turns this column from a cart the
   * buyer can still edit into the unpaid order they are being asked to pay for -- the same
   * lines, the same figures, described as what they now are.
   */
  checkout: Checkout | null;
  /**
   * The card this checkout is about, kept by the host for as long as the checkout lasts.
   *
   * Admission consumes the approval, so from EXECUTION_PENDING onwards the checkout read
   * carries no `approval_card` -- and reading it from there alone made this column fall
   * back to an empty cart at the exact moment the buyer was being asked for money by the
   * provider's sheet. What they are paying for does not stop being true because the
   * approval was spent.
   */
  card: Checkout["approval_card"];
  busySku: string | null;
  /** A write is in flight somewhere in the cart, so every control is held. */
  writing: boolean;
  onSetQuantity: (sku: string, quantity: number) => void;
  onCheckout: () => void;
  className?: string;
}) {
  const quote = useMemo(
    () => (card?.quote != null ? card.quote : (cart?.quote ?? null)),
    [cart, card],
  );
  const lines = useMemo(() => linesOf(quote), [quote]);
  const unpaid = checkout !== null && checkout.state !== "PAID" && card !== null;
  const count = lines.reduce((sum, line) => sum + line.quantity, 0);

  return (
    <aside
      aria-label={unpaid ? "Your unpaid order" : "Your cart"}
      className={cx(
        "flex h-full min-h-0 flex-col border-l border-[var(--rzp-line)] bg-[var(--rzp-navy-deep)]/80",
        className,
      )}
    >
      <header className="flex shrink-0 items-center justify-between gap-2 border-b border-[var(--rzp-line)] px-4 py-3">
        <h2 className="text-sm font-semibold text-white">{unpaid ? "Unpaid order" : "Cart"}</h2>
        {unpaid ? (
          <span className="rounded-full bg-amber-400/15 px-2 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            Awaiting payment
          </span>
        ) : count > 0 ? (
          <span className="rounded-full bg-white/10 px-2 py-0.5 text-[11px] font-medium text-slate-200">
            {count} {count === 1 ? "item" : "items"}
          </span>
        ) : null}
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {lines.length === 0 ? (
          <p className="mt-6 text-center text-xs leading-relaxed text-slate-500">
            Nothing here yet.
            <br />
            Ask for something, or open the store.
          </p>
        ) : (
          <ul role="list" className="space-y-3">
            {lines.map((line) => (
              <li key={line.sku} className="cart-line-enter flex gap-3">
                <Photo sku={line.sku} name={line.name} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-2">
                    <p className="min-w-0 text-[13px] font-medium leading-snug text-slate-100">
                      {line.name}
                    </p>
                    <p className="shrink-0 font-mono text-[13px] font-semibold tabular-nums text-white">
                      <Amount minor={line.subtotalMinor} currency={quote?.currency ?? "INR"} />
                    </p>
                  </div>
                  <p className="mt-0.5 font-mono text-[11px] tabular-nums text-slate-500">
                    <Amount minor={line.unitPriceMinor} currency={quote?.currency ?? "INR"} /> each
                  </p>
                  {/* Editable while it is a cart; a ledger once a checkout is open, because
                      the quantities on screen are then the ones the buyer is approving and
                      a stepper beside them would invite an edit that retires the approval
                      without saying so. Changing the order is still possible -- by asking
                      -- and then the card comes back at the new total. */}
                  {unpaid ? (
                    <p className="mt-1.5 font-mono text-[11px] tabular-nums text-slate-500">
                      × {line.quantity}
                    </p>
                  ) : (
                    <div className="mt-1.5">
                      <Stepper
                        quantity={line.quantity}
                        busy={writing || busySku === line.sku}
                        onChange={(next) => onSetQuantity(line.sku, Math.max(0, next))}
                      />
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {quote !== null && lines.length > 0 ? (
        <footer className="shrink-0 space-y-3 border-t border-[var(--rzp-line)] px-4 py-3">
          {!unpaid ? (
            <DeliveryProgress
              gapMinor={quote.gap_to_free_delivery_minor}
              applied={quote.free_delivery_applied}
              currency={quote.currency}
              totalMinor={quote.total_minor}
            />
          ) : null}

          <div className="space-y-1">
            <Row label="Items" minor={quote.items_subtotal_minor} currency={quote.currency} />
            <Row label="Tax on items" minor={quote.items_tax_minor} currency={quote.currency} />
            <Row label="Delivery" minor={quote.delivery_fee_minor} currency={quote.currency} />
            {quote.delivery_tax_minor > 0 ? (
              <Row
                label="Tax on delivery"
                minor={quote.delivery_tax_minor}
                currency={quote.currency}
              />
            ) : null}
            {quote.discount_minor > 0 ? (
              <Row
                label={quote.offer_label ?? "Offer"}
                minor={-quote.discount_minor}
                currency={quote.currency}
              />
            ) : null}
            <div className="!mt-2 border-t border-white/10 pt-2">
              <Row
                label={unpaid ? "Amount to pay" : "Total"}
                minor={unpaid ? (card?.amount_minor ?? quote.total_minor) : quote.total_minor}
                currency={quote.currency}
                strong
              />
            </div>
          </div>

          {/* No checkout button once one is open: the approval card in the conversation is
              the only place this order moves forward from, and a second control that looks
              like it starts the same thing is how a buyer opens a checkout twice. */}
          {!unpaid ? (
            <button
              type="button"
              onClick={onCheckout}
              disabled={writing}
              className="rzp-action w-full rounded-xl py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              Review order
            </button>
          ) : (
            <p className="text-center text-[11px] leading-relaxed text-slate-500">
              Approve it in the conversation to pay.
            </p>
          )}
        </footer>
      ) : null}
    </aside>
  );
}

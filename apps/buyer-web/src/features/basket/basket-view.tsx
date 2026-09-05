/**
 * The basket screen: what the buyer has, what it costs, and what happens next.
 *
 * Two facts this screen refuses to hide. A line the merchant can no longer price stays
 * on the page, muted, rather than disappearing between renders. And `stale` -- the store
 * moved while this basket was open -- is shown as a notice, because a re-price is a real
 * event in this system, and a storefront that swapped the total silently would be
 * training the buyer to trust a number that had already changed once without telling them.
 *
 * "Proceed to checkout" opens a checkout and hands off. From that moment the basket is
 * closed server-side: it has become checkout version 1, with its own reservation and its
 * own frozen policy receipt, so the stored basket identifier is dropped here rather than
 * left behind to fail the next write.
 */
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useRef, useState, type ReactNode } from "react";

import { useBasketContext } from "@/components/providers";
import { Button, ErrorState, Skeleton, cx } from "@/components/ui";
import { api, newIdempotencyKey } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";

import { BasketLine } from "./basket-line";
import { QuoteSummary } from "./quote-summary";
import { unavailableSkus, useBasket } from "./use-basket";

function Notice({
  tone,
  title,
  children,
}: {
  tone: "amber" | "red";
  title: string;
  children?: ReactNode;
}) {
  const palette =
    tone === "amber"
      ? "border-[var(--amber)] bg-amber-50 text-[var(--ink-2)]"
      : "border-[var(--red)] bg-red-50 text-[var(--ink-2)]";
  return (
    <div role="status" aria-live="polite" className={cx("rounded-[var(--r-md)] border px-4 py-3", palette)}>
      <p className="text-[13px] font-bold text-[var(--ink)]">{title}</p>
      {children ? <div className="mt-1 text-[12px] leading-relaxed">{children}</div> : null}
    </div>
  );
}

function LoadingBasket() {
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
      <div className="space-y-2">
        {[0, 1, 2].map((row) => (
          <div key={row} className="flex items-center gap-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] p-4">
            <Skeleton className="h-16 w-16" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3 w-1/2" />
              <Skeleton className="h-3 w-1/4" />
            </div>
            <Skeleton className="h-9 w-[112px]" />
          </div>
        ))}
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}

export function BasketView() {
  const router = useRouter();
  const { setBasketId } = useBasketContext();
  const { basketId, basket, loading, error, busySku, setQuantity, reload } = useBasket();

  const [opening, setOpening] = useState(false);
  const [handoffError, setHandoffError] = useState<string | null>(null);
  /** Held across retries so a checkout lost to a dropped response is never opened twice. */
  const handoffKey = useRef<string | null>(null);

  const proceed = useCallback(async () => {
    if (!basketId) return;
    setHandoffError(null);
    setOpening(true);
    if (!handoffKey.current) handoffKey.current = newIdempotencyKey();
    try {
      const checkout = await api.openCheckout(basketId, handoffKey.current);
      // The server has closed this basket into version 1. Forget it here too, so a later
      // add opens a new basket instead of writing to one that can only answer 409;
      // dropping the identifier is also what zeroes the count in the header.
      setBasketId(null);
      router.push(`/checkout/${encodeURIComponent(checkout.checkout_id)}`);
    } catch (cause) {
      setHandoffError(humanMessage(cause));
      setOpening(false);
    }
  }, [basketId, router, setBasketId]);

  if (opening) {
    return (
      <div role="status" aria-live="polite" className="flex flex-col items-center gap-3 px-6 py-24 text-center">
        <p className="text-[16px] font-semibold text-[var(--ink)]">Opening your checkout</p>
        <p className="max-w-md text-[13px] text-[var(--ink-4)]">
          The merchant is pricing this basket into a version you can approve, reserving the stock and
          freezing the policies that apply to it.
        </p>
      </div>
    );
  }

  if (loading && !basket) return <LoadingBasket />;

  if (error && !basket) {
    return <ErrorState title="This basket could not be loaded" detail={error} onRetry={() => void reload()} />;
  }

  const lines = basket?.lines ?? [];
  if (!basket || lines.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 px-6 py-24 text-center">
        <p className="text-[16px] font-semibold text-[var(--ink)]">Your basket is empty</p>
        <p className="max-w-md text-[13px] text-[var(--ink-4)]">
          Nothing has been added yet. Items you add are priced by the merchant, not by this page.
        </p>
        <Link
          href="/"
          className="mt-1 inline-flex h-10 items-center justify-center rounded-[var(--r-md)] bg-[var(--green)] px-4 text-[14px] font-semibold text-white transition hover:brightness-95"
        >
          Start shopping
        </Link>
      </div>
    );
  }

  const quote = basket.quote;
  const quoteLines = quote?.lines ?? [];
  const priced = new Set(quoteLines.map((line) => line.sku));
  // Anything the quote left out is a line the merchant declined to price. It keeps its
  // place on the screen; only its price is missing, because there is no price to show.
  const dropped = lines.filter((line) => !priced.has(line.sku));
  // `basket.unavailable` names the same lines, and it is what the merchant says out loud
  // rather than what the quote's omission implies. It is read for the wording below.
  const declared = new Set(unavailableSkus(basket));

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-[20px] font-bold text-[var(--ink)]">Your basket</h1>
        <p className="tnum text-[12px] text-[var(--ink-4)]">
          {lines.length} {lines.length === 1 ? "item" : "items"}
        </p>
      </div>

      {basket.stale ? (
        <Notice tone="amber" title="This basket was re-priced">
          The merchant&rsquo;s catalogue moved while the basket was open, so every amount below has been
          recomputed at the current revision. The figures you see now are the ones a checkout would be
          built from.
        </Notice>
      ) : null}

      {error ? (
        <Notice tone="red" title="The last change did not go through">
          {error}{" "}
          <button
            type="button"
            onClick={() => void reload()}
            className="font-semibold text-[var(--blue)] underline underline-offset-2"
          >
            Reload the basket
          </button>
        </Notice>
      ) : null}

      {handoffError ? <Notice tone="red" title="Checkout could not be opened">{handoffError}</Notice> : null}

      <div className="grid gap-6 lg:grid-cols-[1fr_360px] lg:items-start">
        <ul
          className="overflow-hidden rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-white"
          style={{ boxShadow: "var(--card-shadow)" }}
        >
          {quoteLines.map((line) => (
            <BasketLine
              key={line.sku}
              sku={line.sku}
              name={line.name}
              quantity={line.quantity}
              unitPriceMinor={line.unit_price_minor}
              subtotalMinor={line.subtotal_minor}
              currency={quote?.currency}
              busy={busySku === line.sku}
              onSetQuantity={(quantity) => void setQuantity(line.sku, quantity)}
            />
          ))}
          {dropped.map((line) => (
            <BasketLine
              key={line.sku}
              sku={line.sku}
              name={null}
              quantity={line.quantity}
              unavailable
              busy={busySku === line.sku}
              onSetQuantity={(quantity) => void setQuantity(line.sku, quantity)}
            />
          ))}
        </ul>

        <div className="space-y-3 lg:sticky lg:top-4">
          {quote ? (
            <QuoteSummary quote={quote} repricing={busySku !== null} />
          ) : (
            <Notice tone="amber" title="This basket cannot be priced">
              {declared.size > 0
                ? `The merchant declined ${declared.size} of these lines, so it returned no total. Remove them and the total comes back.`
                : "The merchant returned no quote for this basket, so there is no total to show."}
              <span className="mt-1 block font-mono text-[11px] text-[var(--ink-4)]">code {basket.code}</span>
            </Notice>
          )}

          <Button
            size="lg"
            className="w-full"
            onClick={() => void proceed()}
            disabled={!quote || busySku !== null}
          >
            Proceed to checkout
          </Button>

          <p className="text-[11px] leading-relaxed text-[var(--ink-5)]">
            Opening a checkout freezes this quote as version 1 and reserves the stock. Nothing is charged
            until you approve that exact version on the next screen.
          </p>
        </div>
      </div>
    </div>
  );
}

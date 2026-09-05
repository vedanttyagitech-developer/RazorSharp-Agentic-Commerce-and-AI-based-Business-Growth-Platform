/**
 * The basket screen: what the buyer has, what it costs, and what happens next.
 *
 * Three facts this screen refuses to hide. A line the merchant can no longer price stays
 * on the page rather than disappearing between renders. A line it declined says so in the
 * merchant's own numbers -- what was asked for, what exists -- and keeps every control
 * that could resolve it, because a refusal a buyer cannot act on is just a dead end. And
 * `stale` -- the store moved while this basket was open -- is shown as a notice, because a
 * re-price is a real event in this system, and a storefront that swapped the total
 * silently would be training the buyer to trust a number that had already changed once
 * without telling them.
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
import { requiresOwnDocument } from "@/lib/security/csp";

import { BasketLine } from "./basket-line";
import { QuoteSummary } from "./quote-summary";
import { unavailableBySku, useBasket } from "./use-basket";

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
  const { basketId, basket, names, loading, error, busySku, setQuantity, reload } = useBasket();

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
      // A document navigation, not `router.push`. The checkout is the one route with its
      // own Content-Security-Policy, and a policy belongs to a document: pushing here
      // kept `/basket`'s policy and Razorpay's script was refused on arrival. See
      // `requiresOwnDocument` in `lib/security/csp`.
      const href = `/checkout/${encodeURIComponent(checkout.checkout_id)}`;
      // The rule below recommends `router.push` for an internal route, and for every other
      // internal route it is right. This is the exception it cannot see: a client-side push
      // keeps this document, and with it this document's policy, which does not admit
      // Razorpay. Taking the rule's advice is what left the Pay button reporting that the
      // provider could not be reached.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      if (requiresOwnDocument(href)) window.location.assign(href);
      else router.push(href);
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
  /*
   * Which lines the merchant actually refused, and why.
   *
   * `basket.unavailable` is the merchant saying so; the quote's silence is not. This
   * screen used to mark every line missing from `quote.lines` as unavailable, which is
   * correct only while a quote exists. `merchant_sim.fees.quote_basket` refuses a basket
   * whole rather than pricing the remainder, so when it declines one line the quote is
   * null and "missing from the quote" becomes "all of them": one line over stock struck
   * out three products, replaced the bill with a sentence that said 1 while the page
   * showed 3, and took away the steppers -- removing the one control that could have
   * fixed it. A buyer who added milk and too much rice was told the milk had gone; it had
   * not, it was never asked about. The merchant's own list is the truth about which line
   * is at fault.
   */
  const priced = new Map((quote?.lines ?? []).map((line) => [line.sku, line]));
  const refused = unavailableBySku(basket);
  const declined = basket.unavailable;
  /** Lines the merchant priced happily. Zero of them is possible; saying "the others are fine" then is not. */
  const untouched = lines.length - declined.length;
  /*
   * What the buyer should actually do. A line the merchant still stocks comes down to the
   * count it named; a line it has withdrawn or has none of can only leave. Saying "reduce
   * them" over a delisted product would send the buyer hunting for a quantity that does
   * not exist.
   */
  const fixable = declined.filter((entry) => entry.listed && entry.available_units > 0).length;
  const remedy =
    fixable === declined.length
      ? `Bring ${declined.length === 1 ? "that line" : "those lines"} down to what is available and the total comes back.`
      : fixable === 0
        ? `Remove ${declined.length === 1 ? "it" : "them"} and the total comes back.`
        : "Bring each of those lines down to what is available, or remove it, and the total comes back.";

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
          {/*
            One pass over the basket's own lines, in the basket's own order. Each row is
            told what the quote said about it (nothing, when there is no quote) and what
            the merchant said about it, and decides for itself what it can still offer.
          */}
          {lines.map((line) => {
            const quoted = priced.get(line.sku) ?? null;
            const shortfall = refused.get(line.sku) ?? null;
            return (
              <BasketLine
                key={line.sku}
                sku={line.sku}
                name={quoted?.name ?? names[line.sku] ?? null}
                quantity={quoted?.quantity ?? line.quantity}
                unitPriceMinor={quoted?.unit_price_minor}
                subtotalMinor={quoted?.subtotal_minor}
                currency={quote?.currency}
                shortfall={shortfall}
                // A quote exists, this line is not in it, and the merchant named no
                // reason. Only then is the omission itself the thing to report.
                unexplained={quote !== null && quoted === null && shortfall === null}
                busy={busySku === line.sku}
                onSetQuantity={(quantity) => void setQuantity(line.sku, quantity)}
              />
            );
          })}
        </ul>

        <div className="space-y-3 lg:sticky lg:top-4">
          {quote ? (
            <QuoteSummary quote={quote} repricing={busySku !== null} />
          ) : (
            <Notice tone="amber" title="This basket has no total yet">
              {declined.length > 0 ? (
                <>
                  <p>
                    The merchant could not price{" "}
                    <span className="tnum font-semibold">{declined.length}</span> of these{" "}
                    <span className="tnum">{lines.length}</span> lines, so it returned no total for
                    the basket.
                    {untouched > 0
                      ? ` The other ${untouched === 1 ? "line is" : `${untouched} lines are`} priced as before and will be again.`
                      : ""}
                  </p>
                  {/* The merchant's own integers for the lines it named, restated, not derived. */}
                  <ul className="mt-1.5 space-y-0.5">
                    {declined.map((entry) => (
                      <li key={entry.sku} className="tnum">
                        <span className="font-semibold">{names[entry.sku] ?? entry.sku}</span> &mdash;
                        you asked for {entry.requested},{" "}
                        {entry.listed
                          ? `the merchant has ${entry.available_units}`
                          : "the merchant no longer lists it"}
                      </li>
                    ))}
                  </ul>
                  <p className="mt-1.5">{remedy}</p>
                </>
              ) : (
                "The merchant returned no quote for this basket, so there is no total to show."
              )}
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

"use client";
import { ShoppingCart, Check, MapPin, Package, ChevronDown, ShieldCheck } from "lucide-react";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { FreshnessLine } from "@/components/availability";
import { useBasketRef, useClient } from "@/components/providers";
import { QuoteBreakdown } from "@/components/quote-breakdown";
import { Alert, Button, Spinner, StatusPill } from "@/components/ui";
import { isApiError } from "@/lib/api/problem";
import type { Basket } from "@/lib/api/types";
import { JOURNEY_META } from "@/lib/journey";
import { formatMinor } from "@/lib/money";

import { useBasketActions } from "./use-basket-actions";
import { SafeImage } from "@/components/product-img";
import { getProductImage } from "@/lib/product-images";

export function BasketView() {
  const client = useClient();
  const router = useRouter();
  const { basketId, setBasketId, setLineCount } = useBasketRef();
  const {
    setQuantity,
    busySku,
    isMutating,
    error: actionError,
    feedback,
    lastFailedAction,
    retryLastAction,
  } = useBasketActions();

  const [basket, setBasket] = useState<Basket | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checkingOut, setCheckingOut] = useState(false);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    let cancelled = false;
    if (!basketId) {
      Promise.resolve().then(() => {
        if (!cancelled) setLoaded(true);
      });
      return () => {
        cancelled = true;
      };
    }
    client
      .getBasket(basketId)
      .then((result) => {
        if (cancelled) return;
        setBasket(result);
        setLineCount(result.lines.length);
        setLoaded(true);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        if (isApiError(cause) && cause.status === 404) {
          // The remembered id is stale; forget it so the next add starts a fresh basket.
          setBasketId(null);
          setLineCount(0);
        } else {
          setError(cause instanceof Error ? cause.message : "Basket unavailable");
        }
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [basketId, client, setBasketId, setLineCount]);

  async function change(sku: string, quantity: number, itemName?: string) {
    if (isMutating || checkingOut) return;
    const name = itemName ?? sku;
    const updated = await setQuantity(sku, quantity, name);
    if (updated) {
      setBasket(updated);
      setAnnouncement(
        quantity === 0
          ? `Removed ${name} from basket.`
          : `Updated ${name} quantity to ${quantity}.`,
      );
    }
  }

  async function checkout() {
    if (!basketId || isMutating || checkingOut) return;
    setCheckingOut(true);
    setError(null);
    try {
      const created = await client.checkoutBasket(basketId);
      router.push(`/checkout/${encodeURIComponent(created.checkout_id)}`);
    } catch (cause) {
      setError(
        isApiError(cause)
          ? `${cause.title}${cause.detail ? `: ${cause.detail}` : ""}${cause.code ? ` (${cause.code})` : ""}`
          : cause instanceof Error
            ? cause.message
            : "Checkout failed",
      );
      setCheckingOut(false);
    }
  }

  async function handleRetry() {
    const updated = await retryLastAction();
    if (updated) {
      setBasket(updated);
      setAnnouncement("Basket updated successfully.");
    }
  }

  if (!loaded) {
    return (
      <div className="py-12 text-center">
        <Spinner label="Loading your basket..." />
      </div>
    );
  }

  // Empty basket state
  if (!basketId || !basket || basket.lines.length === 0) {
    return (
      <section
        aria-labelledby="basket-empty-heading"
        className="mx-auto max-w-md space-y-6 rounded-3xl border border-line bg-surface p-10 text-center shadow-sm"
      >
        <div className="mx-auto flex h-20 w-20 items-center justify-center rounded-full bg-[#fbf5ff] text-4xl dark:bg-stone-800">
          <ShoppingCart className="h-10 w-10 stroke-[1.5] text-muted/40" aria-hidden="true" />
        </div>
        <div className="space-y-2">
          <h1 id="basket-empty-heading" className="text-2xl font-black tracking-tight text-foreground">
            Your Basket is Empty
          </h1>
          <p className="text-xs sm:text-sm text-muted">
            You have no items in your basket yet. Explore our grocery catalogue to add fresh staples and dairy.
          </p>
        </div>
        <div>
          <Link
            href="/"
            className="inline-flex min-h-11 items-center justify-center rounded-xl bg-accent px-6 py-2.5 text-sm font-bold text-accent-ink shadow-sm hover:opacity-90 active:scale-95 transition"
          >
            Browse Catalogue
          </Link>
        </div>
        {error ? (
          <Alert tone="danger" title="Basket unavailable" role="alert">
            {error}
          </Alert>
        ) : null}
      </section>
    );
  }

  const quoteMeta = JOURNEY_META.QUOTE_CALCULATED;
  const canCheckout =
    basket.quote !== null &&
    basket.unavailable.length === 0 &&
    basket.lines.length > 0 &&
    !checkingOut &&
    !isMutating;

  return (
    <div className="space-y-6">
      {/* Screen-reader live region for announcements */}
      <div role="status" aria-live="polite" className="sr-only">
        {announcement || feedback?.message}
      </div>

      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-black tracking-tight text-foreground">
            Review Your Basket
          </h1>
          <p className="text-xs text-muted">
            Basket ID: <span className="font-mono">{basket.basket_id}</span> · Prices calculated deterministically by fee engine
          </p>
        </div>
        <div role="status" aria-live="polite">
          <StatusPill
            tone={quoteMeta.tone}
            glyph={quoteMeta.glyph}
            label={basket.quote ? quoteMeta.label : "Awaiting priceable basket"}
          />
        </div>
      </div>

      {/* Feedback banner */}
      {feedback ? (
        <div
          role="status"
          className="rounded-xl border border-emerald-300 bg-emerald-50 px-4 py-2.5 text-xs font-semibold text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950 dark:text-emerald-100 shadow-xs"
        >
          <span className="inline-flex items-center gap-1.5"><Check className="h-3.5 w-3.5 stroke-[2.5] text-emerald-600 dark:text-emerald-400" aria-hidden="true" /><span>{feedback.message}</span></span>
        </div>
      ) : null}

      {/* Action and Checkout Errors */}
      {actionError ? (
        <Alert tone="danger" title="Basket update failed" role="alert">
          <p className="mb-2 text-xs">{actionError}</p>
          {lastFailedAction ? (
            <Button variant="secondary" onClick={() => void handleRetry()}>
              Retry action
            </Button>
          ) : null}
        </Alert>
      ) : null}

      {error ? (
        <Alert tone="danger" title="Checkout could not start" role="alert">
          <p className="text-xs">{error}</p>
        </Alert>
      ) : null}

      {/* 2-Column Split Layout: Items on Left, Sticky Bill Details on Right */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12 items-start">
        {/* Left Column: Delivery pill + Line Items */}
        <div className="space-y-4 lg:col-span-7">
          {/* Simulated Service Area Banner */}
          <div className="flex items-center gap-3 rounded-2xl border border-line bg-[#f8f6fb] dark:bg-stone-800/40 px-4 py-3 text-xs text-muted">
            <MapPin className="h-4 w-4 text-[#0c831f] shrink-0 mt-0.5" aria-hidden="true" />
            <div>
              <p className="font-bold text-foreground">Delivering to: Central Mumbai, 400001</p>
              <p className="text-[11px] text-muted">Quick commerce test simulation · Authoritative catalogue</p>
            </div>
          </div>

          {/* Line Items List */}
          <section aria-labelledby="basket-items-heading" className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 id="basket-items-heading" className="text-base font-bold text-foreground">
                Cart Items ({basket.lines.length})
              </h2>
              <FreshnessLine freshness={basket.freshness} />
            </div>

            <ul className="space-y-3" role="list">
              {basket.lines.map((line) => {
                const priced = basket.quote?.lines.find((q) => q.sku === line.sku);
                const unavailable = basket.unavailable.find((u) => u.sku === line.sku);
                const itemName = priced?.name ?? line.sku;
                const isLineBusy = busySku === line.sku || isMutating;

                return (
                  <li
                    key={line.sku}
                    className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-line bg-surface p-4 shadow-xs transition hover:border-[#0c831f]/40"
                  >
                    <div className="flex items-center gap-3 min-w-48 flex-1">
                      <Link
                        href={`/products/${encodeURIComponent(line.sku)}`}
                        className="h-14 w-14 rounded-xl border border-stone-100 bg-[#f8f8fa] p-1 shrink-0 overflow-hidden flex items-center justify-center focus:outline-none focus:ring-2 focus:ring-[#0c831f]"
                        aria-label={`View ${itemName}`}
                      >
                        <SafeImage
                          src={getProductImage(line.sku)}
                          alt={itemName}
                          fallbackIcon={<Package className="h-8 w-8 stroke-1 text-muted/40" />}
                          className="w-full h-full object-contain"
                        />
                      </Link>

                      <div className="space-y-0.5 flex-1">
                        <Link
                          href={`/products/${encodeURIComponent(line.sku)}`}
                          className="font-bold text-sm text-foreground hover:text-[#0c831f] hover:underline focus:outline-none focus:ring-1 focus:ring-[#0c831f]"
                        >
                          {itemName}
                        </Link>
                        <p className="font-mono text-[11px] text-stone-400">{line.sku}</p>

                        {/* Unavailable warning */}
                        {unavailable ? (
                          <p className="text-xs font-semibold text-rose-600" role="alert">
                            {unavailable.listed
                              ? `Only ${unavailable.available_units} units available in stock`
                              : "This product is no longer listed"}
                          </p>
                        ) : null}

                        {/* Pricing details */}
                        {priced ? (
                          <p className="text-xs text-muted">
                            {formatMinor(priced.unit_price_minor, basket.quote!.currency)} each · Line subtotal:{" "}
                            <strong className="text-foreground tabular-nums font-bold">
                              {formatMinor(priced.subtotal_minor, basket.quote!.currency)}
                            </strong>
                          </p>
                        ) : null}
                      </div>
                    </div>

                    {/* Quantity Controls & Remove */}
                    <div
                      className="flex items-center gap-3"
                      role="group"
                      aria-label={`Quantity controls for ${itemName}`}
                    >
                      <div className="inline-flex min-h-[44px] items-center rounded-xl border border-line bg-surface shadow-xs">
                        <button
                          type="button"
                          onClick={() => void change(line.sku, line.quantity - 1, itemName)}
                          disabled={isLineBusy || checkingOut}
                          className="flex min-h-[44px] min-w-[44px] items-center justify-center text-base font-bold text-muted hover:text-foreground disabled:opacity-30 active:scale-95 transition focus-visible:ring-2 focus-visible:ring-[#0c831f] cursor-pointer touch-manipulation"
                          aria-label={`Decrease quantity of ${itemName}`}
                        >
                          −
                        </button>
                        <span
                          className="w-7 text-center text-xs font-black tabular-nums"
                          aria-live="polite"
                          aria-label={`${line.quantity} units`}
                        >
                          {line.quantity}
                        </span>
                        <button
                          type="button"
                          onClick={() => void change(line.sku, line.quantity + 1, itemName)}
                          disabled={isLineBusy || checkingOut}
                          className="flex min-h-[44px] min-w-[44px] items-center justify-center text-base font-bold text-muted hover:text-foreground disabled:opacity-30 active:scale-95 transition focus-visible:ring-2 focus-visible:ring-[#0c831f] cursor-pointer touch-manipulation"
                          aria-label={`Increase quantity of ${itemName}`}
                        >
                          +
                        </button>
                      </div>

                      <Button
                        variant="ghost"
                        onClick={() => void change(line.sku, 0, itemName)}
                        disabled={isLineBusy || checkingOut}
                        busy={busySku === line.sku}
                        className="min-h-[44px] text-xs text-rose-600 hover:text-rose-700 dark:text-rose-400 font-semibold px-3 py-2"
                        aria-label={`Remove ${itemName} from basket`}
                      >
                        Remove
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>

          {/* Detailed Per-Line Quote & Provenance Disclosure */}
          <details className="group rounded-2xl border border-line bg-surface/60 p-4 text-xs shadow-xs">
            <summary className="flex cursor-pointer items-center justify-between font-bold text-foreground hover:text-accent select-none">
              <span>Deterministic Quote & Content Hash Proof</span>
              <span className="text-xs font-mono text-muted group-open:rotate-180 transition-transform">
                <ChevronDown className="h-3.5 w-3.5 text-muted transition-transform group-open:rotate-180" aria-hidden="true" />
              </span>
            </summary>
            <div className="mt-4 pt-3 border-t border-line/60">
              <QuoteBreakdown quote={basket.quote} unavailable={basket.unavailable} stale={basket.stale} showDeliveryGap={false} />
            </div>
          </details>
        </div>

        {/* Right Column: Sticky Bill Details & Checkout CTA */}
        <div className="space-y-4 lg:col-span-5 lg:sticky lg:top-20">
          <section aria-labelledby="bill-details-heading" className="rounded-3xl border border-line bg-surface p-6 shadow-sm space-y-4">
            <h2 id="bill-details-heading" className="text-base font-black text-foreground">
              Bill Details
            </h2>

            {basket.quote ? (
              <div className="space-y-2.5 text-xs">
                {/* Pre-tax Items Subtotal */}
                <div className="flex items-center justify-between text-muted">
                  <span>Items Subtotal</span>
                  <span className="font-bold text-foreground tabular-nums">
                    {formatMinor(basket.quote.items_subtotal_minor, basket.quote.currency)}
                  </span>
                </div>

                {/* Delivery Fee */}
                <div className="flex items-center justify-between text-muted">
                  <span>Delivery Partner Fee</span>
                  <span className="font-bold tabular-nums">
                    {basket.quote.free_delivery_applied ? (
                      <span className="text-emerald-600 dark:text-emerald-400 font-bold">FREE</span>
                    ) : (
                      formatMinor(basket.quote.delivery_fee_minor, basket.quote.currency)
                    )}
                  </span>
                </div>

                {/* Delivery Gap Progress if applicable */}
                {!basket.quote.free_delivery_applied && basket.quote.gap_to_free_delivery_minor > 0 ? (
                  <div className="rounded-xl border border-amber-200 bg-amber-50 p-2.5 text-[11px] text-amber-900 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-200">
                    Add <strong>{formatMinor(basket.quote.gap_to_free_delivery_minor, basket.quote.currency)}</strong> more for free delivery
                  </div>
                ) : null}

                {/* Taxes & Charges */}
                <div className="flex items-center justify-between text-muted">
                  <span>GST & Applicable Taxes</span>
                  <span className="font-bold text-foreground tabular-nums">
                    {formatMinor(
                      basket.quote.items_tax_minor + basket.quote.delivery_tax_minor,
                      basket.quote.currency,
                    )}
                  </span>
                </div>

                {/* Grand Total */}
                <div className="pt-3 border-t border-line flex items-baseline justify-between">
                  <div>
                    <span className="text-sm font-black text-foreground block">To Pay</span>
                    <span className="text-[10px] text-muted">Inclusive of all taxes</span>
                  </div>
                  <strong className="text-xl font-black text-foreground tabular-nums">
                    {formatMinor(basket.quote.total_minor, basket.quote.currency)}
                  </strong>
                </div>

                {/* Checkout Button */}
                <div className="pt-2">
                  <button
                    type="button"
                    onClick={() => void checkout()}
                    disabled={!canCheckout}
                    className="w-full h-12 rounded-2xl bg-[#0c831f] hover:bg-[#0a721b] disabled:opacity-40 text-white font-bold text-sm shadow-md transition active:scale-98 flex items-center justify-between px-5"
                    aria-label="Proceed to checkout"
                  >
                    <span className="tabular-nums font-extrabold">
                      {formatMinor(basket.quote.total_minor, basket.quote.currency)}
                    </span>
                    <span className="flex items-center gap-1 uppercase tracking-wider text-xs">
                      {checkingOut ? "Starting Checkout..." : "Proceed to Checkout →"}
                    </span>
                  </button>
                </div>

                <div className="pt-2 text-center">
                  <Link
                    href="/"
                    className="text-xs font-semibold text-muted hover:text-accent transition"
                  >
                    ← Continue Shopping
                  </Link>
                </div>
              </div>
            ) : (
              <p className="text-xs text-muted">Awaiting priceable items...</p>
            )}
          </section>

          {/* Quick Commerce Guarantee Card */}
          <div className="rounded-2xl border border-line/60 bg-[#f8f6fb] dark:bg-stone-800/40 p-4 text-[11px] text-muted space-y-1.5">
            <p className="font-bold text-foreground flex items-center gap-1.5">
              <ShieldCheck className="h-4 w-4 text-emerald-600 dark:text-emerald-400 shrink-0" aria-hidden="true" />
              <span>Trusted Checkout Assurance</span>
            </p>
            <p>
              Admitted once via server-side verification. Bound to Version 1 Policy-at-Sale Receipt with content hash protection.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { FreshnessLine } from "@/components/availability";
import { QuoteBreakdown } from "@/components/quote-breakdown";
import { Alert, Button, Spinner, StatusPill } from "@/components/ui";
import { useBasketRef, useClient } from "@/components/providers";
import { isApiError } from "@/lib/api/problem";
import type { Basket } from "@/lib/api/types";
import { JOURNEY_META } from "@/lib/journey";

import { useBasketActions } from "./use-basket-actions";

export function BasketView() {
  const client = useClient();
  const router = useRouter();
  const { basketId, setLineCount } = useBasketRef();
  const { setQuantity, busySku, error: actionError } = useBasketActions();
  const [basket, setBasket] = useState<Basket | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checkingOut, setCheckingOut] = useState(false);

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
        setError(cause instanceof Error ? cause.message : "Basket unavailable");
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [basketId, client, setLineCount]);

  async function change(sku: string, quantity: number) {
    const updated = await setQuantity(sku, quantity);
    if (updated) setBasket(updated);
  }

  async function checkout() {
    if (!basketId) return;
    setCheckingOut(true);
    setError(null);
    try {
      const created = await client.checkoutBasket(basketId);
      router.push(`/checkout/${encodeURIComponent(created.checkout_id)}`);
    } catch (cause) {
      setError(isApiError(cause) ? `${cause.title}${cause.detail ? `: ${cause.detail}` : ""}${cause.code ? ` (${cause.code})` : ""}` : cause instanceof Error ? cause.message : "Checkout failed");
      setCheckingOut(false);
    }
  }

  if (!loaded) return <Spinner label="Loading basket" />;
  if (!basketId || !basket) {
    return (
      <div className="space-y-3">
        <h1 className="text-2xl font-semibold">Basket</h1>
        <p className="text-sm text-muted">Your basket is empty. <Link href="/" className="underline">Find products</Link>.</p>
        {error ? <Alert tone="danger" title="Basket unavailable" role="alert">{error}</Alert> : null}
      </div>
    );
  }

  const quoteMeta = JOURNEY_META.QUOTE_CALCULATED;
  const canCheckout = basket.quote !== null && basket.unavailable.length === 0 && !checkingOut;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Basket <span className="font-mono text-sm text-muted">{basket.basket_id}</span></h1>
        <div role="status" aria-live="polite">
          <StatusPill tone={quoteMeta.tone} glyph={quoteMeta.glyph} label={basket.quote ? quoteMeta.label : "Awaiting a priceable basket"} />
        </div>
      </div>
      <FreshnessLine freshness={basket.freshness} />

      <ul className="divide-y divide-line rounded-lg border border-line bg-surface">
        {basket.lines.map((line) => {
          const priced = basket.quote?.lines.find((quoted) => quoted.sku === line.sku);
          const unavailable = basket.unavailable.find((item) => item.sku === line.sku);
          return (
            <li key={line.sku} className="flex flex-wrap items-center justify-between gap-3 p-3">
              <div>
                <p className="font-medium">{priced?.name ?? line.sku}</p>
                <p className="font-mono text-xs text-muted">{line.sku}</p>
                {unavailable ? <p className="text-xs text-rose-700 dark:text-rose-300">{unavailable.listed ? `Only ${unavailable.available_units} available` : "No longer listed"}</p> : null}
              </div>
              <div className="flex items-center gap-2" role="group" aria-label={`Quantity for ${priced?.name ?? line.sku}`}>
                <Button variant="secondary" onClick={() => void change(line.sku, line.quantity - 1)} busy={busySku === line.sku} aria-label="Decrease quantity">−</Button>
                <span className="w-8 text-center tabular-nums" aria-live="polite">{line.quantity}</span>
                <Button variant="secondary" onClick={() => void change(line.sku, line.quantity + 1)} busy={busySku === line.sku} aria-label="Increase quantity">+</Button>
                <Button variant="ghost" onClick={() => void change(line.sku, 0)} busy={busySku === line.sku}>Remove</Button>
              </div>
            </li>
          );
        })}
      </ul>

      <section aria-labelledby="quote-heading" className="rounded-lg border border-line bg-surface p-4">
        <h2 id="quote-heading" className="mb-3 text-base font-semibold">Deterministic quote</h2>
        <QuoteBreakdown quote={basket.quote} unavailable={basket.unavailable} stale={basket.stale} />
      </section>

      {actionError ? <Alert tone="danger" title="Basket update failed" role="alert">{actionError}</Alert> : null}
      {error ? <Alert tone="danger" title="Checkout could not start" role="alert">{error}</Alert> : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => void checkout()} disabled={!canCheckout} busy={checkingOut}>Proceed to checkout</Button>
        <p className="text-sm text-muted">Checkout creates version 1, a Policy-at-Sale Receipt and a temporary reservation, then asks for your approval.</p>
      </div>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AvailabilityBadge, FreshnessLine } from "@/components/availability";
import { Alert, Button, Spinner } from "@/components/ui";
import { useClient } from "@/components/providers";
import type { Product } from "@/lib/api/types";
import { formatBasisPoints, formatMinor } from "@/lib/money";

import { useBasketActions } from "./use-basket-actions";

export function ProductDetail({ sku }: { sku: string }) {
  const client = useClient();
  const { setQuantity, busySku, error: basketError, lastBasket } = useBasketActions();
  const [product, setProduct] = useState<Product | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [quantity, setQty] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    client
      .getProduct(sku)
      .then((result) => {
        if (!cancelled) {
          setProduct(result);
          setError(null);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Product unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [client, sku, reloadKey]);

  if (error) return <Alert tone="danger" title="Product could not be loaded" role="alert">{error} <Link href="/" className="underline">Back to search</Link></Alert>;
  if (!product) return <Spinner label="Loading product and live availability" />;

  const max = Math.max(1, product.stock_units);
  return (
    <article className="space-y-4">
      <p className="text-sm"><Link href="/" className="underline">← Back to search</Link></p>
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">{product.name_en}</h1>
        <p className="text-muted" lang="hi">{product.name_hi}</p>
        <p className="text-sm text-muted">{product.unit_label} · {product.category} · <span className="font-mono">{product.sku}</span></p>
      </header>
      <div className="flex flex-wrap items-center gap-3">
        <strong className="text-xl tabular-nums">{formatMinor(product.unit_price_minor, product.currency)}</strong>
        <span className="text-sm text-muted">GST {formatBasisPoints(product.tax_bp)} applied per line at quote time</span>
        <AvailabilityBadge product={product} />
      </div>
      <FreshnessLine freshness={product.freshness} />
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-sm">
          <span className="mb-1 block font-medium">Quantity</span>
          <input type="number" min={1} max={max} value={quantity} onChange={(event) => setQty(Math.max(1, Math.min(max, Number(event.target.value) || 1)))} className="w-24 rounded-md border border-line bg-surface px-3 py-2" disabled={!product.is_available} />
        </label>
        <Button onClick={() => void setQuantity(product.sku, quantity)} disabled={!product.is_available} busy={busySku === product.sku}>
          {product.is_available ? `Set ${quantity} in basket` : product.is_listed ? "Sold out" : "Not listed"}
        </Button>
        <Button variant="secondary" onClick={() => setReloadKey((key) => key + 1)}>Re-check availability</Button>
      </div>
      {basketError ? <Alert tone="danger" title="Basket update failed" role="alert">{basketError}</Alert> : null}
      {lastBasket ? (
        <p role="status" className="text-sm">
          Basket updated: {lastBasket.lines.length} line{lastBasket.lines.length === 1 ? "" : "s"}{lastBasket.quote ? <> · total {formatMinor(lastBasket.quote.total_minor, lastBasket.quote.currency)}</> : null}. <Link href="/basket" className="underline">View basket</Link>
        </p>
      ) : null}
    </article>
  );
}

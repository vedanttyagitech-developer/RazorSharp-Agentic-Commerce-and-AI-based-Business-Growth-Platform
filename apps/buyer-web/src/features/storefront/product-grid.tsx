/**
 * The grid every listing screen uses: six cards at 1280, two at 390.
 *
 * It holds no cart state of its own. Quantities and the two handlers arrive as props
 * from the page, so exactly one component in the tree owns the conversation with the
 * cart API and this one cannot start a second, competing one.
 */
"use client";

import type { ReactNode } from "react";

import { EmptyState, Skeleton } from "@/components/ui";
import type { Product } from "@/lib/api/types";

import { ProductCard } from "./product-card";

const COLUMNS =
  "grid grid-cols-2 justify-items-center gap-x-3 gap-y-6 md:grid-cols-3 md:gap-x-5 lg:grid-cols-4 xl:grid-cols-6";

export function ProductGrid({
  products,
  loading = false,
  emptyLabel = "Nothing here yet",
  emptyDetail,
  quantities,
  onAdd,
  onSetQuantity,
  busySku = null,
  renderUnder,
}: {
  products: Product[];
  loading?: boolean;
  emptyLabel?: string;
  emptyDetail?: string;
  /** SKU to the quantity currently on the cart line, from the page's cart hook. */
  quantities?: Record<string, number>;
  onAdd?: (sku: string) => void;
  onSetQuantity?: (sku: string, quantity: number) => void;
  /** The one SKU whose cart write is in flight. */
  busySku?: string | null;
  /** Extra content beneath a card -- search uses it to show why a product matched. */
  renderUnder?: (product: Product) => ReactNode;
}) {
  if (loading && products.length === 0) {
    return (
      <div className={COLUMNS} aria-busy="true" aria-label="Loading products">
        {Array.from({ length: 12 }, (_, index) => (
          <Skeleton key={index} className="h-[315px] w-full max-w-[191px]" />
        ))}
      </div>
    );
  }

  if (products.length === 0) {
    return <EmptyState title={emptyLabel} detail={emptyDetail} />;
  }

  return (
    <div className={COLUMNS}>
      {products.map((product) => (
        <div key={product.sku} className="flex w-full max-w-[191px] flex-col gap-2">
          <ProductCard
            product={product}
            quantity={quantities?.[product.sku] ?? 0}
            onAdd={onAdd}
            onSetQuantity={onSetQuantity}
            busy={busySku === product.sku}
          />
          {renderUnder?.(product)}
        </div>
      ))}
    </div>
  );
}

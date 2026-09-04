import type { Freshness, Product } from "@/lib/api/types";
import { formatTimestamp } from "@/lib/money";

import { StatusPill } from "./ui";

/** Listing and stock are separate facts; sold-out and delisted are different verdicts. */
export function AvailabilityBadge({ product }: { product: Pick<Product, "is_listed" | "is_available" | "stock_units"> }) {
  if (!product.is_listed) return <StatusPill tone="danger" glyph="×" label="Not listed (delisted)" />;
  if (product.stock_units <= 0) return <StatusPill tone="warning" glyph="○" label="Sold out (listed, 0 units)" />;
  return <StatusPill tone="success" glyph="●" label={`In stock · ${product.stock_units} units`} />;
}

export function FreshnessLine({ freshness }: { freshness: Freshness }) {
  return (
    <p className="text-xs text-muted">
      Source <span className="font-mono">{freshness.source}</span> · catalogue revision <span className="font-mono">{freshness.catalogue_revision}</span> · observed {formatTimestamp(freshness.observed_at)}
    </p>
  );
}

'use client';
// The shop's shelf, read from the store rather than written down here.
//
// `lib/demo.ts` carried eight products as a literal. The backend catalogue has 247, so the
// shop was showing three per cent of the shelf -- and the eight were a *copy*: their prices
// and stock were maintained by hand beside the real ones, which is the same "two numbers
// for one fact" the inventory ledger exists to remove. A buyer could be shown 42 units of
// something the store had sold out of.
//
// So the products come from `GET /v1/catalogue/products`, with prices and stock as the
// store reports them. What stays local is presentation and only presentation: a colour and
// a glyph per category, because the catalogue is a commercial record and does not carry
// either, and inventing them server-side would put styling in the merchant's data.

import { useEffect, useState } from 'react';
import { subscribeMerchantChanges } from './merchant-sync';

import { CommerceError, commerce, type ProductCard } from './commerce';
import type { Product } from './demo';

/** The API's `limit` ceiling for this route. Paging is by SKU cursor. */
const PAGE = 100;

/**
 * How each category looks on the shelf.
 *
 * Presentation, not data: nothing here reaches the backend or a bill. The keys are the
 * category slugs the catalogue reports; an unknown one falls back rather than throwing,
 * because a product the shop cannot colour is still a product the shop must sell.
 */
const CATEGORY_STYLE: Record<string, { symbol: string; color: string }> = {
  dairy: { symbol: '🥛', color: '#eaf1fc' },
  produce: { symbol: '🥬', color: '#eef3e9' },
  bakery: { symbol: '🍞', color: '#f7efe7' },
  staples: { symbol: '🌾', color: '#f6f1e4' },
  snacks: { symbol: '🍪', color: '#fdefe6' },
  beverages: { symbol: '☕', color: '#eee9e7' },
  condiments: { symbol: '🧂', color: '#f3f0ea' },
  household: { symbol: '🧴', color: '#eaf0f2' },
  personal_care: { symbol: '🧼', color: '#f2edf6' },
  electronics: { symbol: '🔌', color: '#eceef4' },
};

const FALLBACK = { symbol: '🛒', color: '#f1f2f6' };

/** Title Case for a category slug, for the line under a product name. */
function label(category: string): string {
  return category
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/**
 * One catalogue product in the shape the shelf renders.
 *
 * `id` is the SKU. They used to be different -- `id` was a nickname like `milk` and `sku`
 * the backend identity -- which meant every screen had two names for one product and the
 * basket was keyed on the one the backend has never heard of. One identity, and it is the
 * store's.
 *
 * `price` and `stock` are the store's own figures, carried through unchanged. Nothing here
 * computes a total: a bill is the backend's to price, and the shelf is a shelf.
 */
export function toProduct(card: ProductCard): Product {
  const style = CATEGORY_STYLE[card.category] ?? FALLBACK;
  return {
    id: card.sku,
    sku: card.sku,
    name: card.display_name,
    unit: `${card.unit_label} · ${label(card.category)}`,
    price: card.unit_price_minor,
    category: label(card.category),
    symbol: style.symbol,
    color: style.color,
    stock: card.stock_units,
    isListed: card.is_listed,
    isAvailable: card.is_available,
    // By convention, and only where one exists. Eight of the 247 have artwork; the rest
    // render their category glyph, which `ProductCard` falls back to when the image 404s.
    // A manifest of which files exist would be a third place to keep the same fact.
    image: `/products/${card.sku}.webp`,
  };
}

/**
 * Every listed product in the shop, in catalogue order.
 *
 * Walks the cursor to the end rather than taking the first page: a shelf that stopped at
 * 100 would be the same defect as one that stopped at eight, only harder to notice.
 */
export async function fetchCatalogue(signal?: AbortSignal): Promise<Product[]> {
  const out: Product[] = [];
  let cursor: string | null | undefined;
  do {
    const page = await commerce.catalogue.list({ limit: PAGE, cursor, signal });
    out.push(...page.products.map(toProduct));
    cursor = page.next_cursor;
  } while (cursor);
  return out;
}

/**
 * The shelf, loaded once per surface.
 *
 * Starts empty rather than with a hardcoded sample. A placeholder shelf would render
 * prices and stock the store never said, and the buyer would watch them change under
 * them a moment later -- which is exactly the "two numbers for one fact" this replaced.
 * `loading` lets the screen say it is asking.
 */
export function useCatalogue(): {
  products: Product[];
  loading: boolean;
  error: string | null;
} {
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => {
      setLoading(true);
      setRevision((value) => value + 1);
    };
    return subscribeMerchantChanges(refresh);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const fresh = await fetchCatalogue(controller.signal);
        if (controller.signal.aborted) return;
        setProducts(fresh);
        setError(null);
      } catch (cause) {
        if ((cause as Error)?.name === 'AbortError') return;
        setError(
          cause instanceof CommerceError
            ? cause.detail
            : 'The store could not be reached, so its shelf cannot be shown.',
        );
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [revision]);

  return { products, loading, error };
}

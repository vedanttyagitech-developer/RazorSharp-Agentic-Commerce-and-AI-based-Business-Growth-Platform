/**
 * The shop, inside the copilot.
 *
 * The store used to be the page and the assistant used to be a box floating over it. That
 * is the wrong way round for what this product is: the conversation is how the buyer
 * shops, and the shelf is where they go when they would rather point than describe. So the
 * shelf lives in here now, one press away, and closes back into the conversation it
 * belongs to.
 *
 * Nothing here ranks anything. The catalogue returns products in the merchant's own order
 * and this draws them in that order, because a shop that quietly promotes what it wants
 * sold is a shop whose recommendations cannot be trusted -- and this assistant's
 * suggestions are supposed to be worth listening to.
 *
 * Touching a photograph adds the product. There is no separate plus button: the buyer said
 * "that one" by pressing it, and asking them to then find a small control to confirm what
 * they just pressed is a second question for an act that is already reversible from the
 * cart standing beside it.
 */

/* eslint-disable @next/next/no-img-element */

"use client";

import { useEffect, useMemo, useState } from "react";

import { Amount, cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import type { Product } from "@/lib/api/types";
import { CATEGORIES, PLACEHOLDER_IMAGE, categoryLabel, primaryImage } from "@/lib/product-images";

function Tile({
  item,
  inCart,
  busy,
  onAdd,
}: {
  item: Product;
  inCart: number;
  busy: boolean;
  onAdd: (sku: string) => void;
}) {
  const sold_out = !item.is_available || item.stock_units === 0;
  return (
    <li className="group relative overflow-hidden rounded-2xl border border-[var(--rzp-line)] bg-white/[0.03] transition hover:border-white/20 hover:bg-white/[0.06]">
      <button
        type="button"
        disabled={busy || sold_out}
        onClick={() => onAdd(item.sku)}
        className="block w-full text-left disabled:cursor-not-allowed"
        aria-label={
          sold_out
            ? `${item.display_name}, sold out`
            : `Add ${item.display_name} to cart, ${item.unit_price.display} ${item.unit_price.currency}`
        }
      >
        <div className="relative aspect-square overflow-hidden bg-white/[0.04]">
          <img
            src={primaryImage(item.sku)}
            alt=""
            aria-hidden="true"
            loading="lazy"
            decoding="async"
            className={cx(
              "size-full object-cover transition-transform duration-300 group-hover:scale-105",
              sold_out && "opacity-40 grayscale",
            )}
            onError={(event) => {
              const element = event.currentTarget;
              if (element.src !== PLACEHOLDER_IMAGE) element.src = PLACEHOLDER_IMAGE;
            }}
          />
          {sold_out ? (
            <span className="absolute left-2 top-2 rounded-full bg-black/70 px-2 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-wider text-slate-300">
              Sold out
            </span>
          ) : inCart > 0 ? (
            <span className="absolute right-2 top-2 flex size-6 items-center justify-center rounded-full bg-emerald-500 font-mono text-[11px] font-bold text-white shadow-lg">
              {inCart}
            </span>
          ) : null}
        </div>
        <div className="space-y-1 p-2.5">
          <p className="line-clamp-2 min-h-[2.2rem] text-[12px] font-medium leading-snug text-slate-100">
            {item.display_name}
          </p>
          <div className="flex items-baseline justify-between gap-1">
            <span className="font-mono text-[13px] font-semibold tabular-nums text-white">
              <Amount money={item.unit_price} />
            </span>
            <span className="truncate text-[10px] text-slate-500">{item.unit_label}</span>
          </div>
        </div>
      </button>
    </li>
  );
}

export function StoreSheet({
  open,
  quantities,
  busySku,
  onAdd,
  onClose,
}: {
  open: boolean;
  /** How many of each SKU the cart already holds, so a tile can show its own count. */
  quantities: Record<string, number>;
  busySku: string | null;
  onAdd: (sku: string) => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<readonly Product[]>([]);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void (async () => {
      setLoading(true);
      setProblem(null);
      try {
        const trimmed = query.trim();
        if (trimmed.length > 0) {
          const found = await api.search(trimmed, { limit: 40, signal: controller.signal });
          setItems(found.hits);
        } else {
          const page = await api.products({
            category: category ?? undefined,
            limit: 40,
            signal: controller.signal,
          });
          setItems(page.products);
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setProblem("The shelf could not be read just now.");
      } finally {
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [open, category, query]);

  const aisles = useMemo(() => CATEGORIES.map((slug) => ({ slug, label: categoryLabel(slug) })), []);

  if (!open) return null;

  return (
    <section
      aria-label="The store"
      className="absolute inset-0 z-20 flex flex-col bg-[var(--rzp-navy)]/95 backdrop-blur-xl"
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-[var(--rzp-line)] px-4 py-3">
        <h2 className="text-sm font-semibold text-white">Store</h2>
        <div className="relative ml-2 min-w-0 flex-1">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search the shelf"
            className="h-9 w-full rounded-full border border-white/12 bg-white/[0.05] px-4 text-[13px] text-slate-100 placeholder:text-slate-500 focus:border-white/30 focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the store"
          className="flex size-9 shrink-0 items-center justify-center rounded-full border border-white/12 text-slate-300 transition-colors hover:bg-white/10 hover:text-white"
        >
          <svg viewBox="0 0 16 16" className="size-4" aria-hidden="true">
            <path
              d="M4 4l8 8M12 4l-8 8"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </header>

      {query.trim().length === 0 ? (
        <div className="shrink-0 overflow-x-auto border-b border-white/[0.06] px-4 py-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <div className="flex gap-1.5">
            <button
              type="button"
              onClick={() => setCategory(null)}
              className={cx(
                "shrink-0 rounded-full px-3 py-1 text-[12px] font-medium transition-colors",
                category === null
                  ? "bg-[var(--rzp-blue)] text-white"
                  : "border border-white/12 text-slate-300 hover:bg-white/10",
              )}
            >
              Everything
            </button>
            {aisles.map((aisle) => (
              <button
                key={aisle.slug}
                type="button"
                onClick={() => setCategory(aisle.slug)}
                className={cx(
                  "shrink-0 rounded-full px-3 py-1 text-[12px] font-medium transition-colors",
                  category === aisle.slug
                    ? "bg-[var(--rzp-blue)] text-white"
                    : "border border-white/12 text-slate-300 hover:bg-white/10",
                )}
              >
                {aisle.label}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {problem !== null ? (
          <p className="mt-8 text-center text-xs text-amber-300">{problem}</p>
        ) : loading && items.length === 0 ? (
          <p className="mt-8 text-center text-xs text-slate-500">Reading the shelf…</p>
        ) : items.length === 0 ? (
          <p className="mt-8 text-center text-xs text-slate-500">
            Nothing on this shelf matches that.
          </p>
        ) : (
          <ul
            role="list"
            className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5"
          >
            {items.map((item) => (
              <Tile
                key={item.sku}
                item={item}
                inCart={quantities[item.sku] ?? 0}
                busy={busySku === item.sku}
                onAdd={onAdd}
              />
            ))}
          </ul>
        )}
      </div>

      <p className="shrink-0 border-t border-white/[0.06] px-4 py-2 text-center text-[10px] leading-relaxed text-slate-500">
        Touch a product to add it. Prices come from the merchant&rsquo;s live catalogue, and
        nothing here is ranked by what sells.
      </p>
    </section>
  );
}

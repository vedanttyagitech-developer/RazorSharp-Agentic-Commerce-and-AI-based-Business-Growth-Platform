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
  onOpen,
}: {
  item: Product;
  inCart: number;
  busy: boolean;
  /** Opens the product. Buying is a decision of its own, made on the product itself. */
  onOpen: (item: Product) => void;
}) {
  const sold_out = !item.is_available || item.stock_units === 0;
  return (
    <li className="group relative overflow-hidden rounded-2xl border border-[var(--rzp-line)] bg-white/[0.03] transition hover:border-white/20 hover:bg-white/[0.06]">
      <button
        type="button"
        disabled={busy}
        onClick={() => onOpen(item)}
        className="block w-full text-left disabled:cursor-not-allowed"
        aria-label={`${item.display_name}, ${item.unit_price.display} ${item.unit_price.currency}${sold_out ? ", sold out" : ""}`}
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

/**
 * One product, as the merchant records it.
 *
 * There is no prose description anywhere in this catalogue and none is written here. What
 * a merchant actually holds about a product is its names in both languages, its unit, its
 * price, the tax rate that applies to it, how many are on the shelf and when that was last
 * observed -- and that is a more useful page than invented marketing copy, because every
 * line of it is answerable. The tax rate and the catalogue revision are shown for the same
 * reason the approval card shows a hash: a shop that can say where its numbers came from is
 * a different kind of shop.
 */
function ProductDetail({
  item,
  inCart,
  busy,
  onAdd,
  onBack,
}: {
  item: Product;
  inCart: number;
  busy: boolean;
  onAdd: (sku: string) => void;
  onBack: () => void;
}) {
  const soldOut = !item.is_available || item.stock_units === 0;
  const observed = new Date(item.freshness.observed_at);
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 py-1">
      <button
        type="button"
        onClick={onBack}
        className="flex items-center gap-1.5 self-start text-[12px] font-medium text-slate-400 transition-colors hover:text-white"
      >
        <svg viewBox="0 0 16 16" className="size-3.5" fill="none" aria-hidden="true">
          <path
            d="M10 3.5L5.5 8l4.5 4.5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Back to the shelf
      </button>

      <div className="flex flex-col gap-4 sm:flex-row">
        <div className="card-enter aspect-square w-full overflow-hidden rounded-2xl border border-[var(--rzp-line)] bg-white/[0.04] sm:w-56">
          <img
            src={primaryImage(item.sku)}
            alt=""
            aria-hidden="true"
            className={cx("size-full object-cover", soldOut && "opacity-40 grayscale")}
            onError={(event) => {
              const element = event.currentTarget;
              if (element.src !== PLACEHOLDER_IMAGE) element.src = PLACEHOLDER_IMAGE;
            }}
          />
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <h3 className="text-[17px] font-semibold leading-snug text-white">
              {item.display_name}
            </h3>
            {item.name_hi && item.name_hi !== item.display_name ? (
              <p className="mt-0.5 text-[13px] text-slate-400">{item.name_hi}</p>
            ) : null}
          </div>

          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[24px] font-bold tabular-nums text-white">
              <Amount money={item.unit_price} />
            </span>
            <span className="text-[13px] text-slate-400">per {item.unit_label}</span>
          </div>

          <p
            className={cx(
              "text-[13px] font-medium",
              soldOut ? "text-rose-300" : "text-emerald-300",
            )}
          >
            {soldOut ? "Sold out right now" : `${item.stock_units} on the shelf`}
          </p>

          <button
            type="button"
            disabled={busy || soldOut}
            onClick={() => onAdd(item.sku)}
            className="rzp-action w-full rounded-xl py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto sm:px-8"
          >
            {soldOut ? "Sold out" : inCart > 0 ? `Add another (${inCart} in cart)` : "Add to cart"}
          </button>
        </div>
      </div>

      {/* Four facts a buyer can act on, in the words they would use for them. "Aisle" and
          "tax" were shop-speak: the first is where the thing sits, the second is GST, which
          is what it actually is -- `tax_bp` is an Indian GST rate in basis points and the
          merchant's own tax policy is named for it. Naming it properly costs nothing and
          tells the buyer which rate they are paying. */}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-2xl border border-[var(--rzp-line)] bg-white/[0.03] p-3 text-[12px]">
        <div>
          <dt className="text-slate-500">Found in</dt>
          <dd className="text-slate-200">{categoryLabel(item.category)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">GST on this item</dt>
          <dd className="text-slate-200">
            {(item.tax_bp / 100).toFixed(item.tax_bp % 100 === 0 ? 0 : 2)}%
            {/* Added, not included. The merchant prices its shelf pre-tax and the quote
                carries `items_subtotal` and `items_tax` as separate figures -- ₹40.00 of
                popcorn quotes as ₹40.00 plus ₹4.80. Saying "included" would have been a
                false claim about money on the one screen where a buyer is deciding. */}
            <span className="text-slate-500">
              {item.tax_bp === 0 ? " · no GST on this item" : " · added at checkout"}
            </span>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Product code</dt>
          <dd className="font-mono text-[11px] text-slate-300">{item.sku}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Price checked</dt>
          <dd className="text-slate-200">
            {Number.isNaN(observed.getTime())
              ? "not recorded"
              : observed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
          </dd>
        </div>
      </dl>
      {/* Said plainly, because it is the point: this shop does not keep a copy of the
          merchant's prices and show you that. It reads them, stamps which revision it read,
          and the same figure is what the kernel binds your approval to. */}
      <p className="text-center text-[11px] leading-relaxed text-slate-500">
        Every figure here came from the merchant&rsquo;s live catalogue, revision{" "}
        {item.freshness.catalogue_revision}. This shop keeps no prices of its own and writes
        no descriptions.
      </p>
    </div>
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
  // The product the buyer opened, or null while they are on the shelf. Cleared whenever the
  // shelf itself changes underneath them, so a search cannot leave them looking at a
  // product that is no longer among the results.
  const [showing, setShowing] = useState<Product | null>(null);
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
            onChange={(event) => {
              setQuery(event.target.value);
              setShowing(null);
            }}
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

      {showing === null && query.trim().length === 0 ? (
        <div className="shrink-0 overflow-x-auto border-b border-white/[0.06] px-4 py-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <div className="flex gap-1.5">
            <button
              type="button"
              onClick={() => {
                setCategory(null);
                setShowing(null);
              }}
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
                onClick={() => {
                  setCategory(aisle.slug);
                  setShowing(null);
                }}
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
        {showing !== null ? (
          <ProductDetail
            item={showing}
            inCart={quantities[showing.sku] ?? 0}
            busy={busySku === showing.sku}
            onAdd={onAdd}
            onBack={() => setShowing(null)}
          />
        ) : problem !== null ? (
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
                onOpen={setShowing}
              />
            ))}
          </ul>
        )}
      </div>

    </section>
  );
}

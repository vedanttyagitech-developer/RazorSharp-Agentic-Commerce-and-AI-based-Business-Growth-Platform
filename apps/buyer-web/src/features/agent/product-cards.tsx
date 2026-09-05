/**
 * The shelf a reply draws under its own sentence.
 *
 * RazorAI names products in prose, and prose is a bad way to choose between five of them.
 * These cards are the same products the sentence just listed -- the gateway sends them on
 * the `agent_reply` frame as `items`, offer first -- so the buyer can look at what was
 * described and press the one they meant.
 *
 * Two things this component refuses to do, both for the reason the shelf's own card refuses
 * them. It never computes a price: `unit_price` arrives as the API's money object and is
 * handed to `<Amount/>` unchanged, and an item that arrived without one renders "amount not
 * stated" rather than a zero. And it never decides availability: `available` is the
 * gateway's word, computed from the same `is_available`/`stock_units` the storefront reads,
 * so a sold-out product cannot be pressed here while looking sellable on the shelf.
 *
 * `unit_price` is `unknown` on the wire on purpose, so it is PARSED here rather than cast.
 * A price whose shape drifted becomes "amount not stated" -- legible, and never arithmetic
 * on an unvalidated object.
 */
"use client";

/*
 * Plain <img> rather than next/image, for the same reason the shelf's own card uses one:
 * these are local, already-sized `.webp` files served from `public/products`, and the
 * optimizer would add a request per card to re-encode a file that is already the right
 * size in the right format.
 */
/* eslint-disable @next/next/no-img-element */

import { z } from "zod";

import { Amount, cx } from "@/components/ui";
import type { ReplyItem } from "@/features/voice/wire";
import { MoneySchema, type Money } from "@/lib/api/types";
import { primaryImage } from "@/lib/product-images";

/** Per-card entrance offset, so a row of five arrives as a row and not as a flash. */
const STAGGER_MS = 60;

/**
 * The item's price as a money object, or null when it did not carry a usable one.
 *
 * Parsed, not cast. `<Amount money={null}/>` already renders a dash plus an
 * "amount not stated" note for screen readers, which is the honest rendering of a price
 * this page was never told.
 */
function priceOf(item: ReplyItem): Money | null {
  const parsed = MoneySchema.safeParse(item.unit_price);
  return parsed.success ? parsed.data : null;
}

/**
 * One catalogue row as it appears inside a turn's `structured` payload.
 *
 * Deliberately lenient: the payload is the API's own response forwarded by the bridge -- a
 * search page, or one product's fields flat -- and it carries more than a shelf needs. Only
 * the fields a card draws are named, so a payload that grows a field does not stop the row
 * rendering, while a payload missing `sku` is not a product and is skipped.
 */
const StructuredRowSchema = z.object({
  sku: z.string(),
  display_name: z.string().optional(),
  name: z.string().optional(),
  unit_price: z.unknown().optional(),
  stock_units: z.number().int().nullable().optional(),
  is_available: z.boolean().optional(),
});

const StructuredSchema = z.union([
  z.object({ hits: z.array(z.unknown()) }),
  StructuredRowSchema,
]);

function rowToItem(raw: unknown): ReplyItem | null {
  const parsed = StructuredRowSchema.safeParse(raw);
  if (!parsed.success) return null;
  const row = parsed.data;
  return {
    sku: row.sku,
    name: row.display_name ?? row.name ?? row.sku,
    unit_price: row.unit_price ?? null,
    stock_units: row.stock_units ?? null,
    // The gateway's own rule (`_available` in `gateway/agent_client.py`), repeated here so
    // the typed path and the spoken path cannot disagree about what is sellable: absent is
    // treated as available, an explicit false is not, and zero units is sold out whatever
    // the flag says.
    available: row.is_available !== false && row.stock_units !== 0,
  };
}

/**
 * The products inside a turn's `structured` payload, as the shelf's own item shape.
 *
 * Two payloads carry products: a search page, whose rows are under `hits`, and a single
 * product, whose fields are spread flat. Everything else -- a basket, a checkout, an order,
 * or null -- has no shelf and yields an empty list, which `ProductCards` draws as nothing.
 *
 * Exported so the written conversation draws the same cards from the same rule the spoken
 * one does. Two adapters would be two definitions of "what is on the shelf".
 */
export function itemsFromStructured(structured: unknown): readonly ReplyItem[] {
  const parsed = StructuredSchema.safeParse(structured);
  if (!parsed.success) return [];
  const payload = parsed.data;
  const rows = "hits" in payload ? payload.hits : [payload];
  return rows.map(rowToItem).filter((item): item is ReplyItem => item !== null);
}

export function ProductCards({
  items,
  onAdd,
  busySku = null,
}: {
  items: readonly ReplyItem[];
  /**
   * The shelf's own write. Absent means the row is a display, with no press.
   *
   * The item travels with the sku because the permission slip that follows a press has to
   * name what is being added and what it costs, and the panel holds no catalogue of its own
   * to look either up in. Passing the row the buyer actually pressed is the only way the
   * question can quote a price without inventing one.
   */
  onAdd?: (sku: string, item?: ReplyItem) => void;
  /** The sku a basket write is in flight for, so it cannot be pressed twice. */
  busySku?: string | null;
}) {
  if (items.length === 0) return null;
  return (
    <ul
      role="list"
      aria-label="Products in this reply"
      className="mt-2 flex snap-x snap-mandatory gap-2 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      {items.map((item, index) => {
        const price = priceOf(item);
        const busy = busySku === item.sku;
        return (
          <li
            key={item.sku}
            className="card-enter w-[132px] shrink-0 snap-start rounded-xl border border-white/10 bg-white/[0.06] p-2"
            style={{ animationDelay: `${index * STAGGER_MS}ms` }}
          >
            <img
              src={primaryImage(item.sku)}
              alt={item.name}
              width={96}
              height={96}
              loading="lazy"
              decoding="async"
              className={cx(
                "h-24 w-full rounded-lg object-cover",
                !item.available && "opacity-45 grayscale",
              )}
            />
            <p className="mt-2 line-clamp-2 text-[12px] leading-tight text-slate-200">{item.name}</p>
            <div className="mt-1.5 flex items-center justify-between gap-1">
              <Amount money={price} className="text-[12px] font-medium text-slate-100" />
              {item.available && onAdd ? (
                <button
                  type="button"
                  onClick={() => onAdd(item.sku, item)}
                  disabled={busy}
                  aria-label={`Add ${item.name}`}
                  className="grid h-7 w-7 place-items-center rounded-full bg-[var(--color-primary)] text-[15px] leading-none font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
                >
                  <span aria-hidden="true">+</span>
                </button>
              ) : null}
            </div>
            {!item.available ? (
              <p className="mt-1 text-[11px] font-medium text-slate-400">Out of stock</p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

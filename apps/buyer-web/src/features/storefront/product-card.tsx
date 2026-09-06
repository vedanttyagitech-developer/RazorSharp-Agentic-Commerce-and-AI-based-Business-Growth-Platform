/**
 * The 191 × 315 product card, to the millimetre in `docs/BLINKIT_DESIGN_SPEC.md`.
 *
 * Two things it refuses to do. It never computes a price: `unit_price` arrives from the
 * quote engine as integer paise with the server's own display string, and `Amount` prints
 * it. And it never softens the difference between a product the merchant stocks but has
 * run out of and a product the merchant has taken off the catalogue -- "Out of stock" and
 * "Not available" are different facts about whether waiting will help, and a card that
 * blurs them lies to a buyer about whether to come back tomorrow.
 *
 * The image and the name are one link to the product page; ADD is a sibling, not a child,
 * so a keyboard reaches the control without walking through the link.
 */
"use client";

/*
 * Plain <img> rather than next/image: these are local, already-sized `.webp` files served
 * from this origin under a CSP that names no external host, so the optimiser would resize
 * an asset that is already the size the card draws it at.
 */
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";

import { Amount, Spinner, cx } from "@/components/ui";
import type { Product } from "@/lib/api/types";
import { primaryImage } from "@/lib/product-images";

import { QuantityStepper } from "./quantity-stepper";

export function ProductCard({
  product,
  quantity = 0,
  onAdd,
  onSetQuantity,
  busy = false,
}: {
  product: Product;
  quantity?: number;
  onAdd?: (sku: string) => void;
  onSetQuantity?: (sku: string, quantity: number) => void;
  busy?: boolean;
}) {
  const unavailable = !product.is_available;
  // Listed-but-unavailable means the merchant sells this and has none today. Unlisted
  // means it is off the catalogue entirely. Only the first is worth waiting for.
  const status = product.is_listed ? "Out of stock" : "Not available";
  const inBasket = quantity > 0;

  return (
    <article
      className={cx(
        "flex h-[315px] w-full max-w-[191px] flex-col rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-white pb-3",
        unavailable && "bg-[var(--tint-3)]",
      )}
      style={{ boxShadow: "var(--card-shadow)" }}
    >
      <Link
        href={`/p/${encodeURIComponent(product.sku)}`}
        className="group flex flex-1 flex-col rounded-[var(--r-md)] focus-visible:outline-2"
      >
        <div className="relative w-full overflow-hidden rounded-t-[var(--r-md)]">
          <img
            src={primaryImage(product.sku)}
            alt={product.display_name}
            width={190}
            height={190}
            loading="lazy"
            decoding="async"
            className={cx(
              "aspect-square w-full object-contain transition-transform duration-200 group-hover:scale-[1.03]",
              unavailable && "opacity-45 grayscale",
            )}
          />
          {/*
            The pill in this corner said "8 MINS" on every card. Nothing in a catalogue
            response carries a delivery estimate -- there is no field for one, and no order
            on this platform is fulfilled at all -- so the number was drawn, not read, and
            it was a promise about the buyer's own order rather than a piece of borrowed
            styling. The pill keeps its place and its shape, which is the part of the clone
            worth having, and now carries the one fact the merchant catalogue actually
            sends about how quickly this runs out: `stock_units`, printed as it arrived.

            Hidden below one unit rather than printed as "0 IN STOCK", because a card that
            says nothing is left while offering ADD beside it is arguing with itself.
          */}
          {unavailable || product.stock_units < 1 ? null : (
            <span className="tnum absolute bottom-2 left-2 inline-flex items-center rounded-[4px] bg-white/90 px-1.5 py-0.5 text-[9px] font-bold tracking-wide text-[var(--ink)] shadow-sm">
              {product.stock_units} IN STOCK
            </span>
          )}
        </div>

        <div className="px-2 pt-2">
          <h3
            className={cx(
              "clamp-2 text-[13px] leading-[1.35] font-semibold",
              unavailable ? "text-[var(--ink-5)]" : "text-[var(--ink)]",
            )}
          >
            {product.display_name}
          </h3>
          <p className="mt-1 text-[12px] font-medium text-[var(--ink-unit)]">{product.unit_label}</p>
        </div>
      </Link>

      <div className="mt-auto flex items-center justify-between gap-2 px-2 pt-2">
        <Amount
          money={product.unit_price}
          whole
          className={cx(
            "text-[12px] font-semibold",
            unavailable ? "text-[var(--ink-5)]" : "text-[var(--ink)]",
          )}
        />

        {unavailable ? (
          <span className="inline-flex items-center gap-1 text-[12px] font-semibold text-[var(--ink-5)]">
            <CrossedCircle />
            {status}
          </span>
        ) : inBasket ? (
          <QuantityStepper
            quantity={quantity}
            busy={busy}
            itemLabel={product.display_name}
            onChange={(next) => onSetQuantity?.(product.sku, next)}
          />
        ) : (
          <button
            type="button"
            onClick={() => onAdd?.(product.sku)}
            disabled={busy || !onAdd}
            aria-label={`Add ${product.display_name} to cart`}
            aria-busy={busy || undefined}
            className="inline-flex h-[33px] w-[66px] items-center justify-center rounded-[var(--r-sm)] border border-[var(--green-add)] bg-[var(--green-add-bg)] text-[13px] font-semibold text-[var(--green-add)] transition disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? <Spinner className="h-3.5 w-3.5" /> : "ADD"}
          </button>
        )}
      </div>
    </article>
  );
}

/** A shape, not a colour: the unavailable state has to survive a monochrome screen. */
function CrossedCircle() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <line x1="2.5" y1="9.5" x2="9.5" y2="2.5" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

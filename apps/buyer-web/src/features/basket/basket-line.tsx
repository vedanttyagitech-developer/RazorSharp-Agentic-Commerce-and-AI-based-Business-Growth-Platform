/**
 * One row of the basket.
 *
 * Every amount on it is a field of the quote the server sent: the unit price and the
 * line subtotal are rendered, never multiplied out here. The quantity stepper is the
 * only control that produces a number, and it produces an absolute quantity for the
 * API rather than a delta.
 *
 * A line the merchant declined keeps its name, its photograph and -- this is the part
 * that matters -- its stepper, whenever the merchant has any of the product at all. A
 * line refused for asking 21 of a thing with 20 in stock is not a dead line; it is a
 * line one tap from being fine, and taking the stepper away removed the only control
 * that could fix it. So the shortfall is stated in the merchant's own numbers and the
 * count that would clear it is offered as a button.
 *
 * Only a line with nothing behind it -- delisted, or genuinely at zero -- drops to the
 * muted form with removal as its one option. It stays on screen rather than vanishing,
 * because a line that disappeared on its own would leave the buyer wondering what they
 * lost between one screen and the next.
 */
"use client";

/* eslint-disable @next/next/no-img-element -- local, pre-sized .webp under a CSP that names no external host. */

import { Amount, Spinner, cx } from "@/components/ui";
import type { Unavailability } from "@/lib/api/types";
import { primaryImage } from "@/lib/product-images";

interface BasketLineProps {
  sku: string;
  /**
   * The name the merchant gave this SKU, or null if it has never quoted one. A line the
   * merchant declined has no quote row of its own, so this is the name it last carried.
   */
  name: string | null;
  quantity: number;
  unitPriceMinor?: number;
  subtotalMinor?: number;
  currency?: string;
  /** The merchant's own statement about why this line could not be priced, if it made one. */
  shortfall?: Unavailability | null;
  /**
   * True when this line was left out of the quote and the merchant said nothing about it.
   * Rare, and drawn as the absence it is rather than dressed up as a stock shortfall.
   */
  unexplained?: boolean;
  busy?: boolean;
  onSetQuantity(quantity: number): void;
}

/**
 * The merchant's own photograph. `primaryImage` knows which SKUs have a file on disk and
 * answers with the shared placeholder for the rest, so a missing shot is a drawn absence
 * rather than a broken image the buyer has to interpret.
 */
function LineImage({ sku, alt, muted }: { sku: string; alt: string; muted?: boolean }) {
  return (
    <div
      className={cx(
        "flex h-16 w-16 shrink-0 items-center justify-center overflow-hidden rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)]",
        muted && "opacity-50 grayscale",
      )}
    >
      <img
        src={primaryImage(sku)}
        alt={alt}
        width={64}
        height={64}
        loading="lazy"
        decoding="async"
        className="h-16 w-16 object-contain"
      />
    </div>
  );
}

function StepperButton({
  label,
  glyph,
  onClick,
  disabled,
}: {
  label: string;
  glyph: "minus" | "plus";
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="flex h-full w-9 items-center justify-center text-white transition disabled:cursor-not-allowed disabled:opacity-60"
    >
      <svg viewBox="0 0 16 16" aria-hidden className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M3 8h10" />
        {glyph === "plus" ? <path d="M8 3v10" /> : null}
      </svg>
    </button>
  );
}

function RemoveButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[var(--ink-5)] transition hover:bg-[var(--tint-1)] hover:text-[var(--ink-2)] disabled:cursor-not-allowed disabled:opacity-50"
    >
      <svg viewBox="0 0 16 16" aria-hidden className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round">
        <path d="M4 4l8 8M12 4l-8 8" />
      </svg>
    </button>
  );
}

/** The crossed circle that marks a line the merchant cannot supply at all. */
function CrossedCircle() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round">
      <circle cx="8" cy="8" r="6" />
      <path d="M4 12L12 4" />
    </svg>
  );
}

export function BasketLine({
  sku,
  name,
  quantity,
  unitPriceMinor,
  subtotalMinor,
  currency = "INR",
  shortfall = null,
  unexplained = false,
  busy = false,
  onSetQuantity,
}: BasketLineProps) {
  const label = name ?? sku;

  /*
   * How many of this product the merchant says it can supply right now. A delisted
   * product supplies none whatever its stock figure reads, so the listing flag is checked
   * before the count rather than after it: a count from a product that is no longer for
   * sale is not an offer, and offering to "set the quantity to 20" of something the
   * merchant has withdrawn would be a promise the platform cannot keep.
   */
  const supply = shortfall === null ? null : shortfall.listed ? shortfall.available_units : 0;

  if (supply !== null && supply <= 0) {
    return (
      <li className="flex items-center gap-3 border-b border-[var(--header-line)] px-4 py-3 last:border-b-0">
        <LineImage sku={sku} alt="" muted />
        <div className="min-w-0 flex-1">
          <p className="clamp-2 text-[13px] font-semibold text-[var(--ink-5)]">{label}</p>
          <p className="mt-0.5 font-mono text-[11px] text-[var(--ink-5)]">{sku}</p>
          <p className="mt-1 inline-flex items-center gap-1 rounded-full bg-[var(--tint-1)] px-2 py-0.5 text-[11px] font-semibold text-[var(--ink-3)]">
            <CrossedCircle />
            {shortfall?.listed ? "Out of stock" : "No longer available"}
          </p>
        </div>
        <RemoveButton label={`Remove ${label} from the basket`} onClick={() => onSetQuantity(0)} disabled={busy} />
      </li>
    );
  }

  return (
    <li className="flex items-center gap-3 border-b border-[var(--header-line)] px-4 py-3 last:border-b-0">
      <LineImage sku={sku} alt={label} />

      <div className="min-w-0 flex-1">
        <p className="clamp-2 text-[13px] font-semibold text-[var(--ink)]">{label}</p>
        {unitPriceMinor === undefined ? null : (
          <p className="mt-0.5 text-[12px] font-medium text-[var(--ink-unit)]">
            <Amount minor={unitPriceMinor} currency={currency} /> each
          </p>
        )}

        {/*
          The whole point of the row. `requested` and `available_units` are the merchant's
          own integers, restated rather than recomputed, and the button sets the quantity
          to the count the merchant named -- so the buyer is not asked to work out from
          "we have 20" that they should now press minus once.
        */}
        {shortfall !== null && supply !== null && supply > 0 ? (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
            <p className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-[var(--ink-2)]">
              <svg viewBox="0 0 16 16" aria-hidden className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round">
                <path d="M8 1.5L15 14H1z" />
                <path d="M8 6v3.5" />
                <path d="M8 11.5v.01" />
              </svg>
              <span className="tnum">
                Only {supply} left &mdash; you asked for {shortfall.requested}
              </span>
            </p>
            <button
              type="button"
              onClick={() => onSetQuantity(supply)}
              disabled={busy}
              className="tnum rounded-[var(--r-sm)] border border-[var(--green-add)] bg-[var(--green-add-bg)] px-2 py-0.5 text-[11px] font-bold text-[var(--green-add)] transition hover:brightness-98 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Change to {supply}
            </button>
          </div>
        ) : null}

        {/* A line the quote left out with no word about why. Said plainly, not guessed at. */}
        {unexplained ? (
          <p className="mt-1.5 inline-flex items-center gap-1 rounded-full bg-[var(--tint-1)] px-2 py-0.5 text-[11px] font-semibold text-[var(--ink-3)]">
            <CrossedCircle />
            The merchant did not price this line
          </p>
        ) : null}
      </div>

      <div className="flex shrink-0 flex-col items-end gap-1.5">
        <div
          aria-busy={busy || undefined}
          className={cx(
            "flex h-9 w-[112px] items-center justify-between rounded-[var(--r-sm)] bg-[var(--green)] transition",
            busy && "opacity-70",
          )}
        >
          <StepperButton
            glyph="minus"
            label={quantity <= 1 ? `Remove ${label} from the basket` : `Decrease the quantity of ${label}`}
            onClick={() => onSetQuantity(quantity - 1)}
            disabled={busy}
          />
          <span className="tnum flex items-center justify-center text-[14px] font-bold text-white">
            {busy ? <Spinner className="h-3.5 w-3.5" /> : quantity}
          </span>
          <StepperButton
            glyph="plus"
            label={`Increase the quantity of ${label}`}
            onClick={() => onSetQuantity(quantity + 1)}
            disabled={busy}
          />
        </div>
        {subtotalMinor === undefined ? null : (
          <Amount minor={subtotalMinor} currency={currency} className="text-[13px] font-semibold text-[var(--ink)]" />
        )}
      </div>

      <RemoveButton label={`Remove ${label} from the basket`} onClick={() => onSetQuantity(0)} disabled={busy} />

      {/* The quantity changes under the buyer's hands; say so where a screen reader hears it. */}
      <span className="sr-only" aria-live="polite">
        {label}: {quantity} in the basket
        {shortfall !== null && supply !== null && supply > 0
          ? `, but the merchant has only ${supply}`
          : ""}
      </span>
    </li>
  );
}

/**
 * The live basket, drawn as a strip along the bottom of the RazorAI copilot box.
 *
 * Purely presentational: it takes the lines, the names and the total as props and owns no
 * fetching, because the panel that mounts it already holds the basket and is the single
 * writer for it. Every mutation here is a call back up to that owner -- a step on a line,
 * a checkout press -- so the strip never disagrees with the basket the panel believes in.
 *
 * Dark surface only. This lives inside the copilot box, which has no light mode; it uses
 * slate text on translucent white and borrows the two motion classes the storefront
 * already defines (`cart-line-enter` for a chip arriving, `cart-pulse` for a total that
 * just changed) rather than inventing keyframes it does not own.
 */
"use client";

import { Amount, cx } from "@/components/ui";
import type { Money } from "@/lib/api/types";
import { primaryImage } from "@/lib/product-images";

/** One basket line, reduced to what a chip needs to draw and to step. */
type CartLine = { sku: string; quantity: number };

export type CartStripProps = {
  /** The basket, in the order the panel holds it. */
  lines: readonly CartLine[];
  /** SKU to the merchant's name. A SKU with no entry falls back to the raw code. */
  names: Record<string, string>;
  /** The basket total, or null when the platform cannot state one. */
  total: Money | null;
  /** The one SKU whose write is in flight, or null. Only that chip's steps are frozen. */
  busySku: string | null;
  /** Step a line to a new quantity. The owner decides what a zero means. */
  onSetQuantity: (sku: string, quantity: number) => void;
  /** Fire a checkout. Absent means the strip renders no checkout press at all. */
  onCheckout?: (() => void) | undefined;
  /** Freeze the checkout press while a checkout is being opened. */
  checkoutBusy?: boolean;
  className?: string | undefined;
};

/** The text shown when the basket is empty. One quiet line, no chips. */
const EMPTY_TEXT = "Your cart is empty. Ask for something, or say yes to an offer.";

function stepButtonClass(disabled: boolean): string {
  return cx(
    "flex h-6 w-6 items-center justify-center rounded-md border border-white/10 bg-white/[0.06] text-[15px] leading-none text-slate-200 transition",
    disabled ? "cursor-not-allowed opacity-40" : "hover:bg-white/[0.12]",
  );
}

export function CartStrip({
  lines,
  names,
  total,
  busySku,
  onSetQuantity,
  onCheckout,
  checkoutBusy = false,
  className,
}: CartStripProps) {
  const empty = lines.length === 0;

  return (
    <section
      aria-label="Cart"
      className={cx(
        "rounded-xl border border-white/10 bg-white/[0.06] px-3 py-2 text-slate-200",
        className,
      )}
    >
      {empty ? (
        <p className="py-1 text-[12px] text-slate-400">{EMPTY_TEXT}</p>
      ) : (
        <div className="flex items-center gap-3">
          <ul className="flex min-w-0 flex-1 gap-2 overflow-x-auto">
            {lines.map((line) => {
              const frozen = line.sku === busySku;
              const name = names[line.sku] ?? line.sku;
              return (
                <li
                  key={line.sku}
                  className="cart-line-enter flex shrink-0 items-center gap-2 rounded-xl border border-white/10 bg-white/[0.06] py-1 pl-1 pr-2"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element -- a 28px basket thumb, not a hero image; next/image is not wired for this box */}
                  <img
                    src={primaryImage(line.sku)}
                    alt=""
                    aria-hidden="true"
                    className="h-7 w-7 shrink-0 rounded-lg object-cover"
                    width={28}
                    height={28}
                  />
                  <span className="max-w-[9rem] truncate text-[12px] text-slate-200">{name}</span>
                  <span className="ml-1 flex items-center gap-1">
                    <button
                      type="button"
                      aria-label={`Decrease ${name}`}
                      disabled={frozen}
                      onClick={() => onSetQuantity(line.sku, line.quantity - 1)}
                      className={stepButtonClass(frozen)}
                    >
                      −
                    </button>
                    <span className="tnum min-w-4 text-center text-[13px] font-semibold text-slate-100">
                      {line.quantity}
                    </span>
                    <button
                      type="button"
                      aria-label={`Increase ${name}`}
                      disabled={frozen}
                      onClick={() => onSetQuantity(line.sku, line.quantity + 1)}
                      className={stepButtonClass(frozen)}
                    >
                      +
                    </button>
                  </span>
                </li>
              );
            })}
          </ul>

          <div className="flex shrink-0 items-center gap-3">
            {/*
             * `cart-pulse` is a 700ms one-shot: to replay it every time the total changes
             * we remount the span with a key keyed on the amount, so React runs the enter
             * animation afresh. This deliberately uses no effect, timer or state -- the
             * `react-hooks/set-state-in-effect` rule is on and a keyed remount needs none
             * of them.
             */}
            <span key={total?.minor ?? "none"} className="cart-pulse text-[13px] font-semibold text-slate-100">
              <Amount money={total} />
            </span>
            {onCheckout ? (
              <button
                type="button"
                disabled={checkoutBusy}
                aria-busy={checkoutBusy || undefined}
                onClick={onCheckout}
                className={cx(
                  "rounded-xl border border-white/10 bg-white/[0.06] px-3 py-1.5 text-[13px] font-semibold text-slate-100 transition",
                  checkoutBusy ? "cursor-not-allowed opacity-50" : "hover:bg-white/[0.12]",
                )}
              >
                Checkout
              </button>
            ) : null}
          </div>
        </div>
      )}
    </section>
  );
}

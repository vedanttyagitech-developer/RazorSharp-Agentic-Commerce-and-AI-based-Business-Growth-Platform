/**
 * The in-basket control: `−  qty  +`, in the same 66 × 33 footprint as ADD.
 *
 * Same footprint on purpose. The card must not reflow when a product goes into the
 * basket, because a grid that jumps a row when you tap ADD makes the next tap land on
 * something you did not choose.
 *
 * It reports an absolute quantity rather than a delta. The API's `setLine` takes an
 * absolute number too, so a double-tap that arrives twice sets the same value twice
 * instead of adding twice -- the arithmetic never happens here, and never happens at all.
 */
"use client";

import { cx } from "@/components/ui";

/**
 * The merchant's cap, mirrored from `basket_service.MAX_LINE_QUANTITY`.
 *
 * The `+` control must grey out at the number the server would actually refuse. A lower
 * ceiling invented here would tell a buyer, in an `aria-label` no less, that the merchant
 * has a limit the merchant does not have.
 */
const MAX_LINE_QUANTITY = 99;

export function QuantityStepper({
  quantity,
  onChange,
  max = MAX_LINE_QUANTITY,
  busy = false,
  itemLabel,
  size = "sm",
}: {
  quantity: number;
  onChange: (quantity: number) => void;
  /** The most the caller is willing to put in one basket line. */
  max?: number;
  busy?: boolean;
  /** Named in each control's `aria-label`, so a screen reader hears which product. */
  itemLabel: string;
  size?: "sm" | "lg";
}) {
  const canDecrease = quantity > 0 && !busy;
  const canIncrease = quantity < max && !busy;
  const large = size === "lg";
  const dimensions = large ? "h-[44px] w-[132px] text-[15px]" : "h-[33px] w-[66px] text-[13px]";
  const control = large ? "w-[40px] text-[20px]" : "w-[22px] text-[16px]";

  return (
    <div
      className={cx(
        "inline-flex items-center justify-between overflow-hidden rounded-[var(--r-sm)] bg-[var(--green-add)] font-semibold text-white",
        busy && "opacity-70",
        dimensions,
      )}
      aria-busy={busy || undefined}
    >
      <button
        type="button"
        onClick={() => onChange(quantity - 1)}
        disabled={!canDecrease}
        aria-label={quantity === 1 ? `Remove ${itemLabel} from cart` : `Decrease quantity of ${itemLabel}`}
        className={cx("flex h-full shrink-0 items-center justify-center leading-none disabled:opacity-40", control)}
      >
        <span aria-hidden="true">−</span>
      </button>
      <span
        className="tnum flex-1 text-center leading-none"
        aria-live="polite"
        aria-label={`${quantity} in cart`}
      >
        {quantity}
      </span>
      <button
        type="button"
        onClick={() => onChange(quantity + 1)}
        disabled={!canIncrease}
        aria-label={
          quantity >= max ? `Maximum of ${max} ${itemLabel} already in cart` : `Increase quantity of ${itemLabel}`
        }
        className={cx("flex h-full shrink-0 items-center justify-center leading-none disabled:opacity-40", control)}
      >
        <span aria-hidden="true">+</span>
      </button>
    </div>
  );
}

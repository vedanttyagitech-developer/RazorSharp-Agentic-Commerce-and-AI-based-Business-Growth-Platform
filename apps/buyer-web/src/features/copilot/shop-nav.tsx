/**
 * The shop's three places, across the top: the shelf, your orders, your cart.
 *
 * They are named rather than left as bare glyphs. An icon nobody can put a word to is a
 * control nobody presses, and these are the only three places in the whole application --
 * there is room to say what they are.
 *
 * A `nav` of buttons rather than links: these open a panel over the conversation, they do
 * not navigate away from it, and telling a screen reader they are links would promise a
 * journey that never happens.
 */

"use client";

import { cx } from "@/components/ui";

export type Place = "store" | "orders" | "cart";

function StoreMark() {
  return (
    <svg viewBox="0 0 20 20" className="size-4" fill="none" aria-hidden="true">
      <path
        d="M3 7l1.2-3h11.6L17 7M3 7h14v9a1 1 0 01-1 1H4a1 1 0 01-1-1V7zM7 11h6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function OrdersMark() {
  return (
    <svg viewBox="0 0 20 20" className="size-4" fill="none" aria-hidden="true">
      <path
        d="M5 3h10v14l-5-3-5 3V3zM7.5 7h5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CartMark() {
  return (
    <svg viewBox="0 0 20 20" className="size-4" fill="none" aria-hidden="true">
      <path
        d="M2.5 3h2l2 9h8l2-6H6M8 16.5a1 1 0 100-2 1 1 0 000 2zM14 16.5a1 1 0 100-2 1 1 0 000 2z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Item({
  label,
  active,
  badge = null,
  onClick,
  children,
}: {
  label: string;
  active: boolean;
  badge?: number | null;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        "relative flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors",
        active
          ? "bg-[var(--rzp-blue-soft)] text-white"
          : "text-slate-400 hover:bg-white/[0.06] hover:text-slate-100",
      )}
    >
      {children}
      <span>{label}</span>
      {badge !== null && badge > 0 ? (
        <span className="flex min-w-4 items-center justify-center rounded-full bg-emerald-500 px-1 font-mono text-[9px] font-bold leading-4 text-white">
          {badge}
        </span>
      ) : null}
    </button>
  );
}

export function ShopNav({
  open,
  cartCount,
  onOpen,
  fullscreen,
  onToggleFullscreen,
}: {
  /** Which place is showing, or null when the buyer is in the conversation. */
  open: Place | null;
  cartCount: number;
  onOpen: (place: Place) => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
}) {
  return (
    <nav aria-label="The shop" className="flex items-center gap-1">
      <Item label="Store" active={open === "store"} onClick={() => onOpen("store")}>
        <StoreMark />
      </Item>
      <Item label="Orders" active={open === "orders"} onClick={() => onOpen("orders")}>
        <OrdersMark />
      </Item>
      <Item label="Cart" active={open === "cart"} badge={cartCount} onClick={() => onOpen("cart")}>
        <CartMark />
      </Item>
      <button
        type="button"
        onClick={onToggleFullscreen}
        aria-pressed={fullscreen}
        aria-label={fullscreen ? "Leave full screen" : "Full screen"}
        title={fullscreen ? "Leave full screen" : "Full screen"}
        className="ml-1 flex size-8 items-center justify-center rounded-full text-slate-400 transition-colors hover:bg-white/[0.06] hover:text-slate-100"
      >
        <svg viewBox="0 0 20 20" className="size-[18px]" fill="none" aria-hidden="true">
          {fullscreen ? (
            <path
              d="M8 3v5H3M12 17v-5h5"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          ) : (
            <path
              d="M3 8V3h5M17 12v5h-5"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )}
        </svg>
      </button>
    </nav>
  );
}

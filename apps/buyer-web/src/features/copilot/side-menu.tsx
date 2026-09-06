/**
 * The rail down the left: the shop's own places, always in the same spot.
 *
 * These lived as small icons in the top-right corner, which is where a web page puts its
 * chrome. This is not a page -- it is the shop -- and in an application the way to the
 * shelf, to your orders and to your cart is a standing part of the room rather than four
 * unlabelled glyphs a buyer has to hover to identify. The rail is narrow enough to cost
 * the conversation almost nothing and wide enough to carry a word under each mark, because
 * an icon nobody can name is a control nobody presses.
 *
 * It is a `nav` of buttons rather than links: these open panels over the conversation, they
 * do not navigate away from it, and telling a screen reader they are links would promise a
 * journey that never happens.
 */

"use client";

import { cx } from "@/components/ui";

export type Place = "store" | "orders" | "cart";

function StoreMark() {
  return (
    <svg viewBox="0 0 20 20" className="size-[18px]" fill="none" aria-hidden="true">
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
    <svg viewBox="0 0 20 20" className="size-[18px]" fill="none" aria-hidden="true">
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
    <svg viewBox="0 0 20 20" className="size-[18px]" fill="none" aria-hidden="true">
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
  hint,
  active,
  wide,
  badge = null,
  onClick,
  children,
}: {
  label: string;
  /** What this place is for, shown only when there is room to say it. */
  hint?: string;
  active: boolean;
  wide: boolean;
  badge?: number | null;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      title={wide ? undefined : label}
      className={cx(
        "relative flex w-full rounded-xl transition-colors",
        wide ? "items-center gap-3 px-3 py-2.5 text-left" : "flex-col items-center gap-1 px-1 py-2",
        active
          ? "bg-[var(--rzp-blue-soft)] text-white"
          : "text-slate-400 hover:bg-white/[0.06] hover:text-slate-100",
      )}
    >
      <span className="relative shrink-0">
        {children}
        {badge !== null && badge > 0 && !wide ? (
          <span className="absolute -right-2 -top-1.5 flex min-w-4 items-center justify-center rounded-full bg-emerald-500 px-1 font-mono text-[9px] font-bold leading-4 text-white">
            {badge}
          </span>
        ) : null}
      </span>
      {wide ? (
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium leading-tight">{label}</span>
          {hint !== undefined ? (
            <span className="block truncate text-[11px] leading-tight text-slate-500">{hint}</span>
          ) : null}
        </span>
      ) : (
        <span className="text-[10px] font-medium leading-none">{label}</span>
      )}
      {badge !== null && badge > 0 && wide ? (
        <span className="ml-auto flex min-w-5 items-center justify-center rounded-full bg-emerald-500 px-1.5 font-mono text-[10px] font-bold leading-5 text-white">
          {badge}
        </span>
      ) : null}
    </button>
  );
}

export function SideMenu({
  open,
  wide,
  onToggleWide,
  cartCount,
  onOpen,
  fullscreen,
  onToggleFullscreen,
}: {
  /** Which place is showing, or null when the buyer is in the conversation. */
  open: Place | null;
  /** Wide enough to name each place and say what it is for. */
  wide: boolean;
  onToggleWide: () => void;
  cartCount: number;
  onOpen: (place: Place) => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
}) {
  return (
    <nav
      aria-label="The shop"
      className={cx(
        "flex shrink-0 flex-col gap-1 border-r border-[var(--rzp-line)] bg-[var(--rzp-navy-deep)] px-1.5 py-3 transition-[width] duration-200",
        wide ? "w-[188px]" : "w-[62px]",
      )}
    >
      <button
        type="button"
        onClick={onToggleWide}
        aria-expanded={wide}
        aria-label={wide ? "Collapse the menu" : "Expand the menu"}
        title={wide ? "Collapse the menu" : "Expand the menu"}
        className={cx(
          "mb-1 flex items-center gap-3 rounded-xl px-3 py-2 text-slate-400 transition-colors hover:bg-white/[0.06] hover:text-slate-100",
          wide ? "justify-start" : "justify-center px-1",
        )}
      >
        <svg viewBox="0 0 20 20" className="size-[18px] shrink-0" fill="none" aria-hidden="true">
          <path
            d="M3 5h14M3 10h14M3 15h14"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
        {wide ? <span className="text-[12px] font-medium">Menu</span> : null}
      </button>

      <Item
        label="Store"
        hint="Browse the shelf"
        wide={wide}
        active={open === "store"}
        onClick={() => onOpen("store")}
      >
        <StoreMark />
      </Item>
      <Item
        label="Orders"
        hint="Track or get help"
        wide={wide}
        active={open === "orders"}
        onClick={() => onOpen("orders")}
      >
        <OrdersMark />
      </Item>
      <Item
        label="Cart"
        hint="What you are buying"
        wide={wide}
        active={open === "cart"}
        badge={cartCount}
        onClick={() => onOpen("cart")}
      >
        <CartMark />
      </Item>

      <div className="mt-auto">
        <Item
          label={fullscreen ? "Exit full screen" : "Full screen"}
          wide={wide}
          active={fullscreen}
          onClick={onToggleFullscreen}
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
        </Item>
      </div>
    </nav>
  );
}

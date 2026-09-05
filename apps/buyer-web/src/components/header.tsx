/**
 * The 86px header, drawn to `docs/BLINKIT_DESIGN_SPEC.md`.
 *
 * Below 768px the row cannot hold six things, so it keeps the wordmark and the cart --
 * identity and the way out -- and drops the search field to a second row of its own.
 * The standing note beside the wordmark goes with it, because a buyer on a phone reaches
 * this page from a link and needs the search field more than a sentence about the demo.
 *
 * That note used to read "Delivery in 8 minutes" over a Delhi address. No response on
 * this platform carries a delivery estimate and no order is ever fulfilled, so the number
 * was invented and the address named a shop that does not exist. The clone may borrow
 * Blinkit's layout, type and colour -- a facsimile of a look claims nothing -- but that
 * slot made a promise about the buyer's own order, and this platform cannot keep it. It
 * now says what is true, in the same two lines at the same weights. The address caret went
 * with it: a control that opens nothing is the same small lie in a different place.
 *
 * The cart figures come from the basket context, which took them from the API. The
 * total is the quote's, rendered by `Amount` from integer paise; the header computes
 * nothing.
 */
"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { useBasketContext } from "@/components/providers";
import { SearchBox } from "@/components/search-box";
import { Amount } from "@/components/ui";

/**
 * The wordmark, set in type rather than shipped as an image so it stays crisp and
 * carries a real accessible name. WCAG exempts a logotype from the contrast rule, which
 * is what lets the yellow half sit on white the way the brand draws it.
 */
function Wordmark() {
  return (
    <span aria-hidden="true" className="flex flex-col leading-none">
      <span className="text-[24px] font-extrabold tracking-tight">
        <span className="text-[var(--yellow)]">Razor</span>
        <span className="text-[var(--green)]">Sharp</span>
      </span>
      <span className="mt-0.5 text-[10px] font-bold tracking-[0.18em] text-[var(--ink-3)] uppercase">
        Quick Commerce
      </span>
    </span>
  );
}

function Trolley({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2.5 3.5h2.2l2.1 10.2h10.3l2-7.1H6.2" />
      <circle cx="9" cy="19" r="1.5" />
      <circle cx="17" cy="19" r="1.5" />
    </svg>
  );
}

/**
 * A storefront that shows a Login control it cannot honour is telling its first small
 * lie, and this project's whole argument is about which claims a screen is entitled to
 * make. So the control is real and says what is actually true: the demonstration signs
 * every browser in on its own.
 */
function LoginNote() {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    function onPointer(event: MouseEvent) {
      if (container.current && !container.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  return (
    <div ref={container} className="relative shrink-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className="rounded-[var(--r-sm)] px-2 py-1 text-[16px] font-medium text-[var(--ink-2)] transition hover:text-[var(--ink)]"
      >
        Login
      </button>
      {open ? (
        <div
          role="dialog"
          aria-label="About signing in"
          className="absolute right-0 top-[calc(100%+10px)] z-50 w-[280px] rounded-[var(--r-md)] border border-[var(--card-line)] bg-white p-4 text-[13px] leading-relaxed text-[var(--ink-3)]"
          style={{ boxShadow: "0 8px 24px rgba(0,0,0,.10)" }}
        >
          This is a demonstration storefront. Every browser is signed in automatically as a
          test buyer, so there is no account to log into and no password to give anyone.
        </div>
      ) : null}
    </div>
  );
}

function CartPill() {
  const { itemCount, totalMinor, currency } = useBasketContext();
  const filled = itemCount > 0;

  return (
    <Link
      href="/basket"
      aria-label={filled ? `My cart, ${itemCount} ${itemCount === 1 ? "item" : "items"}` : "My cart, empty"}
      className="flex h-[48px] shrink-0 items-center gap-2 rounded-[var(--r-md)] bg-[var(--tint-1)] px-3 text-[var(--ink)] transition hover:brightness-97 md:px-4"
    >
      <Trolley className="h-[22px] w-[22px]" />
      <span aria-live="polite" className="hidden text-left leading-tight md:block">
        {filled ? (
          <>
            <span className="block text-[13px] font-semibold">
              {itemCount} {itemCount === 1 ? "item" : "items"}
            </span>
            {totalMinor === null ? (
              <span className="block text-[12px] font-medium text-[var(--ink-4)]">quoting…</span>
            ) : (
              <Amount minor={totalMinor} currency={currency} whole className="block text-[12px] font-semibold" />
            )}
          </>
        ) : (
          <span className="text-[14px] font-semibold">My Cart</span>
        )}
      </span>
      {filled ? (
        <span
          aria-hidden="true"
          className="tnum flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--green)] px-1 text-[12px] font-bold text-white md:hidden"
        >
          {itemCount}
        </span>
      ) : null}
    </Link>
  );
}

export function Header() {
  return (
    <header className="sticky top-0 z-40 border-b border-[var(--header-line)] bg-[var(--header-bg)]">
      <div className="column flex h-[64px] items-center gap-3 md:h-[var(--header-h)] md:gap-6">
        <Link href="/" aria-label="RazorSharp Quick Commerce, home" className="shrink-0 rounded-[var(--r-sm)]">
          <Wordmark />
        </Link>

        <span aria-hidden="true" className="hidden h-[32px] w-px bg-[var(--header-line)] md:block" />

        <div className="hidden shrink-0 leading-tight md:block">
          <p className="text-[16px] font-bold text-[var(--ink)]">Nothing here is delivered</p>
          <p className="text-[12px] font-normal text-[var(--ink-3)]">
            A demonstration of the payment path
          </p>
        </div>

        <div className="hidden min-w-0 flex-1 md:block">
          <SearchBox id="header-search" />
        </div>

        <div className="hidden md:block">
          <LoginNote />
        </div>

        <div className="ml-auto md:ml-0">
          <CartPill />
        </div>
      </div>

      <div className="column pb-3 md:hidden">
        <SearchBox id="header-search-compact" />
      </div>
    </header>
  );
}

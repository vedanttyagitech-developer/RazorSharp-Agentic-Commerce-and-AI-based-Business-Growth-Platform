/**
 * The floating button that opens RazorAI.
 *
 * Blue and marked, sitting apart from the storefront's green. On this storefront green is
 * the colour of a control that commits something -- ADD, approve, pay -- and the copilot
 * commits nothing, so it never borrows that colour. The distinction is the point of the
 * whole surface: a buyer should be able to tell, without reading a word, whether what
 * they are looking at was drawn by the agent or by the store.
 *
 * The button owns the open state and nothing else. Focus returns to it on close because
 * the panel restores focus to whatever had it when the drawer opened, which is this.
 */
"use client";

import { usePathname } from "next/navigation";

import { APP_ROUTES } from "@/components/store-chrome";
import { useCallback, useState } from "react";

import { RazorAIMark, RazorAIPanel } from "./razorai-panel";

/**
 * The checkout the buyer is looking at, read off the address bar.
 *
 * The launcher is mounted once in the root layout, so it cannot be handed a checkout id
 * as a prop by the page below it. Reading `/checkout/<id>` is how it learns which
 * checkout a turn should be routed to. Anything else on the path yields null rather than
 * a guess, and the id is passed through unexamined -- the server decides whether it names
 * a checkout this session may read.
 */
function checkoutIdFromPath(pathname: string | null): string | null {
  if (!pathname) return null;
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length !== 2 || segments[0] !== "checkout") return null;
  return decodeURIComponent(segments[1]);
}

export function RazorAILauncher({
  cartId,
  checkoutId,
}: {
  /** The cart this page is about, when it knows better than the cart provider. */
  cartId?: string | null;
  /** Overrides the checkout read from the path, for a surface that knows better. */
  checkoutId?: string | null;
}) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  // A copilot route IS the assistant, drawn full-height with its own header, cart and
  // shelf. This floating box is what a buyer reaches for from the storefront's other
  // pages -- an aisle, a product, an order -- where the assistant is a visitor rather than
  // the room. On a copilot route it would be a second assistant over the first, with its
  // own transcript and its own idea of which cart is current.
  //
  // The same list the header and the footer hide on. It was `pathname === "/"` while there
  // was one such route; the moment `/cart` became another, this button began floating over
  // the cart rail it was offering to open.
  const onHome = APP_ROUTES.has(pathname ?? "");
  const close = useCallback(() => setOpen(false), []);
  const activeCheckoutId = checkoutId ?? checkoutIdFromPath(pathname);

  if (onHome) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open RazorAI"
        aria-haspopup="dialog"
        aria-expanded={open}
        className="ai-orb fixed top-[96px] right-4 z-40 inline-flex h-12 items-center gap-2 rounded-full bg-[var(--blue)] pr-5 pl-4 text-[14px] font-bold text-white transition hover:brightness-95"
        style={{
          boxShadow: "0 6px 20px rgba(37,111,239,0.35)",
          marginBottom: "env(safe-area-inset-bottom, 0px)",
        }}
      >
        <RazorAIMark size={20} />
        RazorAI
      </button>

      <RazorAIPanel
        open={open}
        onClose={close}
        cartId={cartId}
        checkoutId={activeCheckoutId}
      />
    </>
  );
}

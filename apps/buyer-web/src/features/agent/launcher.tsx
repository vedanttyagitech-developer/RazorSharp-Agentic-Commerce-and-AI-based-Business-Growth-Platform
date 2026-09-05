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
import { useEffect, useState } from "react";

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
  basketId,
  checkoutId,
}: {
  /** The basket this page is about, when it knows better than the basket provider. */
  basketId?: string | null;
  /** Overrides the checkout read from the path, for a surface that knows better. */
  checkoutId?: string | null;
}) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  // The copilot is the point of the storefront, so on a screen wide enough to hold it
  // beside the shelf it opens itself once the page is up. Opened after mount rather than
  // as the initial state so the server and the first client render agree; phones keep
  // the launcher, because the panel would cover the shelf there.
  useEffect(() => {
    if (!window.matchMedia("(min-width: 640px)").matches) return undefined;
    const frame = window.requestAnimationFrame(() => setOpen(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const activeCheckoutId = checkoutId ?? checkoutIdFromPath(pathname);

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
        onClose={() => setOpen(false)}
        basketId={basketId}
        checkoutId={activeCheckoutId}
      />
    </>
  );
}

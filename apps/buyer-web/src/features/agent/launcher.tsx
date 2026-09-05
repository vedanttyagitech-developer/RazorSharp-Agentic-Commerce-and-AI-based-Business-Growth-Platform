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
import { useCallback, useEffect, useState } from "react";

import { RazorAIMark, RazorAIPanel } from "./razorai-panel";

/**
 * Where a closed box is remembered: session storage, so the memory is this tab's and ends
 * with it.
 *
 * The launcher lives in the root layout and the checkout opens in a new document, so a
 * memory held in component state would be lost on every full page load -- which is
 * exactly when the box used to reopen, over the approval card the buyer had just closed
 * it to read. Session storage survives the load and not the tab, which is the right life
 * for "I closed this": a buyer who comes back tomorrow should find the copilot open again.
 */
const DISMISSED_KEY = "razorai.dismissed";

function wasDismissed(): boolean {
  try {
    return window.sessionStorage.getItem(DISMISSED_KEY) === "1";
  } catch {
    // Storage may be unavailable. Not knowing is not the same as having been told no.
    return false;
  }
}

function rememberDismissed(): void {
  try {
    window.sessionStorage.setItem(DISMISSED_KEY, "1");
  } catch {
    /* storage may be unavailable; the box opens again on the next home load, no worse */
  }
}

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
  const onHome = pathname === "/";
  // The copilot is the point of the storefront, so on the home shelf, on a screen wide
  // enough to hold it beside the shelf, it opens itself once the page is up. Only there:
  // every other route is a page the buyer navigated to on purpose, and the checkout in
  // particular is a new document whose approval card the box would otherwise cover on
  // every load. And only until the buyer closes it -- a close is remembered for the tab,
  // and the launcher button is how the box comes back. Opened after mount rather than as
  // the initial state so the server and the first client render agree; phones keep the
  // launcher, because the panel would cover the shelf there.
  useEffect(() => {
    if (!onHome || wasDismissed()) return undefined;
    if (!window.matchMedia("(min-width: 640px)").matches) return undefined;
    // A timer rather than an animation frame: frames are paused in a hidden tab, and a
    // buyer who opens the store in a background tab should still find the copilot open.
    const timer = window.setTimeout(() => setOpen(true), 0);
    return () => window.clearTimeout(timer);
  }, [onHome]);
  const close = useCallback(() => {
    rememberDismissed();
    setOpen(false);
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
        onClose={close}
        basketId={basketId}
        checkoutId={activeCheckoutId}
      />
    </>
  );
}

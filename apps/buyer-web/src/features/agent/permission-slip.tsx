/**
 * The chokepoint: the slip the copilot shows before it writes anything.
 *
 * Everything the agent proposes is reversible up to this card, and nothing past it is. So
 * the slip is not a toast and not an alert — it is the system working exactly as designed,
 * pausing to ask for a hand on the wheel. It states the tier as a word, not only a colour,
 * because a buyer on a monochrome screen or with red/green colour blindness must still read
 * how much is at stake; it puts the consequential press under `allowLabel` (the caller names
 * it in the buyer's own verb — "Pay ₹4,200", "Place the order") and a plain ghost Deny beside
 * it; and it answers the keyboard the way a modal should, Enter to allow and Escape to deny,
 * without ever reaching past its own lifetime to the document.
 *
 * The look is the dark authorization slip used across the panel: a `rounded-lg` card with a
 * hairline `border-white/12` over `bg-slate-900/85`, a mono uppercase tier badge, a bold
 * title, a filled Allow and a ghost Deny.
 */
"use client";

import { useEffect, useRef } from "react";

import { cx } from "@/components/ui";

export type PermissionTier = "LOW" | "MEDIUM" | "HIGH";

export interface PermissionSlipProps {
  /** How much is at stake. Drives the badge word AND its colour — never the colour alone. */
  tier: PermissionTier;
  /** The bold headline: what the agent is asking to do, in the buyer's words. */
  title: string;
  /** An optional second line — the specifics the buyer is consenting to. */
  detail?: string;
  /** The verb on the consequential press, e.g. "Pay ₹4,200". Deny is always just "Deny". */
  allowLabel: string;
  onAllow: () => void;
  onDeny: () => void;
  /** True while the decision is in flight — both presses are held disabled. */
  busy?: boolean;
  className?: string;
}

/**
 * The badge for each tier, as a word and a colour that agree.
 *
 * The word is the primary signal and carries the meaning on its own; the colour only
 * reinforces it. Emerald reads as routine, amber as "look before you press", rose as the
 * irreversible ones — a payment, a live order.
 */
const TIER_BADGE: Readonly<Record<PermissionTier, string>> = {
  LOW: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  MEDIUM: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  HIGH: "border-rose-400/30 bg-rose-400/10 text-rose-300",
};

/**
 * The permission slip.
 *
 * Focuses itself on mount so Enter/Escape work immediately, and listens for those keys on
 * its own card rather than on the document — the handler lives and dies with the slip, so a
 * stray keypress after the buyer has decided never reaches a torn-down callback.
 */
export function PermissionSlip({
  tier,
  title,
  detail,
  allowLabel,
  onAllow,
  onDeny,
  busy = false,
  className,
}: PermissionSlipProps) {
  const cardRef = useRef<HTMLDivElement>(null);

  // Move focus onto the card once it exists so the keyboard shortcuts are live without the
  // buyer having to click first. A ref `.focus()` is a DOM call, not a state setter, so it
  // does not trip `react-hooks/set-state-in-effect`.
  useEffect(() => {
    cardRef.current?.focus();
  }, []);

  return (
    <div
      ref={cardRef}
      role="group"
      aria-label="Permission request"
      tabIndex={-1}
      onKeyDown={(event) => {
        if (busy) return;
        if (event.key === "Enter") {
          event.preventDefault();
          onAllow();
        } else if (event.key === "Escape") {
          event.preventDefault();
          onDeny();
        }
      }}
      className={cx(
        "relative overflow-hidden rounded-lg border border-white/12 bg-slate-900/85 p-3 text-slate-200",
        "shadow-[0_8px_28px_rgb(0_0_0/0.55)] outline-none",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span
          className={cx(
            "rounded-full border px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.16em]",
            TIER_BADGE[tier],
          )}
        >
          {tier}
        </span>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
          Permission required
        </span>
      </div>

      <p className="mt-1.5 text-sm font-bold leading-snug text-white">{title}</p>
      {detail ? (
        <p className="mt-0.5 text-[12px] leading-[1.45] text-slate-300">{detail}</p>
      ) : null}

      <div className="mt-2.5 flex gap-1.5">
        <button
          type="button"
          disabled={busy}
          onClick={onAllow}
          className="flex-1 rounded-md bg-emerald-500 px-3 py-1.5 font-mono text-[10px] font-bold uppercase tracking-[0.14em] text-slate-950 transition-colors hover:bg-emerald-400 disabled:opacity-50"
        >
          {allowLabel}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onDeny}
          className="flex-1 rounded-md border border-white/15 bg-transparent px-3 py-1.5 font-mono text-[10px] font-bold uppercase tracking-[0.14em] text-slate-300 transition-colors hover:border-rose-400/60 hover:bg-rose-500/10 hover:text-rose-300 disabled:opacity-50"
        >
          Deny
        </button>
      </div>
    </div>
  );
}

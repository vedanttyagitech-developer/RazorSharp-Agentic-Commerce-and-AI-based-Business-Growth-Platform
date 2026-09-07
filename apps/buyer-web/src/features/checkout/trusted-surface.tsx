/**
 * The visual grammar that separates a proposal from consent.
 *
 * RazorAI can draw anything it likes inside its own panel: a cart, a total, a button
 * that says "Pay". None of it moves money, because none of it is wired to a capability.
 * The controls that do move money live here, and they are drawn in a register the agent
 * cannot borrow: a heavy near-black frame, a filled header strip, and a sentence naming
 * who is acting. A buyer should be able to tell, without reading a word of the copy,
 * whether the thing in front of them is a suggestion or a decision.
 *
 * The frame is deliberately unlike every other surface in this storefront, which is all
 * hairline greys and soft shadows. Distinctiveness is the whole point: a border that
 * looked like the product cards would be a border a screenshot could forge.
 */
"use client";

import type { ReactNode } from "react";

import { cx } from "@/components/ui";

function ShieldGlyph({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      aria-hidden="true"
      focusable="false"
      className={cx("shrink-0", className)}
    >
      <path
        d="M8 1.2 2.6 3.4v4.1c0 3.2 2.2 6.1 5.4 7.3 3.2-1.2 5.4-4.1 5.4-7.3V3.4L8 1.2Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path
        d="M5.6 8.1 7.2 9.7l3.2-3.4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * A region the buyer, and only the buyer, acts inside.
 *
 * `label` names the act rather than the widget, because the header strip is the only
 * part of this component a hurried person reads.
 */
export function TrustedSurface({
  label = "You are approving this. RazorAI cannot.",
  caption,
  tone = "ink",
  children,
  className,
}: {
  label?: string;
  caption?: ReactNode;
  tone?: "ink" | "danger";
  children: ReactNode;
  className?: string;
}) {
  const strip = tone === "danger" ? "bg-[var(--red)]" : "bg-[var(--ink)]";
  const frame = tone === "danger" ? "border-[var(--red)]" : "border-[var(--ink)]";
  const ring =
    tone === "danger" ? "0 0 0 4px rgba(226, 70, 76, 0.10)" : "0 0 0 4px rgba(31, 31, 31, 0.07)";

  return (
    <section
      aria-label={label}
      className={cx(
        "overflow-hidden rounded-[var(--r-lg)] border-2 bg-white",
        frame,
        className,
      )}
      style={{ boxShadow: ring }}
    >
      <p
        className={cx(
          "flex items-center gap-2 px-4 py-2 text-[12px] font-bold tracking-[0.04em] text-white",
          strip,
        )}
      >
        <ShieldGlyph />
        <span>{label}</span>
      </p>
      <div className="px-4 py-4 sm:px-5 sm:py-5">
        {caption ? (
          <p className="mb-4 text-[13px] leading-[1.5] text-[var(--ink-3)]">{caption}</p>
        ) : null}
        {children}
      </div>
    </section>
  );
}

/**
 * The row a trusted surface's controls sit in.
 *
 * Buttons wrap rather than shrink at 390px, because a truncated "Approve" beside a
 * truncated "Reject" is how a person presses the wrong one.
 */
export function TrustedActions({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("flex flex-wrap items-center gap-3", className)}>{children}</div>;
}

/**
 * A fact stated inside a trusted surface: what pressing the button will and will not do.
 *
 * Rendered as a definition list so a screen reader reads the claim with its subject.
 */
export function TrustedFact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
      <dt className="shrink-0 text-[12px] font-semibold text-[var(--ink-4)] sm:w-40">{term}</dt>
      <dd className="text-[13px] leading-[1.5] text-[var(--ink-2)]">{children}</dd>
    </div>
  );
}

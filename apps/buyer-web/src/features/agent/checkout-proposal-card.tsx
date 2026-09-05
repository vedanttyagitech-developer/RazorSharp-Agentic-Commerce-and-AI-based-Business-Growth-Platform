/**
 * The one place RazorAI's checkout proposal grows a press of its own.
 *
 * A buyer says "check me out". The shopping specialist reads the basket and proposes
 * `checkout.create`, naming the basket it read this turn. Until now the card said so and
 * pointed at the basket page, because opening a checkout is a write and writes on this
 * platform happen on the trusted surface. This card is that trusted surface arriving in
 * the panel: pressing it calls the *same* `POST /v1/baskets/{id}/checkout` the basket
 * page's "Proceed to checkout" button calls, under the same `checkout.create` capability,
 * with the same idempotency discipline — and it commits nothing a buyer has not already
 * seen, because opening a checkout only prices version 1 and puts its approval card in
 * front of them. The money is still theirs to approve, on the checkout page, after this.
 *
 * **The press is the buyer's, and it lands on a named basket.** The proposal must carry a
 * `basket_id` for the button to exist at all: a checkout with no basket to build from is
 * not a proposal anyone can act on, and a control that guessed which basket to open would
 * be opening one the buyer never saw. Absent that id — or absent a handler — this card
 * draws no press and only the door to the basket, exactly as the line card does.
 *
 * **A refusal is drawn, not swallowed.** The route can answer that the basket has moved on
 * (409), or the transport can drop. Either is shown in the card verbatim, the same way the
 * line card renders `proposal_superseded`, and neither navigates anywhere. Only a checkout
 * the server actually opened sends the buyer onward, and it sends them to that checkout's
 * own id — never to a route guessed from the basket.
 *
 * The navigation is a document load, not a client-side push. `/checkout/*` is the one
 * route with its own Content-Security-Policy, and a policy belongs to a document; pushing
 * would carry the panel's policy onto it and Razorpay's script would be refused on
 * arrival. See `requiresOwnDocument` in `lib/security/csp`.
 */
"use client";

import Link from "next/link";
import { useRef, useState } from "react";

import { Button } from "@/components/ui";
import { newIdempotencyKey } from "@/lib/api/client";
import { ApiError, humanMessage } from "@/lib/api/problem";
import type { ApprovalCard } from "@/lib/api/types";
import { requiresOwnDocument } from "@/lib/security/csp";

/**
 * The reason the checkout route answers when the basket a confirmed proposal named has
 * moved on. Mirrors the basket route's superseded reason; a drift between the two would
 * make the refusal render as a generic failure, which the test for it catches.
 */
export const SUPERSEDED = "proposal_superseded";

/** What a press on this card sends: the basket to open, and a key minted once for retries. */
export interface CheckoutConfirmation {
  basket_id: string;
  idempotency_key: string;
}

function ArrowRight() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true" fill="none">
      <path
        d="M4 10 H15 M11 6 L15 10 L11 14"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

type Outcome =
  | { phase: "idle" }
  | { phase: "busy" }
  | { phase: "opened" }
  | { phase: "superseded"; detail: string }
  | { phase: "failed"; message: string };

/** Send the buyer to the checkout that was just opened. Overridable so the test can watch it. */
function goToCheckout(checkoutId: string): void {
  const href = `/checkout/${encodeURIComponent(checkoutId)}`;
  // A document navigation, not `router.push`: the checkout carries its own CSP and a push
  // would keep the panel's, refusing Razorpay's script on arrival. This is the exception the
  // lint rule below cannot see — a client-side push keeps this document's policy, which does
  // not admit Razorpay, and is what leaves the Pay button reporting the provider unreachable.
  // The same suppression and reasoning live on `BasketView.proceed`.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  if (requiresOwnDocument(href)) window.location.assign(href);
}

export function CheckoutProposalCard({
  basketId,
  onConfirm,
  navigate = goToCheckout,
}: {
  /** The basket the proposal named. Null when the turn carried none, and then there is no press. */
  basketId: string | null;
  /**
   * Open a checkout on the trusted surface: the panel sends the write and returns the
   * approval card the route answered with. Absent, this card draws no press.
   */
  onConfirm?: (confirmation: CheckoutConfirmation) => Promise<ApprovalCard>;
  /** Where a successfully opened checkout sends the buyer. Injected only by the test. */
  navigate?: (checkoutId: string) => void;
}) {
  const [outcome, setOutcome] = useState<Outcome>({ phase: "idle" });
  const keyRef = useRef<string | null>(null);

  const bound = onConfirm !== undefined && basketId !== null;

  async function confirm(): Promise<void> {
    if (!onConfirm || basketId === null) return;
    if (outcome.phase === "busy") return;
    keyRef.current ??= newIdempotencyKey();
    setOutcome({ phase: "busy" });
    try {
      const card = await onConfirm({ basket_id: basketId, idempotency_key: keyRef.current });
      keyRef.current = null;
      setOutcome({ phase: "opened" });
      // Only a checkout the server actually opened navigates, and only to its own id.
      navigate(card.checkout_id);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.problem.reason === SUPERSEDED) {
        // The server answered, so the key is spent: opening again is a different write and
        // must not share this one's fingerprint.
        keyRef.current = null;
        setOutcome({ phase: "superseded", detail: cause.problem.detail ?? "" });
        return;
      }
      // No answer, or an answer that is not the superseded refusal. The key is kept so a
      // second press retries the same request rather than issuing a second one.
      setOutcome({ phase: "failed", message: humanMessage(cause) });
    }
  }

  return (
    <section
      className="mt-2 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] border-l-2 border-l-[var(--blue)] bg-white p-3"
      aria-label="Proposal from RazorAI"
    >
      <p className="text-[9px] font-bold tracking-[0.08em] text-[var(--blue)] uppercase">Proposed</p>
      <p className="mt-1 text-[13px] font-semibold text-[var(--ink)]">Open a checkout for this basket</p>
      <p className="mt-0.5 text-[12px] text-[var(--ink-3)]">
        The checkout quotes and reserves; you approve a version there.
      </p>

      {outcome.phase === "opened" ? (
        <p
          role="status"
          className="mt-2.5 rounded-[var(--r-sm)] bg-[var(--green-add-bg)] px-2 py-1.5 text-[12px] leading-[1.45] text-[var(--green-add)]"
        >
          Checkout opened. Taking you to it to approve a version.
        </p>
      ) : null}

      {outcome.phase === "superseded" ? (
        <p
          role="status"
          className="mt-2.5 rounded-[var(--r-sm)] bg-amber-50/70 px-2 py-1.5 text-[12px] leading-[1.45] text-[var(--ink-3)]"
        >
          <code className="font-mono text-[11px] text-[var(--amber)]">{SUPERSEDED}</code>{" "}
          {outcome.detail ||
            "This basket moved after the proposal was prepared, so no checkout was opened. Nothing changed."}{" "}
          Ask RazorAI again, or open the checkout from the basket page.
        </p>
      ) : null}

      {bound && outcome.phase !== "opened" && outcome.phase !== "superseded" ? (
        <>
          <Button
            size="sm"
            className="mt-2.5"
            busy={outcome.phase === "busy"}
            onClick={() => void confirm()}
          >
            Open a checkout for this basket
          </Button>
          {outcome.phase === "failed" ? (
            <p role="alert" className="mt-2 text-[12px] leading-[1.45] text-[var(--red)]">
              {outcome.message} No checkout was opened. Pressing again retries the same request.
            </p>
          ) : null}
          <p className="mt-2 text-[12px] leading-[1.45] text-[var(--ink-4)]">
            This opens a checkout and puts its approval card in front of you. Approving and paying
            happen there, on the store&rsquo;s own page, never in this panel.
          </p>
        </>
      ) : null}

      <Link
        href="/basket"
        className="mt-2.5 inline-flex h-8 items-center gap-1.5 rounded-[var(--r-sm)] border border-[var(--blue)] px-3 text-[13px] font-semibold text-[var(--blue)] transition hover:bg-blue-50"
      >
        Open your basket
        <ArrowRight />
      </Link>
      {!bound ? (
        <p className="mt-2 text-[12px] leading-[1.45] text-[var(--ink-4)]">
          RazorAI cannot approve or pay. You open a checkout on the basket page, not here.
        </p>
      ) : null}
    </section>
  );
}

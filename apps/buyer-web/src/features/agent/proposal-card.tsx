/**
 * A proposal, and the door out of the panel.
 *
 * The agent may name a change; it may not make one. Every write the harness can imagine
 * arrives here as a `proposal` block naming identifiers a tool returned during the same
 * turn, with `executes_on: "trusted_surface"`, and this card's only job is to say what is
 * being proposed and send the buyer somewhere else to decide. There is deliberately no
 * approve, add, pay or cancel control anywhere in this file: a control that commits money
 * inside a surface the agent also writes into is the confusion the whole submission
 * exists to refuse.
 *
 * The card renders only the handoffs this storefront can actually honour. An action whose
 * trusted half does not exist yet renders nothing at all rather than a door into a page
 * with no control behind it; a handoff sentence is only worth anything if every one of
 * them is true.
 *
 * `structured` arrives typed as `unknown` because the API declares it so. It is parsed
 * here, with the same schemas the REST reads use, rather than cast: the payload is the
 * verbatim JSON of a read endpoint, so `BasketSchema` and `CheckoutSchema` fit it exactly,
 * and a shape that does not fit renders nothing. An unrecognised proposal is not a reason
 * to guess at a total in front of someone about to spend money.
 */
"use client";

import Link from "next/link";
import { z } from "zod";

import { Amount } from "@/components/ui";
import { BasketSchema, CheckoutSchema, type Money } from "@/lib/api/types";

/**
 * The proposal envelope of `DeterministicRunner`. Loose, and every field beyond `action`
 * optional, because a model-backed specialist proposing through the same seam may carry
 * more; what this card renders is only what it can read.
 */
const ProposalSchema = z
  .object({
    action: z.string(),
    sku: z.string().nullish(),
    quantity: z.number().int().nullish(),
    basket_id: z.string().nullish(),
    order_id: z.string().nullish(),
    executes_on: z.string().nullish(),
    display: z
      .object({ quantity: z.number().int(), name: z.string() })
      .loose()
      .nullish(),
  })
  .loose();

const StructuredSchema = z
  .object({
    kind: z.string().nullish(),
    proposal: ProposalSchema.nullish(),
  })
  .loose();

/** What the card renders once a shape has been recognised. Nothing in it is computed. */
interface Handoff {
  headline: string;
  detail: string | null;
  amount: Money | null;
  href: string;
  cta: string;
  /** The sentence that draws the line: where this is decided, and by whom. */
  note: string;
}

const BASKET_NOTE = "Nothing is added from this panel. You do it on the basket page.";
const CHECKOUT_NOTE = "RazorAI cannot approve or pay. You approve on the checkout page, not here.";

function proposalHandoff(proposal: z.infer<typeof ProposalSchema>): Handoff | null {
  switch (proposal.action) {
    case "basket.update": {
      const name = proposal.display?.name ?? proposal.sku;
      const quantity = proposal.display?.quantity ?? proposal.quantity;
      if (!name || !quantity) return null;
      return {
        headline: `Add ${quantity} × ${name}`,
        detail: proposal.sku ?? null,
        amount: null,
        href: "/basket",
        cta: "Open your basket",
        note: BASKET_NOTE,
      };
    }
    case "checkout.create": {
      if (!proposal.basket_id) return null;
      return {
        headline: "Open a checkout for this basket",
        detail: "The checkout quotes and reserves; you approve a version there.",
        amount: null,
        href: "/basket",
        cta: "Open your basket",
        note: CHECKOUT_NOTE,
      };
    }
    // `refund.request` and `order.propose_cancel` fall through to `null` on purpose.
    //
    // The API has the refund route -- `POST /v1/orders/{id}/refunds`, buyer-only, under a
    // fresh Execution Grant -- but this storefront has no client method for it and the
    // order page renders refunds read-only, so a card sending a buyer there to confirm
    // would be pointing at a control that does not exist. Sending somebody to a door that
    // is not there is worse than saying nothing: it teaches them that the handoff sentence
    // on every other card is decoration. When the order page grows the trusted half, the
    // case comes back here with it and not before.
    default:
      return null;
  }
}

/**
 * A turn that read a basket or a checkout without proposing a write still earns a door.
 *
 * The agent looked at the thing the buyer is deciding about; pointing at the surface
 * where that decision happens is the honest end of the sentence. Every figure shown comes
 * from the payload's own `total`, which is the quote engine's integer paise.
 */
function readHandoff(structured: unknown, kind: string | null | undefined): Handoff | null {
  if (kind === "checkout") {
    const record = structured as { checkout?: unknown };
    const parsed = CheckoutSchema.safeParse(record.checkout);
    if (!parsed.success) return null;
    const checkout = parsed.data;
    return {
      headline: `Checkout, version ${checkout.current_version}`,
      detail: `State ${checkout.state}.`,
      amount: checkout.approval_card?.total ?? null,
      href: `/checkout/${encodeURIComponent(checkout.checkout_id)}`,
      cta: "Open the checkout",
      note: CHECKOUT_NOTE,
    };
  }
  if (kind === "basket") {
    const record = structured as { basket?: unknown };
    const parsed = BasketSchema.safeParse(record.basket);
    if (!parsed.success) return null;
    const basket = parsed.data;
    const count = basket.lines.length;
    if (count === 0) return null;
    return {
      headline: `Your basket, ${count} ${count === 1 ? "line" : "lines"}`,
      detail: basket.stale ? "The basket has moved since it was last priced." : null,
      amount: basket.quote?.total ?? null,
      href: "/basket",
      cta: "Open your basket",
      note: BASKET_NOTE,
    };
  }
  return null;
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

export function ProposalCard({ structured }: { structured: unknown }) {
  if (structured === null || structured === undefined) return null;
  const envelope = StructuredSchema.safeParse(structured);
  if (!envelope.success) return null;

  const { kind, proposal } = envelope.data;
  const handoff = proposal ? proposalHandoff(proposal) : readHandoff(structured, kind);
  if (handoff === null) return null;

  return (
    <section
      className="mt-2 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] border-l-2 border-l-[var(--blue)] bg-white p-3"
      aria-label="Proposal from RazorAI"
    >
      <p className="text-[9px] font-bold tracking-[0.08em] text-[var(--blue)] uppercase">
        Proposed
      </p>
      <p className="mt-1 text-[13px] font-semibold text-[var(--ink)]">{handoff.headline}</p>
      {handoff.detail ? (
        <p className="mt-0.5 text-[12px] text-[var(--ink-3)]">{handoff.detail}</p>
      ) : null}
      {handoff.amount ? (
        <p className="mt-1.5 text-[16px] font-bold text-[var(--ink)]">
          <Amount money={handoff.amount} />
        </p>
      ) : null}

      <Link
        href={handoff.href}
        className="mt-2.5 inline-flex h-8 items-center gap-1.5 rounded-[var(--r-sm)] border border-[var(--blue)] px-3 text-[13px] font-semibold text-[var(--blue)] transition hover:bg-blue-50"
      >
        {handoff.cta}
        <ArrowRight />
      </Link>

      <p className="mt-2 text-[12px] leading-[1.45] text-[var(--ink-4)]">{handoff.note}</p>
    </section>
  );
}

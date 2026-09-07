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
 * The card renders a door only where this storefront has one. An action whose trusted half
 * does not exist yet gets no link -- a door into a page with no control behind it teaches a
 * buyer that the handoff sentence on every other card is decoration -- and instead gets a
 * card that says the surface is missing. Silence would not do: the support specialist's
 * reply ends with "prepared a refund request for you to confirm on the trusted surface",
 * and a panel that renders that sentence and then nothing has let the storefront make a
 * claim it cannot keep. The correction is placed directly under the sentence it corrects.
 *
 * The two cart-line cards live in `cart-proposal-card.tsx` and this component delegates
 * to them. They do carry buttons, and the split is what keeps the paragraph above honest:
 * the only control there is a row of a "which of these did you mean?" question, and pressing
 * one sends another *message* — it writes nothing, holds nothing and commits nothing. When a
 * control that actually commits a cart line eventually exists, it belongs in that file with
 * its own docstring naming the write it makes, not in this one under a sentence that would
 * then be false.
 *
 * The `checkout.create` proposal is delegated the same way, to `CheckoutProposalCard` in
 * `checkout-proposal-card.tsx`, once the panel passes a handler and the proposal names a
 * cart. That card's press opens a checkout on the trusted surface and sends the buyer to
 * its approval page — it still commits no money, because opening a checkout only prices a
 * version for the buyer to approve there. The button lives in that file, under a docstring
 * naming the write, for the same reason the line button does: so this file's promise of no
 * committing control stays true of this file.
 *
 * `structured` arrives typed as `unknown` because the API declares it so. It is parsed
 * here, with the same schemas the REST reads use, rather than cast: the payload is the
 * verbatim JSON of a read endpoint, so `CartSchema` and `CheckoutSchema` fit it exactly,
 * and a shape that does not fit renders nothing. An unrecognised proposal is not a reason
 * to guess at a total in front of someone about to spend money.
 */
"use client";

import Link from "next/link";
import { z } from "zod";

import { Amount } from "@/components/ui";
import { type ApprovalCard, type Cart, CartSchema, CheckoutSchema, type Money } from "@/lib/api/types";
import { requiresOwnDocument } from "@/lib/security/csp";

import {
  ChoiceCard,
  ChoiceProposalSchema,
  type LineConfirmation,
  LineProposalCard,
  LineProposalSchema,
} from "./cart-proposal-card";
import { type CheckoutConfirmation, CheckoutProposalCard } from "./checkout-proposal-card";

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
    cart_id: z.string().nullish(),
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

/**
 * The door out of the panel, drawn as the box's ghost controls are: a hairline pill that
 * brightens under the pointer. The cards in `cart-proposal-card` and
 * `checkout-proposal-card` draw theirs the same way.
 */
const DOOR =
  "mt-2.5 inline-flex h-8 items-center gap-1.5 rounded-full border border-white/15 bg-white/[0.04] px-3 text-[13px] font-semibold text-slate-200 transition-colors hover:border-white/30 hover:bg-white/[0.08] hover:text-white";

const BASKET_NOTE = "Nothing is added from this panel. You do it on the cart page.";
const CHECKOUT_NOTE = "RazorAI cannot approve or pay. You approve on the checkout page, not here.";

function proposalHandoff(proposal: z.infer<typeof ProposalSchema>): Handoff | null {
  switch (proposal.action) {
    case "cart.update": {
      const name = proposal.display?.name ?? proposal.sku;
      const quantity = proposal.display?.quantity ?? proposal.quantity;
      if (!name || !quantity) return null;
      return {
        headline: `Add ${quantity} × ${name}`,
        detail: proposal.sku ?? null,
        amount: null,
        href: "/cart",
        cta: "Open your cart",
        note: BASKET_NOTE,
      };
    }
    case "checkout.create": {
      if (!proposal.cart_id) return null;
      return {
        headline: "Confirm checkout for this cart",
        detail: "The checkout quotes and reserves; you approve a version there.",
        amount: null,
        href: "/cart",
        cta: "Open your cart",
        note: CHECKOUT_NOTE,
      };
    }
    // `refund.request` and `order.propose_cancel` have no case here on purpose: they are
    // handled by `MISSING_SURFACES` below, which draws the absence instead of a door.
    default:
      return null;
  }
}

/**
 * The remedies the support specialist proposes and this storefront cannot take delivery of.
 *
 * The refund route is real -- `POST /v1/orders/{id}/refunds`, buyer-only, under a fresh
 * Execution Grant -- and so is the client method: `requestRefund` is defined in
 * `src/lib/api/client.ts`. What is absent is a caller. No page in this storefront invokes
 * it and the order screen renders refunds read-only, so there is still nowhere to send
 * anyone, and the reply above this card has already told the buyer to confirm somewhere.
 * `where` is the
 * part of that gap this panel can state precisely; it never guesses at an amount, because
 * the server deliberately proposes these with `amount_minor: null`.
 *
 * The path is reachable only for a turn that carries an order id, which this storefront
 * does not yet send. The card is written for the sentence rather than for the route: the
 * server appends that promise to any client that does send one, and a panel that renders
 * the promise owes the correction whether or not it is reached today.
 */
const MISSING_SURFACES: Readonly<Record<string, { noun: string; where: string }>> = {
  "refund.request": {
    noun: "refund",
    where:
      "The platform does have the route — a buyer, and only a buyer, may ask for a refund on an order under a fresh grant — but no page in this storefront offers that control.",
  },
  "order.propose_cancel": {
    noun: "cancellation",
    where: "No page in this storefront offers a control for cancelling an order.",
  },
};

/**
 * A turn that read a cart or a checkout without proposing a write still earns a door.
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
  if (kind === "cart") {
    const record = structured as { cart?: unknown };
    const parsed = CartSchema.safeParse(record.cart);
    if (!parsed.success) return null;
    const cart = parsed.data;
    const count = cart.lines.length;
    if (count === 0) return null;
    return {
      headline: `Your cart, ${count} ${count === 1 ? "line" : "lines"}`,
      detail: cart.stale ? "The cart has moved since it was last priced." : null,
      amount: cart.quote?.total ?? null,
      href: "/cart",
      cta: "Open your cart",
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

/**
 * What the panel draws when the agent named a surface the storefront does not have.
 *
 * Amber and headed "Not prepared", the same vocabulary the denial card uses, because this
 * is the same fact: something was asked for and nothing happened. It carries no link,
 * since the whole point is that there is nowhere to go.
 */
function MissingSurface({ noun, where }: { noun: string; where: string }) {
  return (
    <section
      className="mt-2 rounded-lg border border-white/10 bg-white/[0.04] p-3 text-slate-200"
      aria-label={`No surface for a ${noun}`}
    >
      <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-amber-300">
        Not prepared
      </p>
      <p className="mt-1 text-[13px] font-semibold text-slate-100">
        There is nowhere here to confirm a {noun}
      </p>
      <p className="mt-1 text-[12px] leading-[1.45] text-slate-300">
        RazorAI says one is waiting for you on a trusted surface. {where} Nothing has been
        prepared and nothing is waiting on you, and saying so is better than a button that
        goes nowhere.
      </p>
    </section>
  );
}

export function ProposalCard({
  structured,
  onAsk,
  onConfirmLine,
  onConfirmCheckout,
  onOpened,
}: {
  structured: unknown;
  /**
   * Send another message to RazorAI. Absent while a turn is in flight, which is how the
   * choice card knows to draw its rows as unpressable rather than accepting a press it
   * would drop. It sends a message; it does not write anything.
   */
  onAsk?: (message: string) => void;
  /**
   * Execute a bound line proposal on the trusted surface. The panel owns it because the
   * panel holds the cart context whose header pill must be re-read after the write.
   * Absent, the line card draws no press.
   */
  onConfirmLine?: (confirmation: LineConfirmation) => Promise<Cart>;
  /**
   * Open a checkout on the trusted surface for a bound `checkout.create` proposal. The
   * panel owns it because it holds the cart context it must forget once the cart is
   * closed into a checkout. Absent, the checkout card draws no press — only the door.
   */
  onConfirmCheckout?: (confirmation: CheckoutConfirmation) => Promise<ApprovalCard>;
  /** Given, an opened checkout is handed back instead of navigated to. */
  onOpened?: (checkoutId: string) => void;
}) {
  if (structured === null || structured === undefined) return null;
  const envelope = StructuredSchema.safeParse(structured);
  if (!envelope.success) return null;

  const { kind, proposal } = envelope.data;
  const missing = proposal ? MISSING_SURFACES[proposal.action] : undefined;
  if (missing) return <MissingSurface noun={missing.noun} where={missing.where} />;

  /*
   * The two cart cards are tried before the generic handoff below, and they are tried by
   * *parsing* rather than by switching on `action`: a payload that does not carry the fields
   * they need falls through to the older, plainer card instead of rendering a card with
   * blanks in it. That is what keeps this component correct against a server that has not
   * been deployed yet, and against a model-backed specialist proposing through the same seam
   * with a shape of its own.
   */
  if (proposal) {
    const choice = ChoiceProposalSchema.safeParse(proposal);
    if (choice.success) return <ChoiceCard proposal={choice.data} onAsk={onAsk} />;
    const line = LineProposalSchema.safeParse(proposal);
    if (line.success) return <LineProposalCard proposal={line.data} onConfirm={onConfirmLine} />;
    // A `checkout.create` proposal grows a real press only when the panel passed a handler
    // and the proposal named a cart to open. Without both it falls through to the plain
    // handoff below, which draws the door to the cart and no button — the same graceful
    // degradation the cart cards rely on across a deploy in either order.
    if (proposal.action === "checkout.create" && onConfirmCheckout && proposal.cart_id) {
      return (
        <CheckoutProposalCard
      onOpened={onOpened} cartId={proposal.cart_id} onConfirm={onConfirmCheckout} />
      );
    }
  }

  const handoff = proposal ? proposalHandoff(proposal) : readHandoff(structured, kind);
  if (handoff === null) return null;

  return (
    <section
      className="mt-2 rounded-lg border border-white/10 border-l-2 border-l-primary bg-white/[0.04] p-3 text-slate-200"
      aria-label="Proposal from RazorAI"
    >
      <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-indigo-300">
        Proposed
      </p>
      <p className="mt-1 text-[13px] font-semibold text-slate-100">{handoff.headline}</p>
      {handoff.detail ? (
        <p className="mt-0.5 text-[12px] text-slate-300">{handoff.detail}</p>
      ) : null}
      {handoff.amount ? (
        <p className="mt-1.5 text-[16px] font-bold text-slate-100">
          <Amount money={handoff.amount} />
        </p>
      ) : null}

      {/*
        A plain anchor for the checkout, `next/link` for everything else. `Link` intercepts
        the click and swaps the tree without fetching a document, and the checkout is the
        one route whose Content-Security-Policy differs from the rest of the site -- it
        would arrive under this page's policy, with Razorpay's script refused. See
        `requiresOwnDocument` in `lib/security/csp`.
      */}
      {requiresOwnDocument(handoff.href) ? (
        <a
          href={handoff.href}
          className={DOOR}
        >
          {handoff.cta}
          <ArrowRight />
        </a>
      ) : (
        <Link
          href={handoff.href}
          className={DOOR}
        >
          {handoff.cta}
          <ArrowRight />
        </Link>
      )}

      <p className="mt-2 text-[12px] leading-[1.45] text-slate-400">{handoff.note}</p>
    </section>
  );
}

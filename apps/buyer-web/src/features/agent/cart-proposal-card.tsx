/**
 * The two halves of one conversation about a cart line: *which one*, then *what it costs*.
 *
 * A buyer says "add 2 amul milk". Five products match, so the first card asks which — and
 * pressing a row writes nothing at all. It sends another message naming one SKU, and the
 * turn that comes back carries the second card: the product, the merchant's own unit price,
 * the quantity the line would end up at, and what the cart is worth right now. Two presses
 * rather than one, deliberately. Collapsing them would have the buyer consenting to a price
 * they glimpsed in a list of five while choosing on the name.
 *
 * **The line card's press is the buyer's, and it is bound.** RazorAI proposes; the buyer
 * presses; the platform executes -- on the trusted surface, through the same
 * `PUT /v1/carts/{id}/lines/{sku}` the cart page uses, with one addition: the press
 * sends back the cart hash, unit price and catalogue revision the proposal was prepared
 * against (`binding`), and the server refuses the write if any of them has moved. The card
 * draws that refusal with its reason code verbatim. It draws no press at all when the
 * proposal carries no binding -- no cart open, cart unreadable, or an older envelope --
 * because a control that committed at a price the server had not re-checked would be worse
 * than the door to the cart page it stands beside.
 *
 * **Every figure here is copied, never computed.** `unit_price` and `stock_units` are the
 * product read's; `basket_total` is the quote engine's `total` off the cart read of the
 * same turn. There is no multiplication anywhere in this file — the line subtotal a buyer
 * will owe is the fee engine's to state once the line exists, which is after they add it.
 *
 * **The clamps are drawn, not hidden.** A request for 104 of something becomes a proposal for
 * 99, and the card says so with the original number on it: a quantity the platform
 * substituted and did not mention is a figure nobody agreed to. A request past the shelf
 * count is reported and *not* trimmed, because trimming would read as a hold and nothing is
 * held — stock is reserved at checkout, and no agent on this platform can reserve anything.
 */
"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { z } from "zod";

import { Amount, Button, cx } from "@/components/ui";
import { newIdempotencyKey } from "@/lib/api/client";
import { ApiError, humanMessage } from "@/lib/api/problem";
import { type Cart, type ExpectedCart, ExpectedCartSchema, MoneySchema } from "@/lib/api/types";

/**
 * The reason the cart route answers when a confirmed proposal no longer matches the
 * cart, the price or the catalogue. Mirrors `cart_service.SUPERSEDED`; a drift between
 * the two would make the refusal render as a generic failure, which the test for it catches.
 */
export const SUPERSEDED = "proposal_superseded";

/** One row of a "which of these did you mean" question. Every field is a search hit's. */
const CandidateSchema = z.object({
  sku: z.string(),
  display_name: z.string(),
  unit_label: z.string(),
  unit_price: MoneySchema,
  stock_units: z.number().int(),
  is_available: z.boolean(),
  matched_terms: z.array(z.string()),
});

export const ChoiceProposalSchema = z.object({
  action: z.literal("cart.disambiguate"),
  quantity: z.number().int(),
  candidates: z.array(CandidateSchema).min(2),
});

/**
 * A priced line proposal.
 *
 * `quantity` is the **absolute** quantity the cart route would be sent, and it is nullable
 * because it genuinely is not always knowable: with no cart open there is no line to make
 * absolute against. `delta` is what the buyer asked for. The server keeps those two apart
 * because the route reads an absolute and the message states a delta, and conflating them
 * would take a line of three down to two on a request to add two.
 *
 * **`basket.update` is the wire's spelling, not a name this app may choose.** It is the
 * capability string `Capability.BASKET_UPDATE` — the one the platform withholds from the
 * agent — and the cart rename deliberately kept `basket` in capability strings, tool names,
 * the protocol wire and idempotency operations while renaming everything this storefront
 * names for itself. `display.basket_total` is on the wire for the same reason. Spelling
 * either of them `cart` here does not fail loudly: `safeParse` returns false, the payload
 * falls through to the plainer handoff card, and `runDirectAdd` in `razorai-panel` never
 * runs, so RazorAI silently stops adding lines. That is what happened, and the fixtures in
 * this component's test file are copied off the route so it cannot happen unnoticed again.
 */
export const LineProposalSchema = z.object({
  action: z.literal("basket.update"),
  sku: z.string(),
  delta: z.number().int(),
  current_quantity: z.number().int().nullable(),
  quantity: z.number().int().nullable(),
  clamped_from: z.number().int().nullable(),
  exceeds_stock: z.boolean(),
  blocked_by: z.enum(["no_basket", "basket_unreadable"]).nullable(),
  /** Which cart the write would land on. Null exactly when `blocked_by` is `no_basket`. */
  cart_id: z.string().nullable(),
  /**
   * What the proposal was prepared against, copied from the same turn's `cart.read` and
   * `catalog.get_product`. Null when the cart could not be read, and then there is
   * nothing to bind a press to, so there is no press.
   */
  binding: ExpectedCartSchema.nullable(),
  display: z.object({
    quantity: z.number().int(),
    name: z.string(),
    unit_label: z.string(),
    unit_price: MoneySchema,
    stock_units: z.number().int(),
    basket_total: MoneySchema.nullable(),
  }),
});

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

/** A stated fact about the proposal that the buyer did not ask for. Amber, never red. */
function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-1.5 rounded-md border border-amber-400/30 bg-amber-400/10 px-2 py-1.5 text-[12px] leading-[1.45] text-slate-300">
      {children}
    </p>
  );
}

/**
 * What a press on the line card sends. Every field is copied off the proposal; the key is
 * minted by the card so that retrying the same press after a transport failure is the same
 * request, and a fresh press after an answer is a different one.
 */
export interface LineConfirmation {
  cart_id: string;
  sku: string;
  quantity: number;
  expected: ExpectedCart;
  idempotency_key: string;
}

type Outcome =
  | { phase: "idle" }
  | { phase: "busy" }
  | { phase: "added"; cart: Cart }
  | { phase: "superseded"; detail: string }
  | { phase: "failed"; message: string };

export function LineProposalCard({
  proposal,
  onConfirm,
}: {
  proposal: z.infer<typeof LineProposalSchema>;
  /**
   * Execute the proposal on the trusted surface: the panel sends the write and re-reads
   * the cart the header shows. Absent where there is no cart context to do that with,
   * and then this card draws no press -- the door to the cart is its only control.
   */
  onConfirm?: (confirmation: LineConfirmation) => Promise<Cart>;
}) {
  const { display } = proposal;
  const current = proposal.current_quantity;
  const absolute = proposal.quantity;
  const growing = current !== null && current > 0 && absolute !== null;
  const [outcome, setOutcome] = useState<Outcome>({ phase: "idle" });
  const keyRef = useRef<string | null>(null);

  const bound =
    onConfirm !== undefined &&
    proposal.cart_id !== null &&
    proposal.binding !== null &&
    absolute !== null &&
    proposal.blocked_by === null;

  async function confirm(): Promise<void> {
    if (!onConfirm || proposal.cart_id === null || proposal.binding === null || absolute === null) {
      return;
    }
    if (outcome.phase === "busy") return;
    keyRef.current ??= newIdempotencyKey();
    setOutcome({ phase: "busy" });
    try {
      const cart = await onConfirm({
        cart_id: proposal.cart_id,
        sku: proposal.sku,
        quantity: absolute,
        expected: proposal.binding,
        idempotency_key: keyRef.current,
      });
      keyRef.current = null;
      setOutcome({ phase: "added", cart });
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.problem.reason === SUPERSEDED) {
        // The server answered, so the key is spent: a proposal made again is a different
        // write with a different binding, and it must not share this one's fingerprint.
        keyRef.current = null;
        setOutcome({ phase: "superseded", detail: cause.problem.detail ?? "" });
        return;
      }
      // No answer, or an answer that is not a refusal of the binding. The key is kept so
      // that pressing again retries the same request rather than issuing a second one.
      setOutcome({ phase: "failed", message: humanMessage(cause) });
    }
  }

  // Read off the cart the server returned, never off the proposal: the quantity the line
  // holds now and the total the store re-quoted are the server's figures after the write.
  const added = outcome.phase === "added" ? outcome.cart : null;
  const addedLine = added?.lines.find((line) => line.sku === proposal.sku) ?? null;

  return (
    <section
      className="mt-2 rounded-lg border border-white/10 border-l-2 border-l-primary bg-white/[0.04] p-3 text-slate-200"
      aria-label="Proposal from RazorAI"
    >
      <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-indigo-300">Proposed</p>
      <p className="mt-1 text-[13px] font-semibold text-slate-100">
        {growing
          ? `Take ${display.name} to ${absolute}`
          : `Add ${display.quantity} × ${display.name}`}
      </p>
      <p className="mt-0.5 text-[12px] text-slate-400">
        {proposal.sku} · {display.unit_label}
      </p>

      <dl className="mt-2 flex flex-col gap-1 text-[12px]">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="text-slate-400">The store&rsquo;s price, each</dt>
          <dd className="font-semibold text-slate-100">
            <Amount money={display.unit_price} />
          </dd>
        </div>
        {growing ? (
          <div className="flex items-baseline justify-between gap-3">
            <dt className="text-slate-400">This line</dt>
            <dd className="tnum font-semibold text-slate-100">
              {current} &rarr; {absolute}
            </dd>
          </div>
        ) : null}
        {display.basket_total ? (
          <div className="flex items-baseline justify-between gap-3">
            <dt className="text-slate-400">Your cart right now</dt>
            <dd className="font-semibold text-slate-100">
              <Amount money={display.basket_total} />
            </dd>
          </div>
        ) : null}
      </dl>

      {/*
        No line subtotal and no "cart after" figure. Both would be this component
        multiplying and adding, and the fee engine is the only thing on this platform
        permitted to do either. The store re-quotes when the line is actually added, and
        that quote is the one the buyer is shown next.
      */}

      {/*
        When a clamp fired, the proposed absolute *is* the ceiling, so it is read out of the
        payload rather than restated here. `cart_service.MAX_LINE_QUANTITY` already has one
        copy in this app, in `use-cart.ts`; a second one in a sentence would be the number
        most likely to drift and least likely to be noticed drifting.
      */}
      {proposal.clamped_from !== null && absolute !== null ? (
        <Note>
          You asked for {proposal.clamped_from}. One line holds at most {absolute}, so this
          proposal is for {absolute} — it has not been quietly rounded for you.
        </Note>
      ) : null}
      {proposal.exceeds_stock ? (
        <Note>
          The store lists {display.stock_units} on the shelf, fewer than proposed. Nothing is
          held for you either way: stock is reserved at checkout, and RazorAI cannot reserve
          anything.
        </Note>
      ) : null}
      {proposal.blocked_by === "no_basket" ? (
        <Note>
          You have no cart open yet. Opening one is yours to do — RazorAI has no way to start
          a cart on your behalf, and this proposal names none.
        </Note>
      ) : null}
      {proposal.blocked_by === "basket_unreadable" ? (
        <Note>
          Your cart could not be read this turn, so the quantity this line would end up at is
          not stated here rather than guessed at. The cart page has it exactly.
        </Note>
      ) : null}

      {added ? (
        <p
          role="status"
          className="mt-2.5 rounded-md border border-emerald-400/30 bg-emerald-500/10 px-2 py-1.5 text-[12px] leading-[1.45] text-emerald-200"
        >
          Added. This line is now{" "}
          <span className="tnum font-semibold">{addedLine ? addedLine.quantity : 0}</span>
          {added.quote ? (
            <>
              {" "}
              and your cart is{" "}
              <Amount money={added.quote.total} className="font-semibold" />
            </>
          ) : null}
          , as the store re-quoted it.
        </p>
      ) : null}

      {outcome.phase === "superseded" ? (
        <p
          role="status"
          className="mt-2.5 rounded-md border border-amber-400/30 bg-amber-400/10 px-2 py-1.5 text-[12px] leading-[1.45] text-slate-300"
        >
          <code className="font-mono text-[11px] text-amber-300">{SUPERSEDED}</code>{" "}
          {outcome.detail ||
            "The cart, the price or the catalogue moved after this was prepared, so it was not applied. Nothing changed."}{" "}
          Ask RazorAI again for a fresh proposal, or add it from the cart page.
        </p>
      ) : null}

      {bound && added === null && outcome.phase !== "superseded" ? (
        <>
          <Button
            size="sm"
            className="mt-2.5"
            busy={outcome.phase === "busy"}
            onClick={() => void confirm()}
          >
            {growing ? `Take this line to ${absolute}` : `Add ${display.quantity} to your cart`}
          </Button>
          {outcome.phase === "failed" ? (
            <p role="alert" className="mt-2 text-[12px] leading-[1.45] text-rose-300">
              {outcome.message} Nothing was added. Pressing again retries the same request.
            </p>
          ) : null}
          <p className="mt-2 text-[12px] leading-[1.45] text-slate-400">
            Pressing this sends the store the exact cart, price and catalogue revision this
            was prepared against. If any of them moved, the store refuses and nothing changes.
          </p>
        </>
      ) : null}

      <Link
        href="/cart"
        className="mt-2.5 inline-flex h-8 items-center gap-1.5 rounded-full border border-white/15 bg-white/[0.04] px-3 text-[13px] font-semibold text-slate-200 transition-colors hover:border-white/30 hover:bg-white/[0.08] hover:text-white"
      >
        Open your cart
        <ArrowRight />
      </Link>
      {!bound && added === null ? (
        <p className="mt-2 text-[12px] leading-[1.45] text-slate-400">
          Nothing is added from this panel. You do it on the cart page, and the store re-quotes
          when you do.
        </p>
      ) : null}
    </section>
  );
}

/**
 * "Which of these did you mean?" — and no row is the recommended one.
 *
 * Every row is drawn identically: same weight, same border, same order the search returned
 * them in. A highlighted "best match" would be this surface choosing for someone who has just
 * been told it will not. Declining is the composer, which is one key away and always there,
 * so the sentence under the rows points at it rather than dressing up a decline as a button
 * competing with five accepts.
 *
 * A row the merchant has no stock for is shown and is not pressable: it is on the list because
 * the search matched it, and hiding it would leave the buyer wondering where their brand went.
 */
export function ChoiceCard({
  proposal,
  onAsk,
}: {
  proposal: z.infer<typeof ChoiceProposalSchema>;
  onAsk?: (message: string) => void;
}) {
  return (
    <section
      className="mt-2 rounded-lg border border-white/10 border-l-2 border-l-primary bg-white/[0.04] p-3 text-slate-200"
      aria-label="RazorAI is asking which product you meant"
    >
      <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-indigo-300">
        Which one?
      </p>
      <p className="mt-1 text-[13px] font-semibold text-slate-100">
        {proposal.candidates.length} products matched. Pick one and RazorAI will price{" "}
        {proposal.quantity} of it.
      </p>

      <ul className="mt-2 flex flex-col gap-1.5">
        {proposal.candidates.map((candidate) => {
          const label = `${candidate.display_name}, ${candidate.unit_label}`;
          const pressable = candidate.is_available && onAsk !== undefined;
          return (
            <li key={candidate.sku}>
              <button
                type="button"
                disabled={!pressable}
                onClick={
                  pressable
                    ? () => onAsk(`add ${proposal.quantity} ${candidate.sku}`)
                    : undefined
                }
                aria-label={`Choose ${label}`}
                className={cx(
                  "flex w-full items-baseline justify-between gap-3 rounded-md",
                  "border border-white/10 bg-white/[0.03] px-2.5 py-2 text-left transition-colors",
                  pressable
                    ? "hover:border-white/30 hover:bg-white/[0.08]"
                    : "cursor-not-allowed opacity-55",
                )}
              >
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-semibold text-slate-100">
                    {candidate.display_name}
                  </span>
                  <span className="block text-[11px] text-slate-400">
                    {candidate.unit_label} ·{" "}
                    {candidate.is_available
                      ? `${candidate.stock_units} in stock`
                      : "not available right now"}
                  </span>
                  {/*
                    Why this row is on the list at all. `matched_terms` is the search index's
                    own answer, so a Hinglish query that reached an English product can be
                    checked rather than taken on trust.
                  */}
                  <span className="block text-[11px] text-slate-500">
                    matched {candidate.matched_terms.join(", ")}
                  </span>
                </span>
                <span className="shrink-0 text-[13px] font-semibold text-slate-100">
                  <Amount money={candidate.unit_price} />
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      <p className="mt-2 text-[12px] leading-[1.45] text-slate-400">
        None of these? Say what you meant in the box below — choosing one only asks RazorAI to
        price it, and nothing is added to your cart either way.
      </p>
    </section>
  );
}

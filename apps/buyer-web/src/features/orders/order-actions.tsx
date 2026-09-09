/**
 * What a buyer may *do* about an order, as opposed to what they may read about it.
 *
 * Until this panel existed the order screen was a very careful read-only document: it
 * could tell a buyer, in full, that ₹681.95 had left their account and which webhook
 * proved it, and it offered them no way to ask for a single paisa of it back. That is not
 * a missing feature so much as a missing right, and on a product whose pitch is that it
 * improves merchant revenue, a screen that makes buying easy and unbuying impossible is
 * the exact pressure the product must not apply. So the three controls here are offered
 * plainly, in the order a buyer needs them -- refund, cancel, reach a person -- and the
 * copy on each is flat.
 *
 * **This module decides nothing.** That is the whole design, and it is worth being
 * pedantic about because the tempting version of this screen is the wrong one.
 *
 * The tempting version reads `order.state` and greys out the refund button on a REFUNDED
 * order, hides the cancel button once an order exists, and saves everybody a round trip.
 * It would even be right most of the time. But "most of the time" is the problem: the
 * kernel's refund ledger counts refunds that are in flight at the provider and invisible
 * from here, the attempt may be reconciling, and Safe Mode may be on -- three facts this
 * browser cannot see and each of which changes the answer. A disabled button is the
 * storefront telling the buyer what the platform would say, in the platform's voice,
 * without having asked it. When it guesses right it is still a claim it has no standing
 * to make; when it guesses wrong it silently withholds a remedy the buyer is entitled to,
 * and the buyer never learns that they were refused by a `disabled` attribute rather than
 * by a rule.
 *
 * So every control is live, every press sends a real request, and the answer is rendered
 * as the platform's own -- including "no". Both endpoints answer **HTTP 200 whether they
 * admit or refuse** (ADR 0003 D15), so `allowed` is read and the status code never is, and
 * a refusal is drawn as an answer rather than as an error. A buyer told "a refund is
 * already in flight on this payment" has learned something true and useful. A buyer shown
 * a greyed-out button has learned nothing at all.
 *
 * Two claims this file is careful never to make:
 *
 *  - **That money is coming back, before the platform will send it.** An admitted refund
 *    is admitted, not settled: the kernel wrote the row and put the command in the outbox
 *    under a single-use grant, and the provider has not yet answered. The success panel
 *    says that in those words. "Refunded" is a word reserved for `REFUNDED`, which is the
 *    refunds list's business below, not this panel's.
 *  - **That the platform can put the buyer through to a person.** It cannot: there is no
 *    buyer-facing route that opens a support case, and inventing a button that appears to
 *    file one would be the worst lie on the screen. The third panel says what the platform
 *    already does on its own and hands over the references a human would ask for.
 */
"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { Amount, Badge, Button, cx } from "@/components/ui";
import { codePhrase, reasonSentence } from "@/features/checkout/refusal-card";
import { api, newIdempotencyKey } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { Decision, Order, Refundable, SupportCase } from "@/lib/api/types";

import { MONO, SectionCard } from "./capture-evidence";

/* --------------------------------------------------------------- vocabulary */

/**
 * Reason keys the kernel does not know about, said as sentences.
 *
 * Consulted *after* the checkout screen's own table, never before it, so that a key both
 * screens can receive is worded identically on both. `payment_surface_open` is the case
 * that makes the ordering matter: it refuses a cancellation from the checkout screen and
 * from this one, and a buyer who reads two different explanations of one fact will
 * reasonably conclude that the two screens disagree about their money.
 *
 * A key in neither table is printed as itself. That is deliberate -- a reason this app has
 * no sentence for is still evidence, and replacing it with a soothing generic sentence
 * would be inventing an explanation the kernel did not give.
 */
const ORDER_REASONS: Readonly<Record<string, string>> = {
  // --- refund admission -------------------------------------------------------
  refund_already_in_flight:
    "A refund is already in flight on this payment. The kernel allows one at a time, so this request was refused rather than risking the same money being sent back twice.",
  reconcile_unknown_first:
    "The platform does not yet know the outcome of the last thing that happened to this payment, so it will not start a refund on top of it. Reconciliation settles that against the provider's own record first.",
  nothing_remaining:
    "There is nothing left to refund on this payment. The full amount has already been refunded or is already on its way back.",
  exceeds_remaining:
    "The amount asked for is more than what is still refundable on this payment. The figures are below, and the platform's is the one drawn from its own capture ledger.",
  currency_mismatch:
    "The amount asked for is in a different currency from the one this payment was captured in.",
  non_positive_amount: "A refund has to be for more than zero.",
  state_forbids_refund:
    "This payment is not in a state a refund can be started from. Money that was never captured cannot be sent back.",
  stale_capture_requires_full_refund:
    "This payment was captured against a checkout that was no longer valid, and the platform returns all of that in one piece rather than part of it.",
  grant_already_live:
    "An authority to move money on this payment is already outstanding, so a second was not issued.",
  payment_attempt_not_found: "The platform has no payment attempt on record for this order.",

  // --- the terms this sale was made under -------------------------------------
  // These come from the Policy-at-Sale Receipt, which froze the merchant's rules onto
  // this order when it was placed. They are refusals about what was agreed, not about
  // what the platform can do, so each says whose decision it was.
  outside_refund_window:
    "The refund window on this order has closed. The deadline was set by the terms in force when the order was placed, and a later change to the shop's policy cannot move it in either direction.",
  refund_not_offered:
    "This order was sold under terms that do not offer a refund. That was the shop's policy at the time of the sale, and it is the policy this order is held to.",
  partial_refund_not_offered:
    "This order was sold under terms that refund the whole amount or none of it, so part of it cannot be sent back on its own.",
  at_sale_terms_unverifiable:
    "The platform cannot verify the terms this order was sold under, so it will not decide a refund by guessing at them. This is a fault on the platform's side and needs a person to look at it.",
  at_sale_terms_unreadable:
    "The terms recorded against this order cannot be read, so the platform will not act on what it thinks they meant. This needs a person to look at it.",

  // --- cancellation -----------------------------------------------------------
  version_terminal:
    "This checkout has already finished. It ended in the sale this order records, so there is no longer anything to cancel -- the way back from a completed sale is a refund.",
  already_cancelled: "This checkout was already cancelled.",
  payment_outcome_unknown:
    "The platform cannot yet say whether this payment succeeded, so it will not cancel on top of it. Cancelling something a buyer turns out to have been charged for is worse than not cancelling, so it reconciles against the provider first.",
  cancellation_not_representable:
    "The platform has no way to represent a cancellation from where this checkout currently stands.",
};

/** Recovery codes the checkout screen's table does not carry, as short phrases. */
const ORDER_CODES: Readonly<Record<string, string>> = {
  POLICY_EXCEPTION: "The rules do not allow this",
  RECONCILIATION_IN_PROGRESS: "The platform is still settling what happened",
  PAYMENT_PENDING: "A payment is still in flight",
  PAYMENT_UNKNOWN: "The outcome of the payment is not known",
  HUMAN_REVIEW_REQUIRED: "A person needs to look at this",
};

/**
 * The shared vocabulary first, this screen's own second.
 *
 * Exported so the tests can assert the precedence rather than infer it from rendered
 * prose: the property that matters is that these two screens cannot drift apart, and it
 * is only checkable if the resolution order is something a test can call.
 */
export function sentenceFor(key: string | null | undefined): string | null {
  return reasonSentence(key) ?? (key ? (ORDER_REASONS[key] ?? null) : null);
}

export function phraseFor(code: string | null | undefined): string | null {
  return codePhrase(code) ?? (code ? (ORDER_CODES[code] ?? null) : null);
}

/**
 * Why a buyer is asking, as the stable keys the kernel stores in `reason_code`.
 *
 * Keys rather than the buyer's own words, because this field is written into the refunds
 * row and read back by operators and reconciliation; free prose there is a field nobody
 * can group, and a place personal information ends up by accident. The labels are the
 * buyer's language and the keys are the platform's, which is the same split the kernel
 * makes with `explanation`, in the other direction.
 *
 * `buyer_requested` leads and is the default because it is the one that assumes nothing.
 * A buyer who does not want to give a reason should not have to shop for the least
 * incriminating one, so the neutral option is the one already selected.
 */
const REFUND_REASONS: ReadonlyArray<{ key: string; label: string }> = [
  { key: "buyer_requested", label: "I would like a refund" },
  { key: "item_not_delivered", label: "It never arrived" },
  { key: "item_damaged", label: "It arrived damaged" },
  { key: "wrong_item", label: "It was not what I ordered" },
  { key: "ordered_by_mistake", label: "I ordered it by mistake" },
];

/* ------------------------------------------------------------------ fragments */

/**
 * A delta the kernel sent with a refund denial.
 *
 * `Delta` is a checkout-shaped record -- `approved` against `current` -- and printing
 * those two headings over a refund's figures would be wrong in a way that matters:
 * on `EXCEEDS_REMAINING` the field named `approved` is what the platform will still
 * refund and `current` is what the buyer just asked for, and neither of them is an
 * approval. So the reason picks the labels, and a reason with no entry gets neutral ones
 * rather than the checkout's.
 */
const DELTA_LABELS: Readonly<Record<string, { left: string; right: string }>> = {
  EXCEEDS_REMAINING: { left: "Still refundable", right: "You asked for" },
};

function DeltaFigures({ decision, currency }: { decision: Decision; currency: string }) {
  if (decision.deltas.length === 0) return null;
  return (
    <dl className="mt-3 grid gap-2">
      {decision.deltas.map((delta) => {
        const labels = DELTA_LABELS[delta.reason ?? ""] ?? {
          left: "The platform's figure",
          right: "Your request",
        };
        const money = delta.field_path.endsWith("amount_minor");
        return (
          <div
            key={`${delta.field_path}:${String(delta.approved)}:${String(delta.current)}`}
            className="rounded-[var(--r-sm)] bg-[var(--tint-2)] px-3 py-2"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <dt className="text-[12px] font-semibold text-[var(--ink-4)]">{labels.left}</dt>
              <dd className="text-[14px] font-bold text-[var(--ink)]">
                <Figure value={delta.approved} money={money} currency={currency} />
              </dd>
            </div>
            <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <dt className="text-[12px] font-semibold text-[var(--ink-4)]">{labels.right}</dt>
              <dd className="text-[14px] text-[var(--ink-2)]">
                <Figure value={delta.current} money={money} currency={currency} />
              </dd>
            </div>
          </div>
        );
      })}
    </dl>
  );
}

/**
 * One side of a delta. The kernel types these as unknown, so a value that is not an
 * integer number of paise is printed as itself instead of being coerced into money.
 */
function Figure({ value, money, currency }: { value: unknown; money: boolean; currency: string }) {
  if (money && typeof value === "number" && Number.isInteger(value)) {
    return <Amount minor={value} currency={currency} />;
  }
  return <span className={MONO}>{value === null ? "—" : String(value)}</span>;
}

/**
 * The platform's refusal, drawn as an answer.
 *
 * Not `ErrorState`, and not a red banner with an exclamation mark. A denial here is the
 * kernel doing its job, and dressing it as a fault teaches the buyer that the platform is
 * broken when in fact it is being careful with their money. The code is shown verbatim
 * beside the sentence so that a buyer on the phone to somebody can read out the thing the
 * platform actually said.
 */
function Refusal({
  heading,
  code,
  explanation,
  children,
}: {
  heading: string;
  code: string | null;
  explanation: string | null;
  children?: ReactNode;
}) {
  const sentence = sentenceFor(explanation);
  const phrase = phraseFor(code);
  return (
    <div
      role="status"
      aria-live="polite"
      className="mt-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--amber)] bg-amber-50/40 px-3 py-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="amber">{heading}</Badge>
        <code className={cx(MONO, "rounded-[var(--r-sm)] bg-white px-1.5 py-0.5")}>
          {code || "no code sent"}
        </code>
        {phrase ? <span className="text-[12px] text-[var(--ink-4)]">{phrase}</span> : null}
      </div>
      <p className="mt-2 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
        {sentence ?? (
          <>
            The platform refused and gave a reason this storefront has no sentence for. It is
            printed here exactly as it arrived:{" "}
            <code className={MONO}>{explanation || "none"}</code>
          </>
        )}
      </p>
      {children}
    </div>
  );
}

/** A failure of the request itself, which is the one case that really is an error. */
function Transport({ detail, onRetry }: { detail: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="mt-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--red)] bg-red-50/40 px-3 py-3"
    >
      <p className="text-[13px] font-semibold text-[var(--ink)]">
        This request did not reach the platform
      </p>
      <p className="mt-1 max-w-[70ch] text-[13px] text-[var(--ink-3)]">
        {detail} Nothing was decided, so nothing has changed. Sending it again reuses the same
        request, so a copy that did arrive is replayed rather than run twice.
      </p>
      <Button variant="ghost" size="sm" className="mt-2" onClick={onRetry}>
        Send it again
      </Button>
    </div>
  );
}

/**
 * The confirm and the back-out, deliberately the same size as each other.
 *
 * The rule this enforces is the product's, not this file's: declining must be as easy as
 * accepting. A back-out rendered as a small grey word beside a large coloured button is a
 * choice presented as an afterthought, and on a cancel dialog that is a way of charging
 * people who meant to stop. Both are `size="md"`, both are reachable in one tab, and the
 * back-out is never the one that has to be hunted for.
 */
function ConfirmRow({
  confirmLabel,
  onConfirm,
  onClose,
  busy,
}: {
  confirmLabel: string;
  onConfirm: () => void;
  onClose: () => void;
  busy: boolean;
}) {
  return (
    <div className="mt-4 flex flex-wrap gap-2">
      <Button size="md" onClick={onConfirm} busy={busy}>
        {confirmLabel}
      </Button>
      <Button size="md" variant="ghost" onClick={onClose} disabled={busy}>
        Close
      </Button>
    </div>
  );
}

/* -------------------------------------------------------------------- refund */

type RefundOutcome =
  | { kind: "none" }
  | { kind: "admitted"; amountMinor: number; currency: string; state: string }
  | { kind: "refused"; decision: Decision }
  | { kind: "transport"; detail: string };

/**
 * The refund control. First, because it is the one a buyer of a delivered order needs.
 *
 * **The buyer does not name an amount, and there is no field in which to name one.** The
 * request goes with no amount at all and the kernel resolves it against its own capture
 * ledger. What the panel shows beforehand is that ledger read back from
 * `GET /v1/orders/{id}/refundable` -- the server's single figure, displayed, never re-sent.
 *
 * This used to be a choice: everything, or a number typed into a box. The box was removed
 * because of what stands beside it. The panel below tells the buyer that a disputed order
 * goes to a person who decides what is owed, and one screen cannot say that while also
 * inviting the buyer to enter the sum they believe they are owed. One of those two is a
 * lie about who decides, and it was the typed figure: the kernel refuses any amount above
 * what remains, so the field's only working power was to ask for *less* than the platform
 * would return -- a control whose best outcome is the one the buyer already gets by
 * pressing send, and whose worst is a buyer who under-claims their own money.
 *
 * There is deliberately no version in which this component works out the remaining balance
 * itself: it would have to subtract the refunds it can see from the amount captured, and
 * the refunds it can see are not all of them.
 */
function RefundPanel({
  order,
  onOrder,
}: {
  order: Order;
  onOrder: (order: Order) => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState(REFUND_REASONS[0].key);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<RefundOutcome>({ kind: "none" });
  /**
   * The kernel's figure, or the honest absence of it.
   *
   * `undefined` while the read is in flight or after it has failed, and the panel then says
   * nothing about the amount rather than guessing at one. A failed read must not become a
   * number on this screen -- and it must not disable the button either: the request carries
   * no amount, so a buyer whose read failed can still ask, and the kernel still answers.
   */
  const [ledger, setLedger] = useState<Refundable | undefined>(undefined);

  /**
   * The idempotency key for the request in hand, held across a retry and only across a
   * retry. A send that came back with a decision -- admitted or refused -- has been
   * decided, and composing a fresh request after it must not replay that decision, so the
   * key is dropped as soon as an answer arrives. A send that failed in transport may or
   * may not have landed, and that is exactly the case the key exists for: pressing again
   * replays the stored answer instead of admitting a second refund.
   */
  const key = useRef<string | null>(null);

  const send = useCallback(() => {
    if (key.current === null) key.current = newIdempotencyKey();
    setBusy(true);
    api
      // No amount, ever. The kernel resolves the figure from its own ledger at the moment
      // it decides, which is later than the read below and may differ from it.
      .requestRefund(order.order_id, { reason, amount_minor: null }, key.current)
      .then((result) => {
        key.current = null;
        setBusy(false);
        // The order comes back from inside the transaction that admitted the refund, so it
        // is handed straight up rather than re-fetched: a second read could only be older.
        onOrder(result.order);
        if (result.decision.allowed && result.refund) {
          setOpen(false);
          setOutcome({
            kind: "admitted",
            amountMinor: result.refund.amount_minor,
            currency: result.refund.currency,
            state: result.refund.state,
          });
          return;
        }
        setOutcome({ kind: "refused", decision: result.decision });
      })
      .catch((cause: unknown) => {
        setBusy(false);
        setOutcome({ kind: "transport", detail: humanMessage(cause) });
      });
  }, [onOrder, order.order_id, reason]);

  /**
   * Read the ledger when the panel opens, and again after an answer has changed it.
   *
   * `order.refunds` is in the dependency list rather than the order itself, so a re-render
   * that changed nothing about the money does not re-read, while an admitted refund does.
   *
   * The read is abandoned on unmount and before a re-run, so a slow first response cannot
   * land after a later one and overwrite it. A failure is swallowed into `undefined`: this
   * is a figure the screen would like to show, not a precondition for asking.
   */
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    let live = true;
    api
      .refundable(order.order_id, abort.signal)
      .then((read) => {
        if (live) setLedger(read);
      })
      .catch(() => {
        if (live) setLedger(undefined);
      });
    return () => {
      live = false;
      abort.abort();
    };
  }, [open, order.order_id, order.refunds]);

  return (
    <SectionCard
      title="Ask for a refund"
      subtitle="The platform decides this, and it answers either way."
    >
      <p className="max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
        A refund is a fresh decision by the kernel against its own record of what was
        captured and what has already gone back. It is not a message to the merchant, and
        this page does not work out in advance whether it will be allowed -- it asks, and
        shows you the answer.
      </p>

      {open ? (
        <div className="mt-4 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)] px-3 py-3">
          <fieldset>
            <legend className="text-[13px] font-bold text-[var(--ink)]">Why are you asking?</legend>
            <p className="mt-0.5 text-[12px] text-[var(--ink-4)]">
              Stored with the refund so an operator can see it. You do not have to justify
              yourself; the first option says nothing beyond the request itself.
            </p>
            <div className="mt-2 grid gap-1.5">
              {REFUND_REASONS.map((option) => (
                <label
                  key={option.key}
                  className="flex cursor-pointer items-center gap-2 text-[13px] text-[var(--ink-2)]"
                >
                  <input
                    type="radio"
                    name="refund-reason"
                    value={option.key}
                    checked={reason === option.key}
                    onChange={() => setReason(option.key)}
                    className="accent-[var(--green)]"
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </fieldset>

          <div className="mt-4">
            <h4 className="text-[13px] font-bold text-[var(--ink)]">How much comes back</h4>
            {ledger === undefined ? (
              <p className="mt-1.5 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
                The platform works out the amount from its own record of what was captured
                and what has already gone back. You do not enter a figure, and this screen
                does not calculate one.
              </p>
            ) : ledger.anything_remains ? (
              <>
                <p className="mt-1.5 flex items-baseline gap-1.5 text-[13px] text-[var(--ink-2)]">
                  <span>Still refundable:</span>
                  <Amount
                    minor={ledger.refundable_minor}
                    currency={ledger.currency}
                    className="text-[15px] font-semibold text-[var(--ink)]"
                  />
                </p>
                <p className="mt-1 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
                  The platform&rsquo;s figure, not this page&rsquo;s. It can differ from the
                  order total -- a refund already on its way back counts against it -- and it
                  is read again when the request is decided, so the amount actually returned
                  is the one the platform holds then.
                </p>
              </>
            ) : (
              <p className="mt-1.5 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
                The platform&rsquo;s record shows nothing left to refund on this order right
                now. You can still ask -- the button is live and the answer will say why --
                and if you believe money is owed, the third panel below reaches a person who
                can decide that.
              </p>
            )}
          </div>

          <ConfirmRow
            confirmLabel="Send this request"
            onConfirm={send}
            onClose={() => setOpen(false)}
            busy={busy}
          />
        </div>
      ) : (
        <Button className="mt-4" size="md" onClick={() => setOpen(true)}>
          Ask for a refund
        </Button>
      )}

      <RefundOutcomeView outcome={outcome} currency={order.currency} onRetry={send} />
    </SectionCard>
  );
}

/**
 * Reason keys on which this panel may not say the provider was left alone.
 *
 * A refusal licenses one claim -- that *this request* created nothing -- and the code
 * table only ever describes this request. On these four keys the kernel refused precisely
 * *because* something else is already moving, so the wider sentence "nothing was sent to
 * the provider" is contradicted by the very reason printed above it: a buyer told in one
 * line that a refund is already in flight and in the next that nothing reached Razorpay
 * has been given two incompatible facts, and the more comforting one is the false one.
 *
 * This is the refund path's form of the rule the checkout screen's refusal card spells out
 * for payments: a movement this request did not make is still a movement this screen must
 * not talk over. Wrong in the "no money moved" direction is not the safe direction -- it
 * is the one that sends a buyer away believing nothing is coming when a refund is live.
 */
const MOVEMENT_ELSEWHERE: ReadonlySet<string> = new Set([
  "refund_already_in_flight",
  "reconcile_unknown_first",
  "nothing_remaining",
  "grant_already_live",
]);

/**
 * What came back.
 *
 * The admitted case is the one to read closely. It says the platform *has asked* the
 * provider, and it does not say the money is back, because at `REFUND_PENDING` it is not:
 * the row exists, the grant is issued, the command is in the outbox, and Razorpay has not
 * answered. Writing "refunded" here would be a claim about somebody's bank balance made
 * several seconds before anybody could support it.
 */
function RefundOutcomeView({
  outcome,
  currency,
  onRetry,
}: {
  outcome: RefundOutcome;
  /**
   * The order's currency, carried down rather than assumed. A delta's figures are minor
   * units of whatever the payment was captured in, and a literal "INR" here would render
   * the right number under the wrong symbol the first time this platform sells in anything
   * else -- which is the sort of wrong that reads as correct.
   */
  currency: string;
  onRetry: () => void;
}) {
  if (outcome.kind === "none") return null;
  if (outcome.kind === "transport") return <Transport detail={outcome.detail} onRetry={onRetry} />;
  if (outcome.kind === "refused") {
    return (
      <Refusal
        heading="No refund was started"
        code={outcome.decision.code}
        explanation={outcome.decision.explanation}
      >
        <DeltaFigures decision={outcome.decision} currency={currency} />
        <p className="mt-2 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
          {MOVEMENT_ELSEWHERE.has(outcome.decision.explanation ?? "") ? (
            <>
              This request started nothing. Whether money is already moving on an earlier one
              is not something this panel can tell you; the refunds recorded against this
              order are listed below.
            </>
          ) : (
            <>This request started nothing: no refund was recorded and none was sent to the provider.</>
          )}
        </p>
      </Refusal>
    );
  }
  return (
    <div
      role="status"
      aria-live="polite"
      className="mt-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--green-add)] bg-[var(--green-add-bg)] px-3 py-3"
    >
      <Badge tone="green">Refund requested</Badge>
      <p className="mt-2 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
        The platform admitted a refund of{" "}
        <Amount
          minor={outcome.amountMinor}
          currency={outcome.currency}
          className="font-bold text-[var(--ink)]"
        />{" "}
        and has asked Razorpay to send it back.{" "}
        <strong className="font-semibold">The money has not moved yet.</strong> It is recorded
        below as{" "}
        <code className={cx(MONO, "rounded-[var(--r-sm)] bg-white px-1 py-0.5")}>
          {outcome.state}
        </code>
        , and that row is what will say when the provider has finished. Nothing is sent twice:
        the refund carries a single-use authority that can be spent once.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------- cancel */

type CancelOutcome =
  | { kind: "none" }
  | { kind: "cancelled"; fromState: string | null }
  | { kind: "refused"; code: string | null; explanation: string | null; fromState: string | null }
  | { kind: "unreadable" }
  | { kind: "transport"; detail: string };

/**
 * The cancel control, offered on an order it will almost always refuse.
 *
 * That is not a mistake and it is the clearest illustration of this file's rule. An order
 * exists because a capture landed, which means the checkout behind it reached `PAID`, and
 * `PAID` has no edge to `CANCELLED` -- so the kernel will answer `STALE_CHECKOUT` /
 * `version_terminal`. This component could work that out from `order.state` and hide the
 * button. It does not, for two reasons.
 *
 * The first is that it would be guessing. The checkout is read from the order row, the
 * kernel reads it under a lock, and between those two facts sits everything a worker or a
 * webhook may have done in the meantime.
 *
 * The second is that the refusal is worth reading. A buyer looking for the way out of a
 * purchase does not know the difference between a cancellation and a refund, and being
 * told "this sale has already completed, so the way back is a refund" -- by the platform,
 * in answer to a question they actually asked -- is a better outcome than finding no
 * control at all and concluding there is nothing they can do.
 */
function CancelPanel({ order, onChanged }: { order: Order; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<CancelOutcome>({ kind: "none" });
  const key = useRef<string | null>(null);

  const send = useCallback(() => {
    if (key.current === null) key.current = newIdempotencyKey();
    setBusy(true);
    api
      .cancel(order.checkout_id, "buyer_cancelled", key.current)
      .then((result) => {
        key.current = null;
        setBusy(false);
        setOpen(false);
        const fromState = result.from_state ?? null;
        if (result.allowed === true) {
          setOutcome({ kind: "cancelled", fromState });
          // A cancellation moved the checkout, the reservation and possibly the attempt.
          // None of that is in this response, so the order is re-read rather than patched.
          onChanged();
          return;
        }
        if (result.allowed === false) {
          setOutcome({
            kind: "refused",
            code: result.code ?? null,
            explanation: result.explanation ?? null,
            fromState,
          });
          return;
        }
        // `allowed` is the only field that says which way this went, and the schema makes
        // it optional because the sibling routes on this shape do not send it. An answer
        // without it is one this screen must not interpret in either direction.
        setOutcome({ kind: "unreadable" });
        onChanged();
      })
      .catch((cause: unknown) => {
        setBusy(false);
        setOutcome({ kind: "transport", detail: humanMessage(cause) });
      });
  }, [onChanged, order.checkout_id]);

  return (
    <SectionCard
      title="Cancel this order"
      subtitle="Asked of the platform, not decided here."
    >
      <p className="max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
        Cancelling stops a sale that has not completed. Once a payment has been made, the
        platform will not cancel on top of it -- an order cancelled out from under a buyer
        who has already been charged is the worse of the two mistakes -- and it will say so
        rather than pretend. If that is the answer here, the refund request above is the way
        back.
      </p>

      {open ? (
        <div className="mt-4 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)] px-3 py-3">
          <p className="max-w-[70ch] text-[13px] text-[var(--ink-2)]">
            This asks the platform to cancel checkout{" "}
            <code className={MONO}>{order.checkout_id}</code>. It moves no money by itself,
            and it does not withdraw a payment that has already been made.
          </p>
          <ConfirmRow
            confirmLabel="Send the cancellation"
            onConfirm={send}
            onClose={() => setOpen(false)}
            busy={busy}
          />
        </div>
      ) : (
        <Button className="mt-4" size="md" variant="ghost" onClick={() => setOpen(true)}>
          Cancel this order
        </Button>
      )}

      <CancelOutcomeView outcome={outcome} onRetry={send} />
    </SectionCard>
  );
}

function CancelOutcomeView({ outcome, onRetry }: { outcome: CancelOutcome; onRetry: () => void }) {
  if (outcome.kind === "none") return null;
  if (outcome.kind === "transport") return <Transport detail={outcome.detail} onRetry={onRetry} />;
  if (outcome.kind === "unreadable") {
    return (
      <div
        role="status"
        aria-live="polite"
        className="mt-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-[var(--tint-2)] px-3 py-3"
      >
        <p className="max-w-[70ch] text-[13px] text-[var(--ink-2)]">
          The platform answered without saying whether it cancelled. This screen will not
          guess in either direction; the order is being read again below, and that is what
          says where things stand.
        </p>
      </div>
    );
  }
  if (outcome.kind === "refused") {
    return (
      <Refusal
        heading="Not cancelled"
        code={outcome.code}
        explanation={outcome.explanation}
      >
        <p className="mt-2 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
          {outcome.fromState ? (
            <>
              The platform read this checkout as{" "}
              <code className={cx(MONO, "rounded-[var(--r-sm)] bg-white px-1 py-0.5")}>
                {outcome.fromState}
              </code>{" "}
              when it refused.{" "}
            </>
          ) : null}
          Nothing about this order changed.
        </p>
      </Refusal>
    );
  }
  return (
    <div
      role="status"
      aria-live="polite"
      className="mt-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--green-add)] bg-[var(--green-add-bg)] px-3 py-3"
    >
      <Badge tone="green">Cancelled</Badge>
      <p className="mt-2 max-w-[70ch] text-[13px] text-[var(--ink-2)]">
        The platform cancelled this checkout
        {outcome.fromState ? (
          <>
            {" "}
            from{" "}
            <code className={cx(MONO, "rounded-[var(--r-sm)] bg-white px-1 py-0.5")}>
              {outcome.fromState}
            </code>
          </>
        ) : null}{" "}
        and released the stock it was holding. Where that leaves any money already captured
        is read from the server below, not asserted here.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ escalate */

/**
 * Reaching a person. Third, and honest about what it is.
 *
 * There is no buyer-facing endpoint that opens a support case. The platform has a
 * human-review queue, but it is written to by reconciliation and read by operators, and
 * nothing a buyer can call puts a row in it. A button here that appeared to file a ticket
 * would therefore be a lie of exactly the kind the rest of this screen exists to avoid --
 * worse than the missing control, because a buyer who thinks they have raised a case stops
 * looking for another way to be heard.
 *
 * So this panel does the two truthful things available. It says when the platform has
 * *already* escalated something on this order by itself, which is real and is read off the
 * refund rows rather than assumed. And it hands over the references a person would ask
 * for, in one block that can be copied, so the buyer is not transcribing UUIDs by eye.
 */
function EscalatePanel({ order }: { order: Order }) {
  const escalated = order.refunds.filter((refund) => refund.state === "ESCALATED");
  const references = [
    `Order: ${order.order_id}`,
    `Checkout: ${order.checkout_id} (version ${order.version})`,
    `Payment attempt: ${order.payment.attempt_id}`,
    `Razorpay payment: ${order.payment.razorpay_payment_id ?? "none recorded"}`,
    `Amount: ${order.amount.display} ${order.currency}`,
  ].join("\n");

  return (
    <SectionCard title="Reach a person" subtitle="What this screen can and cannot do for you.">
      {escalated.length > 0 ? (
        <div className="mb-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--amber)] bg-amber-50/40 px-3 py-3">
          <Badge tone="amber">Already with an operator</Badge>
          <p className="mt-2 max-w-[70ch] text-[13px] text-[var(--ink-2)]">
            {escalated.length === 1 ? "A refund on this order has" : `${escalated.length} refunds on this order have`}{" "}
            been escalated by the platform itself: automatic reconciliation could not settle
            it and a human decides what happens next. Nothing is being retried in the
            meantime, and you do not need to ask for this to happen.
          </p>
        </div>
      ) : null}

      <RaiseCase order={order} />

      <p className="mt-4 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
        The references a person will ask for, exactly as the platform holds them.
      </p>

      <ReferenceBlock text={references} />

      <p className="mt-3 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
        The platform escalates on its own when it cannot settle something: a refund whose
        outcome it cannot verify goes to a human rather than being retried, because retrying
        a refund nobody can account for is how the same money gets sent back twice.
      </p>
    </SectionCard>
  );
}

/**
 * Put this order in front of a person.
 *
 * This is the control the paragraph above used to deny existed. It said, in prose, that
 * "there is no route in the platform that lets a buyer put one in the operators' queue,
 * and a button here that looked like it did would be worse than not having one". That was
 * true when it was written and stopped being true when the helpdesk was built: the route
 * is `POST /v1/orders/{id}/support-cases` and nothing had ever called it. A screen telling
 * a buyer that a capability does not exist, while the platform holds it, is worse than a
 * missing button -- the button is absent either way, and now the buyer has been told not
 * to look for one.
 *
 * Nothing here names an amount, and the request has no field for one. A person on the
 * merchant's side reads the case and settles what is owed; a figure typed in here would be
 * a number this screen would then be tempted to render as though somebody had agreed to it.
 *
 * The reasons are `REFUND_REASONS`, reused rather than restated. `SUPPORT_REASONS` on the
 * server is the same closed set -- its own comment says "exactly the keys the order screen
 * already renders" -- and a second list here would be a second place for that vocabulary
 * to drift from the one the merchant's queue filters on. The first attempt at this form
 * invented its own labels and the server refused every one of them, which is the check
 * working: a reason nobody recognises is a case nobody routes.
 */
function RaiseCase({ order }: { order: Order }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState(REFUND_REASONS[0].key);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [raised, setRaised] = useState<SupportCase[]>([]);
  const [loaded, setLoaded] = useState(false);

  // What this buyer has already raised on this order, so the screen never invites a second
  // case for a question somebody is already holding.
  useEffect(() => {
    const controller = new AbortController();
    api
      .supportCases(order.order_id, controller.signal)
      .then((cases) => {
        if (controller.signal.aborted) return;
        setRaised(cases);
        setLoaded(true);
      })
      .catch(() => {
        // A list that cannot be read is not a reason to withhold the control: the buyer
        // can still raise one, and the server refuses a duplicate better than a guess here.
        if (!controller.signal.aborted) setLoaded(true);
      });
    return () => controller.abort();
  }, [order.order_id]);

  const send = useCallback(async () => {
    setBusy(true);
    setFailed(null);
    try {
      const made = await api.raiseSupportCase(order.order_id, { reason, note: note.trim() });
      setRaised((held) => [...held, made]);
      setOpen(false);
      setNote("");
    } catch (cause) {
      setFailed(humanMessage(cause));
    } finally {
      setBusy(false);
    }
  }, [note, order.order_id, reason]);

  return (
    <div>
      {raised.length > 0 ? (
        <div className="mb-3 rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] px-3 py-3">
          <Badge tone="neutral">With a person</Badge>
          <ul className="mt-2 space-y-1">
            {raised.map((one) => (
              <li key={one.case_id} className="text-[13px] text-[var(--ink-2)]">
                {/* The buyer's own words for the key, not the key: `reason` comes back as
                    the platform's vocabulary and a screen that printed `item_damaged` at
                    somebody would be showing them the routing label. */}
                {REFUND_REASONS.find((r) => r.key === one.reason)?.label ?? one.reason} ·{" "}
                <span className="text-[var(--ink-4)]">{one.status}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
            Somebody on the merchant&rsquo;s side answers this. Nothing is decided here, and no
            amount was named: what is owed is theirs to settle.
          </p>
        </div>
      ) : null}

      {open ? (
        <div className="rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] p-3">
          <label className="block text-[12px] font-semibold text-[var(--ink-3)]" htmlFor="case-reason">
            What went wrong
          </label>
          <select
            id="case-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            className="mt-1 w-full rounded-[var(--r-sm)] border-[0.5px] border-[var(--card-line)] bg-white px-2 py-1.5 text-[13px] text-[var(--ink-2)]"
          >
            {REFUND_REASONS.map((one) => (
              <option key={one.key} value={one.key}>
                {one.label}
              </option>
            ))}
          </select>

          <label className="mt-3 block text-[12px] font-semibold text-[var(--ink-3)]" htmlFor="case-note">
            Anything else they should know <span className="font-normal text-[var(--ink-4)]">(optional)</span>
          </label>
          <textarea
            id="case-note"
            value={note}
            maxLength={1000}
            rows={3}
            onChange={(event) => setNote(event.target.value)}
            className="mt-1 w-full rounded-[var(--r-sm)] border-[0.5px] border-[var(--card-line)] px-2 py-1.5 text-[13px] text-[var(--ink-2)]"
          />

          {failed ? (
            <p className="mt-2 text-[12px] text-[var(--red)]">{failed}</p>
          ) : null}

          <div className="mt-3 flex gap-2">
            <Button onClick={() => void send()} disabled={busy}>
              {busy ? "Sending" : "Send to a person"}
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <Button variant="ghost" onClick={() => setOpen(true)} disabled={!loaded}>
          {raised.length > 0 ? "Raise another case" : "Ask a person about this order"}
        </Button>
      )}
    </div>
  );
}

/** The references, copyable, because the alternative is transcribing UUIDs by eye. */
function ReferenceBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied("done");
    } catch {
      setCopied("failed");
    }
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied("idle"), 2000);
  }, [text]);

  return (
    <div className="mt-3">
      <pre
        className={cx(
          MONO,
          "overflow-x-auto whitespace-pre rounded-[var(--r-sm)] bg-[var(--tint-2)] px-3 py-2",
        )}
      >
        {text}
      </pre>
      <Button variant="ghost" size="sm" className="mt-2" onClick={() => void copy()}>
        {copied === "done" ? "Copied" : copied === "failed" ? "Copy failed" : "Copy these references"}
      </Button>
      <span aria-live="polite" className="sr-only">
        {copied === "done"
          ? "References copied"
          : copied === "failed"
            ? "References could not be copied"
            : ""}
      </span>
    </div>
  );
}

/* --------------------------------------------------------------------- panel */

/**
 * The three controls, in the order a buyer reaches for them.
 *
 * `onOrder` takes a fresh order the server has already sent -- the refund route returns
 * one -- and `onChanged` asks the screen to read the order again where no fresh copy came
 * back. Keeping those separate is what stops this panel from ever re-fetching over an
 * answer it was given: a re-read after an admitted refund could only return the same row
 * or an older one, and would flicker the screen for nothing.
 */
export function OrderActions({
  order,
  onOrder,
  onChanged,
}: {
  order: Order;
  onOrder: (order: Order) => void;
  onChanged: () => void;
}) {
  return (
    <>
      <RefundPanel order={order} onOrder={onOrder} />
      <CancelPanel order={order} onChanged={onChanged} />
      <EscalatePanel order={order} />
    </>
  );
}

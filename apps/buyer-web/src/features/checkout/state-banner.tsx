/**
 * Every checkout state, said honestly in one line.
 *
 * The temptation in a payments UI is to collapse the middle of the lifecycle into a
 * spinner and the end of it into a tick. That is a lie of omission: EXECUTION_PENDING,
 * AWAITING_PAYMENT, PAYMENT_UNKNOWN and INVALIDATED_AWAITING_PAYMENT_RESULT are four
 * genuinely different facts about the buyer's money, and a screen that draws them
 * identically has told the buyer nothing. Each sentence below says what is true, what is
 * not yet known, and whether anything has been charged.
 *
 * The vocabulary belongs to the kernel. `MEANINGS` is a total record over
 * `CHECKOUT_STATES`, which is the fourteen members of `CheckoutState`; anything else
 * renders as itself rather than crashing the page, because a storefront that white-screens
 * on an unrecognised string is worse than one that admits it does not recognise it.
 *
 * Specification 8.2 asks the interface to convey sixteen journey stages, and this file
 * used to answer that by inventing six checkout states to match the count -- SUBMITTED,
 * PAYMENT_PENDING, RECONCILING, STALE_CAPTURE, REJECTED and FAILED, none of which a
 * checkout is ever in. The stages are real; they simply do not all live on this machine.
 * Reconciliation is conveyed by PAYMENT_UNKNOWN, whose sentence says what reconciliation
 * is doing about it. A stale capture and its automatic refund are conveyed by
 * INVALIDATED_AWAITING_PAYMENT_RESULT and INVALIDATED here, and by the payment attempt's
 * own STALE_CAPTURE and the refund states on the order screen, which is where the platform
 * actually records them. A stage is conveyed by saying the true thing, not by minting a
 * state to hang it on.
 */
"use client";

import { CHECKOUT_STATES } from "@/lib/api/types";
import { cx } from "@/components/ui";

type Tone = "neutral" | "waiting" | "action" | "good" | "warn" | "bad";

export interface StateMeaning {
  /** The state as a person would say it, not as the database spells it. */
  title: string;
  /** One line: what is true right now, and whether money has moved. */
  sentence: string;
  tone: Tone;
}

type KnownState = (typeof CHECKOUT_STATES)[number];

/**
 * The fourteen. Typed as a total record over `CHECKOUT_STATES`, so adding a state to the
 * shared vocabulary without writing its sentence here is a compile error rather than a
 * blank banner in front of a buyer.
 */
const MEANINGS: Record<KnownState, StateMeaning> = {
  DRAFT: {
    title: "Not priced yet",
    sentence:
      "The basket exists but no priced version has been built from it. There is nothing to approve and nothing to pay.",
    tone: "neutral",
  },
  QUOTED: {
    title: "Priced, not held",
    sentence:
      "A quote has been computed for this checkout. No stock is held for you yet and no approval has been asked for.",
    tone: "neutral",
  },
  RESERVED: {
    title: "Stock held",
    sentence:
      "The items in this order are held for you for a limited time. The hold expires on its own, and when it does the version has to be rebuilt at whatever the price is then.",
    tone: "waiting",
  },
  APPROVAL_REQUIRED: {
    title: "Waiting for you",
    sentence:
      "The merchant has priced this exact version and is waiting for your consent. Nothing is charged unless you approve it and then pay.",
    tone: "action",
  },
  APPROVED: {
    title: "Approved, not submitted",
    sentence:
      "You approved this exact version. It has not been handed to the transaction kernel yet, no payment order exists, and no money has moved.",
    tone: "action",
  },
  EXECUTION_PENDING: {
    title: "Admitted, order being created",
    sentence:
      "The kernel admitted your approval and issued a single-use execution grant. A worker is spending that grant to create the payment order at Razorpay. No payment page exists yet, nothing has been charged, and the grant cannot be spent a second time.",
    tone: "waiting",
  },
  AWAITING_PAYMENT: {
    title: "With Razorpay",
    sentence:
      "A payment order exists at Razorpay for this version. That is all the platform knows: an order is not a payment, and this state is reached when the order is created, which may be before you have opened the payment screen at all. Whether any money has moved is not something this page can tell you \u2014 not from your coming back to it, and not from the payment screen saying it succeeded. Nothing is confirmed until Razorpay's own signed evidence reaches the platform.",
    tone: "waiting",
  },
  PAID: {
    title: "Paid and recorded",
    sentence:
      "Razorpay's own signed evidence confirmed the capture and the order was written against the version you approved. This state is reached from a webhook or a direct fetch from the provider, never from your browser saying so.",
    tone: "good",
  },
  PAYMENT_FAILED: {
    title: "Payment failed",
    sentence:
      "Razorpay confirmed this payment did not go through. Nothing was captured and the hold on your stock has been released. A fresh attempt is possible, and it starts from a current version with a new approval and a new grant rather than reusing this one.",
    tone: "bad",
  },
  PAYMENT_UNKNOWN: {
    title: "Outcome genuinely unknown",
    sentence:
      "The platform asked Razorpay what happened and did not get a definitive answer. This is neither a success nor a failure: it stays unknown, and your stock stays held, until reconciliation resolves it against Razorpay's own record, because writing an order on a guess is how a buyer gets charged for something nobody recorded.",
    tone: "warn",
  },
  INVALIDATED: {
    title: "Superseded",
    sentence:
      "This version was retired because what the merchant is selling changed, so it can no longer be paid for or fulfilled and the approval you gave for it cannot be spent. Anything you still want is a new version, priced now and approved from scratch.",
    tone: "warn",
  },
  INVALIDATED_AWAITING_PAYMENT_RESULT: {
    title: "Superseded while a payment may be in flight",
    sentence:
      "This version was retired while a payment for it may still have been moving, so nothing will be fulfilled against it whatever that payment turns out to have done. The platform is waiting for Razorpay's own answer. If no money left your account there is nothing to return. If money did, that capture is recorded as one taken against a dead version and the platform refunds it in full without anyone asking. And if Razorpay has already sent some or all of it back itself, no second refund is made and a person checks you have been made whole \u2014 because a difference the platform cannot verify is not one it will guess at. Until Razorpay answers, this screen will not tell you which of those happened, because it does not know.",
    tone: "warn",
  },
  CANCELLED: {
    title: "Cancelled",
    sentence:
      "This checkout was ended before any payment, either because you cancelled it or because you declined the version. Any hold on stock has been released and nothing was charged.",
    tone: "neutral",
  },
  EXPIRED: {
    title: "Expired",
    sentence:
      "This checkout ran out of time before it was paid. The version is closed, the hold is released and nothing was charged.",
    tone: "neutral",
  },
};

/**
 * What a state means, or an honest admission that this build does not know.
 *
 * The lookup is `Object.hasOwn` rather than plain indexing because plain indexing walks
 * the prototype chain: a state string of `constructor` resolved to `Object`, `toString` to
 * a function, and each of those is truthy, so the fallback below never ran and the caller
 * was handed a "meaning" whose `title`, `sentence` and `tone` were all undefined. The
 * component then read `TONE_STYLES[undefined].bar` and threw, white-screening the page —
 * the exact failure this function's own contract, three paragraphs up, promises cannot
 * happen. The server owns this vocabulary and `state` is typed as a bare string, so the
 * defence belongs here rather than in a hope about what the server will send. It matters
 * more now than when it was written, not less: this map holds fourteen states where it
 * once held twenty, so more strings than before reach the fallback rather than a hit.
 */
export function stateMeaning(state: string): StateMeaning {
  // `Object.hasOwn`, not plain indexing, for the reason set out above. The one input this
  // function exists to survive is a string the storefront has never seen, and that is
  // exactly the input that reaches it.
  if (Object.hasOwn(MEANINGS, state)) {
    return (MEANINGS as Record<string, StateMeaning>)[state];
  }
  return {
    title: state,
    sentence:
      "This storefront does not have a description for this state. It is shown exactly as the server sent it rather than guessed at.",
    tone: "neutral",
  };
}

/**
 * States after which nothing further happens on its own, so polling stops here.
 *
 * These are the kernel's four terminal checkout states, plus PAYMENT_FAILED. The kernel
 * does not call PAYMENT_FAILED terminal, and it is right not to -- a policy-safe retry
 * re-enters admission from it -- but that retry only ever happens because the buyer asks
 * for it. Polling for a transition nobody is going to make is a spinner pretending to be
 * a fact.
 *
 * INVALIDATED_AWAITING_PAYMENT_RESULT is deliberately not here. It resolves to INVALIDATED
 * on the provider's answer, with nobody pressing anything, and it is the one state where
 * giving up on the read would leave a buyer looking at a screen that has stopped being
 * true.
 */
const TERMINAL = new Set<string>([
  "PAID",
  "INVALIDATED",
  "CANCELLED",
  "EXPIRED",
  "PAYMENT_FAILED",
]);

export function isTerminalState(state: string): boolean {
  return TERMINAL.has(state);
}

const TONE_STYLES: Record<Tone, { bar: string; dot: string; title: string }> = {
  neutral: { bar: "bg-[var(--tint-2)] border-[var(--card-line)]", dot: "bg-[var(--ink-5)]", title: "text-[var(--ink)]" },
  waiting: { bar: "bg-blue-50/70 border-blue-100", dot: "bg-[var(--blue)]", title: "text-[var(--blue)]" },
  action: { bar: "bg-[var(--green-add-bg)] border-[var(--green-add)]/30", dot: "bg-[var(--green)]", title: "text-[var(--green)]" },
  good: { bar: "bg-[var(--green-add-bg)] border-[var(--green-add)]/40", dot: "bg-[var(--green)]", title: "text-[var(--green)]" },
  warn: { bar: "bg-amber-50 border-amber-200", dot: "bg-[var(--amber)]", title: "text-[#8a5a00]" },
  bad: { bar: "bg-red-50 border-red-200", dot: "bg-[var(--red)]", title: "text-[var(--red)]" },
};

/**
 * The banner itself.
 *
 * `aria-live="polite"` because the state changes underneath a buyer who is watching a
 * payment settle, and a change they cannot see is a change they cannot act on. The
 * shape of the dot is not the signal; the words are.
 */
export function StateBanner({
  state,
  detail,
  className,
}: {
  state: string;
  detail?: string;
  className?: string;
}) {
  const meaning = stateMeaning(state);
  const styles = TONE_STYLES[meaning.tone];

  return (
    <div
      role="status"
      aria-live="polite"
      className={cx("rounded-[var(--r-md)] border px-4 py-3", styles.bar, className)}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className={cx("h-2 w-2 shrink-0 rounded-full", styles.dot)} aria-hidden="true" />
        <span className={cx("text-[14px] font-bold", styles.title)}>{meaning.title}</span>
        <code className="tnum rounded-[var(--r-sm)] bg-white/70 px-1.5 py-0.5 font-mono text-[11px] text-[var(--ink-4)]">
          {state}
        </code>
      </div>
      <p className="mt-1.5 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
        {meaning.sentence}
      </p>
      {detail ? <p className="mt-1 text-[12px] text-[var(--ink-4)]">{detail}</p> : null}
    </div>
  );
}

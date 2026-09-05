/**
 * Every checkout state, said honestly in one line.
 *
 * The temptation in a payments UI is to collapse the middle of the lifecycle into a
 * spinner and the end of it into a tick. That is a lie of omission: EXECUTION_PENDING,
 * PAYMENT_UNKNOWN, RECONCILING and STALE_CAPTURE are four genuinely different facts
 * about the buyer's money, and a screen that draws them identically has told the buyer
 * nothing. Each sentence below says what is true, what is not yet known, and whether
 * anything has been charged.
 *
 * The vocabulary belongs to the server. `CHECKOUT_STATES` is the sixteen the
 * specification names, and the four beneath it are states the deployed kernel also
 * writes; anything else renders as itself rather than crashing the page, because a
 * storefront that white-screens on an unrecognised string is worse than one that admits
 * it does not recognise it.
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
 * The sixteen. Typed as a total record over `CHECKOUT_STATES`, so adding a state to the
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
  SUBMITTED: {
    title: "With the kernel",
    sentence:
      "Your approval has been handed to the transaction kernel, which is deciding whether it still matches what the merchant is selling. It will either admit it once or refuse it with the exact differences.",
    tone: "waiting",
  },
  EXECUTION_PENDING: {
    title: "Admitted, order being created",
    sentence:
      "The kernel admitted your approval and issued a single-use execution grant. A worker is spending that grant to create the payment order at Razorpay. No payment page exists yet, nothing has been charged, and the grant cannot be spent a second time.",
    tone: "waiting",
  },
  PAYMENT_PENDING: {
    title: "Payment surface open",
    sentence:
      "A provider order exists and the payment surface is open. Whether money has moved is not yet known to this platform, and will not be until the provider says so.",
    tone: "waiting",
  },
  PAYMENT_UNKNOWN: {
    title: "Outcome genuinely unknown",
    sentence:
      "The platform asked Razorpay what happened and did not get a definitive answer. This is neither a success nor a failure: it stays unknown until reconciliation resolves it, because writing an order on a guess is how a buyer gets charged for something nobody recorded.",
    tone: "warn",
  },
  RECONCILING: {
    title: "Re-asking the provider",
    sentence:
      "The platform is asking Razorpay again, on a schedule, what really happened to this payment, until the provider's own record answers. Reconciliation resolves an unknown outcome from evidence; it never invents one.",
    tone: "warn",
  },
  PAID: {
    title: "Paid and recorded",
    sentence:
      "Razorpay's own signed evidence confirmed the capture and the order was written against the version you approved. This state is reached from a webhook or a direct fetch from the provider, never from your browser saying so.",
    tone: "good",
  },
  STALE_CAPTURE: {
    title: "Captured against a dead version",
    sentence:
      "Money was captured for a version that had already been invalidated, so the capture is real but the order is not. No order is written for it. The platform admits an automatic refund rather than keeping a payment for something you never approved.",
    tone: "bad",
  },
  CANCELLED: {
    title: "Cancelled",
    sentence:
      "This checkout was cancelled before any payment. Any hold on stock has been released and nothing was charged.",
    tone: "neutral",
  },
  EXPIRED: {
    title: "Expired",
    sentence:
      "This checkout ran out of time before it was paid. The version is closed, the hold is released and nothing was charged.",
    tone: "neutral",
  },
  REJECTED: {
    title: "Declined by you",
    sentence:
      "You declined this version. Nothing was charged and the stock went back on the shelf for other buyers.",
    tone: "neutral",
  },
  FAILED: {
    title: "Payment failed",
    sentence:
      "The payment attempt failed at the provider. Nothing was captured, and you can start a fresh attempt from a current version.",
    tone: "bad",
  },
};

/**
 * States the deployed kernel writes that the shared vocabulary does not list.
 *
 * Kept separate from `MEANINGS` rather than merged into it, because `MEANINGS` is a
 * contract with `CHECKOUT_STATES` and this is a courtesy to a running backend.
 */
const ALSO_SEEN: Record<string, StateMeaning> = {
  AWAITING_PAYMENT: {
    title: "Waiting at the provider",
    sentence:
      "The provider order exists and the payment surface is open. The platform is waiting for Razorpay's own evidence, not for your browser.",
    tone: "waiting",
  },
  PAYMENT_FAILED: {
    title: "Declined at the provider",
    sentence:
      "Razorpay declined or abandoned this payment. Nothing was captured, and a new attempt has to start from a current version.",
    tone: "bad",
  },
  INVALIDATED: {
    title: "Superseded",
    sentence:
      "This version was retired because what the merchant is selling changed. A new version has been issued for you to look at, and the old approval cannot be spent.",
    tone: "warn",
  },
  INVALIDATED_AWAITING_PAYMENT_RESULT: {
    title: "Superseded with a payment in flight",
    sentence:
      "This version was retired while a payment for it was still in flight. No order will be written for it, and anything the provider ends up capturing is refunded automatically.",
    tone: "warn",
  },
};

/**
 * What a state means, or an honest admission that this build does not know.
 *
 * The lookups are `Object.hasOwn` rather than plain indexing because plain indexing walks
 * the prototype chain: a state string of `constructor` resolved to `Object`, `toString` to
 * a function, and each of those is truthy, so the fallback below never ran and the caller
 * was handed a "meaning" whose `title`, `sentence` and `tone` were all undefined. The
 * component then read `TONE_STYLES[undefined].bar` and threw, white-screening the page —
 * the exact failure this function's own contract, three paragraphs up, promises cannot
 * happen. The server owns this vocabulary and `state` is typed as a bare string, so the
 * defence belongs here rather than in a hope about what the server will send.
 */
export function stateMeaning(state: string): StateMeaning {
  const known = Object.hasOwn(MEANINGS, state)
    ? (MEANINGS as Record<string, StateMeaning>)[state]
    : Object.hasOwn(ALSO_SEEN, state)
      ? ALSO_SEEN[state]
      : undefined;
  if (known) return known;
  return {
    title: state,
    sentence:
      "This storefront does not have a description for this state. It is shown exactly as the server sent it rather than guessed at.",
    tone: "neutral",
  };
}

/** States after which nothing further happens on its own. Polling stops here. */
const TERMINAL = new Set<string>([
  "PAID",
  "FAILED",
  "PAYMENT_FAILED",
  "CANCELLED",
  "EXPIRED",
  "REJECTED",
  "STALE_CAPTURE",
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

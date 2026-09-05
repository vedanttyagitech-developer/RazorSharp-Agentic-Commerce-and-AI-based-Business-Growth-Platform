/**
 * Sixteen states, sixteen different facts about somebody's money.
 *
 * This banner is the only thing on the storefront that tells a buyer where their payment
 * actually is. Every other component can be wrong and be ugly; this one can be wrong and
 * be a lie — and the two directions of that lie are not symmetric. Telling a buyer
 * "nothing was charged" while `PAYMENT_UNKNOWN` is on the row means the platform does not
 * know that and said it anyway. Telling them "we are still working on it" on `PAID` is
 * merely annoying. So the tests below are organised by what the banner is *licensed to
 * claim* on each state rather than by what it happens to draw.
 *
 * Four of the sixteen only exist against a live backend. A mock that answers every submit
 * with a payment page never produces `EXECUTION_PENDING` (a grant issued, a worker
 * spending it, no payment page yet), never produces `PAYMENT_UNKNOWN` (the provider was
 * asked and did not answer), never produces `RECONCILING` (it is being asked again on a
 * schedule) and never produces `STALE_CAPTURE` (money moved for a version that had
 * already died). Those four are where a UI written against a mock collapses everything
 * into one spinner, and a spinner over `STALE_CAPTURE` is a captured payment the buyer is
 * never told about.
 *
 * Three properties run over the whole vocabulary rather than over a hand-listed sample,
 * because a hand-listed sample is exactly what a seventeenth state slips past:
 *
 *  1. Distinctness. No two of the sixteen share a title and no two share a sentence. A
 *     state that renders as another state is the defect this catches, and it catches it
 *     without anyone having to think of the pair in advance.
 *  2. Licensing. Every flat claim about money — "nothing was charged", "the capture is
 *     real" — is searched for across all sixteen sentences, and a state that makes one
 *     without being licensed for it fails. That is the assertion that a sentence swapped
 *     between two states cannot survive.
 *  3. Accessible text. For every state the `textContent` alone carries the title, the
 *     state code and the sentence, because a buyer who cannot see the amber dot has only
 *     the words, and two of the amber states mean opposite things about reconciliation.
 *
 * The expected titles and fragments are written out here rather than read back from the
 * component. A test that asks the component what it says and then checks that it said it
 * passes on any string.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { CHECKOUT_STATES } from "@/lib/api/types";

import { StateBanner, isTerminalState, stateMeaning } from "./state-banner";

afterEach(cleanup);

/* ------------------------------------------------------------- the vocabulary */

/**
 * The four the deployed kernel writes that the shared vocabulary does not list. The
 * component keeps them in a separate table; this file keeps them in a separate array for
 * the same reason — they are a courtesy to a running backend, not part of the contract.
 */
const ALSO_SEEN = [
  "AWAITING_PAYMENT",
  "PAYMENT_FAILED",
  "INVALIDATED",
  "INVALIDATED_AWAITING_PAYMENT_RESULT",
] as const;

/** A plausible state string that is in neither vocabulary. Nothing has ever written it. */
const UNRECOGNISED = "SETTLEMENT_HELD_BY_ACQUIRER";

const EVERY_KNOWN_STATE: readonly string[] = [...CHECKOUT_STATES, ...ALSO_SEEN];

/** The tones the component declares. A colour outside this set has no styles at all. */
const TONES = ["neutral", "waiting", "action", "good", "warn", "bad"];

/**
 * What each of the sixteen must say, written down independently of the component.
 *
 * `says` is a fragment that belongs to this state's sentence and to no other's, so the
 * cross-check below can assert that a state's screen contains its own sentence and none
 * of the other fifteen.
 */
const EXPECTED: Record<(typeof CHECKOUT_STATES)[number], { title: string; says: string }> = {
  DRAFT: {
    title: "Not priced yet",
    says: "no priced version has been built from it",
  },
  QUOTED: {
    title: "Priced, not held",
    says: "No stock is held for you yet",
  },
  RESERVED: {
    title: "Stock held",
    says: "held for you for a limited time",
  },
  APPROVAL_REQUIRED: {
    title: "Waiting for you",
    says: "is waiting for your consent",
  },
  APPROVED: {
    title: "Approved, not submitted",
    says: "no payment order exists, and no money has moved",
  },
  SUBMITTED: {
    title: "With the kernel",
    says: "deciding whether it still matches what the merchant is selling",
  },
  EXECUTION_PENDING: {
    title: "Admitted, order being created",
    says: "issued a single-use execution grant",
  },
  PAYMENT_PENDING: {
    title: "Payment surface open",
    says: "A provider order exists and the payment surface is open",
  },
  PAYMENT_UNKNOWN: {
    title: "Outcome genuinely unknown",
    says: "did not get a definitive answer",
  },
  RECONCILING: {
    title: "Re-asking the provider",
    says: "asking Razorpay again, on a schedule",
  },
  PAID: {
    title: "Paid and recorded",
    says: "Razorpay's own signed evidence confirmed the capture",
  },
  STALE_CAPTURE: {
    title: "Captured against a dead version",
    says: "Money was captured for a version that had already been invalidated",
  },
  CANCELLED: {
    title: "Cancelled",
    says: "cancelled before any payment",
  },
  EXPIRED: {
    title: "Expired",
    says: "ran out of time before it was paid",
  },
  REJECTED: {
    title: "Declined by you",
    says: "You declined this version",
  },
  FAILED: {
    title: "Payment failed",
    says: "The payment attempt failed at the provider",
  },
};

/**
 * One banner mounted at a time, so `getByRole("status")` always names the state under
 * test rather than the one from the previous turn of a loop.
 */
function banner(state: string, detail?: string): HTMLElement {
  cleanup();
  render(<StateBanner state={state} detail={detail} />);
  return screen.getByRole("status");
}

/** Everything a screen reader would read out for this state, in order. */
function spoken(state: string, detail?: string): string {
  return banner(state, detail).textContent ?? "";
}

/* -------------------------------------------- all sixteen, said in their own words */

describe("each of the sixteen renders its own title, sentence and state code", () => {
  it("draws the title the state was written for", () => {
    for (const state of CHECKOUT_STATES) {
      expect(spoken(state)).toContain(EXPECTED[state].title);
    }
  });

  it("draws the sentence the state was written for, and no other state's", () => {
    for (const state of CHECKOUT_STATES) {
      const text = spoken(state);
      expect(text).toContain(EXPECTED[state].says);
      // The whole point. A sentence copied from a neighbouring state — the classic result
      // of adding EXECUTION_PENDING by duplicating the PAYMENT_PENDING block — fails here.
      for (const other of CHECKOUT_STATES) {
        if (other === state) continue;
        expect(text).not.toContain(EXPECTED[other].says);
      }
    }
  });

  it("prints the raw state string verbatim, in the database's own spelling", () => {
    for (const state of CHECKOUT_STATES) {
      banner(state);
      // Not "Paid", not "paid": the string an engineer can take from a screenshot straight
      // into a `where state = ?` without translating it first.
      expect(screen.getByText(state, { selector: "code" })).toBeDefined();
    }
  });

  it("renders every one of them without needing anything but the state string", () => {
    // No detail, no className, no surrounding data. A banner that only reads correctly
    // when its caller supplies context is a banner that reads wrongly somewhere.
    for (const state of CHECKOUT_STATES) {
      const meaning = stateMeaning(state);
      expect(spoken(state)).toBe(`${meaning.title}${state}${meaning.sentence}`);
    }
  });
});

/* --------------------------------------------------------------- distinctness */

describe("no state renders as another state", () => {
  it("gives each of the sixteen a title no other of the sixteen uses", () => {
    const titles = CHECKOUT_STATES.map((state) => stateMeaning(state).title);
    expect(new Set(titles).size).toBe(CHECKOUT_STATES.length);
  });

  it("gives each of the sixteen a sentence no other of the sixteen uses", () => {
    const sentences = CHECKOUT_STATES.map((state) => stateMeaning(state).sentence);
    expect(new Set(sentences).size).toBe(CHECKOUT_STATES.length);
  });

  it("keeps the four the kernel also writes distinct from the sixteen and from each other", () => {
    const titles = EVERY_KNOWN_STATE.map((state) => stateMeaning(state).title);
    const sentences = EVERY_KNOWN_STATE.map((state) => stateMeaning(state).sentence);
    expect(new Set(titles).size).toBe(EVERY_KNOWN_STATE.length);
    expect(new Set(sentences).size).toBe(EVERY_KNOWN_STATE.length);
  });

  it("puts twenty different screens in front of the buyer", () => {
    // Asserted on the rendered text rather than on the table, because the table being
    // distinct is worth nothing if the component draws two of its rows the same way.
    const screens = new Set(EVERY_KNOWN_STATE.map((state) => spoken(state)));
    expect(screens.size).toBe(EVERY_KNOWN_STATE.length);
  });
});

/* --------------------------------------------- what it is licensed to say about money */

/** Nothing was charged on any of these, and the platform is entitled to say so flatly. */
const NOTHING_CHARGED = [
  "DRAFT",
  "QUOTED",
  "RESERVED",
  "APPROVAL_REQUIRED",
  "APPROVED",
  "SUBMITTED",
  "EXECUTION_PENDING",
  "CANCELLED",
  "EXPIRED",
  "REJECTED",
  "FAILED",
];

/** A provider order exists and the outcome is not the platform's to state, either way. */
const OUTCOME_UNKNOWN = ["PAYMENT_PENDING", "PAYMENT_UNKNOWN", "RECONCILING"];

/** Money demonstrably moved, on the provider's own evidence. */
const MONEY_MOVED = ["PAID", "STALE_CAPTURE"];

/** Sentences that assert, flatly, that the buyer's money did not move. */
const REASSURANCES = [
  /nothing (?:is|was|has been) charged/i,
  /no money has moved/i,
  /nothing was captured/i,
  /nothing to pay/i,
];

/** Sentences that assert, flatly, that it did. */
const CAPTURE_CLAIMS = [/confirmed the capture/i, /money was captured/i, /the capture is real/i];

/** Verdicts nobody is entitled to while the provider has not answered. */
const FLAT_VERDICTS = [
  /you (?:were|have been) charged/i,
  /your payment (?:went through|succeeded)/i,
  /the payment succeeded/i,
  /\bthe payment failed\b/i,
];

describe("what the banner is licensed to claim about the buyer's money", () => {
  it("classifies all sixteen, so a state added without thought fails here first", () => {
    const classified = [...NOTHING_CHARGED, ...OUTCOME_UNKNOWN, ...MONEY_MOVED];
    expect(classified.slice().sort()).toEqual([...CHECKOUT_STATES].sort());
  });

  it("says 'nothing was charged' only on states where nothing was charged", () => {
    // Searched across all sixteen rather than checked on a chosen few: this is the
    // assertion that fails if PAYMENT_UNKNOWN ever inherits a reassurance from the state
    // above it in the table.
    for (const state of CHECKOUT_STATES) {
      const sentence = stateMeaning(state).sentence;
      for (const reassurance of REASSURANCES) {
        if (reassurance.test(sentence)) {
          expect(NOTHING_CHARGED).toContain(state);
        }
      }
    }
  });

  it("claims a capture only on the two states where one happened", () => {
    for (const state of CHECKOUT_STATES) {
      const sentence = stateMeaning(state).sentence;
      for (const claim of CAPTURE_CLAIMS) {
        if (claim.test(sentence)) {
          expect(MONEY_MOVED).toContain(state);
        }
      }
    }
  });

  it("actually says it on the states that are meant to reassure", () => {
    // The licensing property above is one-directional — it would be satisfied by sixteen
    // sentences that say nothing at all. These eight have to say it.
    for (const state of [
      "DRAFT",
      "APPROVAL_REQUIRED",
      "APPROVED",
      "EXECUTION_PENDING",
      "CANCELLED",
      "EXPIRED",
      "REJECTED",
      "FAILED",
    ]) {
      const sentence = stateMeaning(state).sentence;
      expect(REASSURANCES.some((pattern) => pattern.test(sentence))).toBe(true);
    }
  });

  it("makes no claim in either direction while the outcome is genuinely unknown", () => {
    // PAYMENT_UNKNOWN saying "nothing was charged" would be the worst bug this component
    // could carry: the platform asked Razorpay, got no definitive answer, and would be
    // telling the buyer an outcome it does not have. The opposite claim is as bad — a
    // buyer told they paid stops watching a payment that may still fail.
    for (const state of OUTCOME_UNKNOWN) {
      const sentence = stateMeaning(state).sentence;
      for (const pattern of [...REASSURANCES, ...CAPTURE_CLAIMS, ...FLAT_VERDICTS]) {
        expect(pattern.test(sentence)).toBe(false);
      }
    }
  });

  it("says out loud on PAYMENT_UNKNOWN that it is neither outcome", () => {
    const text = spoken("PAYMENT_UNKNOWN");
    expect(text).toContain("Outcome genuinely unknown");
    expect(text).toContain("did not get a definitive answer");
    expect(text).toContain("This is neither a success nor a failure");
    expect(text).toContain("it stays unknown until reconciliation resolves it");
  });

  it("describes RECONCILING as asking again, not as an answer", () => {
    const text = spoken("RECONCILING");
    expect(text).toContain("asking Razorpay again, on a schedule");
    expect(text).toContain("until the provider's own record answers");
    // Reconciliation reads evidence. A reconciler that invented an outcome would be
    // indistinguishable, to the buyer, from one that found it.
    expect(text).toContain("it never invents one");
  });

  it("attributes PAID to the provider's evidence rather than to the browser", () => {
    const text = spoken("PAID");
    expect(text).toContain("Razorpay's own signed evidence confirmed the capture");
    expect(text).toContain("the order was written against the version you approved");
    // A storefront that marks itself paid because the checkout widget closed is a
    // storefront that can be told it was paid by anyone who can close the widget.
    expect(text).toContain("from a webhook or a direct fetch from the provider");
    expect(text).toContain("never from your browser saying so");
  });

  it("says on STALE_CAPTURE that money moved and that no order exists", () => {
    // Both halves, or the state is unreadable. Money moved without the order is the whole
    // meaning; either half alone reads as an ordinary success or an ordinary failure.
    const text = spoken("STALE_CAPTURE");
    expect(text).toContain("Money was captured for a version that had already been invalidated");
    expect(text).toContain("the capture is real but the order is not");
    expect(text).toContain("No order is written for it");
    expect(text).toContain("admits an automatic refund");
    expect(REASSURANCES.some((pattern) => pattern.test(text))).toBe(false);
  });

  it("does not let PAID and STALE_CAPTURE read alike, though both captured money", () => {
    const paid = spoken("PAID");
    const stale = spoken("STALE_CAPTURE");
    expect(paid).toContain("Paid and recorded");
    expect(stale).toContain("Captured against a dead version");
    // An order was written on one and never will be on the other. Nothing may carry over.
    expect(stale).not.toContain("the order was written against the version you approved");
    expect(paid).not.toContain("No order is written for it");
  });
});

/* -------------------------------------------------- EXECUTION_PENDING in particular */

describe("EXECUTION_PENDING, which no mock ever produced", () => {
  it("says a grant exists, a worker is spending it, and it can only be spent once", () => {
    const text = spoken("EXECUTION_PENDING");
    expect(text).toContain("Admitted, order being created");
    expect(text).toContain("The kernel admitted your approval");
    expect(text).toContain("issued a single-use execution grant");
    expect(text).toContain("A worker is spending that grant to create the payment order");
    // Single-use is the buyer-visible half of exactly-once execution: the reason a retry
    // cannot produce two payment orders for one approval.
    expect(text).toContain("the grant cannot be spent a second time");
  });

  it("says no payment page exists yet and nothing has been charged", () => {
    const text = spoken("EXECUTION_PENDING");
    expect(text).toContain("No payment page exists yet");
    expect(text).toContain("nothing has been charged");
  });

  it("does not read as PAYMENT_PENDING, where a payment surface really is open", () => {
    const execution = spoken("EXECUTION_PENDING");
    const payment = spoken("PAYMENT_PENDING");
    expect(execution).not.toBe(payment);
    expect(execution).not.toContain("Payment surface open");
    expect(execution).not.toContain("the payment surface is open");
  });

  it("does not let PAYMENT_PENDING borrow its flat 'nothing has been charged'", () => {
    // The two states differ by exactly one fact: whether a provider order exists. Once it
    // does, the platform has stopped knowing whether money moved, and must stop saying it.
    const payment = spoken("PAYMENT_PENDING");
    expect(payment).not.toContain("nothing has been charged");
    expect(payment).toContain("Whether money has moved is not yet known to this platform");
    expect(payment).toContain("will not be until the provider says so");
  });
});

/* ------------------------------------------- specification 29.8: the words, not the colour */

describe("the words carry the state, never the colour alone", () => {
  it("announces itself as a polite live region so a change is heard, not just seen", () => {
    // The state changes underneath a buyer who is watching a payment settle. A change
    // announced assertively would cut across whatever they were reading; polite waits.
    const region = banner("PAYMENT_UNKNOWN");
    expect(region.getAttribute("role")).toBe("status");
    expect(region.getAttribute("aria-live")).toBe("polite");
  });

  it("carries title, state code and sentence in the accessible text of every state", () => {
    // The assertion that protects a buyer who cannot see red. Run over the sixteen, the
    // four the kernel also writes, and a string from neither list.
    for (const state of [...EVERY_KNOWN_STATE, UNRECOGNISED]) {
      const meaning = stateMeaning(state);
      const region = banner(state);
      const text = region.textContent ?? "";
      expect(text, state).toContain(meaning.title);
      expect(text, state).toContain(state);
      expect(text, state).toContain(meaning.sentence);
      // `textContent` only stands in for what a screen reader reaches if nothing carrying
      // words is hidden from it. Assert that here rather than assume it: a title marked
      // `aria-hidden` alongside the dot would satisfy the three lines above and still
      // leave a blind buyer with a sentence and no state name.
      for (const hidden of region.querySelectorAll('[aria-hidden="true"]')) {
        expect(hidden.textContent, state).toBe("");
      }
    }
  });

  it("hides the coloured dot from assistive technology and gives it nothing to say", () => {
    const region = banner("PAID");
    const hidden = Array.from(region.querySelectorAll('[aria-hidden="true"]'));
    expect(hidden).toHaveLength(1);
    // If the dot ever carried text, hiding it would be hiding meaning.
    expect(hidden[0].textContent).toBe("");
  });

  it("distinguishes two states drawn in the same colour by their words alone", () => {
    // PAYMENT_UNKNOWN and RECONCILING are both amber, so the dot is identical on both —
    // deliberately asserted on the class here, because the point is that the pixel cannot
    // tell them apart and the sentence must. One means nobody knows; the other means
    // somebody is finding out.
    const unknownDot = banner("PAYMENT_UNKNOWN").querySelector('[aria-hidden="true"]')?.className;
    const unknownText = spoken("PAYMENT_UNKNOWN");
    const reconcilingDot = banner("RECONCILING").querySelector('[aria-hidden="true"]')?.className;
    const reconcilingText = spoken("RECONCILING");

    expect(reconcilingDot).toBe(unknownDot);
    expect(reconcilingText).not.toBe(unknownText);
  });

  it("draws APPROVED and PAID in the same green, so only the words separate them", () => {
    // A finding, not a preference. The `action` and `good` tones differ by one border
    // alpha; the dot and the title take the same colour variable. At a glance "Approved,
    // not submitted" and "Paid and recorded" are the same green, and exactly one of them
    // means the buyer's money moved — so the sentences are carrying that difference
    // alone, and this test is what says they still do.
    const approvedDot = banner("APPROVED").querySelector('[aria-hidden="true"]')?.className;
    const approvedText = spoken("APPROVED");
    const paidDot = banner("PAID").querySelector('[aria-hidden="true"]')?.className;
    const paidText = spoken("PAID");

    expect(paidDot).toBe(approvedDot);
    expect(paidText).not.toBe(approvedText);
    expect(approvedText).toContain("no money has moved");
    expect(paidText).toContain("confirmed the capture");
  });

  it("never depends on colour to separate two states that mean different things", () => {
    // Generalised: group the twenty by the tone that picks their colours, and require
    // every group to be readable without it. Any two states sharing a tone must differ in
    // their accessible text.
    const byTone = new Map<string, string[]>();
    for (const state of EVERY_KNOWN_STATE) {
      const tone = stateMeaning(state).tone;
      byTone.set(tone, [...(byTone.get(tone) ?? []), spoken(state)]);
    }
    for (const [tone, texts] of byTone) {
      expect(new Set(texts).size, `two states drawn ${tone} read identically`).toBe(texts.length);
    }
  });
});

/* --------------------------------------------------- a state this build has not heard of */

describe("a state string in neither vocabulary", () => {
  it("renders it as itself with an honest admission rather than crashing the page", () => {
    const text = spoken(UNRECOGNISED);
    expect(text).toContain(UNRECOGNISED);
    expect(text).toContain("This storefront does not have a description for this state");
    expect(text).toContain("shown exactly as the server sent it rather than guessed at");
  });

  it("guesses at none of the sixteen meanings on the way", () => {
    const text = spoken(UNRECOGNISED);
    for (const state of EVERY_KNOWN_STATE) {
      expect(text).not.toContain(stateMeaning(state).sentence);
    }
    // And it claims nothing about money, because it knows nothing about this state.
    for (const pattern of [...REASSURANCES, ...CAPTURE_CLAIMS, ...FLAT_VERDICTS]) {
      expect(pattern.test(text)).toBe(false);
    }
  });

  it("uses the server's own string as the title rather than inventing English for it", () => {
    const meaning = stateMeaning(UNRECOGNISED);
    expect(meaning.title).toBe(UNRECOGNISED);
    expect(meaning.tone).toBe("neutral");
  });

  it("is not terminal, so a client polling an unfamiliar state keeps polling", () => {
    // The safe direction. Stopping on a state nobody recognises would freeze the buyer on
    // whatever the screen said last; polling one state too long only costs a request.
    expect(isTerminalState(UNRECOGNISED)).toBe(false);
  });

  it("treats a mis-cased state as unknown rather than as the state it resembles", () => {
    // The vocabulary is the server's, exactly as spelled. Case-folding "paid" into PAID
    // would be the storefront deciding, on its own, that a buyer had been charged.
    const text = spoken("paid");
    expect(text).not.toContain("Paid and recorded");
    expect(text).not.toContain("confirmed the capture");
    expect(text).toContain("does not have a description for this state");
  });

  it("renders an empty state string without crashing or claiming anything", () => {
    const text = spoken("");
    expect(text).toContain("does not have a description for this state");
    for (const pattern of [...REASSURANCES, ...CAPTURE_CLAIMS]) {
      expect(pattern.test(text)).toBe(false);
    }
  });

  it("does not mistake a state named after an Object.prototype member for a meaning", () => {
    // This was a real crash before the lookup used `Object.hasOwn`. Plain indexing walks
    // the prototype chain, so "constructor" resolved to `Object` and "toString" to a
    // function; both are truthy, the honest-admission fallback never ran, and the caller
    // was handed a meaning whose title, sentence and tone were all undefined. The banner
    // picks its styles by tone, so `TONE_STYLES[undefined].bar` threw and the page went
    // white — the one outcome this component's own contract says it will never produce.
    //
    // Kept as four separate strings rather than one, because they fail for three
    // different reasons: `constructor` and `hasOwnProperty` inherit a function,
    // `toString` inherits one that stringifies harmlessly, and `__proto__` is an accessor
    // that returns the prototype itself.
    for (const inherited of ["constructor", "toString", "hasOwnProperty", "__proto__"]) {
      const meaning = stateMeaning(inherited);
      expect(meaning.title).toBe(inherited);
      expect(meaning.sentence).toContain("does not have a description for this state");
      expect(meaning.tone).toBe("neutral");

      const text = spoken(inherited);
      expect(text).toContain(inherited);
      // And it still claims nothing about the buyer's money, which is the reason the
      // fallback exists rather than a rethrow.
      for (const pattern of [...REASSURANCES, ...CAPTURE_CLAIMS]) {
        expect(pattern.test(text)).toBe(false);
      }
    }
  });
});

/* ------------------------------------------ the four the deployed kernel also writes */

describe("the four states the deployed kernel writes beside the sixteen", () => {
  it("gives each of them a meaning of its own", () => {
    expect(spoken("AWAITING_PAYMENT")).toContain("Waiting at the provider");
    expect(spoken("AWAITING_PAYMENT")).toContain("The provider order exists");
    expect(spoken("PAYMENT_FAILED")).toContain("Declined at the provider");
    expect(spoken("PAYMENT_FAILED")).toContain("Razorpay declined or abandoned this payment");
    expect(spoken("INVALIDATED")).toContain("Superseded");
    expect(spoken("INVALIDATED")).toContain("retired because what the merchant is selling changed");
    expect(spoken("INVALIDATED_AWAITING_PAYMENT_RESULT")).toContain(
      "Superseded with a payment in flight",
    );
    expect(spoken("INVALIDATED_AWAITING_PAYMENT_RESULT")).toContain(
      "retired while a payment for it was still in flight",
    );
  });

  it("does not let any of them collide with one of the sixteen", () => {
    for (const extra of ALSO_SEEN) {
      const text = spoken(extra);
      for (const state of CHECKOUT_STATES) {
        expect(text).not.toContain(EXPECTED[state].says);
        expect(text).not.toContain(stateMeaning(state).sentence);
      }
    }
  });

  it("separates the two that look alike: superseded, and superseded with money in flight", () => {
    // The distinction is the entire reason the kernel writes two states. One is a version
    // that quietly died; the other is a version that died with a payment still running at
    // Razorpay, and only the second promises a refund.
    const plain = spoken("INVALIDATED");
    const inFlight = spoken("INVALIDATED_AWAITING_PAYMENT_RESULT");
    expect(inFlight).toContain("anything the provider ends up capturing is refunded automatically");
    expect(plain).not.toContain("refunded automatically");
    expect(plain).not.toContain("in flight");
  });

  it("claims nothing about money on the two where a payment may be live", () => {
    for (const state of ["AWAITING_PAYMENT", "INVALIDATED_AWAITING_PAYMENT_RESULT"]) {
      const sentence = stateMeaning(state).sentence;
      for (const pattern of [...REASSURANCES, ...CAPTURE_CLAIMS, ...FLAT_VERDICTS]) {
        expect(pattern.test(sentence)).toBe(false);
      }
    }
    // PAYMENT_FAILED is the one of the four that may reassure: the provider declined it,
    // so there is nothing to capture and saying so is true.
    expect(spoken("PAYMENT_FAILED")).toContain("Nothing was captured");
  });

  it("points AWAITING_PAYMENT at the provider's evidence rather than at the browser", () => {
    expect(spoken("AWAITING_PAYMENT")).toContain(
      "waiting for Razorpay's own evidence, not for your browser",
    );
  });
});

/* ----------------------------------------------------------------- isTerminalState */

/** The six of the sixteen after which the checkout does nothing further on its own. */
const TERMINAL_OF_THE_SIXTEEN = [
  "PAID",
  "FAILED",
  "CANCELLED",
  "EXPIRED",
  "REJECTED",
  "STALE_CAPTURE",
];

describe("isTerminalState", () => {
  it("names only states that exist in the vocabulary", () => {
    // Guards the list below against drifting off `CHECKOUT_STATES` and quietly asserting
    // nothing on a state the server stopped writing.
    for (const state of TERMINAL_OF_THE_SIXTEEN) {
      expect(CHECKOUT_STATES as readonly string[]).toContain(state);
    }
  });

  it("answers both directions for every one of the sixteen", () => {
    // Driven off `CHECKOUT_STATES`, so a seventeenth state added to the vocabulary fails
    // here until somebody decides whether a client should stop polling on it.
    for (const state of CHECKOUT_STATES) {
      expect(isTerminalState(state), state).toBe(TERMINAL_OF_THE_SIXTEEN.includes(state));
    }
  });

  it("keeps every state that still resolves itself out of the terminal set", () => {
    // Each of these is still moving: a worker spending a grant, a payment surface open,
    // a reconciliation scheduled. A client that stopped polling here would leave the buyer
    // watching a screen that never becomes true.
    expect(isTerminalState("EXECUTION_PENDING")).toBe(false);
    expect(isTerminalState("PAYMENT_PENDING")).toBe(false);
    expect(isTerminalState("PAYMENT_UNKNOWN")).toBe(false);
    expect(isTerminalState("RECONCILING")).toBe(false);
  });

  it("counts STALE_CAPTURE as terminal even though a refund follows it", () => {
    // The checkout is over: no order will ever be written for that version. The refund is
    // the payments platform's business and does not come back as another checkout state.
    expect(isTerminalState("STALE_CAPTURE")).toBe(true);
  });

  it("ends at the provider's own failure but not at a superseded version", () => {
    expect(isTerminalState("PAYMENT_FAILED")).toBe(true);
    // A superseded version is replaced by a new one the buyer can approve, so the checkout
    // is very much still alive.
    expect(isTerminalState("AWAITING_PAYMENT")).toBe(false);
    expect(isTerminalState("INVALIDATED")).toBe(false);
    expect(isTerminalState("INVALIDATED_AWAITING_PAYMENT_RESULT")).toBe(false);
  });

  it("treats every terminal state as one the buyer can act on, not one that ticks", () => {
    // Each terminal state must already say what happened; a terminal state whose sentence
    // reads as in-progress would strand the buyer on a screen with no next event.
    for (const state of TERMINAL_OF_THE_SIXTEEN) {
      const sentence = stateMeaning(state).sentence;
      expect(sentence).not.toContain("not yet known");
      expect(sentence).not.toContain("is asking Razorpay again");
    }
  });
});

/* --------------------------------------------------------------------- stateMeaning */

describe("stateMeaning", () => {
  it("gives every state a tone the component actually has styles for", () => {
    for (const state of [...EVERY_KNOWN_STATE, UNRECOGNISED]) {
      expect(TONES).toContain(stateMeaning(state).tone);
    }
  });

  it("tones the four money-critical states as what they are", () => {
    // The tone picks the colour, and the colour must not contradict the sentence. An
    // unknown outcome drawn green reads, at a glance, as a receipt.
    expect(stateMeaning("EXECUTION_PENDING").tone).toBe("waiting");
    expect(stateMeaning("PAYMENT_UNKNOWN").tone).toBe("warn");
    expect(stateMeaning("RECONCILING").tone).toBe("warn");
    expect(stateMeaning("STALE_CAPTURE").tone).toBe("bad");
  });

  it("reserves the reassuring tone for the one state that earned it", () => {
    expect(stateMeaning("PAID").tone).toBe("good");
    for (const state of CHECKOUT_STATES) {
      if (state === "PAID") continue;
      expect(stateMeaning(state).tone, state).not.toBe("good");
    }
  });

  it("never draws an unresolved outcome as success or as failure", () => {
    for (const state of OUTCOME_UNKNOWN) {
      const tone = stateMeaning(state).tone;
      expect(tone, state).not.toBe("good");
      expect(tone, state).not.toBe("bad");
    }
  });

  it("returns exactly what the banner renders, so a caller reading it reads the screen", () => {
    // Other components branch on `stateMeaning` to decide what to draw around the banner.
    // If the function and the component ever disagreed, the page would contradict itself.
    for (const state of [...EVERY_KNOWN_STATE, UNRECOGNISED]) {
      const meaning = stateMeaning(state);
      expect(spoken(state)).toBe(`${meaning.title}${state}${meaning.sentence}`);
    }
  });

  it("hands back the same meaning every time it is asked", () => {
    expect(stateMeaning("PAYMENT_UNKNOWN")).toEqual(stateMeaning("PAYMENT_UNKNOWN"));
    expect(stateMeaning(UNRECOGNISED)).toEqual(stateMeaning(UNRECOGNISED));
  });
});

/* ------------------------------------------------------------------- the detail line */

describe("the optional detail line", () => {
  it("renders the caller's detail when there is one", () => {
    const text = spoken("RECONCILING", "Attempt 3 of 8. Next check in about 40 seconds.");
    expect(text).toContain("Attempt 3 of 8. Next check in about 40 seconds.");
  });

  it("keeps the detail inside the live region so it is announced with the state", () => {
    // A refund reference announced separately from the state it belongs to is a reference
    // to nothing.
    const region = banner("STALE_CAPTURE", "Refund rfnd_TYDHez32NF91qq was initiated.");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.textContent).toContain("Refund rfnd_TYDHez32NF91qq was initiated.");
  });

  it("adds nothing at all when the caller passes none", () => {
    const meaning = stateMeaning("PAYMENT_UNKNOWN");
    expect(spoken("PAYMENT_UNKNOWN")).toBe(
      `${meaning.title}PAYMENT_UNKNOWN${meaning.sentence}`,
    );
  });

  it("adds nothing for an empty detail, rather than drawing a blank line", () => {
    // `detail=""` is a caller with nothing to add, not a caller with a gap to draw.
    const meaning = stateMeaning("PAID");
    expect(spoken("PAID", "")).toBe(`${meaning.title}PAID${meaning.sentence}`);
  });

  it("does not let a detail change what the state itself claims", () => {
    // The detail is the caller's, and the sentence is the component's. A caller passing
    // something wrong must not be able to make the state read as another state.
    const text = spoken("PAYMENT_UNKNOWN", "Nothing was charged.");
    expect(text).toContain("This is neither a success nor a failure");
    expect(text).toContain("Outcome genuinely unknown");
    expect(text).toContain("PAYMENT_UNKNOWN");
  });
});

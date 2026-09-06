/**
 * Fourteen states, fourteen different facts about somebody's money.
 *
 * This banner is the only thing on the storefront that tells a buyer where their payment
 * actually is. Every other component can be wrong and be ugly; this one can be wrong and
 * be a lie — and the two directions of that lie are not symmetric. Telling a buyer
 * "nothing was charged" while `PAYMENT_UNKNOWN` is on the row means the platform does not
 * know that and said it anyway. Telling them "we are still working on it" on `PAID` is
 * merely annoying. So the tests below are organised by what the banner is *licensed to
 * claim* on each state rather than by what it happens to draw.
 *
 * One sentence in this file used to be a promise the platform did not keep.
 * `INVALIDATED_AWAITING_PAYMENT_RESULT` said "the full amount is refunded to you" while
 * `transaction_kernel.admit_stale_capture_refund` had no application caller at all, so the
 * only mechanism that could have kept the promise was a function nothing called — and this
 * suite was green on the wording the whole time.
 *
 * It has a caller now, `durable_worker.handlers.stale_capture.admit_stale_refund`, and the
 * copy was rewritten to say what that handler does rather than what it was meant to do: a
 * full automatic refund; nothing on a redelivery that already admitted one; and, when the
 * provider reports it has already sent money back itself, a withheld refund and a
 * human-review case, because a difference the platform cannot verify is not one it will
 * guess at. All three are pinned below, so the branch a buyer is least likely to be told
 * about cannot quietly leave the screen again.
 *
 * There were two such promises. The other went with `STALE_CAPTURE`, which turned out not
 * to be a checkout state at all — it is asserted absent below, and its removal took a
 * refund promise off the screen as a side effect rather than as a decision, which is worth
 * knowing if anyone reinstates it.
 *
 * The agreement between `CHECKOUT_STATES` and the kernel's `CheckoutState` is asserted
 * where the kernel can actually be imported, in
 * `packages/commerce-api/tests/test_capi_storefront_states.py`. This file asserts what that
 * agreement is worth on screen.
 *
 * Two of the fourteen only exist against a live backend. A mock that answers every submit
 * with a payment page never produces `EXECUTION_PENDING` (a grant issued, a worker
 * spending it, no payment page yet) and never produces `PAYMENT_UNKNOWN` (the provider was
 * asked and did not answer). Those are where a UI written against a mock collapses
 * everything into one spinner. `AWAITING_PAYMENT` is the third of that kind and the worst
 * case of it: it is on the happy path, every buyer who reaches Razorpay Checkout passes
 * through it, and until today it had no sentence at all and rendered as its own enum name.
 *
 * Three properties run over the whole vocabulary rather than over a hand-listed sample,
 * because a hand-listed sample is exactly what a fifteenth state slips past:
 *
 *  1. Distinctness. No two states share a title and no two share a sentence. A
 *     state that renders as another state is the defect this catches, and it catches it
 *     without anyone having to think of the pair in advance.
 *  2. Licensing. Every flat claim about money — "nothing was charged", "the capture is
 *     real" — is searched for across every sentence, and a state that makes one
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
 * Six strings the storefront used to carry copy for and that no checkout can hold.
 *
 * `SUBMITTED` and `STALE_CAPTURE` are `PaymentState` members — a payment attempt is
 * submitted, a checkout never is — and the rest were invented outright. They are listed
 * here so their absence is asserted rather than assumed: dead copy reads as coverage, and
 * `REJECTED` in particular carried a fully written sentence about declining a version that
 * no buyer could ever be shown, because declining answers `CANCELLED`.
 */
const PHANTOMS = [
  "SUBMITTED",
  "PAYMENT_PENDING",
  "RECONCILING",
  "STALE_CAPTURE",
  "REJECTED",
  "FAILED",
] as const;

/** A plausible state string that is in neither vocabulary. Nothing has ever written it. */
const UNRECOGNISED = "SETTLEMENT_HELD_BY_ACQUIRER";

/** The tones the component declares. A colour outside this set has no styles at all. */
const TONES = ["neutral", "waiting", "action", "good", "warn", "bad"];

/**
 * What each of the fourteen must say, written down independently of the component.
 *
 * `says` is a fragment that belongs to this state's sentence and to no other's, so the
 * cross-check below can assert that a state's screen contains its own sentence and none
 * of the other thirteen.
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
  EXECUTION_PENDING: {
    title: "Admitted, order being created",
    says: "issued a single-use execution grant",
  },
  AWAITING_PAYMENT: {
    title: "With Razorpay",
    says: "an order is not a payment",
  },
  PAID: {
    title: "Paid and recorded",
    says: "Razorpay's own signed evidence confirmed the capture",
  },
  PAYMENT_FAILED: {
    title: "Payment failed",
    says: "Razorpay confirmed this payment did not go through",
  },
  PAYMENT_UNKNOWN: {
    title: "Outcome genuinely unknown",
    says: "did not get a definitive answer",
  },
  INVALIDATED: {
    title: "Superseded",
    says: "retired because what the merchant is selling changed",
  },
  INVALIDATED_AWAITING_PAYMENT_RESULT: {
    title: "Superseded while a payment may be in flight",
    says: "nothing will be fulfilled against it",
  },
  CANCELLED: {
    title: "Cancelled",
    says: "ended before any payment",
  },
  EXPIRED: {
    title: "Expired",
    says: "ran out of time before it was paid",
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

/* ------------------------------------------- all fourteen, said in their own words */

describe("each of the fourteen renders its own title, sentence and state code", () => {
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

  it("says the plain fact before the mechanism, for every state", () => {
    // A screen about money that people click through without reading has failed whatever
    // else it got right, and "a single-use execution grant" is not a sentence a buyer
    // reads. Each state carries a short true line of its own -- and it has to be genuinely
    // different from the precise account, or it is the same paragraph printed twice.
    for (const state of CHECKOUT_STATES) {
      const meaning = stateMeaning(state);
      expect(meaning.buyer.length, state).toBeGreaterThan(0);
      expect(meaning.buyer.length, state).toBeLessThan(meaning.sentence.length);
      expect(meaning.buyer, state).not.toBe(meaning.sentence);
      const rendered = spoken(state);
      expect(rendered.indexOf(meaning.buyer), state).toBeLessThan(
        rendered.indexOf(meaning.sentence),
      );
    }
  });

  it("renders every one of them without needing anything but the state string", () => {
    // No detail, no className, no surrounding data. A banner that only reads correctly
    // when its caller supplies context is a banner that reads wrongly somewhere.
    for (const state of CHECKOUT_STATES) {
      const meaning = stateMeaning(state);
      // Both lines, in the order a buyer needs them: the plain fact about their money
      // first, then how the platform knows it.
      expect(spoken(state)).toBe(
        `${meaning.title}${state}${meaning.buyer}${meaning.sentence}`,
      );
    }
  });
});

/* --------------------------------------------------------------- distinctness */

describe("no state renders as another state", () => {
  it("gives each of the fourteen a title no other of the fourteen uses", () => {
    const titles = CHECKOUT_STATES.map((state) => stateMeaning(state).title);
    expect(new Set(titles).size).toBe(CHECKOUT_STATES.length);
  });

  it("gives each of the fourteen a sentence no other of the fourteen uses", () => {
    const sentences = CHECKOUT_STATES.map((state) => stateMeaning(state).sentence);
    expect(new Set(sentences).size).toBe(CHECKOUT_STATES.length);
  });

  it("puts fourteen different screens in front of the buyer", () => {
    // Asserted on the rendered text rather than on the table, because the table being
    // distinct is worth nothing if the component draws two of its rows the same way.
    const screens = new Set(CHECKOUT_STATES.map((state) => spoken(state)));
    expect(screens.size).toBe(CHECKOUT_STATES.length);
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
  "EXECUTION_PENDING",
  "PAYMENT_FAILED",
  "CANCELLED",
  "EXPIRED",
];

/**
 * A provider order exists and the outcome is not the platform's to state, either way.
 *
 * `INVALIDATED_AWAITING_PAYMENT_RESULT` belongs here rather than with the retired version:
 * the version is dead, which is settled, but whether money moved for it is exactly what
 * nobody knows yet, and that is the half a buyer cares about.
 */
const OUTCOME_UNKNOWN = [
  "AWAITING_PAYMENT",
  "PAYMENT_UNKNOWN",
  "INVALIDATED_AWAITING_PAYMENT_RESULT",
];

/** Money demonstrably moved, on the provider's own evidence. */
const MONEY_MOVED = ["PAID"];

/**
 * A version is over and no payment was ever in flight for it, so there is nothing to
 * reassure about and nothing to claim. `INVALIDATED` is the only one: it says the version
 * cannot be spent, which is a statement about consent rather than about money.
 */
const NOTHING_TO_SAY = ["INVALIDATED"];

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
  it("classifies every state, so one added without thought fails here first", () => {
    const classified = [
      ...NOTHING_CHARGED,
      ...OUTCOME_UNKNOWN,
      ...MONEY_MOVED,
      ...NOTHING_TO_SAY,
    ];
    expect(classified.slice().sort()).toEqual([...CHECKOUT_STATES].sort());
  });

  it("says 'nothing was charged' only on states where nothing was charged", () => {
    // Searched across the whole vocabulary rather than a chosen few: this is the
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

  it("claims a capture only on the one state where one happened", () => {
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
    // The licensing property above is one-directional — it would be satisfied by a
    // vocabulary of sentences that say nothing at all. These seven have to say it.
    for (const state of [
      "DRAFT",
      "APPROVAL_REQUIRED",
      "APPROVED",
      "EXECUTION_PENDING",
      "PAYMENT_FAILED",
      "CANCELLED",
      "EXPIRED",
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
    expect(text).toContain("it stays unknown, and your stock stays held");
    expect(text).toContain("until reconciliation resolves it");
  });

  it("names reconciliation as reading evidence rather than producing an answer", () => {
    // `RECONCILING` used to be a state of its own and is not one: reconciliation is what
    // resolves `PAYMENT_UNKNOWN`, and the sentence for that state has to say so, because a
    // reconciler that invented an outcome would be indistinguishable, to the buyer, from
    // one that found it.
    const text = spoken("PAYMENT_UNKNOWN");
    expect(text).toContain("until reconciliation resolves it against Razorpay's own record");
    expect(text).toContain("writing an order on a guess");
    expect(stateMeaning("RECONCILING").sentence).toContain(
      "does not have a description for this state",
    );
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

  it("no longer writes a sentence for STALE_CAPTURE, which is a payment state", () => {
    /*
     * This carried the storefront's most confident sentence about a buyer's money —
     * "Money was captured for a version that had already been invalidated … the platform
     * admits an automatic refund" — for a value a checkout cannot hold. `STALE_CAPTURE` is
     * a `PaymentState`, describing an attempt, and the banner renders a checkout.
     *
     * It is asserted absent rather than quietly dropped because dead copy reads as
     * coverage: anyone auditing which states this screen handles would have counted it.
     */
    const meaning = stateMeaning("STALE_CAPTURE");
    expect(meaning.title).toBe("STALE_CAPTURE");
    expect(meaning.sentence).toContain("does not have a description for this state");
    // And with it went one of the two automatic-refund promises the storefront was making.
    expect(meaning.sentence).not.toContain("automatic refund");
  });

  it("does not let PAID and INVALIDATED read alike, though both end the version", () => {
    const paid = spoken("PAID");
    const dead = spoken("INVALIDATED");
    expect(paid).toContain("Paid and recorded");
    expect(dead).toContain("Superseded");
    // An order was written on one and never will be on the other. Nothing may carry over.
    expect(dead).not.toContain("the order was written against the version you approved");
    expect(paid).not.toContain("can no longer be paid for or fulfilled");
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

  it("does not let AWAITING_PAYMENT borrow its flat 'nothing has been charged'", () => {
    // The two states differ by exactly one fact: whether a provider order exists. Once it
    // does, the platform has stopped knowing whether money moved, and must stop saying it.
    // EXECUTION_PENDING is licensed to reassure and its successor is not, which makes this
    // the single most likely place for a sentence to be copied one row too far.
    const awaiting = spoken("AWAITING_PAYMENT");
    expect(awaiting).not.toContain("nothing has been charged");
    expect(awaiting).toContain("Whether any money has moved is not something this page can tell you");
    expect(awaiting).toContain("Nothing is confirmed until Razorpay's own signed evidence");
    expect(spoken("EXECUTION_PENDING")).toContain("nothing has been charged");
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
    // The assertion that protects a buyer who cannot see red. Run over the whole
    // vocabulary and over a string from outside it.
    for (const state of [...CHECKOUT_STATES, UNRECOGNISED]) {
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
    // PAYMENT_UNKNOWN and INVALIDATED_AWAITING_PAYMENT_RESULT are both amber, so the dot
    // is identical on both — deliberately asserted on the class here, because the point is
    // that the pixel cannot tell them apart and the sentence must. One means nobody knows
    // whether money moved; the other means that, and that the version is dead anyway.
    const unknownDot = banner("PAYMENT_UNKNOWN").querySelector('[aria-hidden="true"]')?.className;
    const unknownText = spoken("PAYMENT_UNKNOWN");
    const supersededDot = banner("INVALIDATED_AWAITING_PAYMENT_RESULT").querySelector(
      '[aria-hidden="true"]',
    )?.className;
    const supersededText = spoken("INVALIDATED_AWAITING_PAYMENT_RESULT");

    expect(supersededDot).toBe(unknownDot);
    expect(supersededText).not.toBe(unknownText);
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
    // Generalised: group the fourteen by the tone that picks their colours, and require
    // every group to be readable without it. Any two states sharing a tone must differ in
    // their accessible text.
    const byTone = new Map<string, string[]>();
    for (const state of CHECKOUT_STATES) {
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

  it("guesses at none of the fourteen meanings on the way", () => {
    const text = spoken(UNRECOGNISED);
    for (const state of CHECKOUT_STATES) {
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

  it("has no copy left for any of the six states the platform cannot produce", () => {
    /*
     * Dead copy is not harmless: it reads as coverage. Anyone auditing which states this
     * screen handles would have counted `STALE_CAPTURE` and `RECONCILING` among them, and
     * `REJECTED` carried a fully written sentence — "You declined this version" — that no
     * buyer could ever be shown, because declining a version answers `CANCELLED`.
     *
     * Asserted as a set rather than one by one, so reinstating any of them fails here
     * rather than passing quietly on the strength of the other five.
     */
    for (const phantom of PHANTOMS) {
      const meaning = stateMeaning(phantom);
      expect(meaning.title, phantom).toBe(phantom);
      expect(meaning.sentence, phantom).toContain("does not have a description for this state");
      expect(CHECKOUT_STATES as readonly string[]).not.toContain(phantom);
    }
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
    // Kept as five separate strings rather than one, because they fail for three
    // different reasons: `constructor`, `hasOwnProperty` and `valueOf` inherit a function,
    // `toString` inherits one that stringifies harmlessly, and `__proto__` is an accessor
    // that returns the prototype itself.
    for (const inherited of ["constructor", "toString", "hasOwnProperty", "__proto__", "valueOf"]) {
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

/* --------------------------------------- the four that were nearly missed entirely */

describe("the four states that reached this build with no copy at all", () => {
  /**
   * These four are the reason the vocabulary was wrong in the direction that hurt. The
   * storefront listed six states the platform cannot produce and omitted four it does,
   * and because an unknown state renders as itself rather than crashing, the omission was
   * invisible: a buyer at `AWAITING_PAYMENT` was shown the enum member and nothing else.
   */
  it("gives each of them a sentence rather than its own name", () => {
    for (const state of [
      "AWAITING_PAYMENT",
      "PAYMENT_FAILED",
      "INVALIDATED",
      "INVALIDATED_AWAITING_PAYMENT_RESULT",
    ]) {
      const meaning = stateMeaning(state);
      expect(meaning.title).not.toBe(state);
      expect(meaning.sentence).not.toContain("does not have a description for this state");
    }
  });

  /**
   * `AWAITING_PAYMENT` is on the happy path, and its first draft was false on screen.
   *
   * The copy said Razorpay held the payment and the surface had been opened. Driving a
   * real checkout to this state showed neither: the version reaches it when the worker
   * lands the provider *order*, which can be long before the buyer opens the payment
   * screen at all. Both retracted claims are forbidden here by name, because a sentence
   * that was wrong once is the sentence most likely to come back.
   */
  it("says an order is not a payment, and does not claim the surface was opened", () => {
    const text = spoken("AWAITING_PAYMENT");
    expect(text).toContain("an order is not a payment");
    expect(text).toContain("before you have opened the payment screen");
    expect(text).toContain("Razorpay's own signed evidence");
    expect(text).not.toContain("the surface was opened");
    expect(text).not.toContain("Razorpay has the payment");
  });

  it("separates the two that look alike: superseded, and superseded with money in flight", () => {
    // The distinction is the entire reason the kernel writes two states. One is a version
    // that quietly died; the other died with a payment that may still have been running.
    const plain = spoken("INVALIDATED");
    const inFlight = spoken("INVALIDATED_AWAITING_PAYMENT_RESULT");
    expect(plain).toContain("can no longer be paid for or fulfilled");
    expect(inFlight).toContain("may still have been moving");
    expect(inFlight).toContain("will not tell you which of those happened");
    expect(plain).not.toContain("in flight");
  });

  /**
   * Specification 31.2, the late-capture demonstration, and the hardest sentence in the
   * product. It has to close two doors at once — nothing will be fulfilled, and money that
   * moved comes back — without promising more than the platform actually performs.
   */
  it("describes every refund branch the platform has, not only the one it wants to be true", () => {
    const text = spoken("INVALIDATED_AWAITING_PAYMENT_RESULT");
    expect(text).toContain("nothing will be fulfilled against it");
    expect(text).toContain("If no money left your account there is nothing to return");

    // A promise about somebody's money has to match the handler rather than the intention.
    // `durable_worker.handlers.stale_capture.admit_stale_refund` has three buyer-visible
    // outcomes and this copy asserted only the first of them until the third was built: it
    // refunds in full; on a redelivery it admits nothing a second time; and when the
    // provider reports it has already returned some or all of the money, it withholds and
    // opens a human-review case rather than computing a difference it cannot verify.
    expect(text).toContain("the platform refunds it in full without anyone asking");
    expect(text).toContain("no second refund is made and a person checks you have been made whole");

    // The retired sentence, forbidden by name. It promised one unconditional refund on a
    // path where `admit_stale_capture_refund` had no application caller, and this file
    // pinned its wording green while nothing anywhere held the promise itself.
    expect(text).not.toContain("the full amount is refunded to you");
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
    const text = spoken("AWAITING_PAYMENT");
    expect(text).toContain("not from your coming back to it");
    expect(text).toContain("not from the payment screen saying it succeeded");
    expect(text).toContain("Razorpay's own signed evidence reaches the platform");
  });
});

/* ----------------------------------------------------------------- isTerminalState */

/** The five states after which the checkout does nothing further on its own. */
const TERMINAL_OF_THE_VOCABULARY = [
  "PAID",
  "PAYMENT_FAILED",
  "INVALIDATED",
  "CANCELLED",
  "EXPIRED",
];

describe("isTerminalState", () => {
  it("names only states that exist in the vocabulary", () => {
    // Guards the list below against drifting off `CHECKOUT_STATES` and quietly asserting
    // nothing on a state the server stopped writing.
    for (const state of TERMINAL_OF_THE_VOCABULARY) {
      expect(CHECKOUT_STATES as readonly string[]).toContain(state);
    }
  });

  it("answers both directions for every one of the fourteen", () => {
    // Driven off `CHECKOUT_STATES`, so a fifteenth state added to the vocabulary fails
    // here until somebody decides whether a client should stop polling on it.
    for (const state of CHECKOUT_STATES) {
      expect(isTerminalState(state), state).toBe(TERMINAL_OF_THE_VOCABULARY.includes(state));
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

  it("counts INVALIDATED as terminal, which it was not before", () => {
    /*
     * A retired version does nothing further on its own, and the set used to omit it while
     * carrying three states no checkout can hold. The consequence was not cosmetic: the
     * journey stops polling on a terminal state and the payment panel disables Pay on one,
     * so a superseded version kept a live Pay button over an approval that cannot be spent.
     */
    expect(isTerminalState("INVALIDATED")).toBe(true);
  });

  it("keeps waiting where the provider has not answered", () => {
    expect(isTerminalState("PAYMENT_FAILED")).toBe(true);
    expect(isTerminalState("AWAITING_PAYMENT")).toBe(false);
    expect(isTerminalState("PAYMENT_UNKNOWN")).toBe(false);
    // Dead version, live question. The platform is still asking Razorpay what happened to
    // the money, so this one must keep polling however settled the version is.
    expect(isTerminalState("INVALIDATED_AWAITING_PAYMENT_RESULT")).toBe(false);
  });

  it("treats every terminal state as one the buyer can act on, not one that ticks", () => {
    // Each terminal state must already say what happened; a terminal state whose sentence
    // reads as in-progress would strand the buyer on a screen with no next event.
    for (const state of TERMINAL_OF_THE_VOCABULARY) {
      const sentence = stateMeaning(state).sentence;
      expect(sentence).not.toContain("not yet known");
      expect(sentence).not.toContain("is asking Razorpay again");
    }
  });
});

/* --------------------------------------------------------------------- stateMeaning */

describe("stateMeaning", () => {
  it("gives every state a tone the component actually has styles for", () => {
    for (const state of [...CHECKOUT_STATES, UNRECOGNISED]) {
      expect(TONES).toContain(stateMeaning(state).tone);
    }
  });

  it("tones the four money-critical states as what they are", () => {
    // The tone picks the colour, and the colour must not contradict the sentence. An
    // unknown outcome drawn green reads, at a glance, as a receipt.
    expect(stateMeaning("EXECUTION_PENDING").tone).toBe("waiting");
    expect(stateMeaning("AWAITING_PAYMENT").tone).toBe("waiting");
    expect(stateMeaning("PAYMENT_UNKNOWN").tone).toBe("warn");
    expect(stateMeaning("INVALIDATED_AWAITING_PAYMENT_RESULT").tone).toBe("warn");
    expect(stateMeaning("PAYMENT_FAILED").tone).toBe("bad");
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
    for (const state of [...CHECKOUT_STATES, UNRECOGNISED]) {
      const meaning = stateMeaning(state);
      // Both lines, in the order a buyer needs them: the plain fact about their money
      // first, then how the platform knows it.
      expect(spoken(state)).toBe(
        `${meaning.title}${state}${meaning.buyer}${meaning.sentence}`,
      );
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
      `${meaning.title}PAYMENT_UNKNOWN${meaning.buyer}${meaning.sentence}`,
    );
  });

  it("adds nothing for an empty detail, rather than drawing a blank line", () => {
    // `detail=""` is a caller with nothing to add, not a caller with a gap to draw.
    const meaning = stateMeaning("PAID");
    expect(spoken("PAID", "")).toBe(`${meaning.title}PAID${meaning.buyer}${meaning.sentence}`);
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

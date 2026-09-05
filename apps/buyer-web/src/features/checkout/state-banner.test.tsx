/**
 * The banner's job is to have something true to say about whatever the server sent.
 *
 * Two failures matter and they are opposite. The first is silence: a state in the shared
 * vocabulary with no sentence, so a buyer watching their money settle is shown an enum
 * member. That is how `AWAITING_PAYMENT` and `INVALIDATED_AWAITING_PAYMENT_RESULT` were
 * rendered before this file existed. The second is invention: a sentence written for a
 * state the platform cannot produce, which reads as coverage and is dead. `REJECTED`
 * carried a fully written sentence about declining a version that no buyer could ever
 * reach, because declining answers `CANCELLED`.
 *
 * The agreement between `CHECKOUT_STATES` and the kernel's `CheckoutState` is asserted
 * where the kernel can actually be imported, in
 * `packages/commerce-api/tests/test_capi_storefront_states.py`. This file asserts what
 * that agreement is worth on screen.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { CHECKOUT_STATES } from "@/lib/api/types";

import { StateBanner, isTerminalState, stateMeaning } from "./state-banner";

afterEach(cleanup);

/** The sentence `stateMeaning` returns when this build has no copy for a state. */
const FALLBACK = "This storefront does not have a description for this state.";

describe("stateMeaning", () => {
  it.each(CHECKOUT_STATES)("says something specific about %s", (state) => {
    const meaning = stateMeaning(state);
    expect(meaning.sentence).not.toContain(FALLBACK);
    // The title is the state said as a person would say it, so it must not be the enum
    // name: `title === state` is what the fallback returns and would pass a laxer check.
    expect(meaning.title).not.toBe(state);
  });

  it("admits it does not know a state rather than guessing at one", () => {
    const meaning = stateMeaning("SOMETHING_THE_KERNEL_LEARNED_LATER");
    expect(meaning.title).toBe("SOMETHING_THE_KERNEL_LEARNED_LATER");
    expect(meaning.sentence).toContain(FALLBACK);
  });

  it("has no copy for a state the platform can never produce", () => {
    // REJECTED is the one that was carrying dead copy. Declining a version answers
    // `{"state": "CANCELLED"}`; there is no REJECTED checkout state anywhere.
    expect(stateMeaning("REJECTED").sentence).toContain(FALLBACK);
    for (const phantom of ["SUBMITTED", "PAYMENT_PENDING", "RECONCILING", "STALE_CAPTURE", "FAILED"]) {
      expect(stateMeaning(phantom).sentence).toContain(FALLBACK);
    }
  });
});

describe("the states a buyer reaches around a payment", () => {
  /**
   * The happy path's own state. Every buyer who reaches Razorpay Checkout passes through
   * it, and the one thing it must not do is imply that being here means anything about
   * money (ADR 0003 D8: the browser's return is recorded, never applied as capture).
   */
  it("says AWAITING_PAYMENT settles nothing until the platform verifies it", () => {
    render(<StateBanner state="AWAITING_PAYMENT" />);
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("AWAITING_PAYMENT");
    expect(banner.textContent).toMatch(/Whether any money has moved is not something this page can tell you/);
    expect(banner.textContent).toMatch(/Razorpay's own signed evidence/);
    // The state is reached when the provider ORDER is created, which happens before the
    // buyer opens the payment screen -- the banner was verified on a live checkout sitting
    // at AWAITING_PAYMENT with the Pay button still unpressed. So it must not assert that
    // Razorpay holds a payment or that the surface was opened; both were claimed here and
    // both were false on screen.
    expect(banner.textContent).toMatch(/an order is not a payment/);
    expect(banner.textContent).not.toMatch(/the surface was opened/);
  });

  /**
   * Specification 31.2, the late-capture demonstration, and the hardest sentence in the
   * product. It has to close two doors at once: nothing will be fulfilled, and money that
   * moved comes back -- without promising a refund that does not exist yet.
   */
  it("says INVALIDATED_AWAITING_PAYMENT_RESULT fulfils nothing and promises no refund yet", () => {
    render(<StateBanner state="INVALIDATED_AWAITING_PAYMENT_RESULT" />);
    const text = screen.getByRole("status").textContent ?? "";
    expect(text).toMatch(/nothing will be fulfilled against it/);
    expect(text).toMatch(/If no money left your account there is nothing to return/);
    expect(text).toMatch(/will not tell you which of the two happened/);
  });

  it("keeps polling INVALIDATED_AWAITING_PAYMENT_RESULT, which resolves without the buyer", () => {
    expect(isTerminalState("INVALIDATED_AWAITING_PAYMENT_RESULT")).toBe(false);
  });

  it("stops polling on the kernel's terminal states and on PAYMENT_FAILED", () => {
    for (const state of ["PAID", "INVALIDATED", "CANCELLED", "EXPIRED", "PAYMENT_FAILED"]) {
      expect(isTerminalState(state)).toBe(true);
    }
  });
});

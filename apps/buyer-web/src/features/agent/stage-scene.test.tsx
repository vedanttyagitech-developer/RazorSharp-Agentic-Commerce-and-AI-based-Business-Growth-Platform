/**
 * The scene is a caption, and the tests read it as one: every stage says its line, and no
 * stage draws a control. A caption that quietly grew a button would be a write the buyer
 * never pressed, so the last check in each direction is that there is no `<form>` and no
 * `<button>` anywhere in what this renders.
 *
 * Two facts on the approve line come from the checkout, not the copy, and are checked
 * against the fixture rather than trusted: the version the buyer is being asked to approve
 * and the amount it carries. The pay line's Razorpay order id is present exactly when the
 * attempt carries one and absent otherwise -- the two are opposite facts and are tested
 * apart. And a pending permission slip's summary overrides the stage line outright,
 * because a decision awaited now outranks the stage the checkout happens to sit in.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { Checkout } from "@/lib/api/types";

import { StageScene } from "./stage-scene";

afterEach(cleanup);

/**
 * A checkout at the approval step, trimmed to the fields the scene reads: the approval
 * card's version and `total` money, the attempt's Razorpay order id, and the order id.
 * The API sends more; the schema keeps it and this component ignores it, so the fixture
 * carries only what is asserted on and is cast in as the server's own shape.
 */
const CHECKOUT = {
  checkout_id: "co_1",
  current_version: 2,
  order_id: null,
  approval_card: {
    version: 3,
    total: { minor: 48900, currency: "INR", display: "₹489.00" },
  },
  attempt: {
    razorpay_order_id: "order_Qk7fJ2Xample",
  },
} as unknown as Checkout;

describe("each stage says its line", () => {
  it("discover invites the first word", () => {
    render(<StageScene stage="discover" checkout={null} orderId={null} pendingNote={null} />);
    expect(screen.getByText("Ask for anything, or say what you need")).toBeDefined();
  });

  it("cart offers the two ways to check out", () => {
    render(<StageScene stage="cart" checkout={null} orderId={null} pendingNote={null} />);
    expect(screen.getByText("Ready? Say checkout, or press Checkout")).toBeDefined();
  });

  it("reserve names itself a labelled simulator that moves no money", () => {
    render(<StageScene stage="reserve" checkout={null} orderId={null} pendingNote={null} />);
    expect(
      screen.getByText("Reserve Pay is a labelled simulator; nothing moves money"),
    ).toBeDefined();
  });
});

describe("the approve line carries the checkout's own version and amount", () => {
  it("shows the approval card's version and its total, and asks for the word", () => {
    const { container } = render(
      <StageScene stage="approve" checkout={CHECKOUT} orderId={null} pendingNote={null} />,
    );
    const text = container.textContent ?? "";
    // The version the buyer is being asked to approve is the card's (v3), not the
    // checkout's bare current_version -- the two differ in the fixture on purpose.
    expect(text).toContain("v3");
    // The amount is the server's `total`, rendered by <Amount/>; nothing here formats money.
    expect(text).toContain("₹489.00");
    expect(text).toContain("Say yes to approve");
  });
});

describe("the pay line shows the Razorpay order id exactly when the attempt carries one", () => {
  it("prints the id when the attempt has one", () => {
    const { container } = render(
      <StageScene stage="pay" checkout={CHECKOUT} orderId={null} pendingNote={null} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Approved. Razorpay");
    expect(text).toContain("order_Qk7fJ2Xample");
  });

  it("omits the id when the attempt has none", () => {
    const noAttempt = { ...CHECKOUT, attempt: null } as unknown as Checkout;
    const { container } = render(
      <StageScene stage="pay" checkout={noAttempt} orderId={null} pendingNote={null} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Approved. Razorpay");
    expect(text).not.toContain("order_Qk7fJ2Xample");
  });
});

describe("the order line links to the placed order when its id is known", () => {
  it("links to /orders/{id}", () => {
    render(<StageScene stage="order" checkout={null} orderId="ord_99" pendingNote={null} />);
    const link = screen.getByText("Track it on the order page");
    expect(link).toBeTruthy();
    expect(link.getAttribute("href")).toBe("/orders/ord_99");
  });

  it("still says paid without a link when no order id is known", () => {
    const { container } = render(
      <StageScene stage="order" checkout={null} orderId={null} pendingNote={null} />,
    );
    expect((container.textContent ?? "")).toContain("Track it on the order page");
    expect(container.querySelector("a")).toBeNull();
  });
});

describe("a pending permission slip overrides the stage line", () => {
  it("shows the slip's summary instead of what the stage would say", () => {
    const note = "RazorAI wants to add 2× Amul milk to your cart";
    const { container } = render(
      <StageScene stage="approve" checkout={CHECKOUT} orderId={null} pendingNote={note} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain(note);
    // The approve line's own words are gone -- the slip is the whole caption now.
    expect(text).not.toContain("Say yes to approve");
  });
});

describe("it is a caption, never a control", () => {
  it("renders no form and no button in any stage", () => {
    const stages = ["discover", "cart", "approve", "pay", "order", "reserve"] as const;
    for (const stage of stages) {
      const { container } = render(
        <StageScene stage={stage} checkout={CHECKOUT} orderId="ord_99" pendingNote={null} />,
      );
      expect(container.querySelector("form")).toBeNull();
      expect(container.querySelector("button")).toBeNull();
      cleanup();
    }
  });
});

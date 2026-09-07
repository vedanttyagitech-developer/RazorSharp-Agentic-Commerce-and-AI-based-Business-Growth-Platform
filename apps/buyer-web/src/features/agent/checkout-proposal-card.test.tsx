/**
 * The checkout confirm card, tested against payloads and answers the API actually produces.
 *
 * The card gives RazorAI's `checkout.create` proposal a press of its own: it opens a
 * checkout on the trusted surface — the same `POST /v1/carts/{id}/checkout` the cart
 * page uses — and sends the buyer to that checkout's approval page. It still commits no
 * money; opening a checkout only prices version 1 for the buyer to approve there.
 *
 * Four properties are checked, each mirroring the discipline the line card is already held
 * to:
 *
 * 1. **The button exists only when the proposal names a cart** (and a handler is passed).
 *    A checkout with no cart to build from is not a proposal anyone can act on, so the
 *    card draws only the door.
 * 2. **A press calls the client method once, with that cart id** — nothing recomputed,
 *    nothing invented.
 * 3. **A server refusal renders its reason and does not navigate.** The 409
 *    `proposal_superseded` is drawn verbatim, the same way the line card draws it, and the
 *    buyer is not sent anywhere.
 * 4. **No second press is possible while one is in flight**, and only a checkout the server
 *    actually opened navigates — to its own id.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ApiError } from "@/lib/api/problem";
import { ApprovalCardSchema, type ApprovalCard } from "@/lib/api/types";

import {
  CheckoutProposalCard,
  type CheckoutConfirmation,
  SUPERSEDED,
} from "./checkout-proposal-card";
import { ProposalCard } from "./proposal-card";

afterEach(cleanup);

const CART_ID = "01a07202-1ba8-7297-a54d-5116246acf0f";
const CHECKOUT_ID = "01a07300-9c2b-7bd1-a10e-77f0e0e0e0e0";

/**
 * `POST /v1/carts/{id}/checkout` answers **201 with version 1's approval card**, not a
 * checkout. This fixture is that card, put through the client's own schema so a payload
 * that has drifted out of the contract fails here rather than rendering something plausible.
 * Money is integer minor units throughout — 72195 paise, never 721.95.
 */
const CARD: ApprovalCard = ApprovalCardSchema.parse({
  checkout_id: CHECKOUT_ID,
  version: 1,
  content_hash: "opened-version-1",
  policy_receipt_id: "rcpt_01",
  policy_receipt_hash: "rcpt-hash",
  amount_minor: 72195,
  currency: "INR",
  total: { minor: 72195, currency: "INR", display: "721.95" },
  expires_at: null,
  reservation: { reservation_id: "rsv_01", state: "HELD", expires_at: null },
  quote: null,
  previous_version: null,
  deltas: [],
});

/** The `checkout.create` envelope the shopping specialist proposes once it has read a cart. */
const CHECKOUT_PROPOSAL = {
  kind: "cart",
  proposal: {
    action: "checkout.create",
    cart_id: CART_ID,
    executes_on: "trusted_surface",
  },
};

/** The same proposal with no cart in context — nothing a press could open. */
const NO_BASKET_PROPOSAL = {
  kind: "cart",
  proposal: {
    action: "checkout.create",
    cart_id: null,
    executes_on: "trusted_surface",
  },
};

describe("the checkout card's press exists only when there is a cart to open", () => {
  it("draws no button without a handler — only the door to the cart", () => {
    render(<CheckoutProposalCard cartId={CART_ID} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    const door = screen.getByRole("link", { name: /Open your cart/ });
    expect(door.getAttribute("href")).toBe("/cart");
  });

  it("draws no button when the proposal names no cart, handler or not", () => {
    render(<CheckoutProposalCard cartId={null} onConfirm={vi.fn()} navigate={vi.fn()} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("draws the button once a handler is passed and a cart is named", () => {
    render(<CheckoutProposalCard cartId={CART_ID} onConfirm={vi.fn()} navigate={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Confirm checkout/ })).toBeDefined();
  });

  it("gives the proposal card a real button only when the panel wired a handler with a cart", () => {
    const { rerender } = render(<ProposalCard structured={CHECKOUT_PROPOSAL} />);
    // No handler: the plain handoff, whose only control is the door.
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("link", { name: /Open your cart/ }).getAttribute("href")).toBe(
      "/cart",
    );

    rerender(
      <ProposalCard
        structured={CHECKOUT_PROPOSAL}
        onConfirmCheckout={vi.fn<(c: CheckoutConfirmation) => Promise<ApprovalCard>>(async () => CARD)}
      />,
    );
    expect(screen.getByRole("button", { name: /Confirm checkout/ })).toBeDefined();

    // A handler but a proposal naming no cart: no press. The `checkout.create` handoff has
    // always required a cart id to render at all, so the card falls through to nothing —
    // there is no cart to open and none to link to.
    rerender(
      <ProposalCard
        structured={NO_BASKET_PROPOSAL}
        onConfirmCheckout={vi.fn<(c: CheckoutConfirmation) => Promise<ApprovalCard>>(async () => CARD)}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/Confirm checkout for this cart/)).toBeNull();
  });
});

describe("a press opens the named checkout, once, and lands the buyer on its approval page", () => {
  it("calls the client method exactly once with the proposal's cart id, then navigates to the returned checkout", async () => {
    const onConfirm = vi.fn<(c: CheckoutConfirmation) => Promise<ApprovalCard>>(async () => CARD);
    const navigate = vi.fn();
    render(<CheckoutProposalCard cartId={CART_ID} onConfirm={onConfirm} navigate={navigate} />);

    fireEvent.click(screen.getByRole("button", { name: /Confirm checkout/ }));
    await screen.findByText(/Checkout opened/);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const sent = onConfirm.mock.calls[0][0];
    expect(sent.cart_id).toBe(CART_ID);
    expect(typeof sent.idempotency_key).toBe("string");
    expect(sent.idempotency_key.length).toBeGreaterThan(0);
    // Navigated to the checkout the server opened, by its own id — never guessed from the cart.
    expect(navigate).toHaveBeenCalledWith(CHECKOUT_ID);
    // No second press once opened.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

describe("a refusal is drawn honestly and never navigates", () => {
  it("renders the superseded reason verbatim, offers no second press, and does not navigate", async () => {
    const onConfirm = vi.fn<(c: CheckoutConfirmation) => Promise<ApprovalCard>>(async () => {
      throw new ApiError({
        type: "about:blank",
        title: "That proposal is out of date",
        status: 409,
        detail: "This cart moved after the proposal was prepared, so no checkout was opened. Nothing changed.",
        reason: SUPERSEDED,
      });
    });
    const navigate = vi.fn();
    render(<CheckoutProposalCard cartId={CART_ID} onConfirm={onConfirm} navigate={navigate} />);

    fireEvent.click(screen.getByRole("button", { name: /Confirm checkout/ }));
    const status = (await screen.findByText(SUPERSEDED)).closest("[role=status]");
    expect(status?.textContent).toContain("Nothing changed");
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the press after a transport failure, says no checkout was opened, does not navigate, and retries with the same key", async () => {
    const onConfirm = vi.fn<(c: CheckoutConfirmation) => Promise<ApprovalCard>>(async () => {
      throw new ApiError({ type: "about:blank", title: "Unreachable", status: 0 });
    });
    const navigate = vi.fn();
    render(<CheckoutProposalCard cartId={CART_ID} onConfirm={onConfirm} navigate={navigate} />);

    fireEvent.click(screen.getByRole("button", { name: /Confirm checkout/ }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("No checkout was opened");
    expect(navigate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Confirm checkout/ }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(2));
    // The same press again is the same request: one key, minted once, kept until answered.
    expect(onConfirm.mock.calls[1][0].idempotency_key).toBe(onConfirm.mock.calls[0][0].idempotency_key);
  });
});

describe("no second press lands while the first is in flight", () => {
  it("does not open twice while a press is still pending", async () => {
    let release: (card: ApprovalCard) => void = () => {};
    const onConfirm = vi.fn<(c: CheckoutConfirmation) => Promise<ApprovalCard>>(
      () =>
        new Promise<ApprovalCard>((resolve) => {
          release = resolve;
        }),
    );
    const navigate = vi.fn();
    render(<CheckoutProposalCard cartId={CART_ID} onConfirm={onConfirm} navigate={navigate} />);

    const press = screen.getByRole("button", { name: /Confirm checkout/ });
    fireEvent.click(press);
    fireEvent.click(press);
    expect(onConfirm).toHaveBeenCalledTimes(1);

    release(CARD);
    await screen.findByText(/Checkout opened/);
    expect(navigate).toHaveBeenCalledWith(CHECKOUT_ID);
  });
});

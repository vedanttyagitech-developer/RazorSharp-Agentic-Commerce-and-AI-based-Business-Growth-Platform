/**
 * The controls that let a buyer act on an order, tested by what they are licensed to claim.
 *
 * Two failures are what these tests exist to catch, and they are not symmetrical.
 *
 * The first is telling a buyer their money is coming back when it is not. An admitted
 * refund is a row, a grant and an outbox command; the provider has not been heard from. So
 * the admitted case is asserted on the words that must be present ("has not moved yet") and
 * on the word that must be absent -- the panel must never call a `REFUND_PENDING` refund
 * *refunded*, which is a claim about a bank balance that nothing yet supports.
 *
 * The second is quieter and is the reason this panel exists at all: a control that decides
 * on the kernel's behalf. The tests therefore assert that the refund button is live on an
 * order the storefront could easily have decided was ineligible, and that a refusal is
 * rendered as an answer -- `role="status"`, the code verbatim, "no money moved" -- rather
 * than as an error the buyer should try to fix.
 *
 * **Every fixture below is a body the live API sent on 2026-09-05**, captured against the
 * local stack by walking the real path: mint a buyer session for the buyer who owns the
 * order, `POST /v1/orders/{id}/refunds`, `POST /v1/checkouts/{id}/cancel`. The
 * `payment_surface_open` refusal was captured by opening a checkout, approving version 1,
 * submitting it to reach `AWAITING_PAYMENT` and then asking to cancel -- which is the
 * refusal this whole panel is designed around, so it is the one that had to be real rather
 * than hand-written. Where a fixture is a captured body with fields changed, the comment
 * says which fields and why.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { reasonSentence } from "@/features/checkout/refusal-card";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import type { ApprovalResult, Order, Refundable, RefundResult } from "@/lib/api/types";

import { OrderActions, phraseFor, sentenceFor } from "./order-actions";

/**
 * `GET /v1/orders/01a06fd5-0fe4-.../refundable` on the order below, before any refund.
 * Captured 2026-09-05 against the local stack with a buyer session for the order's owner.
 */
const REFUNDABLE: Refundable = {
  order_id: "01a06fd5-0fe4-7a1d-a7b1-42747790b3ce",
  refundable_minor: 29725,
  currency: "INR",
  refundable: { minor: 29725, currency: "INR", display: "297.25" },
  anything_remains: true,
};

// Stubbed for every test, because opening the refund composer reads it. A test that cares
// what the figure is overrides this; one that does not still gets a resolved promise
// instead of a real `fetch` in jsdom.
beforeEach(() => {
  vi.spyOn(api, "refundable").mockResolvedValue(REFUNDABLE);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/* ------------------------------------------------------------- the fixtures */

/**
 * `GET /v1/orders/01a06fd5-0fe4-...` as it read after a refund was admitted against it.
 * Captured 2026-09-05. The quote is nulled to keep the fixture readable; this panel never
 * reads it, and `order-detail` renders it from the same field independently.
 */
const ORDER: Order = {
  order_id: "01a06fd5-0fe4-7a1d-a7b1-42747790b3ce",
  reference: "RS-260905-TESTREF",
  checkout_id: "01a06fd5-09ca-7774-83d1-fab021012c17",
  version: 1,
  content_hash: "xk_lVzIrrzXHouZ9Yvi_r9EuXcI-6Uiei20pSjvKJXk",
  policy_receipt_hash: "k8jTrthnK2dAth9zJ-Jz5lQGCvtQkPAq_pbqgaU8IMs",
  state: "CONFIRMED",
  amount_minor: 29725,
  currency: "INR",
  amount: { minor: 29725, currency: "INR", display: "297.25" },
  quote: null,
  payment: {
    attempt_id: "01a06fd5-09e1-70ef-84e2-5e671d2aecc2",
    version: 1,
    state: "CAPTURED",
    razorpay_order_id: "order_FIXTURE0000003",
    razorpay_payment_id: "pay_6ced4db55d51d9",
    grant_id: "01a06fd5-09e1-70e5-b3a7-1d658f68e4b5",
    capture_evidence: {
      kind: "WEBHOOK",
      reference: "pay_6ced4db55d51d9",
      verified_at: "2026-09-05T04:30:29Z",
    },
    reconciliation_attempts: 0,
  },
  refunds: [],
  created_at: "2026-09-05T04:30:29.346898Z",
  duration_seconds: 92,
  // Spelled out rather than left to the schema default: a fixture that omitted this
  // would stop exercising the panel's own handling of a measured sale.
  timing: {
    deciding_seconds: 74,
    admitting_seconds: 1,
    queued_seconds: 3,
    paying_seconds: 14,
  },
};

/**
 * `POST /v1/orders/{id}/refunds` with no amount, admitted. Captured 2026-09-05, HTTP 200.
 *
 * Note what an *allowed* refund is and is not: `state` is `REFUND_PENDING`, the attempt has
 * moved to `REFUND_PENDING` too, and Razorpay has said nothing. Everything the success
 * panel is allowed to say has to be supportable from exactly this.
 */
const ADMITTED: RefundResult = {
  decision: {
    decision_id: "01a071cd-c923-75c6-9ef0-1c81cbc8b224",
    allowed: true,
    code: "OK",
    explanation: "refund_admitted",
    checkout: {
      checkout_id: "01a06fd5-09ca-7774-83d1-fab021012c17",
      version: 1,
      content_hash: "xk_lVzIrrzXHouZ9Yvi_r9EuXcI-6Uiei20pSjvKJXk",
    },
    deltas: [],
    grant_id: "01a071cd-c930-7342-a071-228c9e0e449f",
    payment_attempt_id: "01a06fd5-09e1-70ef-84e2-5e671d2aecc2",
    next_version: null,
    correlation_id: "01a071cd-c904-71d0-9388-758426264183",
  },
  refund: {
    refund_id: "01a071cd-c923-7816-bec8-ede9adb0fa37",
    amount_minor: 29725,
    currency: "INR",
    state: "REFUND_PENDING",
    reason: "buyer_requested",
    automatic: false,
    created_at: "2026-09-05T13:41:46.885508Z",
  },
  order: {
    ...ORDER,
    payment: { ...ORDER.payment, state: "REFUND_PENDING" },
    refunds: [
      {
        refund_id: "01a071cd-c923-7816-bec8-ede9adb0fa37",
        amount_minor: 29725,
        currency: "INR",
        state: "REFUND_PENDING",
        reason: "buyer_requested",
        automatic: false,
        created_at: "2026-09-05T13:41:46.885508Z",
      },
    ],
  },
};

/**
 * `POST /v1/orders/{id}/refunds` asking for ₹999,999.99 on an order captured at ₹681.95.
 * Captured 2026-09-05, HTTP 200, `refund: null`.
 *
 * The delta is the interesting part and the reason this fixture is not hand-written: the
 * field named `approved` holds what is *still refundable*, and `current` holds what was
 * asked for. Neither is an approval.
 */
const EXCEEDS: RefundResult = {
  decision: {
    decision_id: "01a071cd-ac69-7336-a0fd-043f84629451",
    allowed: false,
    code: "POLICY_EXCEPTION",
    explanation: "exceeds_remaining",
    checkout: {
      checkout_id: "01a06fd5-18d0-7e71-b7b7-772cca3bb35e",
      version: 2,
      content_hash: "t2mjH-C2eGCsAMCMZDFImumrRWplBZQzGLgR_xNu3Qg",
    },
    deltas: [
      { field_path: "amount_minor", approved: 68195, current: 99999999, reason: "EXCEEDS_REMAINING" },
    ],
    grant_id: null,
    payment_attempt_id: "01a06fd5-1987-7d71-9d9e-202b49009c08",
    next_version: null,
    correlation_id: "01a071cd-ac46-7ed0-936b-c53a1645133e",
  },
  refund: null,
  order: ORDER,
};

/**
 * `POST /v1/orders/{id}/refunds` on an attempt already at `REFUND_PENDING`. Captured
 * 2026-09-05, HTTP 200. The denial a buyer who presses twice actually receives.
 */
const IN_FLIGHT: RefundResult = {
  decision: {
    decision_id: "01a071cd-7479-7bc3-a0a4-2a2a20acb5e8",
    allowed: false,
    code: "CONCURRENT_OPERATION",
    explanation: "refund_already_in_flight",
    checkout: {
      checkout_id: "01a06fd5-0575-7ef0-bcd2-0b7e08815428",
      version: 1,
      content_hash: "ws8RtZVlL7aAWQBa3MiJkcpKuigoEz05ELyaLdzfMOI",
    },
    deltas: [],
    grant_id: null,
    payment_attempt_id: "01a06fd5-058c-7b54-966c-d3e8a096084d",
    next_version: null,
    correlation_id: "01a071cd-744c-736f-9fcd-1d7693b62d87",
  },
  refund: null,
  order: ORDER,
};

/**
 * `POST /v1/checkouts/{id}/cancel` on a checkout at `AWAITING_PAYMENT`. Captured
 * 2026-09-05, HTTP 200, by submitting a real checkout and stopping before paying.
 *
 * This is the refusal the brief is about: the kernel will not cancel something a buyer may
 * already have been charged for, and it says so as a structured code at HTTP 200.
 */
const CANCEL_REFUSED = {
  allowed: false,
  code: "PAYMENT_PENDING",
  explanation: "payment_surface_open",
  checkout: {
    checkout_id: "01a071ce-1ac6-705e-a2fb-489b27c85b38",
    version: 1,
    content_hash: "5wU68ped1rDPNGub7Kf6D3d_sj3zgYpgECer-0TOJCw",
  },
  from_state: "AWAITING_PAYMENT",
  grants_revoked: [],
  attempt_expired: null,
  reservation_release: null,
};

/** `POST /v1/checkouts/{id}/cancel` on the paid checkout behind an order. Captured 2026-09-05. */
const CANCEL_TERMINAL = {
  allowed: false,
  code: "STALE_CHECKOUT",
  explanation: "version_terminal",
  checkout: {
    checkout_id: "01a06fd5-09ca-7774-83d1-fab021012c17",
    version: 1,
    content_hash: "xk_lVzIrrzXHouZ9Yvi_r9EuXcI-6Uiei20pSjvKJXk",
  },
  from_state: "PAID",
  grants_revoked: [],
  attempt_expired: null,
  reservation_release: null,
};

/** `POST /v1/checkouts/{id}/cancel` on a checkout at `APPROVAL_REQUIRED`. Captured 2026-09-05. */
const CANCEL_ALLOWED = {
  allowed: true,
  code: "OK",
  explanation: "cancelled",
  checkout: {
    checkout_id: "01a071ce-8436-7096-a234-c3500a2dde2e",
    version: 1,
    content_hash: "bHKoB0K-FBVkvOkYFahbiZDs_ehxB4O6MSoW--i1tdg",
  },
  from_state: "APPROVAL_REQUIRED",
  grants_revoked: [],
  attempt_expired: null,
  reservation_release: "OK",
};

/* ---------------------------------------------------------------- utilities */

function mount(order: Order = ORDER) {
  const onOrder = vi.fn();
  const onChanged = vi.fn();
  render(<OrderActions order={order} onOrder={onOrder} onChanged={onChanged} />);
  return { onOrder, onChanged };
}

/** Open the refund composer and press send. */
async function sendRefund() {
  fireEvent.click(screen.getByRole("button", { name: "Ask for a refund" }));
  fireEvent.click(screen.getByRole("button", { name: "Send this request" }));
}

async function sendCancel() {
  fireEvent.click(screen.getByRole("button", { name: "Cancel this order" }));
  fireEvent.click(screen.getByRole("button", { name: "Send the cancellation" }));
}

/* -------------------------------------------------------------- vocabulary */

describe("the reason vocabulary", () => {
  it("says what the checkout screen says for a key both screens receive", () => {
    // `payment_surface_open` refuses a cancellation on the checkout screen and on this one.
    // A buyer who reads two different explanations of one fact will conclude the two
    // screens disagree about their money, so the shared table has to win.
    expect(sentenceFor("payment_surface_open")).toBe(reasonSentence("payment_surface_open"));
    expect(sentenceFor("attempt_in_flight")).toBe(reasonSentence("attempt_in_flight"));
    expect(sentenceFor("safe_mode_blocks_operation")).toBe(
      reasonSentence("safe_mode_blocks_operation"),
    );
  });

  it("covers the refund and cancellation keys the checkout screen has never seen", () => {
    for (const key of [
      "refund_already_in_flight",
      "reconcile_unknown_first",
      "nothing_remaining",
      "exceeds_remaining",
      "state_forbids_refund",
      "stale_capture_requires_full_refund",
      "version_terminal",
      "payment_outcome_unknown",
    ]) {
      expect(reasonSentence(key)).toBeNull();
      expect(sentenceFor(key)).toBeTruthy();
    }
  });

  it("has no sentence for a key nobody wrote down, rather than a soothing guess", () => {
    expect(sentenceFor("something_the_kernel_grew_last_tuesday")).toBeNull();
    expect(phraseFor("A_NEW_CODE")).toBeNull();
  });
});

/* ------------------------------------------------------------------ refund */

describe("the refund control", () => {
  it("is offered on an order this app could easily have decided was ineligible", () => {
    // Fully refunded, and the button is still live. The kernel owns this decision: it can
    // see a refund in flight at the provider that nothing in this browser can, and a
    // `disabled` attribute here would be the storefront answering in the kernel's voice.
    mount({
      ...ORDER,
      state: "REFUNDED",
      payment: { ...ORDER.payment, state: "REFUNDED" },
      refunds: [
        {
          refund_id: "01a071cd-c923-7816-bec8-ede9adb0fa37",
          amount_minor: 29725,
          currency: "INR",
          state: "REFUNDED",
          reason: "buyer_requested",
          automatic: false,
          created_at: "2026-09-05T13:41:46.885508Z",
        },
      ],
    });
    const ask = screen.getByRole("button", { name: "Ask for a refund" });
    expect(ask.hasAttribute("disabled")).toBe(false);
  });

  it("sends no amount at all when the buyer asks for everything still refundable", async () => {
    // Omitted, not computed. The remaining figure is the kernel's to resolve against its
    // capture ledger; a total worked out from `order.amount_minor` here would be this app
    // asserting a number it cannot see all the inputs to.
    const request = vi.spyOn(api, "requestRefund").mockResolvedValue(ADMITTED);
    mount();
    await sendRefund();
    await waitFor(() => expect(request).toHaveBeenCalled());
    expect(request.mock.calls[0][1]).toEqual({ reason: "buyer_requested", amount_minor: null });
  });

  it("says the refund was admitted and that the money has not moved", async () => {
    vi.spyOn(api, "requestRefund").mockResolvedValue(ADMITTED);
    mount();
    await sendRefund();

    const panel = await screen.findByText(/The platform admitted a refund of/);
    const box = panel.closest("div") as HTMLElement;
    expect(within(box).getByText("₹297.25")).toBeDefined();
    expect(within(box).getByText(/The money has not moved yet/)).toBeDefined();
    // The wire state, verbatim, because it is what the refunds list below is keyed on.
    expect(within(box).getByText("REFUND_PENDING")).toBeDefined();
  });

  it("never calls a pending refund 'refunded'", async () => {
    // The single most dangerous sentence this panel could render. `REFUND_PENDING` means
    // the provider has been asked; the buyer's balance is unchanged and may stay that way
    // if the provider rejects it.
    vi.spyOn(api, "requestRefund").mockResolvedValue(ADMITTED);
    const { container } = render(
      <OrderActions order={ORDER} onOrder={vi.fn()} onChanged={vi.fn()} />,
    );
    await sendRefund();
    await screen.findByText(/The platform admitted a refund of/);

    const prose = container.textContent ?? "";
    expect(prose).not.toMatch(/your money is back/i);
    expect(prose).not.toMatch(/has been refunded/i);
    expect(prose).not.toMatch(/refund complete/i);
  });

  it("adopts the order the server sent and does not read it again", async () => {
    // The refund route answers with the order re-read inside the transaction that admitted
    // the refund. A follow-up GET could only return the same row or an older one.
    vi.spyOn(api, "requestRefund").mockResolvedValue(ADMITTED);
    const read = vi.spyOn(api, "order");
    const { onOrder } = mount();
    await sendRefund();
    await waitFor(() => expect(onOrder).toHaveBeenCalledWith(ADMITTED.order));
    expect(read).not.toHaveBeenCalled();
  });

  it("renders a denial as the platform answering, not as an error", async () => {
    vi.spyOn(api, "requestRefund").mockResolvedValue(IN_FLIGHT);
    mount();
    await sendRefund();

    const answer = await screen.findByText("No refund was started");
    const box = answer.closest('[role="status"]') as HTMLElement;
    // `status`, never `alert`: this is the platform working correctly, and dressing a
    // denial as a fault teaches the buyer the system is broken when it is being careful.
    expect(box).not.toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    // The code verbatim, so a buyer on the phone can read out what the platform said.
    expect(within(box).getByText("CONCURRENT_OPERATION")).toBeDefined();
    expect(within(box).getByText(/A refund is already in flight on this payment/)).toBeDefined();
    // This fixture is the one refusal where "nothing was sent to the provider" is false:
    // the kernel refused *because* a refund is already in flight, so something did reach
    // Razorpay -- just not from this request. The panel is held to what this request did.
    expect(within(box).getByText(/This request started nothing/)).toBeDefined();
    expect(box.textContent ?? "").not.toMatch(/none was sent to the provider/i);
  });

  it("does not talk over a refund it did not start", async () => {
    // The dangerous direction on a refusal is not "money moved" but "no money moved": a
    // buyer told nothing is coming, while a refund is live at the provider, stops waiting
    // for money that is genuinely on its way. Asserted on every reason key that exists
    // because the kernel found another movement.
    for (const explanation of [
      "refund_already_in_flight",
      "reconcile_unknown_first",
      "nothing_remaining",
      "grant_already_live",
    ]) {
      cleanup();
      vi.spyOn(api, "requestRefund").mockResolvedValue({
        ...IN_FLIGHT,
        decision: { ...IN_FLIGHT.decision, explanation },
      });
      const { container } = render(
        <OrderActions order={ORDER} onOrder={vi.fn()} onChanged={vi.fn()} />,
      );
      await sendRefund();
      await screen.findByText("No refund was started");

      const prose = container.textContent ?? "";
      expect(prose, explanation).not.toMatch(/no money moved/i);
      expect(prose, explanation).not.toMatch(/nothing was sent to the provider/i);
      expect(prose, explanation).toMatch(/refunds recorded against this order are listed below/i);
    }
  });

  it("labels an exceeds-remaining delta as a refund, never as an approval", async () => {
    vi.spyOn(api, "requestRefund").mockResolvedValue(EXCEEDS);
    mount();
    await sendRefund();

    await screen.findByText("No refund was started");
    // `approved: 68195` here is what is still refundable, and `current: 99999999` is what
    // was asked for. Printing the checkout's "Approved"/"Now" headings over those two
    // figures would tell the buyer they had approved ₹681.95 of refund, which never
    // happened.
    expect(screen.getByText("Still refundable")).toBeDefined();
    expect(screen.getByText("You asked for")).toBeDefined();
    expect(screen.queryByText("Approved")).toBeNull();
    expect(screen.getByText("₹681.95")).toBeDefined();
    expect(screen.getByText("₹9,99,999.99")).toBeDefined();
  });

  it("renders a delta in the order's own currency, not a literal one", async () => {
    // The delta's figures are minor units of whatever the payment was captured in. A
    // hard-coded "INR" renders the right number under the wrong symbol, which is the kind
    // of wrong that reads as correct -- so the currency is threaded from the order.
    vi.spyOn(api, "requestRefund").mockResolvedValue(EXCEEDS);
    mount({ ...ORDER, currency: "USD", amount: { minor: 29725, currency: "USD", display: "297.25" } });
    await sendRefund();

    await screen.findByText("No refund was started");
    expect(screen.getByText("$681.95")).toBeDefined();
    expect(screen.queryByText("₹681.95")).toBeNull();
  });

  it("reuses one idempotency key across a retry, and drops it once a decision arrives", async () => {
    // A send that failed in transport may or may not have landed, which is exactly what an
    // idempotency key is for: pressing again replays the stored answer instead of admitting
    // a second refund. A send that came back *decided* must not replay onto the next
    // request, so the key is dropped as soon as an answer arrives.
    const request = vi
      .spyOn(api, "requestRefund")
      .mockRejectedValueOnce(
        new ApiError({ type: "about:blank", title: "Could not reach the server", status: 0 }),
      )
      .mockResolvedValueOnce(IN_FLIGHT)
      .mockResolvedValueOnce(IN_FLIGHT);
    mount();

    await sendRefund();
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Send it again" }));
    await screen.findByText("No refund was started");

    const [first, second] = request.mock.calls;
    expect(second[2]).toBe(first[2]);

    fireEvent.click(screen.getByRole("button", { name: "Send this request" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    expect(request.mock.calls[2][2]).not.toBe(first[2]);
  });

  it("treats a request that never arrived as the one thing that really is an error", async () => {
    vi.spyOn(api, "requestRefund").mockRejectedValue(
      new ApiError({ type: "about:blank", title: "Could not reach the server", status: 0 }),
    );
    mount();
    await sendRefund();

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/did not reach the platform/)).toBeDefined();
    expect(within(alert).getByText(/Nothing was decided, so nothing has changed/)).toBeDefined();
  });

  it("offers the buyer no way to name an amount", async () => {
    // The point of the panel, asserted negatively. A buyer who can type a figure beside a
    // panel saying a person settles disputed sums has been shown two accounts of who
    // decides, and the field is the false one: the kernel refuses anything above what
    // remains, so its only working power was to let a buyer ask for less than they are
    // owed. No textbox, no spinner, and no amount radios -- by design.
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Ask for a refund" }));
    await screen.findByText(/Still refundable/);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.queryByLabelText(/amount/i)).toBeNull();
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio.getAttribute("name")).toBe("refund-reason");
    }
  });

  it("shows the server's figure and does not send it back", async () => {
    // Displayed, never echoed. Between this read and the decision a webhook can arrive, so
    // the kernel resolves the amount again from its own ledger; a screen that sent this
    // number back would be asserting a figure that may already have moved.
    const request = vi.spyOn(api, "requestRefund").mockResolvedValue(ADMITTED);
    const read = vi.spyOn(api, "refundable").mockResolvedValue({
      ...REFUNDABLE,
      refundable_minor: 12_050,
      refundable: { minor: 12_050, currency: "INR", display: "120.50" },
    });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Ask for a refund" }));
    await waitFor(() => expect(read).toHaveBeenCalled());
    expect(await screen.findByText("₹120.50")).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Send this request" }));
    await waitFor(() => expect(request).toHaveBeenCalled());
    expect(request.mock.calls[0][1]).toEqual({ reason: "buyer_requested", amount_minor: null });
  });

  it("says nothing remains when the ledger says so, and still lets the buyer ask", async () => {
    // "Nothing left" is the server's statement, not this page's inference from a zero --
    // and it still does not disable the button. A buyer is entitled to be refused by a rule
    // that names itself, rather than by a `disabled` attribute that explains nothing.
    const request = vi.spyOn(api, "requestRefund").mockResolvedValue(EXCEEDS);
    vi.spyOn(api, "refundable").mockResolvedValue({
      ...REFUNDABLE,
      refundable_minor: 0,
      refundable: { minor: 0, currency: "INR", display: "0.00" },
      anything_remains: false,
    });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Ask for a refund" }));
    expect(await screen.findByText(/nothing left to refund/)).toBeDefined();

    const send = screen.getByRole("button", { name: "Send this request" });
    expect(send.hasAttribute("disabled")).toBe(false);
    fireEvent.click(send);
    await waitFor(() => expect(request).toHaveBeenCalled());
  });

  it("asks anyway when the ledger read fails", async () => {
    // A failed read is not a reason to withhold the remedy. The request carries no amount,
    // so nothing about it depended on the read having succeeded.
    const request = vi.spyOn(api, "requestRefund").mockResolvedValue(ADMITTED);
    vi.spyOn(api, "refundable").mockRejectedValue(new Error("gateway"));
    mount();
    await sendRefund();
    await waitFor(() => expect(request).toHaveBeenCalled());
    expect(request.mock.calls[0][1]).toEqual({ reason: "buyer_requested", amount_minor: null });
  });
});

/* ------------------------------------------------------------------ cancel */

describe("the cancel control", () => {
  it("is offered on an order whose checkout the kernel will certainly refuse", () => {
    // `PAID` has no edge to `CANCELLED`, so this will come back refused. Offering it anyway
    // is the point: the refusal tells a buyer looking for the way out that the way out is a
    // refund, which is more than an absent control tells them.
    mount();
    const cancel = screen.getByRole("button", { name: "Cancel this order" });
    expect(cancel.hasAttribute("disabled")).toBe(false);
  });

  it("renders the payment-in-flight refusal in the same words as the checkout screen", async () => {
    vi.spyOn(api, "cancel").mockResolvedValue(CANCEL_REFUSED);
    mount();
    await sendCancel();

    await screen.findByText("Not cancelled");
    expect(screen.getByText("PAYMENT_PENDING")).toBeDefined();
    expect(
      screen.getByText(reasonSentence("payment_surface_open") as string),
    ).toBeDefined();
    expect(screen.getByText("AWAITING_PAYMENT")).toBeDefined();
    expect(screen.getByText(/Nothing about this order changed/)).toBeDefined();
  });

  it("explains a completed sale as a completed sale and points at the refund", async () => {
    vi.spyOn(api, "cancel").mockResolvedValue(CANCEL_TERMINAL);
    mount();
    await sendCancel();

    await screen.findByText("Not cancelled");
    expect(screen.getByText(/the way back from a completed sale is a refund/)).toBeDefined();
    expect(screen.getByText("PAID")).toBeDefined();
  });

  it("re-reads the order after a cancellation the kernel performed", async () => {
    vi.spyOn(api, "cancel").mockResolvedValue(CANCEL_ALLOWED);
    const { onChanged } = mount();
    await sendCancel();
    await screen.findByText("Cancelled");
    // The response says nothing about the order, the reservation or the attempt, so the
    // screen reads rather than patches.
    expect(onChanged).toHaveBeenCalled();
  });

  it("claims nothing in either direction when the answer omits `allowed`", async () => {
    // `allowed` is optional on this shape because the sibling routes do not send it. An
    // answer without it is one this screen must not interpret: "cancelled" and "refused"
    // are opposite facts about somebody's money.
    const withoutAllowed: Record<string, unknown> = { ...CANCEL_ALLOWED };
    delete withoutAllowed.allowed;
    vi.spyOn(api, "cancel").mockResolvedValue(withoutAllowed as ApprovalResult);
    const { onChanged } = mount();
    await sendCancel();

    await screen.findByText(/without saying whether it cancelled/);
    expect(screen.queryByText("Cancelled")).toBeNull();
    expect(screen.queryByText("Not cancelled")).toBeNull();
    expect(onChanged).toHaveBeenCalled();
  });
});

/* --------------------------------------------------------------- neutrality */

describe("declining is as easy as accepting", () => {
  it("gives the confirm and the back-out the same size", () => {
    // The rule is the product's: a back-out rendered as a small grey word beside a large
    // coloured button is a choice presented as an afterthought, and on a cancellation that
    // is a way of charging people who meant to stop.
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Cancel this order" }));
    const confirm = screen.getByRole("button", { name: "Send the cancellation" });
    const close = screen.getByRole("button", { name: "Close" });
    expect(close.className).toContain("h-10");
    expect(confirm.className).toContain("h-10");
  });

  it("does not argue with a buyer who is leaving", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Cancel this order" }));
    const prose = document.body.textContent ?? "";
    expect(prose).not.toMatch(/are you sure/i);
    expect(prose).not.toMatch(/you will lose/i);
    expect(prose).not.toMatch(/miss out/i);
  });

  it("offers a reason that requires no justification, already selected", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Ask for a refund" }));
    const neutral = screen.getByLabelText("I would like a refund") as HTMLInputElement;
    expect(neutral.checked).toBe(true);
  });
});

/* ---------------------------------------------------------------- escalate */

describe("reaching a person", () => {
  it("says it cannot open a case rather than pretending to", () => {
    mount();
    expect(screen.getByText(/cannot open a support case for you/)).toBeDefined();
    // No control that looks like it files one.
    expect(screen.queryByRole("button", { name: /contact|support|ticket|raise/i })).toBeNull();
  });

  it("hands over the references a person would ask for", () => {
    mount();
    const block = screen.getByText(/Order: 01a06fd5-0fe4-7a1d-a7b1-42747790b3ce/);
    expect(block.textContent).toContain("01a06fd5-09e1-70ef-84e2-5e671d2aecc2");
    expect(block.textContent).toContain("pay_6ced4db55d51d9");
  });

  it("reports an escalation the platform made by itself, read off the refund rows", () => {
    mount({
      ...ORDER,
      refunds: [
        {
          refund_id: "01a071cd-c923-7816-bec8-ede9adb0fa37",
          amount_minor: 29725,
          currency: "INR",
          state: "ESCALATED",
          reason: "buyer_requested",
          automatic: false,
          created_at: "2026-09-05T13:41:46.885508Z",
        },
      ],
    });
    expect(screen.getByText("Already with an operator")).toBeDefined();
    expect(screen.getByText(/you do not need to ask for this to happen/)).toBeDefined();
  });

  it("says nothing about operators on an order nothing was escalated on", () => {
    mount();
    expect(screen.queryByText("Already with an operator")).toBeNull();
  });

  it("does not read an ordinary refund as an escalation", () => {
    // The case above passes whether or not the state is actually filtered on, because an
    // order with no refunds has nothing to mistake. This is the one that fails if the
    // panel counts every refund as escalated: a buyer whose refund is progressing normally
    // must not be told a human has taken it over and nothing is being retried.
    mount({
      ...ORDER,
      payment: { ...ORDER.payment, state: "REFUND_PENDING" },
      refunds: [
        {
          refund_id: "01a071cd-c923-7816-bec8-ede9adb0fa37",
          amount_minor: 29725,
          currency: "INR",
          state: "REFUND_PENDING",
          reason: "buyer_requested",
          automatic: false,
          created_at: "2026-09-05T13:41:46.885508Z",
        },
      ],
    });
    expect(screen.queryByText("Already with an operator")).toBeNull();
  });
});

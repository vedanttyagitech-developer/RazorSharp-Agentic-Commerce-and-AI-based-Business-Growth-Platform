/**
 * One rule, on the two screens that break it most visibly: an order is named by its
 * reference, never by its UUID.
 *
 * `order_reference` renders `orders.id` as `RS-260908-K7M4QX2` and is derived on the
 * server precisely so no client ever computes a second version of an order's name. Both
 * payloads have carried `reference` for some time; both screens went on printing the raw
 * id anyway, which is the shape of defect this codebase keeps producing -- a field that
 * arrives, is never read, and is invisible because what renders instead is not obviously
 * wrong. A UUID on a confirmation screen looks like a design decision.
 *
 * What each test pins is the *hierarchy*, not the presence: the id is still on both
 * screens, because it is what every table joins on and this is a platform whose screens
 * show their evidence. It is no longer the thing called the order.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Order, OrderSummary } from "@/lib/api/types";

import { OrderBody } from "./order-detail";
import { OrderRow } from "./order-list";

const ORDER_ID = "01a0786d-30ce-7e03-a33e-351594c47565";
const REFERENCE = "RS-260908-K7M4QX2";

function summary(overrides: Partial<OrderSummary> = {}): OrderSummary {
  return {
    order_id: ORDER_ID,
    reference: REFERENCE,
    checkout_id: "01a0786d-0000-7e03-a33e-351594c47565",
    version: 1,
    payment_attempt_id: "01a0786d-1111-7e03-a33e-351594c47565",
    policy_receipt_hash: "policyExample",
    state: "CONFIRMED",
    amount_minor: 31500,
    currency: "INR",
    amount: { minor: 31500, currency: "INR", display: "₹315.00" },
    capture_evidence: null,
    razorpay_order_id: null,
    razorpay_payment_id: null,
    refunded_minor: 0,
    refund_count: 0,
    created_at: "2026-09-08T15:23:47.000Z",
    age_seconds: 120,
    ...overrides,
  } as OrderSummary;
}

function order(overrides: Partial<Order> = {}): Order {
  return {
    order_id: ORDER_ID,
    reference: REFERENCE,
    checkout_id: "01a0786d-0000-7e03-a33e-351594c47565",
    version: 1,
    content_hash: "contentExample",
    policy_receipt_hash: "policyExample",
    state: "CONFIRMED",
    amount_minor: 31500,
    currency: "INR",
    amount: { minor: 31500, currency: "INR", display: "₹315.00" },
    quote: null,
    payment: {
      attempt_id: "01a0786d-1111-7e03-a33e-351594c47565",
      version: 1,
      state: "CAPTURED",
      razorpay_key_id: null,
      razorpay_order_id: null,
      razorpay_payment_id: null,
      grant_id: null,
      capture_evidence: null,
      reconciliation_attempts: 0,
    },
    refunds: [],
    created_at: "2026-09-08T15:23:47.000Z",
    ...overrides,
  } as Order;
}

function body(o: Order) {
  return (
    <OrderBody order={o} onOrder={() => {}} onChanged={() => {}} rereadFailed={null} />
  );
}

afterEach(cleanup);

describe("the order's name on the confirmation screen", () => {
  it("is the reference, with the id kept as evidence beneath it", () => {
    render(body(order()));

    expect(screen.getByText(REFERENCE)).toBeDefined();
    expect(screen.getByText(ORDER_ID)).toBeDefined();
  });

  it("falls back to the id when an API has not sent a reference", () => {
    render(body(order({ reference: undefined })));

    expect(screen.getByText(ORDER_ID)).toBeDefined();
    expect(screen.queryAllByText(ORDER_ID)).toHaveLength(1);
  });
});

describe("the order's name in the list", () => {
  it("is the reference, not a truncated UUID", () => {
    render(<OrderRow order={summary()} />);

    expect(screen.getByText(REFERENCE)).toBeDefined();
    expect(screen.queryByText(/01a0786d…7565/)).toBeNull();
  });

  it("is what a screen reader is given, so the row can be said out loud", () => {
    render(<OrderRow order={summary()} />);

    const label = screen.getByRole("link").getAttribute("aria-label") ?? "";
    expect(label).toContain(REFERENCE);
    expect(label).not.toContain(ORDER_ID);
  });

  it("keeps the whole id reachable, because that is what rows join on", () => {
    render(<OrderRow order={summary()} />);

    expect(screen.getByTitle(ORDER_ID)).toBeDefined();
  });
});

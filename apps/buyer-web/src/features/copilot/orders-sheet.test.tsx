/**
 * The Return control appears for the sale that was sold with returns, and for no other.
 *
 * `return_offered` is resolved on the server from the Policy-at-Sale Receipt, so what this
 * file pins is the half that can drift without anybody noticing: that the copilot obeys it,
 * per order, rather than showing one control for a whole list. Two orders side by side with
 * different answers is the case that matters — it is what the receipt promises and what a
 * merchant changing their mind actually produces.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OrderSummary } from "@/lib/api/types";

const orders = vi.fn();
vi.mock("@/lib/api/client", () => ({
  api: {
    orders: (...args: unknown[]) => orders(...args),
  },
}));

const { OrdersSheet } = await import("./orders-sheet");

function order(overrides: Partial<OrderSummary>): OrderSummary {
  return {
    order_id: "01a0786d-30ce-7e03-a33e-351594c47565",
    reference: "RS-260908-SOLDNEW",
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
    return_offered: false,
    return_closes_at: null,
    ...overrides,
  } as OrderSummary;
}

function sheet() {
  return <OrdersSheet open onClose={() => {}} onAsk={() => {}} />;
}

afterEach(() => {
  cleanup();
  orders.mockReset();
});

describe("the Return control", () => {
  it("is offered on a sale whose receipt offered returns", async () => {
    orders.mockResolvedValue({
      orders: [order({ return_offered: true, return_closes_at: "2026-09-15T15:23:47.000Z" })],
    });
    render(sheet());

    await waitFor(() => expect(screen.getByText("Return an item")).toBeDefined());
  });

  it("is absent on a sale whose receipt did not", async () => {
    orders.mockResolvedValue({ orders: [order({ return_offered: false })] });
    render(sheet());

    await waitFor(() => expect(screen.getByText("Track it")).toBeDefined());
    expect(screen.queryByText("Return an item")).toBeNull();
  });

  it("answers per order, because each one carries its own frozen terms", async () => {
    orders.mockResolvedValue({
      orders: [
        order({ reference: "RS-260908-SOLDNEW", return_offered: true }),
        order({
          order_id: "01a0786d-9999-7e03-a33e-351594c47565",
          reference: "RS-260906-SOLDOLD",
          return_offered: false,
        }),
      ],
    });
    const { container } = render(sheet());

    await waitFor(() => expect(screen.getByText("RS-260908-SOLDNEW")).toBeDefined());
    const rows = container.querySelectorAll("li");
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).queryByText("Return an item")).not.toBeNull();
    expect(
      within(rows[1] as HTMLElement).queryByText("Return an item"),
      "an order sold before returns were offered must not grow the control from its neighbour",
    ).toBeNull();
  });
});

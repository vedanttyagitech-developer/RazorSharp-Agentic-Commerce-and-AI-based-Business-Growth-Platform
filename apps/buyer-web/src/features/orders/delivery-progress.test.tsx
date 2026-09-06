/**
 * The delivery strip's one load-bearing property: it never claims a stage it was not told.
 *
 * The visual is incidental. What these tests protect is that "reached" always has a server
 * field behind it and that the three stages this platform cannot report stay marked as
 * unreported -- because the moment one of them renders as done, the strip is a fixture and
 * every other figure on the page inherits the doubt.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Order } from "@/lib/api/types";

import { DeliveryProgress, stagesFor } from "./delivery-progress";

function orderIn(state: string): Order {
  return {
    order_id: "01a0722b-2ed0-7962-8e0d-9a775e8f3de0",
    reference: "RS-260905-TESTREF",
    checkout_id: "01a0722b-0000-7962-8e0d-9a775e8f3de0",
    version: 1,
    content_hash: "wXRkaeUUBGAExample",
    policy_receipt_hash: "policyExample",
    state,
    amount_minor: 5750,
    currency: "INR",
    amount: { minor: 5750, currency: "INR" },
    quote: null,
    payment: {
      state: "CAPTURED",
      provider: "razorpay",
      razorpay_key_id: null,
      razorpay_order_id: "order_FIXTURE0000002",
      amount_minor: 5750,
      currency: "INR",
      merchant_name: "Blinkit",
      description: "Order",
    },
    refunds: [],
    created_at: "2026-09-05T15:23:47.000Z",
  } as unknown as Order;
}

describe("stagesFor", () => {
  it("marks a stage reached only when a real field backs it", () => {
    const stages = stagesFor(orderIn("CONFIRMED"));
    const reached = stages.filter((stage) => stage.status === "reached");
    expect(reached.map((stage) => stage.key)).toEqual(["placed", "paid"]);
  });

  it("carries the server's own timestamp on placed, and invents none elsewhere", () => {
    const stages = stagesFor(orderIn("CONFIRMED"));
    expect(stages[0].at).toBe("2026-09-05T15:23:47.000Z");
    // Every other stage has no timestamp at all rather than a plausible one.
    expect(stages.slice(1).every((stage) => stage.at === null)).toBe(true);
  });

  it("never reports packing, travel or arrival as done", () => {
    for (const state of ["CONFIRMED", "FULFILMENT_BLOCKED", "CANCELLED", "REFUNDED"]) {
      const stages = stagesFor(orderIn(state));
      for (const key of ["packed", "on-its-way", "delivered"]) {
        const stage = stages.find((candidate) => candidate.key === key);
        expect(stage?.status, `${key} on ${state}`).not.toBe("reached");
      }
    }
  });

  it("ends the track rather than advancing it on a withdrawn sale", () => {
    const stages = stagesFor(orderIn("CANCELLED"));
    expect(stages.find((stage) => stage.key === "paid")?.status).toBe("ended");
    expect(stages.find((stage) => stage.key === "on-its-way")?.status).toBe("ended");
  });

  it("says why the store has not packed when fulfilment is held", () => {
    const stages = stagesFor(orderIn("FULFILMENT_BLOCKED"));
    expect(stages.find((stage) => stage.key === "packed")?.note).toMatch(/operator decision/);
  });
});

describe("DeliveryProgress", () => {
  afterEach(cleanup);

  it("draws the full quick-commerce track", () => {
    render(<DeliveryProgress order={orderIn("CONFIRMED")} />);
    for (const label of [
      "Order placed",
      "Payment confirmed",
      "Packed at the store",
      "On its way",
      "Delivered",
    ]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("labels the unreported stages instead of estimating them", () => {
    const { container } = render(<DeliveryProgress order={orderIn("CONFIRMED")} />);
    // Scoped to the track: the footer legend also uses the words "Not reported" to explain
    // what the label means, and counting that would make this assertion pass for the wrong
    // reason -- three stages carry it, and the fourth occurrence is prose.
    const track = container.querySelector("ol");
    expect(track).not.toBeNull();
    expect(within(track as HTMLElement).getAllByText("Not reported")).toHaveLength(3);
    expect(screen.getByText(/does not invent a position it was never told/)).toBeTruthy();
  });

  it("counts only the stages it can actually report", () => {
    const { container } = render(<DeliveryProgress order={orderIn("CONFIRMED")} />);
    expect(container.textContent).toMatch(/2\s*of\s*5\s*stages reported/);
  });

  it("shows no arrival estimate anywhere", () => {
    const { container } = render(<DeliveryProgress order={orderIn("CONFIRMED")} />);
    // An ETA is the specific invention this component exists to avoid.
    expect(container.textContent).not.toMatch(/\bmin(ute)?s?\b|\bETA\b|arriving/i);
  });

  it("says the sale ended on a refunded order rather than showing it in transit", () => {
    const { container } = render(<DeliveryProgress order={orderIn("REFUNDED")} />);
    expect(container.textContent).toMatch(/settled back, so this sale is closed rather than in transit/);
  });
});

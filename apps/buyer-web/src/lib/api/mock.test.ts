import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createMockClient, diffContent, mockSignature } from "./mock";
import type { ApprovalCard } from "./types";

beforeEach(() => {
  window.sessionStorage.clear();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("mock journey", () => {
  it("walks discovery -> quote -> approval -> denial with delta -> N+1 -> exactly-once payment -> verified capture", async () => {
    const client = createMockClient();

    // 1. Multilingual grounded discovery, availability reported honestly.
    const hindi = await client.search({ q: "दूध" });
    const hinglish = await client.search({ q: "doodh" });
    expect(hindi.hits.map((hit) => hit.sku)).toEqual(hinglish.hits.map((hit) => hit.sku));
    expect(hindi.hits[0].freshness.source).toContain("merchant-sim");
    const paneer = (await client.search({ q: "paneer" })).hits[0];
    expect(paneer.is_listed).toBe(true);
    expect(paneer.stock_units).toBe(0);
    expect(paneer.is_available).toBe(false);
    const chocos = (await client.search({ q: "chocos" })).hits[0];
    expect(chocos.is_listed).toBe(false);

    // 2. Basket and deterministic quote (half-up tax per line, Rs 25 delivery under Rs 499).
    const basket = await client.createBasket();
    await client.setBasketLine(basket.basket_id, "GRO-DAIRY-001", 2);
    const quoted = await client.setBasketLine(basket.basket_id, "GRO-DAIRY-003", 1);
    expect(quoted.quote).not.toBeNull();
    const quote = quoted.quote!;
    expect(quote.lines.find((line) => line.sku === "GRO-DAIRY-003")?.tax_minor).toBe(225);
    expect(quote.items_subtotal_minor).toBe(5600 + 4500);
    expect(quote.delivery_fee_minor).toBe(2500);
    expect(quote.delivery_tax_minor).toBe(450);
    expect(quote.total_minor).toBe(10100 + 225 + 2500 + 450);
    expect(quote.gap_to_free_delivery_minor).toBe(49900 - 10100);
    expect(quote.content_hash).toMatch(/^[0-9a-f]{64}$/);

    // 3. Checkout: version 1, receipt, reservation, approval card.
    const checkout = await client.checkoutBasket(basket.basket_id);
    expect(checkout.state).toBe("APPROVAL_REQUIRED");
    const card1 = checkout.approval_card as ApprovalCard;
    expect(card1.version).toBe(1);
    expect(card1.amount_minor).toBe(quote.total_minor);
    expect(card1.reservation?.state).toBe("ACTIVE");

    // 4. Trusted approval echoes exactly; a tampered echo is refused.
    await expect(client.approveVersion(checkout.checkout_id, 1, { content_hash: card1.content_hash, amount_minor: card1.amount_minor - 1, currency: "INR" })).rejects.toMatchObject({ status: 422, code: "AUTHORITY_INSUFFICIENT" });
    const approved = await client.approveVersion(checkout.checkout_id, 1, { content_hash: card1.content_hash, amount_minor: card1.amount_minor, currency: card1.currency });
    expect(approved.checkout.state).toBe("APPROVED");

    // 5-7. Merchant state changes underneath; kernel refuses version 1 with the exact delta.
    const denied = await client.submitVersion(checkout.checkout_id, 1);
    expect(denied.outcome).toBe("REAPPROVAL_REQUIRED");
    expect(denied.decision.allowed).toBe(false);
    expect(denied.decision.next_version).toBe(2);
    const paths = denied.decision.deltas.map((delta) => delta.field_path);
    expect(paths).toContain("lines[0].unit_price_minor");
    expect(paths).toContain("delivery_fee_minor");
    expect(paths).toContain("total_minor");
    expect(denied.checkout.versions[0].state).toBe("INVALIDATED");
    const card2 = denied.checkout.approval_card as ApprovalCard;
    expect(card2.version).toBe(2);
    expect(card2.previous_version).toBe(1);
    expect(card2.content_hash).not.toBe(card1.content_hash);
    expect(card2.amount_minor).toBe(card1.amount_minor + 2 * 1000 + 2000 + 360);

    // Version 1 can never be approved again.
    await expect(client.approveVersion(checkout.checkout_id, 1, { content_hash: card1.content_hash, amount_minor: card1.amount_minor, currency: "INR" })).rejects.toMatchObject({ status: 409, code: "REAPPROVAL_REQUIRED" });

    // 8-9. Fresh approval on N+1, admitted exactly once with one grant and one order.
    await client.approveVersion(checkout.checkout_id, 2, { content_hash: card2.content_hash, amount_minor: card2.amount_minor, currency: card2.currency });
    const admitted = await client.submitVersion(checkout.checkout_id, 2);
    expect(admitted.outcome).toBe("OK");
    expect(admitted.decision.grant_id).toMatch(/^grt_/);
    expect(admitted.checkout.state).toBe("AWAITING_PAYMENT");
    expect(admitted.checkout.attempt?.razorpay_order_id).toMatch(/^order_MOCK/);

    const duplicate = await client.submitVersion(checkout.checkout_id, 2);
    expect(duplicate.outcome).toBe("DUPLICATE_OPERATION");
    expect(duplicate.attempt_id).toBe(admitted.attempt_id);

    const handoff = await client.getPaymentHandoff(checkout.checkout_id);
    expect(handoff.razorpay_key_id).toMatch(/^rzp_test_/);
    expect(handoff.amount_minor).toBe(card2.amount_minor);

    // Browser callback is recorded, never treated as capture.
    const orderId = handoff.razorpay_order_id!;
    await expect(client.verifyPayment({ checkout_id: checkout.checkout_id, razorpay_order_id: orderId, razorpay_payment_id: "pay_X", razorpay_signature: "bad" })).rejects.toMatchObject({ status: 422 });
    const verification = await client.verifyPayment({ checkout_id: checkout.checkout_id, razorpay_order_id: orderId, razorpay_payment_id: "pay_X", razorpay_signature: mockSignature(orderId, "pay_X") });
    expect(verification.evidence_kind).toBe("BROWSER_CALLBACK");
    expect(verification.state).toBe("SUBMITTED");
    expect((await client.getCheckout(checkout.checkout_id)).state).toBe("AWAITING_PAYMENT");

    // Provider evidence arrives: authorized, then captured; order confirmed; replay ignored.
    await vi.advanceTimersByTimeAsync(1200);
    expect((await client.getCheckout(checkout.checkout_id)).attempt?.state).toBe("AUTHORIZED");
    await vi.advanceTimersByTimeAsync(1200);
    const paid = await client.getCheckout(checkout.checkout_id);
    expect(paid.state).toBe("PAID");
    expect(paid.attempt?.state).toBe("CAPTURED");
    expect(paid.attempt?.capture_evidence?.kind).toBe("WEBHOOK");
    expect(paid.order_id).toMatch(/^ord_/);
    await vi.advanceTimersByTimeAsync(1200);
    const timeline = await client.getTimeline(checkout.checkout_id);
    expect(timeline.rows.filter((row) => row.scenario_injection)).toHaveLength(2);
    expect(timeline.rows.some((row) => row.action === "WEBHOOK_DUPLICATE_IGNORED")).toBe(true);

    // 10. Proof chain verifies end to end.
    const proof = await client.getProof(checkout.checkout_id);
    expect(proof.links).toHaveLength(10);
    expect(proof.verdict.ok).toBe(true);

    const order = await client.getOrder(paid.order_id!);
    expect(order.amount_minor).toBe(card2.amount_minor);
    expect(order.content_hash).toBe(card2.content_hash);
  });

  it("resumes the event stream from Last-Event-ID", async () => {
    const client = createMockClient();
    const basket = await client.createBasket();
    await client.setBasketLine(basket.basket_id, "GRO-BAKE-002", 1);
    const checkout = await client.checkoutBasket(basket.basket_id);
    const all: string[] = [];
    const stop = client.subscribeEvents(checkout.checkout_id, { onEvent: (event) => all.push(event.event_id) });
    await vi.advanceTimersByTimeAsync(1);
    stop();
    expect(all.length).toBeGreaterThan(2);
    const resumed: string[] = [];
    const stopResumed = client.subscribeEvents(checkout.checkout_id, { lastEventId: all[1], onEvent: (event) => resumed.push(event.event_id) });
    await vi.advanceTimersByTimeAsync(1);
    stopResumed();
    expect(resumed).toEqual(all.slice(2));
  });
});

describe("diffContent", () => {
  it("emits one delta per changed leaf with a stable reason key", () => {
    const deltas = diffContent({ lines: [{ sku: "a", unit_price_minor: 1 }], total_minor: 1 }, { lines: [{ sku: "a", unit_price_minor: 2 }], total_minor: 2 });
    expect(deltas).toEqual([
      { field_path: "lines[0].unit_price_minor", approved: 1, current: 2, reason: "PRICE_CHANGED" },
      { field_path: "total_minor", approved: 1, current: 2, reason: "TOTAL_CHANGED" },
    ]);
  });
});

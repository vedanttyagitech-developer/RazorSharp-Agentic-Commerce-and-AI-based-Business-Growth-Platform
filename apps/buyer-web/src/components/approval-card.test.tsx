import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApprovalCard as ApprovalCardData, ApprovalEcho } from "@/lib/api/types";

import { ApprovalCard, DeltaView } from "./approval-card";

afterEach(cleanup);

const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

function card(overrides: Partial<ApprovalCardData> = {}): ApprovalCardData {
  return {
    checkout_id: "chk_0001",
    version: 1,
    content_hash: HASH_A,
    policy_receipt_id: "psr_0001",
    policy_receipt_hash: "c".repeat(64),
    amount_minor: 34000,
    currency: "INR",
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    reservation: { reservation_id: "rsv_0001", state: "ACTIVE", expires_at: new Date(Date.now() + 900_000).toISOString() },
    quote: {
      currency: "INR",
      lines: [{ sku: "GRO-DAIRY-001", name: "Milk", quantity: 2, unit_price_minor: 2800, subtotal_minor: 5600, tax_bp: 0, tax_minor: 0 }],
      items_subtotal_minor: 5600,
      items_tax_minor: 0,
      delivery_fee_minor: 2500,
      delivery_tax_minor: 450,
      total_minor: 34000,
      free_delivery_applied: false,
      gap_to_free_delivery_minor: 44300,
      source: "merchant-sim:demo-grocery/v1",
      catalogue_revision: 3,
      content_hash: HASH_A,
    },
    previous_version: null,
    deltas: [],
    ...overrides,
  };
}

describe("ApprovalCard", () => {
  it("submits exactly the content hash, amount and currency it displayed, and nothing else", () => {
    const onApprove = vi.fn<(echo: ApprovalEcho) => void>();
    render(<ApprovalCard card={card()} onApprove={onApprove} />);

    expect(screen.getByTestId("approval-content-hash").textContent).toContain(HASH_A.slice(0, 8));
    expect(screen.getByTestId("approval-total").textContent).toContain("340.00");
    expect(screen.getByTestId("approval-currency").textContent).toBe("INR");
    expect(screen.getByTestId("approval-version").textContent).toBe("1");
    expect(screen.getByTestId("approval-checkout-id").textContent).toBe("chk_0001");
    expect(screen.getByTestId("approval-receipt-hash").textContent).toContain("cccccccc");

    fireEvent.click(screen.getByTestId("approve-button"));

    expect(onApprove).toHaveBeenCalledTimes(1);
    const echo = onApprove.mock.calls[0][0];
    expect(echo).toEqual({ content_hash: HASH_A, amount_minor: 34000, currency: "INR" });
    expect(Object.keys(echo).sort()).toEqual(["amount_minor", "content_hash", "currency"]);
  });

  it("echoes the newer version after the card is replaced, never a stale hash", () => {
    const onApprove = vi.fn<(echo: ApprovalEcho) => void>();
    const { rerender } = render(<ApprovalCard card={card()} onApprove={onApprove} />);
    rerender(<ApprovalCard card={card({ version: 2, content_hash: HASH_B, amount_minor: 39500, previous_version: 1 })} onApprove={onApprove} />);

    expect(screen.getByTestId("approval-total").textContent).toContain("395.00");
    fireEvent.click(screen.getByTestId("approve-button"));
    expect(onApprove.mock.calls[0][0]).toEqual({ content_hash: HASH_B, amount_minor: 39500, currency: "INR" });
  });

  it("renders as evidence without an approve control once approved", () => {
    render(
      <ApprovalCard
        card={card()}
        onApprove={() => undefined}
        approved={{ approval_id: "apr_0001", version: 1, content_hash: HASH_A, policy_receipt_hash: "c".repeat(64), amount_minor: 34000, currency: "INR", approved_at: new Date().toISOString(), expires_at: new Date().toISOString(), authority_epoch: 1 }}
      />,
    );
    expect(screen.queryByTestId("approve-button")).toBeNull();
    expect(screen.getByText("Approval recorded")).toBeTruthy();
    expect(screen.getByText(/apr_0001/)).toBeTruthy();
  });
});

describe("DeltaView", () => {
  it("renders field_path / approved / current rows and names the invalidated and next versions", () => {
    render(
      <DeltaView
        currency="INR"
        invalidatedVersion={1}
        nextVersion={2}
        deltas={[
          { field_path: "lines[0].unit_price_minor", approved: 2800, current: 3800, reason: "PRICE_CHANGED" },
          { field_path: "delivery_fee_minor", approved: 2500, current: 4500, reason: "DELIVERY_FEE_CHANGED" },
          { field_path: "total_minor", approved: 34000, current: 39500, reason: "TOTAL_CHANGED" },
        ]}
      />,
    );
    expect(screen.getByRole("heading", { level: 2 }).textContent).toContain("version 1 is invalidated; approve version 2");
    expect(screen.getByText("lines[0].unit_price_minor")).toBeTruthy();
    expect(screen.getByText("PRICE_CHANGED")).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(4);
    expect(screen.getByText("39500")).toBeTruthy();
  });
});

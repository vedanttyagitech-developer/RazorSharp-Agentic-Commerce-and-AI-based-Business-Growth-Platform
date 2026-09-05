/**
 * The approval card has to say what is being approved.
 *
 * The whole claim of this platform is that consent binds to exact bytes. A card that
 * shows a total and nothing else asks a buyer to consent to a number, which is the one
 * thing consent is not supposed to be. This screen shipped that way: the read path sent
 * `approval_card.quote: null`, an auditor approved ₹1,374.45 for three lines, and the
 * card never named a product.
 *
 * Two behaviours are pinned below, and they are opposite halves of the same rule.
 *
 *  1. Given the breakdown, every line is named, with the quantities and the per-line
 *     figures the server sent, and the rows the buyer reads are the components of the
 *     total on the button they are about to press.
 *  2. Given no breakdown, the card says so and renders no table. The storefront does not
 *     reconstruct a breakdown it was not sent — a plausible one assembled here would be
 *     the browser asserting something about bytes it never saw.
 *
 * The fixture is a real payload, copied from `GET /v1/checkouts/{id}` against the running
 * API. Its `tax_bp: null` is not a gap in the capture: the hashed checkout document
 * records the tax charged, never the rate behind it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import type { ApprovalCard as ApprovalCardData } from "@/lib/api/types";

import { ApprovalCard } from "./approval-card";

afterEach(cleanup);

const CARD: ApprovalCardData = {
  checkout_id: "01a07169-ead6-7052-99d3-d017e36c0c93",
  version: 1,
  content_hash: "JT_-aFMjqUznsunI3AO60E4wbBpH3pDSH9s9qRCykyg",
  policy_receipt_id: "01a07169-eb07-743c-8df5-5fcd70e77802",
  policy_receipt_hash: "6KAVSh-tCcQUakdYCLGh-C2Ryo5kjmh08XEz9TnzT2s",
  amount_minor: 60863,
  currency: "INR",
  total: { minor: 60863, currency: "INR", display: "608.63" },
  expires_at: "2026-09-05T12:07:41.933575Z",
  reservation: {
    reservation_id: "01a07169-eb03-7d34-84d5-f215ae4a067d",
    state: "ACTIVE",
    expires_at: "2026-09-05T12:07:41.933575Z",
  },
  quote: {
    currency: "INR",
    lines: [
      {
        sku: "AASH-STPL-002",
        name: "Aashirvaad Shudh Chakki Atta 5 kg",
        quantity: 1,
        unit_price_minor: 25500,
        subtotal_minor: 25500,
        tax_bp: null,
        tax_minor: 1275,
      },
      {
        sku: "AMUL-DAIRY-002",
        name: "Amul Gold Full Cream Milk 1 L",
        quantity: 2,
        unit_price_minor: 7300,
        subtotal_minor: 14600,
        tax_bp: null,
        tax_minor: 0,
      },
      {
        sku: "AMUL-DAIRY-005",
        name: "Amul Butter (Salted) 100 g",
        quantity: 3,
        unit_price_minor: 5800,
        subtotal_minor: 17400,
        tax_bp: null,
        tax_minor: 2088,
      },
    ],
    items_subtotal_minor: 57500,
    items_tax_minor: 3363,
    delivery_fee_minor: 0,
    delivery_tax_minor: 0,
    total_minor: 60863,
    total: { minor: 60863, currency: "INR", display: "608.63" },
    free_delivery_applied: true,
    gap_to_free_delivery_minor: null,
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 0,
    content_hash: "JT_-aFMjqUznsunI3AO60E4wbBpH3pDSH9s9qRCykyg",
  },
  previous_version: null,
  deltas: [],
};

const noop = () => {};

/** The table row whose header cell carries this SKU. */
function lineFor(sku: string): HTMLElement {
  const code = screen.getByText(sku, { selector: "code" });
  const row = code.closest("tr");
  if (!row) throw new Error(`no line rendered for ${sku}`);
  return row;
}

describe("a card that was sent its breakdown", () => {
  it("names every line, with the quantity and the figures the server sent", () => {
    render(<ApprovalCard card={CARD} onApprove={noop} onReject={noop} />);

    expect(screen.getByText("Aashirvaad Shudh Chakki Atta 5 kg")).toBeDefined();
    expect(screen.getByText("Amul Gold Full Cream Milk 1 L")).toBeDefined();
    expect(screen.getByText("Amul Butter (Salted) 100 g")).toBeDefined();

    const butter = lineFor("AMUL-DAIRY-005");
    expect(within(butter).getByText("3")).toBeDefined();
    expect(within(butter).getByText("₹58.00")).toBeDefined();
    expect(within(butter).getByText("₹20.88")).toBeDefined();
    expect(within(butter).getByText("₹174.00")).toBeDefined();
  });

  it("shows a zero-tax line as ₹0.00 rather than dropping the cell", () => {
    render(<ApprovalCard card={CARD} onApprove={noop} onReject={noop} />);

    // Milk is taxed at 0 bp. Zero is a figure the merchant stated, so it is printed;
    // an empty cell would read as "not charged yet" beside two lines that were.
    const milk = lineFor("AMUL-DAIRY-002");
    expect(within(milk).getByText("₹0.00")).toBeDefined();
    expect(within(milk).getByText("₹146.00")).toBeDefined();
  });

  it("renders the components of the total the buyer is asked to approve", () => {
    render(<ApprovalCard card={CARD} onApprove={noop} onReject={noop} />);

    // 57500 + 3363 + 0 + 0 = 60863, the amount on the header and on the button.
    expect(screen.getByText("₹575.00")).toBeDefined();
    expect(screen.getByText("₹33.63")).toBeDefined();
    expect(screen.getAllByText("₹608.63").length).toBeGreaterThanOrEqual(3);
  });

  it("does not claim the response was missing a breakdown", () => {
    render(<ApprovalCard card={CARD} onApprove={noop} onReject={noop} />);
    expect(screen.queryByText(/will not reconstruct a breakdown/)).toBeNull();
  });
});

describe("a card that was sent no breakdown", () => {
  it("says the response did not carry one, and invents nothing", () => {
    render(
      <ApprovalCard card={{ ...CARD, quote: null }} onApprove={noop} onReject={noop} />,
    );

    expect(screen.getByText(/will not reconstruct a breakdown/)).toBeDefined();
    // No product name, no line table: the card has nothing to say about the items and
    // says nothing, rather than the shape of a breakdown with figures behind it.
    expect(screen.queryByText("Amul Butter (Salted) 100 g")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    // The binding figure is still stated. Absent detail is not absent consent.
    expect(screen.getAllByText("₹608.63").length).toBeGreaterThanOrEqual(2);
  });
});

describe("the amount on the button", () => {
  it("is the server's total, and pressing it is what records consent", async () => {
    const approve = vi.fn();
    render(<ApprovalCard card={CARD} onApprove={approve} onReject={noop} />);

    const button = screen.getByRole("button", { name: /Approve/ });
    expect(button.textContent).toContain("₹608.63");
    button.click();
    expect(approve).toHaveBeenCalledTimes(1);
  });
});

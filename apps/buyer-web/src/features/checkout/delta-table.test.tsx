/**
 * The delta table has one job that cannot be got wrong: decide whether a row is money.
 *
 * A row it wrongly treats as money renders paise that were never paise — `quantity: 2`
 * becoming ₹0.02 — and a row it wrongly treats as plain renders `9224` as a bare number
 * beside a rupee figure. Both are the same failure in opposite directions: the table
 * asserting something about a value that the value does not say about itself.
 *
 * So every test below fixes one of the four quadrants — money path with numbers, money
 * path without numbers, plain path, and a value the server did not send at all — and the
 * last of them is the one that matters: an absent side must render as absent, never as
 * zero.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import type { Delta } from "@/lib/api/types";

import { DeltaTable, readableField } from "./delta-table";

afterEach(cleanup);

/** The delta the live kernel sent with the captured `REAPPROVAL_REQUIRED` refusal. */
const TOTAL_MOVED: Delta = {
  field_path: "total",
  approved: 8550,
  current: 9224,
  reason: "total_changed",
};

const NAMES = { "AMUL-DAIRY-001": "Amul Taaza Toned Milk 500 ml" };

/** The row whose header cell names this field path, as its four cells. */
function rowFor(path: string): HTMLElement {
  const code = screen.getByText(path, { selector: "code" });
  const row = code.closest("tr");
  if (!row) throw new Error(`no row rendered for ${path}`);
  return row;
}

describe("a money-valued delta", () => {
  it("renders both sides and the difference as money", () => {
    render(<DeltaTable deltas={[TOTAL_MOVED]} currency="INR" names={NAMES} />);

    const row = rowFor("total");
    expect(within(row).getByText("₹85.50")).toBeDefined();
    expect(within(row).getByText("₹92.24")).toBeDefined();
    // The one computed figure on the screen: 9224 − 8550, both integers the server sent.
    expect(within(row).getByText("+₹6.74")).toBeDefined();
  });

  it("signs a fall as a fall rather than dropping the direction", () => {
    render(<DeltaTable deltas={[{ ...TOTAL_MOVED, approved: 9224, current: 8550 }]} />);
    expect(screen.getByText("−₹6.74")).toBeDefined();
  });

  it("prints the kernel's own reason key beside the row", () => {
    render(<DeltaTable deltas={[TOTAL_MOVED]} />);
    expect(screen.getByText("total_changed", { selector: "code" })).toBeDefined();
  });

  it("treats every `_minor` leaf as money without needing a list of them", () => {
    const deltas: Delta[] = [
      { field_path: "lines[AMUL-DAIRY-001].unit_price_minor", approved: 2800, current: 3137, reason: null },
      { field_path: "delivery_fee_minor", approved: 2500, current: 0, reason: null },
    ];
    render(<DeltaTable deltas={deltas} names={NAMES} />);

    const price = rowFor("lines[AMUL-DAIRY-001].unit_price_minor");
    expect(within(price).getByText("₹28.00")).toBeDefined();
    expect(within(price).getByText("₹31.37")).toBeDefined();
    expect(within(price).getByText("+₹3.37")).toBeDefined();

    const delivery = rowFor("delivery_fee_minor");
    expect(within(delivery).getByText("₹0.00")).toBeDefined();
    expect(within(delivery).getByText("−₹25.00")).toBeDefined();
  });

  it("carries a currency other than rupees through both sides", () => {
    render(<DeltaTable deltas={[TOTAL_MOVED]} currency="USD" />);
    expect(screen.getByText("$85.50")).toBeDefined();
    expect(screen.getByText("$92.24")).toBeDefined();
  });
});

describe("a delta that is not money", () => {
  it("renders a boolean pair as words and says only that it changed", () => {
    const delta: Delta = {
      field_path: "lines[AMUL-DAIRY-001].is_available",
      approved: true,
      current: false,
      reason: "went_out_of_stock",
    };
    render(<DeltaTable deltas={[delta]} names={NAMES} />);

    const row = rowFor("lines[AMUL-DAIRY-001].is_available");
    expect(within(row).getByText("yes")).toBeDefined();
    expect(within(row).getByText("no")).toBeDefined();
    expect(within(row).getByText("changed")).toBeDefined();
    // Nothing in this row is a rupee figure, because nothing in it is an amount.
    expect(row.textContent).not.toContain("₹");
  });

  it("renders a quantity as the integer it is, not as paise", () => {
    const delta: Delta = {
      field_path: "lines[AMUL-DAIRY-001].quantity",
      approved: 2,
      current: 3,
      reason: null,
    };
    render(<DeltaTable deltas={[delta]} names={NAMES} />);

    const row = rowFor("lines[AMUL-DAIRY-001].quantity");
    expect(within(row).getByText("2")).toBeDefined();
    expect(within(row).getByText("3")).toBeDefined();
    expect(row.textContent).not.toContain("₹0.02");
    expect(row.textContent).not.toContain("₹");
  });

  it("renders a replaced set of line items as a list, naming what it can name", () => {
    const delta: Delta = {
      field_path: "line_items",
      approved: { "AMUL-DAIRY-001": 2 },
      current: { "AMUL-DAIRY-001": 1, "AASH-STPL-002": 1 },
      reason: null,
    };
    render(<DeltaTable deltas={[delta]} names={NAMES} />);

    const row = rowFor("line_items");
    expect(within(row).getAllByText("Amul Taaza Toned Milk 500 ml").length).toBeGreaterThan(0);
    // A SKU with no name on the quote is printed as the SKU. A product invented to fill
    // the gap would be a product the buyer never approved.
    expect(within(row).getByText("AASH-STPL-002")).toBeDefined();
    expect(row.textContent).not.toContain("₹");
  });

  it("prints an unrecognised path verbatim rather than guessing at it", () => {
    const delta: Delta = {
      field_path: "merchant.policy.some_unknown_leaf",
      approved: "old",
      current: "new",
      reason: null,
    };
    render(<DeltaTable deltas={[delta]} />);

    expect(screen.getByText("merchant.policy.some_unknown_leaf", { selector: "code" })).toBeDefined();
    expect(screen.getByText("old")).toBeDefined();
    expect(screen.getByText("new")).toBeDefined();
  });
});

describe("nothing is invented", () => {
  it("renders an absent side as 'not set', never as zero", () => {
    const delta: Delta = {
      field_path: "policy_receipt_hash",
      approved: null,
      current: "SuUWvKxDTnuYUnGDMWcuTHhf_EECKzxk6WX_Am-VpJM",
      reason: null,
    };
    render(<DeltaTable deltas={[delta]} />);

    const row = rowFor("policy_receipt_hash");
    expect(within(row).getByText("not set")).toBeDefined();
    expect(row.textContent).not.toContain("0");
  });

  it("refuses to render money for a money-named path whose sides are not numbers", () => {
    // The shape a service sending decimal strings would produce. `_minor` says money,
    // the values do not, and the table believes the values.
    const delta: Delta = {
      field_path: "total_minor",
      approved: "85.50",
      current: "92.24",
      reason: null,
    };
    render(<DeltaTable deltas={[delta]} />);

    const row = rowFor("total_minor");
    expect(within(row).getByText("85.50")).toBeDefined();
    expect(within(row).getByText("changed")).toBeDefined();
    expect(row.textContent).not.toContain("₹");
  });

  it("refuses to render money when only one side is a number", () => {
    const delta: Delta = { field_path: "total", approved: 8550, current: null, reason: null };
    render(<DeltaTable deltas={[delta]} />);

    const row = rowFor("total");
    expect(within(row).getByText("not set")).toBeDefined();
    expect(within(row).getByText("changed")).toBeDefined();
    expect(row.textContent).not.toContain("₹");
  });

  it("says the server listed nothing rather than drawing an empty table", () => {
    render(<DeltaTable deltas={[]} />);
    expect(screen.getByText(/listed no field-level differences/)).toBeDefined();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("renders one row per delta the server sent, and no more", () => {
    const deltas: Delta[] = [
      TOTAL_MOVED,
      { field_path: "lines[AMUL-DAIRY-001].unit_price_minor", approved: 2800, current: 3137, reason: null },
      { field_path: "catalogue_revision", approved: 16, current: 17, reason: null },
    ];
    render(<DeltaTable deltas={deltas} names={NAMES} />);
    // Three rows plus the header row.
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });
});

describe("readableField", () => {
  it("names the product when the quote named it, and the SKU when it did not", () => {
    expect(readableField("lines[AMUL-DAIRY-001].unit_price_minor", NAMES)).toBe(
      "Amul Taaza Toned Milk 500 ml — unit price",
    );
    expect(readableField("lines[AASH-STPL-002].quantity", NAMES)).toBe("AASH-STPL-002 — quantity");
  });

  it("says a bare leaf in English and leaves an unknown one recognisable", () => {
    expect(readableField("total")).toBe("Order total");
    expect(readableField("delivery_fee_minor")).toBe("Delivery fee");
    expect(readableField("merchant.some_unknown_leaf")).toBe("Some unknown leaf");
  });
});

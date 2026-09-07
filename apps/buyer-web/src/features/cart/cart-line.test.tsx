/**
 * A refused line is only worth drawing if the buyer can act on it.
 *
 * The failure these tests exist to prevent had one shape: asking for 21 of a product with
 * 20 in stock removed the quantity stepper from the row, so the only control that could
 * bring the cart back to a priceable state was gone. The buyer was left holding a line
 * marked "No longer available" for a product the merchant had twenty of, with no way back
 * except deleting it. Every assertion below is about the difference between "we cannot
 * sell you this" and "we cannot sell you this many", which are opposite facts wearing very
 * similar words.
 *
 * The counts here are the ones the live API returned on 2026-09-05 for a cart whose
 * AMUL-DAIRY-005 line was pushed to 21: `{requested: 21, available_units: 20, listed: true}`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Unavailability } from "@/lib/api/types";

import { CartLine } from "./cart-line";

afterEach(cleanup);

const SKU = "AMUL-DAIRY-005";
const NAME = "Amul Butter (Salted) 100 g";

/** The live refusal: one more was asked for than the merchant holds. */
const OVER_STOCK: Unavailability = { sku: SKU, requested: 21, available_units: 20, listed: true };

function renderLine(props: Partial<Parameters<typeof CartLine>[0]> = {}) {
  const onSetQuantity = vi.fn();
  render(
    <ul>
      <CartLine
        sku={SKU}
        name={NAME}
        quantity={21}
        onSetQuantity={onSetQuantity}
        {...props}
      />
    </ul>,
  );
  return onSetQuantity;
}

describe("a line refused for asking more than the merchant has", () => {
  it("keeps the stepper, which is the only control that can undo the refusal", () => {
    renderLine({ shortfall: OVER_STOCK });
    expect(screen.getByRole("button", { name: `Increase the quantity of ${NAME}` })).toBeDefined();
    expect(screen.getByRole("button", { name: `Decrease the quantity of ${NAME}` })).toBeDefined();
  });

  it("keeps the product's name rather than falling back to the raw SKU", () => {
    renderLine({ shortfall: OVER_STOCK });
    expect(screen.getByText(NAME)).toBeDefined();
  });

  it("states what the merchant has, which is the number that fixes the cart", () => {
    renderLine({ shortfall: OVER_STOCK });
    // Both integers, the merchant's own, and neither derived from the other.
    expect(screen.getByText(/Only 20 left/)).toBeDefined();
    expect(screen.getByText(/you asked for 21/)).toBeDefined();
  });

  it("offers the available count as an absolute quantity, not as a decrement", () => {
    const onSetQuantity = renderLine({ shortfall: OVER_STOCK });
    fireEvent.click(screen.getByRole("button", { name: "Change to 20" }));
    // 20, not 21 - 1. The API takes an absolute quantity and the button must carry the
    // merchant's number through untouched.
    expect(onSetQuantity).toHaveBeenCalledWith(20);
  });

  it("does not call it unavailable, because the merchant is still selling it", () => {
    renderLine({ shortfall: OVER_STOCK });
    expect(screen.queryByText("No longer available")).toBeNull();
    expect(screen.queryByText("Out of stock")).toBeNull();
  });
});

describe("a line the merchant genuinely cannot supply", () => {
  it("says out of stock when the product is listed and has none", () => {
    renderLine({ shortfall: { sku: SKU, requested: 2, available_units: 0, listed: true } });
    expect(screen.getByText("Out of stock")).toBeDefined();
    // Nothing to step to, so the stepper would only be a way to fail again.
    expect(screen.queryByRole("button", { name: `Increase the quantity of ${NAME}` })).toBeNull();
    expect(screen.getByRole("button", { name: `Remove ${NAME} from the cart` })).toBeDefined();
  });

  it("says no longer available when the product has left the catalogue", () => {
    renderLine({ shortfall: { sku: SKU, requested: 2, available_units: 0, listed: false } });
    expect(screen.getByText("No longer available")).toBeDefined();
  });

  it("never offers a quantity from a delisted product's stock figure", () => {
    // A delisted product can report units it will not sell. Offering to "change to 5"
    // would be a promise the platform cannot back.
    renderLine({ shortfall: { sku: SKU, requested: 8, available_units: 5, listed: false } });
    expect(screen.getByText("No longer available")).toBeDefined();
    expect(screen.queryByRole("button", { name: /Change to/ })).toBeNull();
  });
});

describe("a line nothing is wrong with", () => {
  it("carries no shortfall note and no unexplained note", () => {
    renderLine({ quantity: 2, unitPriceMinor: 5800, subtotalMinor: 11600 });
    expect(screen.queryByText(/Only \d+ left/)).toBeNull();
    expect(screen.queryByText("The merchant did not price this line")).toBeNull();
    expect(screen.getByRole("button", { name: `Increase the quantity of ${NAME}` })).toBeDefined();
  });

  it("renders the merchant's own amounts and groups them by lakh", () => {
    renderLine({ quantity: 1, unitPriceMinor: 12689900, subtotalMinor: 12689900 });
    expect(screen.getAllByText("₹1,26,899.00").length).toBeGreaterThan(0);
  });
});

describe("a line the quote dropped without saying why", () => {
  it("reports the omission itself rather than inventing a stock reason", () => {
    renderLine({ unexplained: true });
    expect(screen.getByText("The merchant did not price this line")).toBeDefined();
    expect(screen.queryByText(/Only \d+ left/)).toBeNull();
    // It is still the buyer's line, so it keeps its controls.
    expect(screen.getByRole("button", { name: `Increase the quantity of ${NAME}` })).toBeDefined();
  });
});

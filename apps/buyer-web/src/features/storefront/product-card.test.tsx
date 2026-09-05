/**
 * The card makes two claims a buyer acts on, and both are easy to blur.
 *
 * The first is whether waiting will help. "Out of stock" says the merchant sells this and
 * has none today; "Not available" says it is off the catalogue. A card that renders one
 * word over both facts sends a buyer back tomorrow for something that will never return,
 * so these are tested as two different renderings rather than as one greyed-out state.
 *
 * The second is the price. It comes from `unit_price` — the `MoneyOut` the quote engine
 * sent — and the tests below prove that by handing the card a `unit_price_minor` that
 * disagrees with it. If the card ever started deriving its own figure from the loose
 * integer, or worse from a tax rate and a unit price, these tests fail.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { Product } from "@/lib/api/types";

import { ProductCard } from "./product-card";

afterEach(cleanup);

/** `GET /v1/catalogue/search?q=doodh`, captured from the live API on 2026-09-05. */
const MILK: Product = {
  sku: "AMUL-DAIRY-001",
  display_name: "Amul Taaza Toned Milk 500 ml",
  name_en: "Amul Taaza Toned Milk 500 ml",
  name_hi: "अमूल ताज़ा टोंड दूध 500 मिली",
  category: "dairy",
  unit_label: "500 ml",
  unit_price_minor: 2800,
  unit_price: { minor: 2800, currency: "INR", display: "28.00" },
  currency: "INR",
  tax_bp: 0,
  stock_units: 48,
  is_listed: true,
  is_available: true,
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 16,
    observed_at: "2026-09-05T03:47:41.529868Z",
  },
};

/** `GET /v1/catalogue/products?limit=1`, captured 2026-09-05. Taxed, and not round. */
const ATTA: Product = {
  ...MILK,
  sku: "AASH-STPL-002",
  display_name: "Aashirvaad Shudh Chakki Atta 5 kg",
  name_en: "Aashirvaad Shudh Chakki Atta 5 kg",
  name_hi: "आशीर्वाद शुद्ध चक्की आटा 5 किलो",
  category: "staples",
  unit_label: "5 kg",
  unit_price_minor: 25500,
  unit_price: { minor: 25500, currency: "INR", display: "255.00" },
  tax_bp: 500,
  stock_units: 18,
};

describe("the price", () => {
  it("is the `unit_price` the quote engine sent, in the compact form the grid uses", () => {
    render(<ProductCard product={MILK} />);
    expect(screen.getByText("₹28")).toBeDefined();
  });

  it("comes from `unit_price` and not from the loose integer beside it", () => {
    // Same product, with the two fields deliberately disagreeing. The card must read the
    // `MoneyOut`, which is the field carrying the server's own currency and rendering.
    const disagreeing: Product = { ...MILK, unit_price_minor: 999999 };
    render(<ProductCard product={disagreeing} />);
    expect(screen.getByText("₹28")).toBeDefined();
    expect(screen.queryByText("₹9,999.99")).toBeNull();
  });

  it("keeps the paise when the amount is not exact rupees", () => {
    const odd: Product = { ...MILK, unit_price: { minor: 3137, currency: "INR", display: "31.37" } };
    render(<ProductCard product={odd} />);
    expect(screen.getByText("₹31.37")).toBeDefined();
  });

  it("never applies the tax rate the product carries", () => {
    // `tax_bp: 500` is on this product and tax is the merchant's to compute. A card that
    // multiplied would draw ₹267.75; the only correct figure here is the one sent.
    render(<ProductCard product={ATTA} />);
    expect(screen.getByText("₹255")).toBeDefined();
    expect(screen.queryByText("₹267.75")).toBeNull();
  });

  it("renders an unusual currency with the server's own symbol handling", () => {
    const foreign: Product = {
      ...MILK,
      unit_price: { minor: 2800, currency: "GBP", display: "28.00" },
    };
    render(<ProductCard product={foreign} />);
    expect(screen.getByText("GBP 28")).toBeDefined();
  });
});

describe("sold out and delisted are different things", () => {
  const soldOut: Product = { ...MILK, is_listed: true, is_available: false, stock_units: 0 };
  const delisted: Product = { ...MILK, is_listed: false, is_available: false, stock_units: 0 };

  it("says 'Out of stock' for a product the merchant still sells", () => {
    render(<ProductCard product={soldOut} />);
    expect(screen.getByText("Out of stock")).toBeDefined();
    expect(screen.queryByText("Not available")).toBeNull();
  });

  it("says 'Not available' for a product that has left the catalogue", () => {
    render(<ProductCard product={delisted} />);
    expect(screen.getByText("Not available")).toBeDefined();
    expect(screen.queryByText("Out of stock")).toBeNull();
  });

  it("offers no ADD on either, so a buyer cannot start a basket that cannot be quoted", () => {
    render(<ProductCard product={soldOut} onAdd={() => undefined} />);
    expect(screen.queryByRole("button", { name: /Add .* to basket/ })).toBeNull();
    cleanup();

    render(<ProductCard product={delisted} onAdd={() => undefined} />);
    expect(screen.queryByRole("button", { name: /Add .* to basket/ })).toBeNull();
  });

  it("still prints the price of an unavailable product rather than hiding it", () => {
    // The merchant is still quoting a figure for this SKU; suppressing it would be a
    // second, unstated claim about a product that is only out of stock.
    render(<ProductCard product={soldOut} />);
    expect(screen.getByText("₹28")).toBeDefined();
  });

  it("marks the unavailable state with a shape as well as a colour", () => {
    const { container } = render(<ProductCard product={soldOut} />);
    expect(container.querySelector("svg")).not.toBeNull();
  });
});

describe("the stock pill", () => {
  it("prints `stock_units` exactly as the merchant sent it", () => {
    render(<ProductCard product={MILK} />);
    expect(screen.getByText("48 IN STOCK")).toBeDefined();
  });

  it("says nothing at all rather than '0 IN STOCK' beside an ADD button", () => {
    render(<ProductCard product={{ ...MILK, stock_units: 0 }} />);
    expect(screen.queryByText(/IN STOCK/)).toBeNull();
    expect(screen.getByRole("button", { name: /Add .* to basket/ })).toBeDefined();
  });

  it("promises no delivery time, because no catalogue response carries one", () => {
    const { container } = render(<ProductCard product={MILK} />);
    expect(container.textContent).not.toMatch(/MINS?\b/i);
  });
});

describe("the controls", () => {
  it("adds by SKU and names the product to a screen reader", () => {
    const onAdd = vi.fn();
    render(<ProductCard product={MILK} onAdd={onAdd} />);

    const add = screen.getByRole("button", { name: "Add Amul Taaza Toned Milk 500 ml to basket" });
    add.click();
    expect(onAdd).toHaveBeenCalledWith("AMUL-DAIRY-001");
  });

  it("shows the quantity the server holds once the line exists", () => {
    render(<ProductCard product={MILK} quantity={2} onSetQuantity={() => undefined} />);
    expect(screen.getByLabelText("2 in basket")).toBeDefined();
    expect(screen.queryByRole("button", { name: /Add .* to basket/ })).toBeNull();
  });

  it("links the card to the product's own page", () => {
    render(<ProductCard product={MILK} />);
    const link = screen.getByRole("link", { name: /Amul Taaza Toned Milk 500 ml/ });
    expect(link.getAttribute("href")).toBe("/p/AMUL-DAIRY-001");
  });
});

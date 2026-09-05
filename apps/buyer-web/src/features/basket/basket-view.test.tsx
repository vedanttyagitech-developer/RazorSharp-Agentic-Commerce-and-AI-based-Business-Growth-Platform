/**
 * Which lines the basket screen marks, and on whose authority.
 *
 * The bug this file locks shut: the screen decided a line was unavailable by noting it was
 * absent from `quote.lines`. That test is right only while a quote exists. The merchant
 * answers a basket it cannot price with `quote: null`, at which point *every* line is
 * absent from the quote — so one line asking for 21 of a product with 20 in stock struck
 * out all three lines as bare SKUs, stamped each "No longer available", removed every
 * stepper, and printed a sentence saying the merchant declined 1 line directly beneath
 * three lines it had visibly declined.
 *
 * `basket.unavailable` is the merchant saying which line it refused. The quote's silence
 * is not. These tests assert the screen reads the first and not the second, using the
 * exact response the live API returned on 2026-09-05 for that basket.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { Basket } from "@/lib/api/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/components/providers", () => ({ useBasketContext: () => ({ setBasketId: vi.fn() }) }));

const useBasket = vi.fn();
vi.mock("./use-basket", async () => {
  const actual = await vi.importActual<typeof import("./use-basket")>("./use-basket");
  return { ...actual, useBasket: () => useBasket() };
});

const { BasketView } = await import("./basket-view");

afterEach(cleanup);

const NAMES: Record<string, string> = {
  "AASH-STPL-002": "Aashirvaad Shudh Chakki Atta 5 kg",
  "ACT-SNCK-019": "Act II Butter Popcorn 150 g",
  "AMUL-DAIRY-005": "Amul Butter (Salted) 100 g",
};

const FRESHNESS = {
  source: "merchant-sim:demo-grocery/v1",
  catalogue_revision: 1509,
  observed_at: "2026-09-05T11:38:49.751425Z",
};

/**
 * `PUT /v1/baskets/{id}/lines/AMUL-DAIRY-005 {"quantity": 21}` against a basket already
 * holding three lines, captured verbatim. Twenty butters exist; twenty-one were asked for.
 */
const REFUSED: Basket = {
  basket_id: "01a0715c-ffa1-7297-a31f-eed873277d94",
  lines: [
    { sku: "AASH-STPL-002", quantity: 1 },
    { sku: "ACT-SNCK-019", quantity: 3 },
    { sku: "AMUL-DAIRY-005", quantity: 21 },
  ],
  code: "STALE_CHECKOUT",
  quote: null,
  unavailable: [{ sku: "AMUL-DAIRY-005", requested: 21, available_units: 20, listed: true }],
  freshness: FRESHNESS,
  stale: false,
};

beforeEach(() => {
  useBasket.mockReturnValue({
    basketId: REFUSED.basket_id,
    basket: REFUSED,
    quantities: {},
    names: NAMES,
    loading: false,
    error: null,
    busySku: null,
    add: vi.fn(),
    setQuantity: vi.fn(),
    reload: vi.fn(),
  });
});

describe("one line over stock", () => {
  it("marks that line and no other", () => {
    render(<BasketView />);
    // The one the merchant named.
    expect(screen.getByText(/Only 20 left/)).toBeDefined();
    // And exactly one such mark on the page, not one per line.
    expect(screen.getAllByRole("button", { name: /^Change to/ })).toHaveLength(1);
  });

  it("leaves the other two lines with their names and their steppers", () => {
    render(<BasketView />);
    for (const name of ["Aashirvaad Shudh Chakki Atta 5 kg", "Act II Butter Popcorn 150 g"]) {
      expect(screen.getByText(name)).toBeDefined();
      expect(screen.getByRole("button", { name: `Increase the quantity of ${name}` })).toBeDefined();
    }
  });

  it("keeps the refused line's own stepper too, so the buyer can climb back down", () => {
    render(<BasketView />);
    expect(
      screen.getByRole("button", { name: "Decrease the quantity of Amul Butter (Salted) 100 g" }),
    ).toBeDefined();
  });

  it("calls no line unavailable, because the merchant withdrew nothing", () => {
    render(<BasketView />);
    expect(screen.queryByText("No longer available")).toBeNull();
    expect(screen.queryByText("Out of stock")).toBeNull();
  });

  it("counts the refused lines the same way in words as it marks them on screen", () => {
    render(<BasketView />);
    // "1 of these 3 lines" — the sentence that used to say 1 while striking through 3.
    const notice = screen.getByText(/could not price/);
    expect(notice.textContent).toMatch(/1 of these 3 lines/);
  });

  it("tells the buyer the count that would fix it", () => {
    render(<BasketView />);
    expect(screen.getByText(/you asked for 21, the merchant has 20/)).toBeDefined();
    expect(screen.getByText(/Bring that line down to what is available/)).toBeDefined();
  });

  it("still refuses to show a total, because the merchant sent none", () => {
    render(<BasketView />);
    expect(screen.queryByText("Bill details")).toBeNull();
    expect(screen.getByRole("button", { name: "Proceed to checkout" }).hasAttribute("disabled")).toBe(
      true,
    );
  });
});

describe("a line the merchant has withdrawn", () => {
  it("is the one case that offers removal alone", () => {
    useBasket.mockReturnValue({
      ...useBasket(),
      basket: {
        ...REFUSED,
        unavailable: [
          { sku: "AMUL-DAIRY-005", requested: 21, available_units: 0, listed: false },
        ],
      },
    });
    render(<BasketView />);
    expect(screen.getByText("No longer available")).toBeDefined();
    expect(screen.getByText(/the merchant no longer lists it/)).toBeDefined();
    expect(screen.getByText(/Remove it and the total comes back/)).toBeDefined();
    expect(screen.queryByRole("button", { name: /^Change to/ })).toBeNull();
    // The two healthy lines are untouched by their neighbour's fate.
    expect(
      screen.getByRole("button", { name: "Increase the quantity of Act II Butter Popcorn 150 g" }),
    ).toBeDefined();
  });
});

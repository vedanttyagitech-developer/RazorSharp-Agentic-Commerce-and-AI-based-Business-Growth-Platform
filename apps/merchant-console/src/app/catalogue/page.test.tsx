/**
 * The price field, which is the one input in this console that can set a live price.
 *
 * It takes integer paise because the API stores integer paise, and the failure it is
 * built to prevent is the operator who types the rupee figure out of habit.
 * `Number.parseInt("28.50", 10)` is `28`, and an integrality check applied *after* that
 * parse can never fail — the truncation has already happened. So a validator built on
 * parsing would accept `28.50`, send `28`, and set the live price of a ₹28.50 product to
 * ₹0.28 with the button still enabled and nothing on screen saying a digit was dropped.
 * Every refusal below is a shape a parser would have swallowed.
 *
 * The assertions are made three ways on purpose, because one of them alone is not a
 * defence: the button is disabled, the field is marked `aria-invalid`, and the live region
 * beside it says what is wrong. A disabled button with no explanation is an operator
 * filing a bug about a broken console.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CataloguePage as CataloguePageData, Injection, Product } from "@/lib/api/types";

import CataloguePage from "./page";

const mockApi = vi.hoisted(() => ({
  products: vi.fn(),
  search: vi.fn(),
  inject: vi.fn(),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

const SKU = "AMUL-DAIRY-001";

const product: Product = {
  sku: SKU,
  display_name: "Amul Gold Full Cream Milk 500 ml",
  name_en: "Amul Gold Full Cream Milk 500 ml",
  name_hi: "अमूल गोल्ड फुल क्रीम दूध 500 मिली",
  category: "dairy",
  unit_label: "500 ml",
  unit_price_minor: 3400,
  unit_price: { minor: 3400, currency: "INR", display: "34.00" },
  currency: "INR",
  tax_bp: 500,
  stock_units: 40,
  is_listed: true,
  is_available: true,
  freshness: {
    source: "merchant_catalogue",
    catalogue_revision: 16,
    observed_at: "2026-09-05T03:00:00.000000Z",
  },
};

const page: CataloguePageData = {
  products: [product],
  next_cursor: null,
  limit: 50,
  matched: 1,
  counts_by_category: { dairy: 24, staples: 31 },
  revision: 16,
};

/** Open the injection editor on the one row, and hand back its price controls. */
async function openPriceEditor(): Promise<{ input: HTMLInputElement; button: HTMLButtonElement }> {
  render(<CataloguePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Inject" }));
  const input = await screen.findByLabelText(`Unit price in paise for ${SKU}`);
  const button = screen.getByRole("button", { name: "Set price" });
  return { input: input as HTMLInputElement, button: button as HTMLButtonElement };
}

beforeEach(() => {
  mockApi.products.mockResolvedValue(page);
  mockApi.search.mockResolvedValue(null);
  mockApi.inject.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the catalogue price field", () => {
  it("opens showing the SKU's current price in paise, exactly as the API sent it", async () => {
    const { input, button } = await openPriceEditor();
    expect(input.value).toBe("3400");
    expect(button.disabled).toBe(false);
    expect(screen.getByText("Will set ₹34.00")).toBeTruthy();
  });

  // The table of refusals. Each entry is a shape that a parse-then-check validator accepts
  // and this one must not, plus empty, which has no number in it at all.
  const refused: Array<[string, string]> = [
    ["28.50", "a rupee figure typed into a paise field: parses to 28, which is ₹0.28"],
    ["1e3", "exponent notation: parses to 1000 while reading as the digit one"],
    ["300abc", "digits with a tail: parses to 300 and drops the rest silently"],
    ["0", "free is not a price the merchant simulator should be asked to set"],
    ["", "no entry at all"],
  ];

  for (const [entry, why] of refused) {
    it(`refuses ${JSON.stringify(entry)} — ${why}`, async () => {
      const { input, button } = await openPriceEditor();
      fireEvent.change(input, { target: { value: entry } });

      expect(button.disabled).toBe(true);
      expect(input.getAttribute("aria-invalid")).toBe("true");
      expect(
        screen.getByText("Digits only, and more than zero. A decimal point is not a paise figure."),
      ).toBeTruthy();

      // The strongest form of the assertion: nothing is sent even if the button is pressed.
      fireEvent.click(button);
      expect(mockApi.inject).not.toHaveBeenCalled();
    });
  }

  it("accepts 2800 and sends it to the API as the integer 2800", async () => {
    const injection: Injection = {
      injection_id: "01a06fa8-0000-7000-8000-000000000001",
      kind: "PRICE_SET",
      label: "Price set to ₹28.00",
      sku: SKU,
      note: "",
      currency: "INR",
      deltas: [{ field: "unit_price_minor", before: 3400, after: 2800 }],
      revision_before: 16,
      revision_after: 17,
      injected_at: "2026-09-05T03:10:00.000000Z",
      audit_event_id: "01a06fa8-0000-7000-8000-000000000002",
      scenario_run_id: "01a06fa8-0000-7000-8000-000000000003",
      audit_payload: {},
    };
    mockApi.inject.mockResolvedValue(injection);

    const { input, button } = await openPriceEditor();
    fireEvent.change(input, { target: { value: "2800" } });

    expect(button.disabled).toBe(false);
    expect(input.getAttribute("aria-invalid")).toBe(null);
    // Read back as money before it is sent, in the same notation the row above uses.
    expect(screen.getByText("Will set ₹28.00")).toBeTruthy();

    fireEvent.click(button);
    await waitFor(() => expect(mockApi.inject).toHaveBeenCalledTimes(1));
    expect(mockApi.inject).toHaveBeenCalledWith({
      kind: "PRICE_SET",
      sku: SKU,
      value: 2800,
      note: "",
    });
    // An integer, not a string and not a rounded float. The kernel revalidates against
    // this number at admission, so a paise figure that arrived as `28.5` would be a
    // different price from the one on the screen.
    const [sent] = mockApi.inject.mock.calls[0] as [{ value: number }];
    expect(Number.isSafeInteger(sent.value)).toBe(true);
  });

  it("refuses a leading-plus and a leading-minus, which parse but are not a count of paise", async () => {
    const { input, button } = await openPriceEditor();
    for (const entry of ["+2800", "-2800", " 2800 abc", "2 800"]) {
      fireEvent.change(input, { target: { value: entry } });
      expect(button.disabled).toBe(true);
    }
  });

  it("still refuses after a valid entry is edited back into an invalid one", async () => {
    const { input, button } = await openPriceEditor();
    fireEvent.change(input, { target: { value: "2800" } });
    expect(button.disabled).toBe(false);
    fireEvent.change(input, { target: { value: "2800." } });
    expect(button.disabled).toBe(true);
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });
});

describe("the catalogue's category counts", () => {
  // `counts_by_category` is computed by the API across the whole catalogue whatever filter
  // is in force, so the chip standing for "no category filter" has to be the sum of those
  // and not `matched`, which counts only what the current filter selected.
  it("counts the 'all categories' chip from counts_by_category, not from matched", async () => {
    render(<CataloguePage />);
    const all = await screen.findByRole("button", { name: /all categories/ });
    const summed = Object.values(page.counts_by_category).reduce((total, count) => total + count, 0);
    expect(summed).toBe(55);
    expect(page.matched).toBe(1);
    expect(all.textContent?.replace(/\s+/g, "")).toBe("allcategories55");
  });
});

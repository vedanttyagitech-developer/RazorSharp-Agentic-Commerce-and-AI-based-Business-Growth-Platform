import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BasketRefContext, ClientContext } from "@/components/providers";
import type { CommerceClient } from "@/lib/api/client";
import type { Basket, SearchResponse } from "@/lib/api/types";
import { BasketView } from "./basket-view";
import { SearchPanel } from "./search-panel";
import { ProductDetail } from "./product-detail";

afterEach(cleanup);

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/basket",
  useSearchParams: () => new URLSearchParams(),
}));

const MOCK_FRESHNESS = {
  source: "merchant-sim:demo-grocery/v1",
  catalogue_revision: 3,
  observed_at: new Date().toISOString(),
};

function sampleBasket(lines: { sku: string; quantity: number }[] = []): Basket {
  const itemsSubtotal = lines.reduce((sum, l) => sum + l.quantity * 2800, 0);
  const deliveryFee = itemsSubtotal >= 49900 ? 0 : 2500;
  const deliveryTax = Math.floor((deliveryFee * 1800 + 5000) / 10000);
  const total = itemsSubtotal + deliveryFee + deliveryTax;

  return {
    basket_id: "bsk_test_001",
    lines,
    code: "OK",
    quote:
      lines.length > 0
        ? {
            currency: "INR",
            lines: lines.map((l) => ({
              sku: l.sku,
              name: l.sku === "GRO-DAIRY-001" ? "Amul Taaza Toned Milk 500 ml" : l.sku,
              quantity: l.quantity,
              unit_price_minor: 2800,
              subtotal_minor: l.quantity * 2800,
              tax_bp: 0,
              tax_minor: 0,
            })),
            items_subtotal_minor: itemsSubtotal,
            items_tax_minor: 0,
            delivery_fee_minor: deliveryFee,
            delivery_tax_minor: deliveryTax,
            total_minor: total,
            free_delivery_applied: itemsSubtotal >= 49900,
            gap_to_free_delivery_minor: Math.max(0, 49900 - itemsSubtotal),
            source: "merchant-sim:demo-grocery/v1",
            catalogue_revision: 3,
            content_hash: "0".repeat(64),
          }
        : null,
    unavailable: [],
    freshness: MOCK_FRESHNESS,
    stale: false,
  };
}

describe("Basket Interactions", () => {
  let mockClient: Partial<CommerceClient>;

  beforeEach(() => {
    mockClient = {
      mode: "mock",
      getBasket: vi.fn().mockResolvedValue(sampleBasket([{ sku: "GRO-DAIRY-001", quantity: 2 }])),
      setBasketLine: vi.fn().mockImplementation((_basketId, sku, qty) => {
        return Promise.resolve(sampleBasket(qty > 0 ? [{ sku, quantity: qty }] : []));
      }),
      checkoutBasket: vi.fn(),
      search: vi.fn().mockResolvedValue({
        query: "",
        normalized_query: "",
        locale: "en-IN",
        hits: [],
        freshness: MOCK_FRESHNESS,
      } as SearchResponse),
    };
  });

  it("renders empty basket state with browsing call to action when basket has 0 items", async () => {
    mockClient.getBasket = vi.fn().mockResolvedValue(sampleBasket([]));

    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_empty",
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <BasketView />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      const heading = screen.getByRole("heading", { level: 1 });
      expect(heading.textContent).toContain("Your Basket is Empty");
    });
    expect(screen.getByRole("link", { name: /Browse Catalogue/i })).toBeTruthy();
  });

  it("prevents duplicate-click mutations when user rapidly clicks quantity buttons", async () => {
    let resolveMutation!: (value: Basket) => void;
    const pendingPromise = new Promise<Basket>((resolve) => {
      resolveMutation = resolve;
    });

    const setLineSpy = vi.fn().mockReturnValueOnce(pendingPromise);
    mockClient.setBasketLine = setLineSpy;

    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_test_001",
            setBasketId: () => undefined,
            lineCount: 1,
            setLineCount: () => undefined,
            totalMinor: 5600,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <BasketView />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("link", { name: "Amul Taaza Toned Milk 500 ml" })).toBeTruthy();
    });

    const increaseBtn = screen.getByRole("button", { name: /Increase quantity/i });

    // First click initiates mutation and sets in-flight state
    fireEvent.click(increaseBtn);
    await waitFor(() => {
      expect(setLineSpy).toHaveBeenCalledTimes(1);
    });

    // Rapid second click while first is pending must be blocked by isMutating check
    fireEvent.click(increaseBtn);
    expect(setLineSpy).toHaveBeenCalledTimes(1);

    // Now resolve the first mutation
    resolveMutation(sampleBasket([{ sku: "GRO-DAIRY-001", quantity: 3 }]));
    await waitFor(() => {
      expect(screen.getByLabelText("3 units")).toBeTruthy();
    });
  });

  it("retains previous authoritative quantity and displays retry action on failed update", async () => {
    const setLineSpy = vi.fn().mockRejectedValueOnce(new Error("Network timeout: merchant connector unreachable"));
    mockClient.setBasketLine = setLineSpy;

    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_test_001",
            setBasketId: () => undefined,
            lineCount: 1,
            setLineCount: () => undefined,
            totalMinor: 5600,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <BasketView />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("link", { name: "Amul Taaza Toned Milk 500 ml" })).toBeTruthy();
    });

    const increaseBtn = screen.getByRole("button", { name: /Increase quantity/i });
    fireEvent.click(increaseBtn);

    // Verify error alert is displayed
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Network timeout");
    });

    // The displayed quantity remains the authoritative server value (2), not the attempted (3)
    expect(screen.getByLabelText("2 units")).toBeTruthy();

    // Verify Retry button is rendered
    const retryBtn = screen.getByRole("button", { name: /Retry action/i });
    expect(retryBtn).toBeTruthy();

    // When clicked, retry triggers the mutation again
    setLineSpy.mockResolvedValueOnce(sampleBasket([{ sku: "GRO-DAIRY-001", quantity: 3 }]));
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(setLineSpy).toHaveBeenCalledTimes(2);
      expect(screen.getByLabelText("3 units")).toBeTruthy();
    });
  });

  it("renders SearchPanel with ADD button and transitions to stepper upon addition", async () => {
    const sampleProduct = {
      sku: "GRO-DAIRY-001",
      display_name: "Amul Taaza Toned Milk 500 ml",
      name_en: "Amul Taaza Toned Milk 500 ml",
      name_hi: "अमूल ताज़ा टोन्ड दूध ५०० मिली",
      category: "dairy",
      unit_label: "500 ml",
      unit_price_minor: 2800,
      currency: "INR",
      tax_bp: 0,
      stock_units: 42,
      is_listed: true,
      is_available: true,
      freshness: MOCK_FRESHNESS,
    };

    mockClient.search = vi.fn().mockResolvedValue({
      query: "",
      normalized_query: "",
      locale: "en-IN",
      hits: [{ ...sampleProduct, score: 100, matched_terms: ["catalogue"] }],
      freshness: MOCK_FRESHNESS,
    });

    mockClient.createBasket = vi.fn().mockResolvedValue({ basket_id: "bsk_fresh" });
    mockClient.setBasketLine = vi.fn().mockResolvedValue(sampleBasket([{ sku: "GRO-DAIRY-001", quantity: 1 }]));

    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: null,
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <SearchPanel />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Add Amul Taaza Toned Milk 500 ml to basket/i })).toBeTruthy();
    });

    const addBtn = screen.getByRole("button", { name: /Add Amul Taaza Toned Milk 500 ml to basket/i });
    fireEvent.click(addBtn);

    await waitFor(() => {
      expect(mockClient.setBasketLine).toHaveBeenCalledWith("bsk_fresh", "GRO-DAIRY-001", 1);
      expect(screen.getByRole("group", { name: /Quantity controls for Amul Taaza Toned Milk 500 ml/i })).toBeTruthy();
      expect(screen.getByLabelText("1 units")).toBeTruthy();
    });
  });

  it("renders ProductDetail with authoritative price and GST disclosure", async () => {
    mockClient.getProduct = vi.fn().mockResolvedValue({
      sku: "GRO-DAIRY-001",
      display_name: "Amul Taaza Toned Milk 500 ml",
      name_en: "Amul Taaza Toned Milk 500 ml",
      name_hi: "अमूल ताज़ा टोन्ड दूध ५०० मिली",
      category: "dairy",
      unit_label: "500 ml",
      unit_price_minor: 2800,
      currency: "INR",
      tax_bp: 500,
      stock_units: 20,
      is_listed: true,
      is_available: true,
      freshness: MOCK_FRESHNESS,
    });

    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_test_001",
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <ProductDetail sku="GRO-DAIRY-001" />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Amul Taaza Toned Milk 500 ml" })).toBeTruthy();
      expect(screen.getByText(/Applicable GST: 5%/i)).toBeTruthy();
      expect(screen.getByText("₹28.00")).toBeTruthy();
    });
  });
});

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BasketRefContext, ClientContext } from "@/components/providers";
import type { CommerceClient } from "@/lib/api/client";
import type { Basket } from "@/lib/api/types";
import { CategoryView } from "./category-view";
import { ProductDetail } from "./product-detail";

afterEach(cleanup);

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

const MOCK_FRESHNESS = {
  source: "merchant-sim:demo-grocery/v1",
  catalogue_revision: 3,
  observed_at: new Date().toISOString(),
};

const SAMPLE_OIL_PRODUCTS = [
  {
    sku: "GRO-STPL-OIL-001",
    display_name: "Freedom Refined Sunflower Oil 1 L",
    name_en: "Freedom Refined Sunflower Oil 1 L",
    name_hi: "फ्रीडम रिफाइंड सूरजमुखी तेल 1 लीटर",
    category: "staples",
    unit_label: "1 L",
    unit_price_minor: 17900,
    currency: "INR",
    tax_bp: 500,
    stock_units: 30,
    is_listed: true,
    is_available: true,
    score: 100,
    matched_terms: ["oil", "sunflower"],
    freshness: MOCK_FRESHNESS,
  },
  {
    sku: "GRO-STPL-OIL-002",
    display_name: "Fortune Kachi Ghani Mustard Oil 1 L",
    name_en: "Fortune Kachi Ghani Mustard Oil 1 L",
    name_hi: "फॉर्च्यून कच्ची घानी सरसों का तेल 1 लीटर",
    category: "staples",
    unit_label: "1 L",
    unit_price_minor: 20700,
    currency: "INR",
    tax_bp: 500,
    stock_units: 25,
    is_listed: true,
    is_available: true,
    score: 95,
    matched_terms: ["oil", "mustard"],
    freshness: MOCK_FRESHNESS,
  },
];

function sampleBasket(lines: { sku: string; quantity: number }[] = []): Basket {
  const itemsSubtotal = lines.reduce((sum, l) => sum + l.quantity * 17900, 0);
  return {
    basket_id: "bsk_cat_001",
    lines,
    code: "OK",
    quote:
      lines.length > 0
        ? {
            currency: "INR",
            lines: lines.map((l) => ({
              sku: l.sku,
              name: l.sku,
              quantity: l.quantity,
              unit_price_minor: 17900,
              subtotal_minor: l.quantity * 17900,
              tax_bp: 500,
              tax_minor: 895,
            })),
            items_subtotal_minor: itemsSubtotal,
            items_tax_minor: 895,
            delivery_fee_minor: 0,
            delivery_tax_minor: 0,
            total_minor: itemsSubtotal + 895,
            free_delivery_applied: true,
            gap_to_free_delivery_minor: 0,
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

describe("Zepto Category View & Product Detail UI Specifications", () => {
  let mockClient: Partial<CommerceClient>;

  beforeEach(() => {
    mockClient = {
      mode: "mock",
      getBasket: vi.fn().mockResolvedValue(sampleBasket([])),
      setBasketLine: vi.fn().mockResolvedValue(sampleBasket([{ sku: "GRO-STPL-OIL-001", quantity: 1 }])),
      createBasket: vi.fn().mockResolvedValue({ basket_id: "bsk_cat_001" }),
      getProduct: vi.fn().mockResolvedValue({
        sku: "GRO-STPL-OIL-001",
        display_name: "Freedom Refined Sunflower Oil 1 L",
        name_en: "Freedom Refined Sunflower Oil 1 L",
        name_hi: "फ्रीडम रिफाइंड सूरजमुखी तेल 1 लीटर",
        category: "staples",
        unit_label: "1 L",
        unit_price_minor: 17900,
        currency: "INR",
        tax_bp: 500,
        stock_units: 30,
        is_listed: true,
        is_available: true,
        freshness: MOCK_FRESHNESS,
      }),
    };
  });

  it("renders Category View with subcategory sidebar, promo store banner, and product grid matching Zepto", async () => {
    const onSelectCategory = vi.fn();

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
          <CategoryView
            category="staples"
            hits={SAMPLE_OIL_PRODUCTS}
            onSelectCategory={onSelectCategory}
          />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    // 1. Breadcrumbs & Subcategories
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toBeTruthy();
    expect(screen.getAllByText("Oil").length).toBeGreaterThanOrEqual(2);

    // 2. Subcategories sidebar
    expect(screen.getByText("Healthy Picks")).toBeTruthy();
    expect(screen.getByText("Olive & Cold Pressed")).toBeTruthy();
    expect(screen.getByText("Atta")).toBeTruthy();

    // 3. Category Promo Store Banner
    expect(screen.getByText("UP TO 60% OFF")).toBeTruthy();
    expect(screen.getByText("OLIVE OIL STORE")).toBeTruthy();
    expect(screen.getByText(/Explore/i)).toBeTruthy();

    // 4. Products grid
    expect(screen.getByText("Freedom Refined Sunflower Oil 1 L")).toBeTruthy();
    expect(screen.getByText("Fortune Kachi Ghani Mustard Oil 1 L")).toBeTruthy();
    expect(screen.getByText("₹179.00")).toBeTruthy();
    expect(screen.getByText("₹207.00")).toBeTruthy();

    // 5. Overlapping ADD button
    const addBtn = screen.getByRole("button", { name: /Add Freedom Refined Sunflower Oil 1 L to basket/i });
    expect(addBtn).toBeTruthy();

    fireEvent.click(addBtn);
    await waitFor(() => {
      expect(mockClient.setBasketLine).toHaveBeenCalled();
    });
  });

  it("renders Product Detail page matching Zepto 2-column layout with full-width CTA directly beneath image", async () => {
    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_cat_001",
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <ProductDetail sku="GRO-STPL-OIL-001" />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      // Heading
      expect(screen.getByRole("heading", { name: "Freedom Refined Sunflower Oil 1 L" })).toBeTruthy();
      // Price
      expect(screen.getByText("₹179.00")).toBeTruthy();
      // Open Box Verification
      expect(screen.getAllByText(/Open Box Verification/i).length).toBeGreaterThan(0);
      // Net Qty & Rating
      expect(screen.getByText(/Net Qty: 1 L/i)).toBeTruthy();
      expect(screen.getByText("★")).toBeTruthy();
      // UPI / Platform Fee highlight card
      expect(screen.getByText(/₹0 Platform Fee with Instant UPI/i)).toBeTruthy();
      // Assurance badges
      expect(screen.getByText("7 Days Quality Guarantee")).toBeTruthy();
      expect(screen.getByText("Fast Delivery")).toBeTruthy();
      // Full-width Add to Cart CTA
      expect(screen.getByRole("button", { name: /Add Freedom Refined Sunflower Oil 1 L to basket/i })).toBeTruthy();
    });
  });

  it("renders Apple iPhone Product Detail page matching Zepto Screenshot 3", async () => {
    mockClient.getProduct = vi.fn().mockResolvedValue({
      sku: "ELEC-IPHONE-16",
      display_name: "Apple iPhone 17 Pro | 256 GB | Cosmic Orange",
      name_en: "Apple iPhone 17 Pro | 256 GB | Cosmic Orange",
      name_hi: "एप्पल आईफोन 17 प्रो",
      category: "electronics",
      unit_label: "1 pc",
      unit_price_minor: 12689900,
      currency: "INR",
      tax_bp: 1800,
      stock_units: 12,
      is_listed: true,
      is_available: true,
      freshness: MOCK_FRESHNESS,
    });

    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_cat_001",
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <ProductDetail sku="ELEC-IPHONE-16" />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /Apple iPhone 17 Pro/i })).toBeTruthy();
      expect(screen.getByText("₹1,26,899.00")).toBeTruthy();
      expect(screen.getByText("Apple ›")).toBeTruthy();
      expect(screen.getByText(/Compare/i)).toBeTruthy();
      expect(screen.getByText(/No Cost EMI available/i)).toBeTruthy();
      expect(screen.getAllByText(/Open Box Verification/i).length).toBeGreaterThan(0);
      expect(screen.getByRole("button", { name: /Add Apple iPhone 17 Pro/i })).toBeTruthy();
    });
  });
});

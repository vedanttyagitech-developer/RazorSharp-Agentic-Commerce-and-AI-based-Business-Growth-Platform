/**
 * `useCart` against a provider whose count moves without this hook moving it.
 *
 * The cart is written from more than one place on the same screen -- the shelf's own
 * stepper through this hook, and the press on a RazorAI line proposal through the panel.
 * The provider publishes counts; this hook holds lines. These tests pin the one rule that
 * keeps the two from disagreeing in front of a buyer: a provider count the held lines do
 * not add up to is re-read, and a count they do add up to is not.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import { type Cart, CartSchema } from "@/lib/api/types";

import { useCart } from "./use-cart";

const CART_ID = "01a07202-1ba8-7297-a54d-5116246acf0f";

const mocks = vi.hoisted(() => ({
  api: { cart: vi.fn(), setLine: vi.fn(), createCart: vi.fn() },
  newIdempotencyKey: vi.fn(() => "key-1"),
  context: {
    cartId: "01a07202-1ba8-7297-a54d-5116246acf0f" as string | null,
    lineCount: 1,
    itemCount: 1,
    totalMinor: 2800 as number | null,
    currency: "INR",
    setCartId: vi.fn(),
    setLineCount: vi.fn(),
    refresh: vi.fn(async () => {}),
  },
}));

vi.mock("@/lib/api/client", () => ({
  api: mocks.api,
  newIdempotencyKey: mocks.newIdempotencyKey,
}));
vi.mock("@/components/providers", () => ({
  useCartContext: () => mocks.context,
}));

/** A cart holding `quantity` of one line, unquoted: the count is all these tests read. */
function holding(quantity: number): Cart {
  return CartSchema.parse({
    cart_id: CART_ID,
    lines: [{ sku: "AMUL-DAIRY-001", quantity }],
    code: "quoted",
    quote: null,
    unavailable: [],
    freshness: { source: "merchant_sim", catalogue_revision: 7, observed_at: "2026-09-05T14:40:00Z" },
    stale: false,
  });
}

beforeEach(() => {
  mocks.context.itemCount = 1;
  mocks.api.cart.mockReset();
  mocks.api.setLine.mockReset();
});
afterEach(cleanup);

describe("the held lines follow the provider's count", () => {
  it("re-reads when the provider's count is not what the held lines add up to", async () => {
    mocks.api.cart.mockResolvedValue(holding(1));
    const { result, rerender } = renderHook(() => useCart());
    await waitFor(() => expect(result.current.quantities["AMUL-DAIRY-001"]).toBe(1));
    expect(mocks.api.cart).toHaveBeenCalledTimes(1);

    // The panel's press took the line to 3 and asked the provider to re-read.
    mocks.api.cart.mockResolvedValue(holding(3));
    mocks.context.itemCount = 3;
    rerender();
    await waitFor(() => expect(result.current.quantities["AMUL-DAIRY-001"]).toBe(3));
    expect(mocks.api.cart).toHaveBeenCalledTimes(2);
  });

  it("does not re-read when the provider's count is what the held lines add up to", async () => {
    mocks.api.cart.mockResolvedValue(holding(1));
    const { result, rerender } = renderHook(() => useCart());
    await waitFor(() => expect(result.current.quantities["AMUL-DAIRY-001"]).toBe(1));
    rerender();
    rerender();
    expect(mocks.api.cart).toHaveBeenCalledTimes(1);
  });

  it("costs no second fetch after its own write, once the provider catches up", async () => {
    mocks.api.cart.mockResolvedValue(holding(1));
    mocks.api.setLine.mockResolvedValue(holding(2));
    const { result, rerender } = renderHook(() => useCart());
    await waitFor(() => expect(result.current.quantities["AMUL-DAIRY-001"]).toBe(1));

    await act(async () => {
      await result.current.setQuantity("AMUL-DAIRY-001", 2);
    });
    expect(result.current.quantities["AMUL-DAIRY-001"]).toBe(2);
    // The provider re-read and now says what this hook already holds.
    mocks.context.itemCount = 2;
    rerender();
    await act(async () => {});
    expect(mocks.api.cart).toHaveBeenCalledTimes(1);
  });
});

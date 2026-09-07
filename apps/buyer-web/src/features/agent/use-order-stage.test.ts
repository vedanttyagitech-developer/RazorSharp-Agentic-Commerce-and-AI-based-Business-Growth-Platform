/**
 * The order-stage hook derives one of six journey stages from the path and, when there is
 * a checkout to read, that checkout's live state — and keeps the checkout current with a
 * 2.5s poll it cleans up on unmount and on an id change.
 *
 * The path and the checkout state are the whole input, so both are mocked: `next/navigation`
 * for `usePathname` (the `vi.hoisted` + `mocks.pathname` pattern the launcher test uses) and
 * `@/lib/api/client` for `api.checkout` (the `vi.hoisted` api-object pattern the cart test
 * uses). Fake timers drive the poll. No jest-dom — plain `toBe` / `toEqual` / `toBeDefined`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import type { Checkout } from "@/lib/api/types";

import { useOrderStage } from "./use-order-stage";

const mocks = vi.hoisted(() => ({
  pathname: "/" as string | null,
  api: {
    checkout: vi.fn(),
  },
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
}));

vi.mock("@/lib/api/client", () => ({
  api: mocks.api,
  newIdempotencyKey: () => "key-1",
}));

/**
 * A checkout in the given state, trimmed to the fields the hook reads. The kernel sends a
 * full checkout; the hook only ever looks at `state`, so the rest is filled to satisfy the
 * type without pretending the shape is under test here.
 */
function checkoutInState(state: string): Checkout {
  return {
    checkout_id: "chk_1",
    cart_id: "bsk_1",
    state,
    current_version: 1,
    versions: [],
    approval_card: null,
    attempt: null,
    order_id: null,
    order_reference: null,
    deltas: [],
    cancellable: true,
    updated_at: "2026-09-05T00:00:00Z",
  } as Checkout;
}

beforeEach(() => {
  mocks.pathname = "/";
  mocks.api.checkout.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe("stage from the path and the cart, with no checkout to read", () => {
  it("is discover on an ordinary path with an empty cart", () => {
    mocks.pathname = "/";
    const { result } = renderHook(() => useOrderStage({ hasLines: false }));
    expect(result.current.stage).toBe("discover");
    expect(result.current.checkout).toBe(null);
    expect(mocks.api.checkout).not.toHaveBeenCalled();
  });

  it("is cart on an ordinary path once the cart has lines", () => {
    mocks.pathname = "/";
    const { result } = renderHook(() => useOrderStage({ hasLines: true }));
    expect(result.current.stage).toBe("cart");
  });

  it("is reserve on /reserve-pay, whatever the cart holds", () => {
    mocks.pathname = "/reserve-pay";
    const { result } = renderHook(() => useOrderStage({ hasLines: false }));
    expect(result.current.stage).toBe("reserve");
  });

  it("is order on /orders/{id}", () => {
    mocks.pathname = "/orders/01a07300-9c2b-7bd1-a10e-77f0e0e0e0e0";
    const { result } = renderHook(() => useOrderStage({ hasLines: true }));
    expect(result.current.stage).toBe("order");
  });
});

describe("stage from a checkout's state", () => {
  const cases: ReadonlyArray<[string, string]> = [
    ["APPROVAL_REQUIRED", "approve"],
    ["APPROVED", "pay"],
    ["EXECUTION_PENDING", "pay"],
    ["AWAITING_PAYMENT", "pay"],
    ["PAYMENT_UNKNOWN", "pay"],
    ["PAID", "order"],
  ];

  for (const [state, expected] of cases) {
    it(`maps ${state} -> ${expected} for a /checkout/{id} path`, async () => {
      vi.useFakeTimers();
      mocks.pathname = "/checkout/chk_1";
      mocks.api.checkout.mockResolvedValue(checkoutInState(state));

      const { result } = renderHook(() => useOrderStage({ hasLines: true }));

      // Immediate first read is deferred to a 0ms macrotask; flush it and its promise.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(mocks.api.checkout).toHaveBeenCalledWith("chk_1");
      expect(result.current.stage).toBe(expected);
      expect(result.current.checkout?.state).toBe(state);
    });
  }

  it("reads a checkout the panel passed via checkoutId when the path names none", async () => {
    vi.useFakeTimers();
    mocks.pathname = "/";
    mocks.api.checkout.mockResolvedValue(checkoutInState("APPROVAL_REQUIRED"));

    const { result } = renderHook(() => useOrderStage({ hasLines: true, checkoutId: "chk_1" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(mocks.api.checkout).toHaveBeenCalledWith("chk_1");
    expect(result.current.stage).toBe("approve");
  });

  it("falls through to cart/discover while the checkout state is not yet known", () => {
    vi.useFakeTimers();
    mocks.pathname = "/checkout/chk_1";
    // Never resolves this tick — state unknown.
    mocks.api.checkout.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useOrderStage({ hasLines: true, checkoutId: "chk_1" }));
    // Before any read resolves, the cart rule governs.
    expect(result.current.stage).toBe("cart");
    expect(result.current.checkout).toBe(null);
  });
});

describe("the poll", () => {
  it("re-reads on the 2.5s interval and keeps the latest checkout", async () => {
    vi.useFakeTimers();
    mocks.pathname = "/checkout/chk_1";
    mocks.api.checkout
      .mockResolvedValueOnce(checkoutInState("APPROVAL_REQUIRED"))
      .mockResolvedValueOnce(checkoutInState("PAID"));

    const { result } = renderHook(() => useOrderStage({ hasLines: true }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.stage).toBe("approve");
    expect(mocks.api.checkout).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(mocks.api.checkout).toHaveBeenCalledTimes(2);
    expect(result.current.stage).toBe("order");
  });

  it("clears the interval on unmount — no read after the hook is gone", async () => {
    vi.useFakeTimers();
    mocks.pathname = "/checkout/chk_1";
    mocks.api.checkout.mockResolvedValue(checkoutInState("APPROVAL_REQUIRED"));

    const { unmount } = renderHook(() => useOrderStage({ hasLines: true }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mocks.api.checkout).toHaveBeenCalledTimes(1);

    unmount();
    const callsAtUnmount = mocks.api.checkout.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(mocks.api.checkout.mock.calls.length).toBe(callsAtUnmount);
  });

  it("keeps the previous checkout when a poll read fails", async () => {
    vi.useFakeTimers();
    mocks.pathname = "/checkout/chk_1";
    mocks.api.checkout
      .mockResolvedValueOnce(checkoutInState("APPROVAL_REQUIRED"))
      .mockRejectedValueOnce(new Error("network down"));

    const { result } = renderHook(() => useOrderStage({ hasLines: true }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.stage).toBe("approve");

    // Second poll rejects — the previous value must survive.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(mocks.api.checkout).toHaveBeenCalledTimes(2);
    expect(result.current.stage).toBe("approve");
    expect(result.current.checkout?.state).toBe("APPROVAL_REQUIRED");
  });
});

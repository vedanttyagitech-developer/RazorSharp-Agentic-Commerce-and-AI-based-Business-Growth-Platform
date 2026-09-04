"use client";

import { useCallback, useState } from "react";

import { useBasketRef, useClient } from "@/components/providers";
import { isApiError } from "@/lib/api/problem";
import type { Basket } from "@/lib/api/types";

/**
 * Basket mutations shared by the storefront, product and basket pages. PUT sets an
 * absolute quantity, so "add one" reads the current line first; the server re-quotes.
 *
 * The remembered basket id is a convenience, not truth: if the API no longer knows it
 * (session expired, backend restarted, fixture reset) the id is dropped and a fresh
 * basket is created instead of failing every add from then on.
 */
export function useBasketActions() {
  const client = useClient();
  const { basketId, setBasketId, setLineCount } = useBasketRef();
  const [busySku, setBusySku] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastBasket, setLastBasket] = useState<Basket | null>(null);

  const loadOrCreate = useCallback(async (): Promise<Basket> => {
    if (basketId) {
      try {
        return await client.getBasket(basketId);
      } catch (cause) {
        if (!(isApiError(cause) && cause.status === 404)) throw cause;
        setBasketId(null);
      }
    }
    const created = await client.createBasket();
    setBasketId(created.basket_id);
    return created;
  }, [basketId, client, setBasketId]);

  const mutate = useCallback(
    async (sku: string, quantityFor: (current: Basket) => number): Promise<Basket | null> => {
      setBusySku(sku);
      setError(null);
      try {
        const current = await loadOrCreate();
        const basket = await client.setBasketLine(current.basket_id, sku, quantityFor(current));
        setLineCount(basket.lines.length);
        setLastBasket(basket);
        return basket;
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Basket update failed");
        return null;
      } finally {
        setBusySku(null);
      }
    },
    [client, loadOrCreate, setLineCount],
  );

  const setQuantity = useCallback((sku: string, quantity: number) => mutate(sku, () => quantity), [mutate]);
  const addOne = useCallback(
    (sku: string) => mutate(sku, (current) => (current.lines.find((line) => line.sku === sku)?.quantity ?? 0) + 1),
    [mutate],
  );

  return { basketId, busySku, error, lastBasket, addOne, setQuantity };
}

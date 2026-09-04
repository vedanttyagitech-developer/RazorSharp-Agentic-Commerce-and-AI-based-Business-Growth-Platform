"use client";

import { useCallback, useState } from "react";

import { useBasketRef, useClient } from "@/components/providers";
import type { Basket } from "@/lib/api/types";

/**
 * Basket mutations shared by the storefront, product and basket pages. PUT sets an
 * absolute quantity, so "add one" reads the current line first; the server re-quotes.
 */
export function useBasketActions() {
  const client = useClient();
  const { basketId, setBasketId, setLineCount } = useBasketRef();
  const [busySku, setBusySku] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastBasket, setLastBasket] = useState<Basket | null>(null);

  const ensureBasket = useCallback(async (): Promise<string> => {
    if (basketId) return basketId;
    const created = await client.createBasket();
    setBasketId(created.basket_id);
    return created.basket_id;
  }, [basketId, client, setBasketId]);

  const setQuantity = useCallback(
    async (sku: string, quantity: number): Promise<Basket | null> => {
      setBusySku(sku);
      setError(null);
      try {
        const id = await ensureBasket();
        const basket = await client.setBasketLine(id, sku, quantity);
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
    [client, ensureBasket, setLineCount],
  );

  const addOne = useCallback(
    async (sku: string): Promise<Basket | null> => {
      setBusySku(sku);
      setError(null);
      try {
        const id = await ensureBasket();
        const current = await client.getBasket(id);
        const existing = current.lines.find((line) => line.sku === sku)?.quantity ?? 0;
        const basket = await client.setBasketLine(id, sku, existing + 1);
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
    [client, ensureBasket, setLineCount],
  );

  return { basketId, busySku, error, lastBasket, addOne, setQuantity };
}

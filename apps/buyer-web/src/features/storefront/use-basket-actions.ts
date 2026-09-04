"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useBasketRef, useClient } from "@/components/providers";
import { isApiError } from "@/lib/api/problem";
import type { Basket } from "@/lib/api/types";

export interface BasketFeedback {
  sku: string;
  message: string;
}

export interface FailedBasketAction {
  sku: string;
  quantity: number;
}

/**
 * Basket mutations shared by the storefront, product and basket pages.
 *
 * Prevents accidental duplicate clicks while an action is pending.
 * Tracks failure states with recovery without falsely indicating success.
 */
export function useBasketActions() {
  const client = useClient();
  const { basketId, setBasketId, setLineCount, setTotalMinor, setCurrency } = useBasketRef();
  const [busySku, setBusySku] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<BasketFeedback | null>(null);
  const [lastBasket, setLastBasket] = useState<Basket | null>(null);
  const [lastFailedAction, setLastFailedAction] = useState<FailedBasketAction | null>(null);

  // In-flight guard ref to prevent race conditions during rapid multi-clicks
  const isMutatingRef = useRef(false);

  // Synchronize lastBasket on mount if a basket ID is already active in session/context
  useEffect(() => {
    let cancelled = false;
    if (basketId && !lastBasket) {
      client
        .getBasket(basketId)
        .then((basket) => {
          if (!cancelled) {
            setLastBasket(basket);
            setLineCount(basket.lines.length);
            setTotalMinor(basket.quote?.total_minor ?? null);
            if (basket.quote?.currency) setCurrency(basket.quote.currency);
          }
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [basketId, client, lastBasket, setCurrency, setLineCount, setTotalMinor]);

  const ensureBasketId = useCallback(async (): Promise<string> => {
    if (basketId) return basketId;
    const created = await client.createBasket();
    setBasketId(created.basket_id);
    return created.basket_id;
  }, [basketId, client, setBasketId]);

  const mutate = useCallback(
    async (
      sku: string,
      targetQuantity: number,
      displayName?: string,
    ): Promise<Basket | null> => {
      // Prevent accidental duplicate clicks while an action is pending
      if (isMutatingRef.current) {
        return null;
      }

      isMutatingRef.current = true;
      setBusySku(sku);
      setError(null);
      setLastFailedAction(null);

      try {
        let activeBasketId = await ensureBasketId();
        let basket: Basket;
        try {
          basket = await client.setBasketLine(activeBasketId, sku, targetQuantity);
        } catch (cause) {
          if (isApiError(cause) && cause.status === 404) {
            // Stale basket ID; create fresh and retry once
            setBasketId(null);
            const fresh = await client.createBasket();
            setBasketId(fresh.basket_id);
            activeBasketId = fresh.basket_id;
            basket = await client.setBasketLine(activeBasketId, sku, targetQuantity);
          } else {
            throw cause;
          }
        }

        setLineCount(basket.lines.length);
        setTotalMinor(basket.quote?.total_minor ?? null);
        if (basket.quote?.currency) setCurrency(basket.quote.currency);
        setLastBasket(basket);

        const name = displayName ?? sku;
        if (targetQuantity === 0) {
          setFeedback({ sku, message: `Removed ${name} from basket` });
        } else {
          setFeedback({ sku, message: `Updated ${name} (qty: ${targetQuantity}) in basket` });
        }

        return basket;
      } catch (cause) {
        const message = cause instanceof Error ? cause.message : "Basket update failed";
        setError(message);
        setLastFailedAction({ sku, quantity: targetQuantity });
        return null;
      } finally {
        isMutatingRef.current = false;
        setBusySku(null);
      }
    },
    [client, ensureBasketId, setBasketId, setLineCount, setTotalMinor, setCurrency],
  );

  const setQuantity = useCallback(
    (sku: string, quantity: number, displayName?: string) =>
      mutate(sku, quantity, displayName),
    [mutate],
  );

  const addOne = useCallback(
    (sku: string, displayName?: string) => {
      if (isMutatingRef.current) return Promise.resolve(null);
      const existing = lastBasket?.lines.find((line) => line.sku === sku)?.quantity ?? 0;
      return mutate(sku, existing + 1, displayName);
    },
    [lastBasket, mutate],
  );

  const retryLastAction = useCallback(async (): Promise<Basket | null> => {
    if (!lastFailedAction) return null;
    return mutate(lastFailedAction.sku, lastFailedAction.quantity);
  }, [lastFailedAction, mutate]);

  const clearError = useCallback(() => setError(null), []);
  const clearFeedback = useCallback(() => setFeedback(null), []);

  return {
    basketId,
    busySku,
    isMutating: busySku !== null,
    error,
    feedback,
    lastBasket,
    lastFailedAction,
    addOne,
    setQuantity,
    retryLastAction,
    clearError,
    clearFeedback,
  };
}

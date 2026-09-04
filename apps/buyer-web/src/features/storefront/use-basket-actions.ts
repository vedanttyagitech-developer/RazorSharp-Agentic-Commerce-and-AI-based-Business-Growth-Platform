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

  const loadOrCreate = useCallback(async (): Promise<Basket> => {
    if (basketId) {
      try {
        const current = await client.getBasket(basketId);
        setLastBasket(current);
        return current;
      } catch (cause) {
        if (!(isApiError(cause) && cause.status === 404)) throw cause;
        setBasketId(null);
      }
    }
    const created = await client.createBasket();
    setBasketId(created.basket_id);
    const emptyFresh: Basket = {
      basket_id: created.basket_id,
      lines: [],
      code: "OK",
      quote: null,
      unavailable: [],
      freshness: {
        source: "commerce-api",
        catalogue_revision: 0,
        observed_at: new Date().toISOString(),
      },
      stale: false,
    };
    setLastBasket(emptyFresh);
    return emptyFresh;
  }, [basketId, client, setBasketId]);

  const mutate = useCallback(
    async (
      sku: string,
      targetQuantity: number | ((current: Basket) => number),
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
        let currentBasket = lastBasket;
        if (!currentBasket || currentBasket.basket_id !== basketId) {
          currentBasket = await loadOrCreate();
        }

        const quantity =
          typeof targetQuantity === "function" ? targetQuantity(currentBasket) : targetQuantity;
        let activeBasketId = currentBasket.basket_id;

        // Optimistic update: render instant feedback on mobile while network mutation resolves
        const previousBasket = currentBasket;
        const optimisticLines = currentBasket.lines
          .filter((line) => line.sku !== sku || quantity > 0)
          .map((line) => (line.sku === sku ? { ...line, quantity } : line));
        if (quantity > 0 && !optimisticLines.some((l) => l.sku === sku)) {
          optimisticLines.push({
            sku,
            quantity,
          });
        }
        const optimisticBasket: Basket = {
          ...currentBasket,
          lines: optimisticLines,
        };
        setLastBasket(optimisticBasket);
        setLineCount(optimisticLines.length);

        let basket: Basket;
        try {
          basket = await client.setBasketLine(activeBasketId, sku, quantity);
        } catch (cause) {
          // Honest rollback to authoritative server state on any failure/rejection
          setLastBasket(previousBasket);
          setLineCount(previousBasket.lines.length);
          setTotalMinor(previousBasket.quote?.total_minor ?? null);
          if (previousBasket.quote?.currency) setCurrency(previousBasket.quote.currency);
          if (isApiError(cause) && cause.status === 404) {
            // Stale basket ID; create fresh and retry once
            setBasketId(null);
            const fresh = await client.createBasket();
            setBasketId(fresh.basket_id);
            activeBasketId = fresh.basket_id;
            const emptyRetry: Basket = {
              basket_id: fresh.basket_id,
              lines: [],
              code: "OK",
              quote: null,
              unavailable: [],
              freshness: {
                source: "commerce-api",
                catalogue_revision: 0,
                observed_at: new Date().toISOString(),
              },
              stale: false,
            };
            const retryQty =
              typeof targetQuantity === "function" ? targetQuantity(emptyRetry) : targetQuantity;
            basket = await client.setBasketLine(activeBasketId, sku, retryQty);
          } else {
            throw cause;
          }
        }

        setLineCount(basket.lines.length);
        setTotalMinor(basket.quote?.total_minor ?? null);
        if (basket.quote?.currency) setCurrency(basket.quote.currency);
        setLastBasket(basket);

        const name = displayName ?? sku;
        if (quantity === 0) {
          setFeedback({ sku, message: `Removed ${name} from basket` });
        } else {
          setFeedback({ sku, message: `Updated ${name} (qty: ${quantity}) in basket` });
        }

        return basket;
      } catch (cause) {
        const message = cause instanceof Error ? cause.message : "Basket update failed";
        setError(message);
        if (typeof targetQuantity === "number") {
          setLastFailedAction({ sku, quantity: targetQuantity });
        }
        return null;
      } finally {
        isMutatingRef.current = false;
        setBusySku(null);
      }
    },
    [basketId, client, lastBasket, loadOrCreate, setBasketId, setLineCount, setTotalMinor, setCurrency],
  );

  const setQuantity = useCallback(
    (sku: string, quantity: number, displayName?: string) =>
      mutate(sku, quantity, displayName),
    [mutate],
  );

  const addOne = useCallback(
    async (sku: string, displayName?: string): Promise<Basket | null> => {
      if (isMutatingRef.current) return null;
      // Re-read authoritative basket state to ensure accurate quantity increment
      if (basketId) {
        try {
          const fresh = await client.getBasket(basketId);
          setLastBasket(fresh);
          const existing = fresh.lines.find((line) => line.sku === sku)?.quantity ?? 0;
          return mutate(sku, existing + 1, displayName);
        } catch {
          // If refresh fails, fall back to mutating with function
        }
      }
      return mutate(
        sku,
        (current) => (current.lines.find((line) => line.sku === sku)?.quantity ?? 0) + 1,
        displayName,
      );
    },
    [basketId, client, mutate],
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

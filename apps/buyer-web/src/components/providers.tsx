/**
 * The one piece of state the whole storefront shares: which basket this browser is
 * holding, and how much is in it.
 *
 * It lives outside React, in the small store below, and the provider only subscribes to
 * it. That is a deliberate inversion. The basket is written from the header, from every
 * listing page and from the basket screen, it has to survive a reload, and it has to
 * agree across two open tabs; a value with that many writers and that lifetime is an
 * external store, and pretending otherwise means an effect on mount racing the first
 * render to catch up. `useSyncExternalStore` is the supported way to read one, so the
 * cart pill is right on the first paint after a reload instead of flickering from empty,
 * and a `storage` event from another tab is already the subscription it needs.
 *
 * What is persisted and what is not is the load-bearing decision here. The identifier
 * and the line count go to `localStorage`; the quantities and the total never do. Those
 * two come from `GET /v1/baskets/{id}` on every visit and are the server's own figures,
 * `total_minor` copied across untouched. A remembered count one line out of date is a
 * cosmetic error. A remembered total is a price a buyer could read as current after the
 * merchant has already moved it, and refusing exactly that staleness is what this
 * project is for.
 */
"use client";

import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

import { ApiError, api } from "@/lib/api/client";

const STORAGE_KEY = "acr.basket";

export interface BasketSnapshot {
  /** The basket/cart this browser holds, or null before anything has been added. */
  basketId: string | null;
  cartId: string | null;
  /** Distinct lines, which is what the API's `lines` array counts. */
  lineCount: number;
  /** Quantities summed across lines. A count of things, never a sum of money. */
  itemCount: number;
  /** The quote's `total_minor`, verbatim. Null until the server has quoted. */
  totalMinor: number | null;
  currency: string;
}

export interface BasketContextValue extends BasketSnapshot {
  setBasketId: (id: string | null) => void;
  setCartId: (id: string | null) => void;
  setLineCount: (count: number) => void;
  /** Re-read the basket from the API and republish every number in this context. */
  refresh: () => Promise<void>;
}

const NO_BASKET: BasketSnapshot = Object.freeze({
  basketId: null,
  cartId: null,
  lineCount: 0,
  itemCount: 0,
  totalMinor: null,
  currency: "INR",
});

/* -------------------------------------------------------------------- the store */

let snapshot: BasketSnapshot = NO_BASKET;
let readStorageOnce = false;
const listeners = new Set<() => void>();

/*
 * Every storage access is wrapped because `localStorage` is not merely empty in a
 * private window or under a blocked-cookies setting: reading the property throws.
 */

function readStored(): { basketId: string; lineCount: number } | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const record = parsed as { basketId?: unknown; lineCount?: unknown };
    if (typeof record.basketId !== "string" || record.basketId === "") return null;
    const count = typeof record.lineCount === "number" && Number.isFinite(record.lineCount) ? record.lineCount : 0;
    return { basketId: record.basketId, lineCount: count };
  } catch {
    return null;
  }
}

function writeStored(basketId: string | null, lineCount: number): void {
  try {
    if (basketId) window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ basketId, lineCount }));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Persistence is a convenience; a browser that refuses it still gets a live basket
    // for as long as the tab is open.
  }
}

function emit(): void {
  for (const listener of listeners) listener();
}

/**
 * Adopt what storage holds. Anything the server told us about a *different* basket is
 * dropped rather than carried over, because a total belongs to one basket only.
 */
function adoptStored(): void {
  const stored = readStored();
  if (!stored) {
    if (snapshot.basketId !== null) snapshot = NO_BASKET;
    return;
  }
  if (stored.basketId === snapshot.basketId) {
    if (stored.lineCount !== snapshot.lineCount) snapshot = { ...snapshot, lineCount: stored.lineCount };
    return;
  }
  snapshot = { ...NO_BASKET, basketId: stored.basketId, cartId: stored.basketId, lineCount: stored.lineCount };
}

/** Merge a change in, persist the durable half of it, and tell every subscriber. */
function publish(change: Partial<BasketSnapshot>): void {
  const next = { ...snapshot, ...change };
  if (
    next.basketId === snapshot.basketId &&
    next.lineCount === snapshot.lineCount &&
    next.itemCount === snapshot.itemCount &&
    next.totalMinor === snapshot.totalMinor &&
    next.currency === snapshot.currency
  ) {
    return;
  }
  snapshot = next;
  writeStored(next.basketId, next.lineCount);
  emit();
}

function getSnapshot(): BasketSnapshot {
  if (!readStorageOnce) {
    readStorageOnce = true;
    adoptStored();
  }
  return snapshot;
}

/** No browser, no basket. The server renders an empty cart and the client corrects it. */
function getServerSnapshot(): BasketSnapshot {
  return NO_BASKET;
}

function onStorageEvent(event: StorageEvent): void {
  // A null key means the whole store was cleared, which concerns this basket too.
  if (event.key !== null && event.key !== STORAGE_KEY) return;
  adoptStored();
  emit();
}

function subscribe(onStoreChange: () => void): () => void {
  listeners.add(onStoreChange);
  window.addEventListener("storage", onStorageEvent);
  return () => {
    listeners.delete(onStoreChange);
    window.removeEventListener("storage", onStorageEvent);
  };
}

/* ---------------------------------------------------------------- the operations */

/** Adopt a basket/cart, or forget the one held. Its figures are unknown until `refresh`. */
function setBasketId(id: string | null): void {
  if (id === snapshot.basketId) return;
  publish({ ...NO_BASKET, basketId: id, cartId: id });
}

const setCartId = setBasketId;

function setLineCount(count: number): void {
  if (!snapshot.basketId) return;
  publish({ lineCount: count });
}

async function refresh(): Promise<void> {
  const id = snapshot.basketId;
  if (!id) return;
  try {
    const basket = await api.basket(id);
    // Another tab may have moved on while this request was in flight.
    if (snapshot.basketId !== id) return;
    publish({
      lineCount: basket.lines.length,
      itemCount: basket.lines.reduce((total, line) => total + line.quantity, 0),
      totalMinor: basket.quote ? basket.quote.total_minor : null,
      currency: basket.quote ? basket.quote.currency : "INR",
    });
  } catch (error) {
    /*
     * A basket the server has never heard of is not a basket. Forgetting it is the
     * honest response: keeping the remembered count would show a cart badge for
     * something that can no longer be quoted, approved or paid for. Every other failure
     * is transient and leaves the last known figures alone.
     */
    if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
      if (snapshot.basketId === id) publish(NO_BASKET);
    }
  }
}

/* ----------------------------------------------------------------- the provider */

const BasketContext = createContext<BasketContextValue | null>(null);

/** The basket the header, the basket screen and RazorAI's proposals all agree on. */
export function useBasketContext(): BasketContextValue {
  const value = useContext(BasketContext);
  if (!value) throw new Error("useBasketContext must be used inside <Providers>");
  return value;
}

export function Providers({ children }: { children: ReactNode }) {
  const basket = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  // Whichever basket this browser turns out to be holding -- restored from storage,
  // adopted here, or adopted in another tab -- reconcile it with the server once.
  useEffect(() => {
    void refresh();
  }, [basket.basketId]);

  const value = useMemo<BasketContextValue>(
    () => ({ ...basket, setBasketId, setCartId, setLineCount, refresh }),
    [basket],
  );

  return <BasketContext.Provider value={value}>{children}</BasketContext.Provider>;
}

export const useCartContext = useBasketContext;
export type CartSnapshot = BasketSnapshot;
export type CartContextValue = BasketContextValue;

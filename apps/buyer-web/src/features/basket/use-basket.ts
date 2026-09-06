/**
 * The basket, as the rest of the storefront sees it.
 *
 * Three surfaces write to one basket -- a product card, the basket page, and RazorAI
 * proposing an addition -- so the quantity a buyer sees has to come from one place. That
 * place is the server. Every mutation here is `PUT .../lines/{sku}` with an absolute
 * quantity, and the response is the whole re-quoted basket, which is stored verbatim.
 * This module never adds two amounts together; it does not know how to.
 *
 * Quantity is the one number the browser is allowed to have an opinion about, because a
 * quantity is buyer intent rather than merchant arithmetic. A tap on `+` shows the new
 * count immediately and the money panel dims until the server has re-priced it, so the
 * buyer never reads a total that no component actually computed.
 *
 * Writes are serialized through a promise chain. The quantity is absolute, so two taps
 * that overtake each other would otherwise leave the basket holding whichever response
 * happened to land second.
 */
"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { useBasketContext } from "@/components/providers";
import { api, newIdempotencyKey } from "@/lib/api/client";
import { ApiError, humanMessage } from "@/lib/api/problem";
import type { Basket, Unavailability } from "@/lib/api/types";

/** Mirrors `basket_service.MAX_LINE_QUANTITY`, so the refusal is legible before the round trip. */
const MAX_LINE_QUANTITY = 99;

/**
 * Where the remembered product names live, and why they are allowed to live there.
 *
 * `providers.tsx` sets the rule this follows: the basket identifier and the line count are
 * persisted, quantities and totals never are, because a remembered figure is a price a
 * buyer could read as current after the merchant has moved it. A name is on the harmless
 * side of that line -- it is a label, not an amount, nothing is computed from it, and a
 * stale one costs a buyer nothing. It is kept only so that a buyer who reloads a basket
 * the merchant has declined sees three products rather than three raw SKUs; every price
 * on the page still comes from the response in hand.
 */
const NAMES_KEY = "acr.basket.names";

/** One frozen instance, so the hydration render does not hand a new object down each time. */
const EMPTY_NAMES: Readonly<Record<string, string>> = Object.freeze({});

/*
 * Every storage access is wrapped because `localStorage` is not merely empty in a private
 * window or under a blocked-cookies setting: reading the property itself throws.
 */

function readStoredNames(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(NAMES_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const out: Record<string, string> = {};
    for (const [sku, name] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof name === "string" && name !== "") out[sku] = name;
    }
    return out;
  } catch {
    return {};
  }
}

function writeStoredNames(names: Record<string, string>): void {
  try {
    window.localStorage.setItem(NAMES_KEY, JSON.stringify(names));
  } catch {
    // A browser that will not store this still renders every name it was just sent.
  }
}

/** Nothing to subscribe to: hydration happens once and never happens again. */
function subscribeToNothing(): () => void {
  return () => {};
}

/**
 * True for the hydration render, false for every render after it.
 *
 * The basket identifier is restored from `localStorage`, which the server could not read,
 * so during hydration this hook is looking at a snapshot that says "no basket" whether or
 * not the buyer has one. Rendering "your cart is empty" from that snapshot would be a
 * claim about the buyer's basket that nothing has checked. Once React has hydrated it
 * switches to the client snapshot, and an absent identifier then means what it says.
 */
function useHydrating(): boolean {
  return useSyncExternalStore(
    subscribeToNothing,
    () => false,
    () => true,
  );
}

export interface UseBasket {
  basketId: string | null;
  basket: Basket | null;
  quantities: Record<string, number>;
  /** The last name the merchant gave each SKU, by SKU. See `names` below. */
  names: Record<string, string>;
  loading: boolean;
  error: string | null;
  busySku: string | null;
  add(sku: string): Promise<void>;
  setQuantity(sku: string, quantity: number): Promise<void>;
  reload(): Promise<void>;
}

/**
 * What the merchant said about each line it could not price, by SKU.
 *
 * The whole record is handed over, counts included. An earlier version took only the SKU,
 * on the reasoning that the counts were the merchant's to explain -- but the record comes
 * off the same `basket` the caller is rendering, so `available_units` is no less the
 * merchant's word than the SKU beside it. Dropping it left the screen saying a line was
 * refused without saying what would un-refuse it, which is the one number the buyer needs.
 */
export function unavailableBySku(basket: Basket | null): Map<string, Unavailability> {
  if (!basket) return new Map();
  return new Map(basket.unavailable.map((entry) => [entry.sku, entry]));
}

/**
 * True when the basket this browser remembers no longer exists on the server.
 *
 * Two shapes mean that: a 404 naming a basket (the API restarted, or the row was never
 * this buyer's), and a 409 naming a status (the basket was consumed into a checkout).
 * A 404 that names a SKU is a different fact entirely -- the product does not exist --
 * and must not be recovered from by silently opening a new basket.
 */
function isBasketGone(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status === 404) return error.problem.basket_id !== undefined;
  if (error.status === 409) return error.problem.basket_status !== undefined;
  return false;
}

export function useBasket(): UseBasket {
  const { basketId, itemCount, setBasketId, setLineCount, refresh } = useBasketContext();
  const hydrating = useHydrating();

  const [stored, setStored] = useState<Basket | null>(null);
  /**
   * The last name each SKU was quoted under, kept across responses.
   *
   * A basket line is `{sku, quantity}`; the human name of a product reaches this screen
   * only on a quote row. So when the merchant declines to price the basket, the quote is
   * null and every name on the page would vanish at once, leaving three raw SKUs where a
   * moment earlier there were three products. This is not a figure and nothing is derived
   * from it -- it is the merchant's own label for that SKU, held from the response before,
   * so the two lines that are still perfectly fine keep looking like the things they are.
   */
  const [learnedNames, setLearnedNames] = useState<Record<string, string>>(readStoredNames);
  /*
   * True while a re-read the buyer asked for is in flight. The read on mount is not
   * counted here: `settling` below already says that an identifier has been seen and no
   * basket has come back for it yet, and deriving that rather than storing it keeps this
   * hook from setting state during an effect.
   */
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busySku, setBusySku] = useState<string | null>(null);
  /** Quantities the buyer has asked for and the server has not yet confirmed. */
  const [pending, setPending] = useState<Record<string, number>>({});

  /*
   * A basket answers for exactly one identifier. Matching the two here, rather than
   * clearing the state whenever the identifier changes, keeps the lines of a basket that
   * has just been left behind from being shown in the frame between a new identifier
   * arriving and its first quote coming back.
   */
  const basket = stored !== null && stored.basket_id === basketId ? stored : null;

  // The chained writes below run outside the render that scheduled them, so they read
  // the current basket through refs rather than through a captured closure.
  const basketIdRef = useRef<string | null>(basketId);
  const chainRef = useRef<Promise<void>>(Promise.resolve());
  /**
   * One idempotency key per (basket, sku, quantity) attempt, held until that attempt
   * succeeds. A retry after a lost response therefore carries the key the server may
   * already have recorded, and replays the stored answer instead of applying the write
   * a second time.
   */
  const keysRef = useRef<Map<string, string>>(new Map());

  const quantities = useMemo<Record<string, number>>(() => {
    const merged: Record<string, number> = {};
    for (const line of basket?.lines ?? []) merged[line.sku] = line.quantity;
    for (const [sku, quantity] of Object.entries(pending)) merged[sku] = quantity;
    for (const [sku, quantity] of Object.entries(merged)) {
      if (quantity <= 0) delete merged[sku];
    }
    return merged;
  }, [basket, pending]);

  const quantitiesRef = useRef(quantities);

  /**
   * Store a basket the server sent, and learn the product names it came with.
   *
   * Every write of the basket goes through here so no response can update the lines
   * without also updating the names, which is how the two would drift apart.
   */
  const acceptBasket = useCallback((next: Basket): void => {
    setStored(next);
    setLearnedNames((current) => {
      const held = new Set(next.lines.map((line) => line.sku));
      const merged: Record<string, string> = {};
      // Names for SKUs still in the basket, so the map cannot grow without bound and a
      // line removed today cannot put a name on a screen tomorrow.
      for (const [sku, name] of Object.entries(current)) {
        if (held.has(sku)) merged[sku] = name;
      }
      for (const line of next.quote?.lines ?? []) merged[line.sku] = line.name;
      const same =
        Object.keys(merged).length === Object.keys(current).length &&
        Object.entries(merged).every(([sku, name]) => current[sku] === name);
      if (same) return current;
      writeStoredNames(merged);
      return merged;
    });
  }, []);

  /*
   * Nothing remembered is shown during hydration. The initialiser above reads storage the
   * server could not, so the hydration render has to be handed the empty map the server
   * rendered from; a frame later `hydrating` is false and the remembered names appear.
   */
  const names = hydrating ? EMPTY_NAMES : learnedNames;

  useEffect(() => {
    basketIdRef.current = basketId;
  }, [basketId]);
  // Kept in a ref so the effect that compares it to the provider's count can run on the
  // provider's figure alone, without re-running every time the lines themselves change.
  const basketRef = useRef<Basket | null>(null);
  useEffect(() => {
    basketRef.current = basket;
  }, [basket]);

  useEffect(() => {
    quantitiesRef.current = quantities;
  }, [quantities]);

  // The header renders its own count, so it is told whenever this hook learns a new
  // one. Publishing a zero while the first read is still in flight would blank the badge
  // the provider had just restored, so silence is kept until there is something to say.
  useEffect(() => {
    if (basket) setLineCount(basket.lines.length);
    else if (!basketId) setLineCount(0);
  }, [basket, basketId, setLineCount]);

  useEffect(() => {
    if (!basketId) return;
    const controller = new AbortController();
    api
      .basket(basketId, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        acceptBasket(next);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // A basket the server has forgotten is not an error the buyer can act on. Drop
        // the identifier; the next add opens a fresh one.
        if (isBasketGone(cause)) setBasketId(null);
        else setError(humanMessage(cause));
      });
    return () => controller.abort();
  }, [acceptBasket, basketId, setBasketId]);

  const openBasket = useCallback(async (): Promise<string> => {
    const created = await api.createBasket();
    basketIdRef.current = created.basket_id;
    setBasketId(created.basket_id);
    acceptBasket(created);
    return created.basket_id;
  }, [acceptBasket, setBasketId]);

  /** One line write, with a single recovery attempt when the basket has gone away. */
  const writeLine = useCallback(
    async (id: string, sku: string, quantity: number): Promise<Basket> => {
      let target = id;
      // Two passes at most, written as a bounded loop rather than a recursive call so the
      // bound is visible: the second pass exists only for a basket the server no longer
      // has, and a failure on it is reported rather than recovered from again.
      for (let pass = 0; pass < 2; pass += 1) {
        const attempt = `${target}|${sku}|${quantity}`;
        let key = keysRef.current.get(attempt);
        if (!key) {
          key = newIdempotencyKey();
          keysRef.current.set(attempt, key);
        }
        try {
          const next = await api.setLine(target, sku, quantity, key);
          keysRef.current.delete(attempt);
          return next;
        } catch (cause) {
          if (pass === 1 || !isBasketGone(cause)) throw cause;
          // That key belongs to a basket that no longer exists. The retry is a different
          // request against a different basket, so it earns a key of its own; a key kept
          // for a failed attempt is kept only when retrying it would be the same request.
          keysRef.current.delete(attempt);
          target = await openBasket();
        }
      }
      throw new Error("A line write is bounded to two passes and reached neither outcome.");
    },
    [openBasket],
  );

  const setQuantity = useCallback(
    async (sku: string, quantity: number): Promise<void> => {
      const wanted = Math.trunc(quantity);
      if (wanted < 0) return;
      if (wanted > MAX_LINE_QUANTITY) {
        setError(`A cart may hold at most ${MAX_LINE_QUANTITY} of one product.`);
        return;
      }

      setError(null);
      setPending((current) => ({ ...current, [sku]: wanted }));

      const run = async (): Promise<void> => {
        setBusySku(sku);
        try {
          const id = basketIdRef.current ?? (await openBasket());
          const next = await writeLine(id, sku, wanted);
          acceptBasket(next);
          // The provider publishes the item count and the total the header shows, and it
          // owns no setter for either. Asking it to re-read is the only way to keep the
          // pill in the header from disagreeing with the panel on this page.
          void refresh();
        } catch (cause) {
          setError(humanMessage(cause));
        } finally {
          // Drop the optimistic quantity only if the buyer has not asked for another one
          // since; otherwise the later request owns it and will clear it in its turn.
          setPending((current) => {
            if (current[sku] !== wanted) return current;
            const rest = { ...current };
            delete rest[sku];
            return rest;
          });
          setBusySku((current) => (current === sku ? null : current));
        }
      };

      chainRef.current = chainRef.current.then(run, run);
      await chainRef.current;
    },
    [acceptBasket, openBasket, refresh, writeLine],
  );

  const add = useCallback(
    (sku: string): Promise<void> => setQuantity(sku, (quantitiesRef.current[sku] ?? 0) + 1),
    [setQuantity],
  );

  const reload = useCallback(async (): Promise<void> => {
    const id = basketIdRef.current;
    if (!id) return;
    setReloading(true);
    try {
      const next = await api.basket(id);
      acceptBasket(next);
      setError(null);
    } catch (cause) {
      if (isBasketGone(cause)) setBasketId(null);
      else setError(humanMessage(cause));
    } finally {
      setReloading(false);
    }
  }, [acceptBasket, setBasketId]);

  // Another surface can write this basket and ask the provider to re-read it: the press on
  // a RazorAI line proposal does, and so would another tab. The provider publishes counts,
  // not lines, so this hook cannot take the lines from it. What it can do is notice that
  // the count the provider now holds is not the count of the lines held here, and re-read
  // -- otherwise the shelf's stepper says 1 beside a header that says 3, on one screen.
  //
  // Keyed on the provider's figure alone, deliberately. After this hook's own write the
  // response is accepted first and the provider re-reads after; both then agree, so the
  // hook's own presses cost no second fetch. Only a count that moved without this hook
  // moving it triggers one.
  useEffect(() => {
    const held = basketRef.current;
    if (held === null) return;
    const counted = held.lines.reduce((total, line) => total + line.quantity, 0);
    if (counted !== itemCount) void reload();
  }, [itemCount, reload]);

  // A basket identifier with nothing read against it yet is still loading, whichever
  // request is in flight. Deriving it here keeps the empty state from appearing between
  // the identifier arriving and its first quote coming back.
  const settling = basketId !== null && basket === null && error === null;

  return {
    basketId,
    basket,
    quantities,
    names,
    loading: hydrating || settling || reloading,
    error,
    busySku,
    add,
    setQuantity,
    reload,
  };
}

/**
 * Which stage of the order the buyer is standing in, so the copilot's rail and scene can
 * follow the same journey the trusted surface is on rather than guessing from chat.
 *
 * The stage is DERIVED, never stored: the path the buyer is on and — when there is a
 * checkout to read — that checkout's own state are the whole input. A checkout id can
 * arrive two ways: the buyer is on `/checkout/{id}`, or the panel is showing an inline
 * checkout and passes its id. Either way the hook polls `api.checkout(id)` every 2.5s and
 * keeps the latest, so a version that moves from APPROVAL_REQUIRED to PAID under the
 * buyer's feet moves the rail with it. A failed poll keeps the previous value — a rail
 * that threw because one read failed would take the whole copilot down with it.
 *
 * The kernel owns the checkout vocabulary; this file maps only the states that name a
 * stage and lets every other state fall through to the cart/discover rule, so an
 * unknown state degrades to "cart or discover" instead of crashing the page.
 */
"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";

import { api } from "@/lib/api/client";
import type { Checkout } from "@/lib/api/types";

export type OrderStage = "discover" | "cart" | "approve" | "pay" | "order" | "reserve";

/** The checkout id carried by a `/checkout/{id}` path, or null if the path is anything else. */
function checkoutIdFromPath(pathname: string | null): string | null {
  if (!pathname) return null;
  const match = /^\/checkout\/([^/?#]+)/.exec(pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

/** True when the path is `/orders/{id}` — an order already placed. */
function isOrderPath(pathname: string | null): boolean {
  return pathname != null && /^\/orders\/[^/?#]+/.test(pathname);
}

/** True when the path is the reserve-and-pay surface. */
function isReservePath(pathname: string | null): boolean {
  return pathname != null && /^\/reserve-pay(\/|$|\?|#)/.test(pathname);
}

/** Map a checkout's kernel state to the stage it names, or null when it names none. */
function stageForCheckoutState(state: string): OrderStage | null {
  switch (state) {
    case "APPROVAL_REQUIRED":
      return "approve";
    case "APPROVED":
    case "EXECUTION_PENDING":
    case "AWAITING_PAYMENT":
    case "PAYMENT_UNKNOWN":
      return "pay";
    case "PAID":
      return "order";
    default:
      return null;
  }
}

export function useOrderStage({
  hasLines,
  checkoutId,
}: {
  hasLines: boolean;
  checkoutId?: string | null;
}): { stage: OrderStage; checkout: Checkout | null } {
  const pathname = usePathname();

  // The id we actually poll: the path's own checkout id wins, else the one the panel passed.
  const activeCheckoutId = checkoutIdFromPath(pathname) ?? checkoutId ?? null;

  const [checkout, setCheckout] = useState<Checkout | null>(null);

  useEffect(() => {
    if (!activeCheckoutId) {
      // No checkout to read: drop any stale value so the fall-through rule governs. Deferred
      // off the effect body so no setter runs synchronously during the effect.
      const clearTimer = window.setTimeout(() => setCheckout(null), 0);
      return () => window.clearTimeout(clearTimer);
    }

    let cancelled = false;

    const read = () => {
      api
        .checkout(activeCheckoutId)
        .then((next) => {
          if (!cancelled) setCheckout(next);
        })
        .catch(() => {
          // Swallow: a failed read keeps the previous value. The rail must not throw.
        });
    };

    // An immediate first read, deferred to a macrotask so no setter fires in the effect body.
    const firstTimer = window.setTimeout(read, 0);
    const interval = window.setInterval(read, 2500);

    return () => {
      cancelled = true;
      window.clearTimeout(firstTimer);
      window.clearInterval(interval);
    };
  }, [activeCheckoutId]);

  // The checkout we report is only the one for the currently active id — a value left over
  // from a previous id must not colour this render.
  const currentCheckout = activeCheckoutId ? checkout : null;

  let stage: OrderStage;
  if (isReservePath(pathname)) {
    stage = "reserve";
  } else if (isOrderPath(pathname)) {
    stage = "order";
  } else {
    const fromCheckout = currentCheckout ? stageForCheckoutState(currentCheckout.state) : null;
    if (fromCheckout) {
      stage = fromCheckout;
    } else {
      stage = hasLines ? "cart" : "discover";
    }
  }

  return { stage, checkout: currentCheckout };
}

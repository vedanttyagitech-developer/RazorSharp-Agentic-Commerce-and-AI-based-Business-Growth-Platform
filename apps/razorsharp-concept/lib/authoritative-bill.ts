// One bill, produced by the backend, shown to the buyer before they choose how to pay.
//
// THE DEFECT THIS EXISTS TO REMOVE
// --------------------------------
// The review screen rendered a total the browser had computed from `lib/demo.ts` prices,
// and Reserve Pay then created a real backend checkout and discovered a different one.
// `reserve-checkout.tsx` even had to warn about it:
//
//     const mismatch = !!card && card.amount_minor !== total;
//     "The backend bill differs from the earlier visual preview."
//
// A buyer who has already chosen a payment method and is then shown a different number has
// been asked to approve twice for one purchase, and the first ask was against a figure no
// system will honour. Worse, the two payment paths disagreed about what was being bought:
// the manual path approved the local number, Reserve approved the backend one.
//
// So the checkout is created BEFORE the payment choice is offered, and the bill on screen is
// the approval card's own quote. Local prices remain only for the shelf, where they are a
// catalogue rendering and bind nothing.
//
// Nothing here computes an amount. `totalMinor()` reads what the backend sent, and throws if
// it sent neither form, because a total the frontend derived is a number the buyer never
// approved.

import { useCallback, useEffect, useEffectEvent, useRef, useState } from 'react';
import {
  CommerceError,
  commerce,
  idempotencyKey,
  type ApprovalCard,
  type Cart,
  type CheckoutView,
} from './commerce';

/** What the review is currently able to show. Discriminated, so no boolean pairs. */
export type BillState =
  | { status: 'idle' }
  | { status: 'building'; step: 'cart' | 'lines' | 'checkout' }
  /**
   * `cart` is null when the bill was *recovered* rather than built here: the checkout
   * already existed, its cart is closed behind it, and re-quoting a closed cart would
   * answer 409. Nothing renders from it -- the card's own quote is what the screen shows,
   * and that is the figure the Kernel binds an approval to.
   */
  | { status: 'ready'; card: ApprovalCard; cart: Cart | null }
  | { status: 'recovering'; view: CheckoutView }
  | { status: 'unavailable'; error: CommerceError; retryable: boolean };

export type BillRequest = { sku: string; quantity: number }[];

/** Checkout states a buyer can still be shown a card for and act on. */
const RESUMABLE = new Set(['RESERVED', 'APPROVAL_REQUIRED']);

/**
 * The buyer's own unfinished checkout for exactly these lines, if one exists.
 *
 * Only states the buyer can still act on. A checkout that is already past approval --
 * money may be moving -- must not be handed back as "here is your bill, choose how to
 * pay": that would invite a second payment for a purchase already in flight.
 *
 * Lines must match exactly. A checkout is frozen at the moment it was created, so one
 * built for a different basket is a different purchase, and adopting it would show the
 * buyer a total for something they did not ask for.
 *
 * A failed lookup must stop the build: it does not prove that no checkout exists.
 * Creating another checkout while the earlier payment state is unknown is not recovery.
 */
async function recoverCheckout(
  lines: BillRequest,
  signal: AbortSignal,
  cartId?: string,
): Promise<ApprovalCard | CheckoutView | null> {
  const wanted = signature(lines);
  let cursor: string | undefined;
  do {
  const page = await commerce.checkout.list({ live: !cartId, limit: 100, cursor, signal });
  cursor=page.next_cursor??undefined;
  for (const row of page.checkouts) {
    if (cartId && row.cart_id !== cartId) continue;
    if (!cartId && !RESUMABLE.has(row.state)) continue;
    const view = await commerce.checkout.read(row.checkout_id, signal);
    // A buyer editing a reviewed cart invalidates its old bill and reopens the cart.
    // That retired version has no payment to recover. Confirm the OPEN cart before
    // building its new bill; never skip an admitted attempt or a confirmed sale.
    if (cartId && ['INVALIDATED','CANCELLED','EXPIRED'].includes(view.state) && (!view.attempt || ['FAILED','EXPIRED'].includes(view.attempt.state)) && !view.order_id) {
      // /carts/current returns only the buyer's OPEN cart.
      const current = await commerce.cart.current(signal);
      if (current.cart?.cart_id === cartId) continue;
    }
    if (cartId && !RESUMABLE.has(view.state)) return view;
    const card = view.approval_card;
    if (!card) continue;
    const held = card.quote.lines.map((line) => ({
      sku: line.sku,
      quantity: line.quantity,
    }));
    if (signature(held) === wanted) return card;
  }
  } while(cursor&&!signal.aborted);
  return null;
}

/**
 * One logical "open this basket as a checkout" mutation, and its retries.
 *
 * The keys are held per attempt-of-a-basket rather than regenerated per call, because a
 * retried `PUT .../lines/{sku}` carrying a fresh key is a second write, and a retried
 * `POST .../checkout` carrying a fresh key is a second checkout holding a second stock
 * review against the same buyer.
 */
type Keys = { cart: string; lines: Map<string, string>; checkout: string };

function freshKeys(): Keys {
  return { cart: idempotencyKey(), lines: new Map(), checkout: idempotencyKey() };
}

/**
 * The keys held for one basket, and the basket they belong to.
 *
 * They are minted per *basket*, not per build. A retry, a closed and reopened review, or
 * any other re-run for the same lines is the same logical operation and must carry the
 * same keys -- which is what makes the backend replay its original answer instead of
 * executing again. Minting fresh ones on every build was exactly the mistake the header
 * above warns about: a retried `POST .../checkout` with a new key is a second checkout
 * creating a second checkout for one purchase, and the buyer would be paying
 * against a card they were never shown.
 */
type HeldKeys = { signature: string; keys: Keys };

function signature(request: BillRequest): string {
  return request
    .filter((line) => line.quantity > 0)
    .map((line) => `${line.sku}:${line.quantity}`)
    .sort()
    .join('|');
}

/**
 * Build the authoritative bill for exactly these lines.
 *
 * Rebuilt when the basket changes, because a checkout is frozen at the moment it is created:
 * a buyer who changes their basket is buying something else and needs a card that says so.
 * The previous card is dropped rather than patched -- there is no such thing as editing an
 * approved version, and pretending otherwise is how a stale hash reaches the Kernel.
 */
export function useAuthoritativeBill(request: BillRequest, active: boolean, cartId?: string): {
  state: BillState;
  retry: () => void;
} {
  const [state, setState] = useState<BillState>({ status: 'idle' });
  const held = useRef<HeldKeys | null>(null);
  const builtFor = useRef<string | null>(null);
  const [attempt,setAttempt] = useState(0);

  const wanted = `${cartId ?? 'new'}|${signature(request)}`;

  const build = useCallback(
    async (lines: BillRequest, signal: AbortSignal) => {
      const wantedNow = `${cartId ?? 'new'}|${signature(lines)}`;
      if (held.current?.signature !== wantedNow) {
        held.current = { signature: wantedNow, keys: freshKeys() };
      }
      const keys = held.current.keys;

      setState({ status: 'building', step: 'cart' });
      try {
        // Before building anything: does this buyer already have an unfinished checkout for
        // exactly these lines? A response that was lost on the way back still created a
        // checkout, and the idempotency keys only replay while this component remembers
        // them. Asking the server is what covers a reload, a second tab, or a device the
        // keys never existed on -- and it is the difference between recovering a purchase
        // and starting a second one that holds the same stock twice.
        const recovered = await recoverCheckout(lines, signal, cartId);
        if (recovered) {
          setState('state' in recovered ? {status:'recovering',view:recovered} : { status: 'ready', card: recovered, cart: null });
          return;
        }

        const cart = cartId ? await commerce.cart.read(cartId, signal) : await commerce.cart.create(keys.cart, signal);
        if (cartId && signature(cart.lines) !== signature(lines)) throw new CommerceError(409, "Cart changed", "Your cart changed. Refresh it before reviewing this order.");
        setState({ status: 'building', step: 'lines' });

        let latest: Cart = cart;
        for (const line of cartId ? [] : lines) {
          if (line.quantity <= 0) continue;
          let key = keys.lines.get(line.sku);
          if (!key) {
            key = idempotencyKey();
            keys.lines.set(line.sku, key);
          }
          latest = await commerce.cart.setLine(
            cart.cart_id,
            line.sku,
            line.quantity,
            key,
            signal,
          );
        }

        // A line the merchant cannot fulfil must stop the checkout here, where the buyer is
        // looking at the basket, rather than at admission where it reads as a refusal.
        if (latest.unavailable?.length) {
          setState({
            status: 'unavailable',
            error: new CommerceError(
              409,
              'Some items are no longer available',
              `The store cannot fulfil: ${latest.unavailable.map((u) => u.sku).join(', ')}. ` +
                'Adjust the cart and try again.',
              latest.code,
            ),
            retryable: false,
          });
          return;
        }

        setState({ status: 'building', step: 'checkout' });
        const card = await commerce.cart.checkout(cart.cart_id, keys.checkout, signal);
        setState({ status: 'ready', card, cart: latest });
      } catch (cause) {
        if ((cause as Error)?.name === 'AbortError') return;
        const error =
          cause instanceof CommerceError
            ? cause
            : new CommerceError(0, 'Could not price this order', String(cause));
        // 409 CONCURRENT_OPERATION on checkout creation is NOT retryable with the same
        // basket: it means the stock this basket needs is held elsewhere, and on the demo
        // catalogue it can mean held permanently by completed sales. Retrying the identical
        // request would spin.
        setState({
          status: 'unavailable',
          error,
          retryable: error.unreachable || error.status >= 500,
        });
      }
    },
    [cartId],
  );

  const startBuild=useEffectEvent((controller:AbortController)=>{
    if (!active || !wanted) {if(!active)setState({status:'idle'});return;}
    if(builtFor.current===wanted && state.status==='ready')return;
    builtFor.current=wanted;
    void build(request.filter(l=>l.quantity>0),controller.signal);
  });
  useEffect(()=>{const controller=new AbortController();const frame=requestAnimationFrame(()=>startBuild(controller));return()=>{cancelAnimationFrame(frame);controller.abort()}},[wanted,active,attempt,build]);

  const retry = useCallback(() => {
    builtFor.current = null;
    setAttempt(n=>n+1);
    setState({ status: 'idle' });
  }, []);

  return { state, retry };
}

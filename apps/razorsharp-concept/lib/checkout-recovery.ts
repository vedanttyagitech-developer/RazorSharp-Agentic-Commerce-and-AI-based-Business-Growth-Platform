import type { CheckoutView } from './commerce';

export function canResumeManualCheckout(view: Pick<CheckoutView, 'state'|'order_id'|'attempt'>): boolean {
  return !view.order_id && view.state === 'AWAITING_PAYMENT' &&
    !!view.attempt?.razorpay_order_id && ['SUBMITTED','AUTHORIZED'].includes(view.attempt.state);
}

/** States the backend has finished with. None of them can become a payment. */
const SPENT_STATES: ReadonlySet<string> = new Set(['CANCELLED', 'EXPIRED', 'INVALIDATED']);

/**
 * A bill that is over, and never had a payment behind it.
 *
 * There is nothing to cancel here: the version is already retired and its stock hold is
 * already released, which is *why* the backend reports it as not cancellable. Requiring a
 * cancellation first left the buyer reading "refresh stock and review the updated bill" on
 * a screen with no control that could do it -- the instruction was right and unreachable.
 *
 * The `!attempt` is doing real work and is not a formality. It is the difference between
 * "nothing was ever started" and "something was started and this checkout was closed around
 * it", and only the first is safe to replace without asking the backend anything.
 */
export function isSpentCheckout(
  view: Pick<CheckoutView, 'state' | 'order_id' | 'attempt'>,
): boolean {
  return !view.order_id && !view.attempt && SPENT_STATES.has(view.state);
}

/**
 * A new review is safe only when nothing is in flight behind the old bill.
 *
 * Two ways to know that. Either the backend still offers to cancel it -- a live checkout,
 * where cancellation is what releases the hold and retires the version, and skipping it
 * would leak a reservation -- or it is already spent, in which case there is nothing left
 * to release.
 */
export function canRefreshCheckout(
  view: Pick<CheckoutView, 'attempt' | 'order_id' | 'state' | 'cancellable'>,
): boolean {
  if (isSpentCheckout(view)) return true;
  return (
    !view.order_id &&
    view.cancellable &&
    (!view.attempt || view.state === 'PAYMENT_FAILED')
  );
}

/** Call only after backend cancellation succeeds; never discard another purchase's retry key. */
export function clearCancelledCheckoutRecovery(checkoutId: string): void {
  for (const key of ['rs-manual-pending', 'rs-reserve-pending']) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
      if ((saved?.checkoutId ?? saved?.card?.checkout_id) === checkoutId)
        sessionStorage.removeItem(key);
    } catch {
      /* Backend state remains authoritative when storage is inaccessible. */
    }
  }
}

/**
 * True when this bill has no payment behind it of any kind.
 *
 * The ordinary way to get here is an approval that was recorded and then refused admission
 * -- an expired stock hold, say -- which leaves the checkout APPROVED with no attempt and no
 * grant. There is nothing in flight, nothing to reconcile and nothing to wait for.
 */
export function hasNoPaymentAttempt(view: Pick<CheckoutView, 'order_id'|'attempt'>): boolean {
  return !view.order_id && !view.attempt;
}

/** Describe observed state, never turn elapsed time into a failed-payment claim. */
export function recoveryMessage(view: Pick<CheckoutView, 'state'|'order_id'|'attempt'>): string {
  if(view.order_id) return 'Your payment is confirmed and your order is placed.';
  if(view.attempt?.state==='ESCALATED') return 'This payment needs merchant review because its outcome could not be verified. Do not reopen it or pay again. Contact the merchant with this checkout reference.';
  if(view.state==='PAYMENT_FAILED'||view.attempt?.state==='FAILED') return 'The backend confirmed that the previous payment failed. Review current stock and prices before trying again.';
  if(['CANCELLED','EXPIRED','INVALIDATED'].includes(view.state)&&!view.attempt) return 'This earlier checkout is no longer valid. Refresh stock and review the updated bill.';
  if(canResumeManualCheckout(view)) return 'You already started a Razorpay payment. Resume the same checkout below to finish it. If you already paid, wait for verification; do not pay again.';
  // Before this branch existed, a bill with no attempt fell through to the sentence below
  // and was described as a payment "awaiting a verified outcome" whose status was being
  // checked. Both halves were false -- there is no attempt to check -- and the buyer was
  // told to keep waiting for an answer that could never arrive.
  if(hasNoPaymentAttempt(view)) return 'No payment was started for this bill and nothing has been charged. Refresh stock and review the latest bill when you want to buy these items.';
  return 'An earlier payment is still awaiting a verified outcome. We are checking its status. Do not start another payment for this purchase.';
}

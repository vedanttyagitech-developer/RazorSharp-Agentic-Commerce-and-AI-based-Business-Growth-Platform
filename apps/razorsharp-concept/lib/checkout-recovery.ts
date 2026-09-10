import type { CheckoutView } from './commerce';

/** A new review is safe only when the backend allows cancellation of the old bill. */
export function canRefreshCheckout(
  view: Pick<CheckoutView, 'attempt' | 'order_id' | 'state' | 'cancellable'>,
): boolean {
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

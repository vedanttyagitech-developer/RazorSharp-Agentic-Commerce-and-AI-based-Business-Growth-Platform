import type {CheckoutView} from './commerce';

/** A new review is safe only when the backend allows cancellation of the old bill. */
export function canRefreshCheckout(view: Pick<CheckoutView, 'attempt'|'order_id'|'state'|'cancellable'>): boolean {
  return !view.order_id && view.cancellable && (!view.attempt || view.state === 'PAYMENT_FAILED');
}

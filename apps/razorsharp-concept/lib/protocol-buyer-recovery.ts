import { CommerceError, commerce, type ApprovalCard } from './commerce';
import { canRefreshCheckout, isSpentCheckout, clearCancelledCheckoutRecovery } from './checkout-recovery';

// Only explicit basket validation failures unlock editing. In particular, timeouts,
// idempotency conflicts and failures reading an already-created checkout must retain the key.
export function basketWasRejected(error: unknown): boolean {
  return error instanceof CommerceError && (
    (error.status === 409 && ['Cart cannot be priced', 'Cart is empty'].includes(error.title)) ||
    (error.status === 404 && error.title === 'Product not found') ||
    (error.status === 422 && ['Invalid quantity', 'Cart too large', 'Duplicate SKU', 'Request validation failed'].includes(error.title))
  );
}

export async function retireForFreshReview(card: ApprovalCard) {
  const view = await commerce.checkout.read(card.checkout_id);
  if (!canRefreshCheckout(view)) throw Error('Payment may be in progress. Check its status before replacing this checkout.');
  if (!isSpentCheckout(view)) {
    const decision = await commerce.checkout.cancel(card.checkout_id, crypto.randomUUID());
    if (!decision.allowed) throw Error(decision.explanation || 'The checkout cannot be replaced yet.');
  }
  clearCancelledCheckoutRecovery(card.checkout_id);
  return card.quote.lines.map(line => ({sku: line.sku, quantity: line.quantity}));
}

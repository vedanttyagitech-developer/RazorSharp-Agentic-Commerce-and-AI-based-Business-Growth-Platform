import { commerce, idempotencyKey, type Cart } from './commerce';
import type { VoiceOffer } from './voice/client';

/** One serialized writer for the cart shared by buttons and voice proposals. */
export class DurableCart {
  cart: Cart | null = null;
  private queue: Promise<unknown> = Promise.resolve();
  private pending: {
    cartId: string;
    sku: string;
    quantity: number;
    key: string;
    expected?: VoiceOffer['binding'];
  } | null = null;
  private createKey = idempotencyKey();
  private newKey: string | null = null;
  constructor(private changed: (cart: Cart | null) => void) {}
  private publish(cart: Cart | null) {
    this.cart = cart;
    this.changed(cart);
    return cart;
  }
  private serialize<T>(work: () => Promise<T>): Promise<T> {
    const next = this.queue.then(work);
    this.queue = next.catch(() => undefined);
    return next;
  }
  private async createEmptyCart() {
    const cart = await commerce.cart.create(this.createKey);
    this.createKey = idempotencyKey();
    return cart;
  }
  restore() {
    return this.serialize(async () => {
      if (this.pending)
        throw Error('Retry the pending cart update before continuing.');
      const { cart } = await commerce.cart.current();
      if (cart) return this.publish(cart);
      // Payment-bound carts belong to Earlier purchases, never the editable basket.
      // Leave creation explicit when an earlier checkout exists.
      const page = await commerce.checkout.list({ limit: 1 });
      return this.publish(page.checkouts.length ? null : await this.createEmptyCart());
    });
  }
  change(sku: string, delta: number, proposal?: VoiceOffer) {
    return this.serialize(async () => {
      if (this.pending) throw Error('The previous cart update needs a retry.');
      if (!Number.isInteger(delta) || !delta)
        throw Error('Invalid cart quantity.');
      if (!this.cart) {
        const { cart: current } = await commerce.cart.current();
        this.publish(current ?? await this.createEmptyCart());
      }
      const cart = await commerce.cart.read(this.cart!.cart_id);
      this.publish(cart);
      if (proposal?.cartId && proposal.cartId !== cart.cart_id)
        throw Error('That suggestion belongs to another cart. Ask again.');
      if (proposal?.blockedBy && proposal.blockedBy !== 'no_basket')
        throw Error(
          'That suggestion cannot be applied. Refresh your cart and ask again.',
        );
      const before = cart.lines.find((line) => line.sku === sku)?.quantity ?? 0;
      const quantity =
        proposal?.absoluteQuantity ?? Math.max(0, before + delta);
      this.pending = {
        cartId: cart.cart_id,
        sku,
        quantity,
        key: idempotencyKey(),
        expected: proposal?.binding,
      };
      const result = await this.execute();
      return { cart: result, delta: quantity - before, quantity };
    });
  }
  private async execute() {
    const operation = this.pending!;
    try {
      const result = await commerce.cart.setLine(
        operation.cartId,
        operation.sku,
        operation.quantity,
        operation.key,
        undefined,
        operation.expected ?? undefined,
      );
      this.pending = null;
      this.publish(result);
      return result;
    } catch (error) {
      // A definite client refusal did not mutate. Transport/5xx outcomes retain the
      // exact absolute quantity and key, so retry cannot increment the cart twice.
      const status = (error as { status?: number }).status;
      if (status && status >= 400 && status < 500) this.pending = null;
      throw error;
    }
  }
  retry() {
    return this.serialize(() =>
      this.pending
        ? this.execute()
        : Promise.reject(Error('Refresh the cart, then try the change again.')),
    );
  }
  newCart() {
    return this.serialize(async () => {
      if (this.pending) throw Error('Resolve the pending cart update first.');
      this.newKey ??= idempotencyKey();
      const cart = await commerce.cart.create(this.newKey);
      this.newKey = null;
      return this.publish(cart);
    });
  }
}

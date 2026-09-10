// Razorpay Standard Checkout, loaded into this document and opened on the buyer's own click.
//
// Why the provider's own screen and not a form of ours: specification 2.4. Card numbers,
// UPI PINs and OTPs are entered on Razorpay's surface, inside Razorpay's iframe, and never
// reach this application, its state, its logs or its backend. The platform's own evidence
// rules depend on that boundary -- `commerce_protocols.core.evidence` screens PAN and CVC
// out of the immutable chain precisely because they must never have been there.
//
// Three things this module refuses to do, each of which the old preview did:
//
//   1. **It does not decide the outcome.** `handler` fires when Razorpay hands the browser
//      back a payment id. That is a *report*, not a confirmation: `POST /v1/payments/verify`
//      records it as BROWSER_CALLBACK evidence and the backend still waits for the webhook.
//      A caller that treats `handler` as success is asserting a capture nobody verified.
//   2. **It does not treat dismissal as failure.** Closing the modal leaves the attempt
//      exactly where it was -- possibly paid, in a tab the buyer switched away from. The
//      only honest reading is "outcome unknown", which is what `dismissed` means here.
//   3. **It does not compute an amount.** Everything it opens with came from the payment
//      handoff, which reads the amount the Execution Grant was issued for.

/** The fields Razorpay hands back when a payment completes in the browser. */
export type RazorpayReturn = {
  razorpay_payment_id: string;
  razorpay_order_id: string;
  razorpay_signature: string;
};

export type RazorpayHandoff = {
  keyId: string;
  orderId: string;
  amountMinor: number;
  currency: string;
  merchantName: string;
  description: string;
};

/** How the modal ended. `paid` is a browser report; only the backend can confirm it. */
export type RazorpayOutcome =
  | { kind: 'reported'; report: RazorpayReturn }
  | { kind: 'dismissed' }
  | { kind: 'failed'; code: string; description: string };

export class RazorpayUnavailableError extends Error {
  constructor(readonly cause_: 'blocked' | 'offline') {
    super(
      cause_ === 'blocked'
        ? "Razorpay's checkout script did not load. An extension, a content blocker or a " +
            'content-security policy is refusing it.'
        : 'Razorpay could not be reached. Payment status is not confirmed; recover this same checkout before trying again.',
    );
    this.name = 'RazorpayUnavailableError';
  }
}

const SCRIPT = 'https://checkout.razorpay.com/v1/checkout.js';

type RazorpayInstance = { open: () => void; close: () => void };
type RazorpayGlobal = new (options: Record<string, unknown>) => RazorpayInstance;

declare global {
  interface Window {
    Razorpay?: RazorpayGlobal;
  }
}

let loading: Promise<RazorpayGlobal> | null = null;

/**
 * Load `checkout.js` once per document and hand back the constructor.
 *
 * Memoised on the promise rather than on a boolean: two buttons clicked in the same tick
 * would otherwise append two script tags, and the second `onload` would resolve against a
 * global the first had already installed.
 */
export function loadRazorpay(): Promise<RazorpayGlobal> {
  if (typeof window === 'undefined')
    return Promise.reject(new RazorpayUnavailableError('blocked'));
  if (window.Razorpay) return Promise.resolve(window.Razorpay);
  if (loading) return loading;

  loading = new Promise<RazorpayGlobal>((resolve, reject) => {
    const tag = document.createElement('script');
    tag.src = SCRIPT;
    tag.async = true;
    tag.onload = () => {
      // Loaded but no global: something served a different body at that URL. Treated as
      // blocked rather than retried, because retrying fetches the same wrong thing.
      if (window.Razorpay) resolve(window.Razorpay);
      else reject(new RazorpayUnavailableError('blocked'));
    };
    tag.onerror = () => {
      loading = null;
      reject(new RazorpayUnavailableError('offline'));
    };
    document.head.appendChild(tag);
  });
  return loading;
}

/** True for a Razorpay test-mode key. The key id is public and says which mode it is. */
export function isTestKey(keyId: string): boolean {
  return keyId.startsWith('rzp_test_');
}

/**
 * Open Razorpay Checkout and resolve with how it ended.
 *
 * Resolves exactly once. Razorpay can fire `payment.failed` and then `ondismiss` for one
 * modal, and a caller that saw both would report a failure and then an unknown for the same
 * attempt -- so the first outcome wins and the rest are dropped.
 */
export async function openRazorpay(handoff: RazorpayHandoff): Promise<RazorpayOutcome> {
  const Razorpay = await loadRazorpay();
  return new Promise<RazorpayOutcome>((resolve) => {
    let settled = false;
    const settle = (outcome: RazorpayOutcome) => {
      if (settled) return;
      settled = true;
      resolve(outcome);
    };

    const instance = new Razorpay({
      key: handoff.keyId,
      // `order_id` is what binds this modal to the order the worker created under the
      // Execution Grant. Without it Razorpay would happily take a payment against no order,
      // and no webhook could ever be matched back to this checkout.
      order_id: handoff.orderId,
      amount: handoff.amountMinor,
      currency: handoff.currency,
      name: handoff.merchantName,
      description: handoff.description,
      // Explicit demo identity, only for public Test Mode keys. Never apply to live payments.
      ...(isTestKey(handoff.keyId) ? {
        prefill: { name: 'Vedant Tyagi', contact: '+919876543210' },
        notes: { demo_billed_to: 'Vedant Tyagi', demo_identity: 'test_mode_only' },
      } : {}),
      // Account settings may still require contact; the demo prefill supplies it.
      hidden: { contact: true },
      retry: { enabled: false },
      handler: (report: RazorpayReturn) => settle({ kind: 'reported', report }),
      modal: { ondismiss: () => settle({ kind: 'dismissed' }), escape: true },
      theme: { color: '#0b0b0c' },
    });

    const failed = (event: { error?: { code?: string; description?: string } }) =>
      settle({
        kind: 'failed',
        code: event?.error?.code || 'PAYMENT_FAILED',
        description: event?.error?.description || 'Razorpay reported that the payment failed.',
      });
    (instance as unknown as { on?: (name: string, fn: typeof failed) => void }).on?.(
      'payment.failed',
      failed,
    );

    instance.open();
  });
}

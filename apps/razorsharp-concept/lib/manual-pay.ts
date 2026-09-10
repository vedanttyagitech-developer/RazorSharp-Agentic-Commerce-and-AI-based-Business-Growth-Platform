// Manual checkout: the buyer approves, the Kernel admits, the worker creates a real
// Razorpay order, and Razorpay's own screen takes the payment.
//
// The order of those four is the whole point and cannot be rearranged. Razorpay Checkout is
// opened against an `order_id` that exists only because the Kernel admitted this exact
// version and issued an Execution Grant the worker spent creating it. A screen that opened
// the provider first would be collecting money for a purchase nothing had authorised.
//
// WHAT "CONFIRMED" MEANS HERE
// ---------------------------
// Not that Razorpay's modal closed happily. ADR 0003 D8: the browser's return is recorded
// as BROWSER_CALLBACK evidence and capture is applied only from WEBHOOK or PROVIDER_FETCH.
// So the browser reports, and then this module *waits for the backend to agree* by polling
// until an `order_id` exists. Every intermediate state is rendered as "checking", because
// that is what is true.
//
// AND WHAT IT NEVER SAYS
// ----------------------
// "Failed", unless something said so. A dismissed modal, a closed tab, a poll that ran out
// of patience -- all of those are *unresolved*, and telling a buyer their payment failed
// when it may have succeeded is how somebody pays twice.

import {
  CommerceError,
  commerce,
  admitted,
  type ApprovalCard,
  type CheckoutView,
  type Decision,
  type PaymentHandoff,
} from './commerce';

/** How long to wait for the worker to create the provider order before saying so. */
const PROVIDER_ORDER_TIMEOUT_MS = 45_000;
const PROVIDER_ORDER_POLL_MS = 900;
/** How long a confirmation may take before the wait is *labelled* slow. Never abandoned. */
export const SLOW_CONFIRMATION_MS = 40_000;
const CONFIRMATION_POLL_MS = 1_500;

/**
 * The grant was issued and no provider order appeared before the deadline.
 *
 * Deliberately not "failed". The approval is admitted, the Execution Grant exists, and the
 * order may appear a second later -- so this says what is true and tells the buyer not to
 * start a second payment.
 *
 * `attemptState` is what the backend says the attempt is, because the two cases behind this
 * timeout are different and only that field separates them. An attempt still sitting at
 * CREATED means nothing has consumed the grant: on a local demo that is the Action Executor
 * not running, which is a real failure mode this screen used to render as forty-five seconds
 * of silence followed by "still preparing". The API cannot create the order itself -- its
 * database role has no write on a financial table -- so waiting longer would never help.
 */
export class ProviderOrderPending extends Error {
  constructor(readonly attemptState: string | null) {
    super(
      attemptState === 'CREATED'
        ? 'Your approval is recorded and nothing has been charged, but the store has not ' +
            'started the payment. Its payment worker has not picked this up. Do not start ' +
            'another payment.'
        : attemptState === null
          ? 'Your approval is recorded and nothing has been charged. The payment attempt ' +
              'could not be read, so its state is unknown rather than failed. Do not start ' +
              'another payment.'
          : 'The payment is authorised and the provider order is still being created. ' +
              'Nothing has been charged. Do not start another payment.',
    );
    this.name = 'ProviderOrderPending';
  }
}

/**
 * The keys for one manual purchase, kept for its retries and across a reload.
 *
 * `approve` and `verify` are separate because they are separate mutations: replaying the
 * approval must not replay the callback, and a fresh key on either is a second action. They
 * are minted once per attempt-at-this-card and stored, so a buyer who reloads mid-payment
 * recovers the same request instead of opening a second one.
 */
export type ManualKeys = { approve: string; verify: string };

export type ManualPending = {
  checkoutId: string;
  version: number;
  contentHash: string;
  amountMinor: number;
  currency: string;
  keys: ManualKeys;
  /** Set once the browser has been handed back a payment id. */
  reported?: boolean;
};

const STORAGE_KEY = 'rs-manual-pending';

export function readPending(): ManualPending | null {
  if (typeof sessionStorage === 'undefined') return null;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ManualPending) : null;
  } catch {
    return null;
  }
}

export function writePending(pending: ManualPending): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(pending));
  } catch {
    // A browser refusing session storage is not a reason to refuse a payment. The recovery
    // it enables is a convenience; the backend's idempotency is what actually prevents a
    // double charge, and that does not live here.
  }
}

export function clearPending(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* see writePending */
  }
}

export function pendingFor(card: ApprovalCard): ManualPending {
  const existing = readPending();
  if (
    existing &&
    existing.checkoutId === card.checkout_id &&
    existing.version === card.version &&
    existing.contentHash === card.content_hash
  )
    return existing;
  return {
    checkoutId: card.checkout_id,
    version: card.version,
    contentHash: card.content_hash,
    amountMinor: card.amount_minor,
    currency: card.currency,
    keys: { approve: crypto.randomUUID(), verify: crypto.randomUUID() },
  };
}

/**
 * This version already carries an approval, and that approval never started a payment.
 *
 * Deliberately separate from a replay. Both arrive as the same `DUPLICATE_OPERATION` with
 * `already_approved`, because from the approval's point of view they ARE the same event --
 * but one of them has an admitted attempt behind it and the other does not, and the second
 * can never grow one. The version's admission was refused (an expired stock hold is the
 * ordinary way to get here), and a refused admission writes no attempt and no grant.
 *
 * Nothing was charged, and nothing is in flight. The way forward is a fresh bill against a
 * fresh hold, which is what the refusal's own guidance already said.
 */
export class ApprovalStartedNoPayment extends Error {
  constructor(readonly checkoutState: string) {
    super(
      'This bill was already approved, and that approval did not start a payment. Nothing ' +
        'has been charged and nothing is in flight. Refresh stock and review the latest ' +
        'bill before paying.',
    );
    this.name = 'ApprovalStartedNoPayment';
  }
}

/**
 * Record the buyer's consent bound to this exact bill and ask the Kernel to admit it.
 *
 * Throws `KernelRefusal` on a refusal, which arrives as HTTP 200 with `allowed: false`.
 *
 * `DUPLICATE_OPERATION` is not a refusal to surface -- but it is not automatically good
 * news either, and it used to be read as though it were. The backend answers
 * `already_approved: this version already carries a recorded approval` in two situations
 * that want opposite things from this screen:
 *
 *   1. A recovery. The first request was admitted, an attempt and a grant exist, and the
 *      buyer reloaded mid-payment. Continuing is exactly right.
 *   2. A version whose admission was REFUSED after the approval had been recorded. No
 *      attempt was written and none ever will be. Continuing meant polling forty-five
 *      seconds for a provider order that cannot exist and then telling the buyer the
 *      store's payment worker had not picked their payment up -- an accusation against a
 *      worker that was running perfectly, about a payment that was never started.
 *
 * The decision body is identical in both, so the checkout itself is asked. That is a read:
 * it records nothing, approves nothing, and cannot start a second payment. There is no race
 * to lose -- a duplicate is only ever answered once the first admission has committed, so
 * an attempt that exists is already visible here.
 */
export async function approve(pending: ManualPending, signal?: AbortSignal): Promise<Decision> {
  writePending(pending); // Persist the retry key before sending a request with an unknown outcome.
  const decision = await commerce.checkout.approveAndPay(
    pending.checkoutId,
    pending.version,
    {
      content_hash: pending.contentHash,
      amount_minor: pending.amountMinor,
      currency: pending.currency,
    },
    pending.keys.approve,
    signal,
  );
  if (!decision.allowed && decision.code === 'DUPLICATE_OPERATION') {
    const view = await commerce.checkout.read(pending.checkoutId, signal);
    if (!view.attempt && !view.order_id) throw new ApprovalStartedNoPayment(view.state);
    return decision;
  }
  return admitted(decision);
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });

/**
 * Wait for the Action Executor to create the Razorpay order, then hand back the handoff.
 *
 * Polled rather than awaited inline because the API physically cannot create it: the
 * handoff endpoint runs on the app role, which has no write on a financial table. The order
 * appears when the worker has spent the Execution Grant, and until then `razorpay_order_id`
 * is honestly null.
 *
 * Running out of patience throws `ProviderOrderPending`, never a failure: the grant is
 * issued and the order may appear a second later.
 */
export async function awaitProviderOrder(
  checkoutId: string,
  signal?: AbortSignal,
): Promise<PaymentHandoff & { razorpay_key_id: string; razorpay_order_id: string }> {
  const deadline = Date.now() + PROVIDER_ORDER_TIMEOUT_MS;
  for (;;) {
    const handoff = await commerce.checkout.payment(checkoutId, signal);
    if (handoff.window_closed) throw new ProviderOrderPending(handoff.state ?? null);
    if (handoff.razorpay_order_id && handoff.razorpay_key_id)
      return handoff as PaymentHandoff & { razorpay_key_id: string; razorpay_order_id: string };
    // The attempt's own state is carried into the error: it is the difference between "a
    // worker is working on it" and "nothing has picked this up", and both look identical
    // from here without it.
    if (Date.now() >= deadline) throw new ProviderOrderPending(handoff.state ?? null);
    await sleep(PROVIDER_ORDER_POLL_MS, signal);
  }
}

/** What the backend has settled on, once it has settled on anything. */
export type Settlement =
  | { kind: 'confirmed'; orderId: string; reference: string | null; view: CheckoutView }
  | { kind: 'failed'; state: string; view: CheckoutView };

/**
 * Poll the checkout until the backend has a verified outcome, and return what it is.
 *
 * There is no timeout on purpose. A payment whose outcome is unknown stays unknown until
 * evidence arrives, and a client that gave up and rendered something definite would be
 * inventing the one fact this whole platform exists to prove. The caller aborts the signal
 * when the screen goes away; `onSlow` lets it say the wait is long without ending it.
 */
export async function awaitSettlement(
  checkoutId: string,
  options: { signal?: AbortSignal; onSlow?: () => void; onConnectionChange?: (online: boolean) => void; reconcile?: boolean } = {},
): Promise<Settlement> {
  const { signal, onSlow, onConnectionChange } = options;
  const slowAt = Date.now() + SLOW_CONFIRMATION_MS;
  let announced = false;
  let disconnected = false;
  let retryDelay = CONFIRMATION_POLL_MS;
  let nextReconciliation = 0;
  for (;;) {
    let view: CheckoutView;
    try {
      view = await commerce.checkout.read(checkoutId, signal);
      if (options.reconcile && !view.order_id && Date.now() >= nextReconciliation &&
          ['AWAITING_PAYMENT', 'PAYMENT_UNKNOWN'].includes(view.state)) {
        await commerce.payments.reconcile(checkoutId, signal);
        nextReconciliation = Date.now() + 15_000;
      }
      if (disconnected) onConnectionChange?.(true);
      disconnected = false;
      retryDelay = CONFIRMATION_POLL_MS;
    } catch (cause) {
      if (!(cause instanceof CommerceError) || !(cause.unreachable || cause.status === 429 || cause.status >= 500)) throw cause;
      if (!disconnected) onConnectionChange?.(false);
      disconnected = true;
      await sleep(retryDelay, signal);
      retryDelay = Math.min(10_000, retryDelay * 2);
      continue;
    }
    if (view.order_id)
      return { kind: 'confirmed', orderId: view.order_id, reference: view.order_reference, view };
    // Only states the Kernel itself calls finished. PAYMENT_UNKNOWN is deliberately absent:
    // unknown is not failed, and reconciliation is still working on it.
    if (view.state === 'PAYMENT_FAILED' || view.state === 'CANCELLED' || view.state === 'EXPIRED' || view.attempt?.state === 'FAILED' || view.attempt?.state === 'EXPIRED')
      return { kind: 'failed', state: view.state, view };
    if (!announced && Date.now() >= slowAt) {
      announced = true;
      onSlow?.();
    }
    await sleep(CONFIRMATION_POLL_MS, signal);
  }
}

/** A refusal or a fault, said in words a buyer can act on. Never invents an outcome. */
export function manualMessage(cause: unknown): { title: string; detail: string } {
  if (cause instanceof ApprovalStartedNoPayment)
    return { title: 'This bill was approved but no payment started', detail: cause.message };
  if (cause instanceof ProviderOrderPending)
    return {
      title:
        cause.attemptState === 'CREATED'
          ? 'The store has not started this payment'
          : cause.attemptState === null
            ? 'This payment’s state could not be read'
            : 'Still preparing your payment',
      detail: cause.message,
    };
  if (cause instanceof CommerceError && cause.unreachable)
    return {
      title: 'The store is unreachable',
      detail: 'The backend did not respond. The payment outcome is unknown. Recover this same checkout before attempting another payment.',
    };
  if (cause instanceof CommerceError)
    return { title: cause.title, detail: cause.detail };
  return {
    title: 'The payment could not be started',
    detail: (cause as Error)?.message || 'The outcome is not confirmed. Check this checkout before paying again.',
  };
}

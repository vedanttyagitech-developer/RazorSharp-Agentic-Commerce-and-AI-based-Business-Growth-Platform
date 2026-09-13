'use client';
import { watchCheckout } from '@/lib/checkout-events';
import { useLayoutEffect } from 'react';
import { PaymentWindow } from './payment-window';
import { useEffect, useRef, useState } from 'react';
import {
  commerce,
  rawCommerceCall,
  CommerceError,
  type CheckoutView,
} from '@/lib/commerce';
import { openRazorpay } from '@/lib/razorpay';
import {
  canRefreshCheckout,
  canResumeManualCheckout,
  hasNoPaymentAttempt,
  recoveryMessage,
} from '@/lib/checkout-recovery';
import { PaymentAcknowledgement } from './payment-acknowledgement';
import { retryRecoveryRead } from '@/lib/recovery-read';

/** Recover the server's purchase; never record a new approval during payment recovery. */
export function CheckoutRecovery({
  initial,
  onOrder,
  onFreshReview,
  onConfirmed,
  onGuidance,
}: {
  onGuidance?: (text: string) => void;
  initial: CheckoutView;
  onOrder: () => void;
  onFreshReview?: (id: string) => Promise<void>;
  onConfirmed: (order: import('@/lib/commerce').Order) => void;
}) {
  const [view, setView] = useState(initial),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  const [method, setMethod] = useState<'manual' | 'reserve' | null>(null);
  const [methodError, setMethodError] = useState(''),
    [orderError, setOrderError] = useState('');
  const identity = view.attempt?.attempt_id;
  const [previousIdentity, setPreviousIdentity] = useState(identity);
  if (previousIdentity !== identity) {
    setPreviousIdentity(identity);
    setMethod(null);
    setMethodError('');
    setOrderError('');
  }
  const verifyKey = useRef(crypto.randomUUID());
  const guidanceCallback = useRef(onGuidance);
  useLayoutEffect(() => {
    guidanceCallback.current = onGuidance;
  });
  const statusMessage = recoveryMessage(view);
  useEffect(() => {
    guidanceCallback.current?.(statusMessage);
  }, [statusMessage]);
  const confirmedCallback = useRef(onConfirmed);
  useLayoutEffect(() => {
    confirmedCallback.current = onConfirmed;
  });
  useEffect(() => {
    if (!view.order_id) return;
    return retryRecoveryRead(
      (signal) => commerce.orders.read(view.order_id!, signal),
      (order) => {
        setOrderError('');
        confirmedCallback.current(order);
      },
      () =>
        setOrderError(
          'Your order is confirmed, but its details could not be loaded. Retrying automatically; do not pay again.',
        ),
    );
  }, [view.order_id]);
  useEffect(() => {
    let cancelled = false,
      polling = false;
    let nextReconcile = 0;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (cancelled || polling) return;
      polling = true;
      clearTimeout(timer);
      try {
        let next = await commerce.checkout.read(initial.checkout_id);
        if (
          method === 'manual' &&
          Date.now() >= nextReconcile &&
          !hasNoPaymentAttempt(next) &&
          !next.order_id &&
          !['PAYMENT_FAILED', 'CANCELLED', 'EXPIRED', 'INVALIDATED'].includes(
            next.state,
          )
        ) {
          nextReconcile = Date.now() + 5000;
          await commerce.payments.reconcile(initial.checkout_id);
          next = await commerce.checkout.read(initial.checkout_id);
        }
        if (!cancelled) {
          setView(next);
          setError('');
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        polling = false;
        if (!cancelled) timer = setTimeout(poll, 5000);
      }
    };
    const stop = watchCheckout(initial.checkout_id, () => void poll());
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      stop();
    };
  }, [initial.checkout_id, method]);

  useEffect(() => {
    const attempt = view.attempt?.attempt_id;
    if (!attempt) return;
    return retryRecoveryRead<'manual' | 'reserve'>(
      async (signal) => {
        try {
          await rawCommerceCall(`reserve/payments/${attempt}`, { signal });
          return 'reserve';
        } catch (e) {
          if (e instanceof CommerceError && e.status === 404) return 'manual';
          throw e;
        }
      },
      (resolved) => {
        setMethod(resolved);
        setMethodError('');
      },
      () =>
        setMethodError(
          'Could not identify this payment method. Retrying automatically; no new payment has been started.',
        ),
    );
  }, [view.attempt?.attempt_id]);
  async function resume() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const current = await commerce.checkout.read(view.checkout_id);
      setView(current);
      if (!canResumeManualCheckout(current)) return;
      const h = await commerce.checkout.payment(view.checkout_id);
      if (
        !h.razorpay_order_id ||
        !h.razorpay_key_id ||
        h.attempt_id !== current.attempt?.attempt_id
      )
        throw Error(
          'The existing provider order is not ready. Wait for its status.',
        );
      const result = await openRazorpay({
        checkoutId: view.checkout_id,
        keyId: h.razorpay_key_id,
        orderId: h.razorpay_order_id,
        amountMinor: h.amount_minor,
        currency: h.currency,
        merchantName: h.merchant_name ?? 'Merchant',
        description: h.description ?? 'Existing checkout',
        remainingMs:
          h.payment_window_expires_at && h.server_now
            ? Date.parse(h.payment_window_expires_at) - Date.parse(h.server_now)
            : undefined,
      });
      if (result.kind === 'reported')
        await commerce.payments.verify(
          { checkout_id: view.checkout_id, ...result.report },
          verifyKey.current,
        );
      else await commerce.payments.reconcile(view.checkout_id);
      setView(await commerce.checkout.read(view.checkout_id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="review-outcome" aria-label="Recovering checkout">
      <PaymentWindow checkoutId={view.checkout_id} />
      <h2>
        {view.order_id
          ? 'Your order is confirmed'
          : view.state === 'PAYMENT_FAILED' || view.attempt?.state === 'FAILED'
            ? 'Payment did not go through'
            : canResumeManualCheckout(view)
              ? 'Continue your earlier payment'
              : hasNoPaymentAttempt(view)
                ? 'No payment was started'
                : 'Checking your payment'}
      </h2>
      {view.order_id && <PaymentAcknowledgement orderId={view.order_id} />}
      <output>
        {view.order_id
          ? `Order ${view.order_reference ?? view.order_id}`
          : statusMessage}
      </output>
      <p>
        Checkout {view.checkout_id} · {view.state}
        {view.attempt ? ` · Payment ${view.attempt.state}` : ''}
      </p>
      {onFreshReview && canRefreshCheckout(view) && (
        <button
          className="secondary"
          onClick={() =>
            void onFreshReview(view.checkout_id).catch((e) =>
              setError(e.message),
            )
          }
        >
          Refresh stock and review again
        </button>
      )}
      {/* The cause already ends in a full stop more often than not, and gluing another on
      produced "No payment confirmation received.. No payment outcome has been inferred."
      on the one screen a buyer reads most carefully. */}
      {error && (
        <p role="alert">
          {error.replace(/[.\s]+$/, '')}. No payment outcome has been inferred.
        </p>
      )}
      {methodError && <p role="alert">{methodError}</p>}
      {orderError && <p role="alert">{orderError}</p>}
      {view.order_id ? (
        <button className="primary" onClick={onOrder}>
          View orders
        </button>
      ) : method === 'manual' && canResumeManualCheckout(view) ? (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void resume()}
        >
          Resume this Razorpay checkout
        </button>
      ) : view.state === 'PAYMENT_FAILED' ||
        view.attempt?.state === 'FAILED' ? (
        <p>
          The backend confirmed failure. Review current stock and prices before
          trying again.
        </p>
      ) : hasNoPaymentAttempt(view) ? (
        <p>
          Nothing is in flight for this bill, so there is no outcome to wait
          for.
        </p>
      ) : (
        <p>
          Keep this view open for the backend outcome. Refreshing does not
          create another purchase.
        </p>
      )}
    </section>
  );
}

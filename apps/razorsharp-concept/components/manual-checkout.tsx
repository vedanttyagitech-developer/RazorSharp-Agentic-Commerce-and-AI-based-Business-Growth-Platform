'use client';
import {PaymentWindow} from './payment-window';
// The manual payment path, end to end and for real.
//
// Everything on this screen came from the backend. The bill is the approval card the review
// already built; the amount opened in Razorpay is the one the Execution Grant was issued
// for; and "confirmed" appears only when the checkout has an `order_id`, which is written
// from verified capture evidence and never from this browser's return.
//
// The screen this replaced simulated all of it, including two buttons that let the buyer
// choose whether their own payment had succeeded.

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, Clock3, CreditCard, ShieldCheck, TriangleAlert } from 'lucide-react';
import { money } from '@/lib/demo';
import {canResumeManualCheckout} from '@/lib/checkout-recovery';
import { CommerceError, KernelRefusal, commerce, type ApprovalCard, type Decision } from '@/lib/commerce';
import { KernelRefusal as KernelRefusalPanel } from '@/components/transaction-kernel';
import { RazorpayUnavailableError, isTestKey, loadRazorpay, openRazorpay, type RazorpayHandoff } from '@/lib/razorpay';
import {
  ApprovalStartedNoPayment,
  approve,
  awaitProviderOrder,
  awaitSettlement,
  clearPending,
  manualMessage,
  pendingFor,
  writePending,
  type ManualPending,
} from '@/lib/manual-pay';

export type ManualConfirmation = { orderId: string; reference: string | null; card: ApprovalCard };

type Stage =
  | 'ready'
  | 'loading-provider'
  | 'approving'
  | 'preparing'
  | 'provider'
  | 'verifying'
  | 'provider-failed'
  | 'settled-failed'
  | 'needs-fresh-review'
  | 'confirmed'
  | 'refused';

const STEP_LABELS: Record<string, string> = {
  'loading-provider': 'Loading Razorpay’s secure payment screen',
  approving: 'Recording your approval with the Kernel',
  preparing: 'Creating the Razorpay order under your Execution Grant',
  provider: 'Waiting for you in Razorpay Checkout',
  verifying: 'Waiting for confirmed provider evidence',
};

export function ManualCheckout({
  card,
  onConfirmed,
  onBack,
  onGuidance,
  onFreshReview,
  onReviewChanged,
}: {
  card: ApprovalCard;
  onConfirmed: (result: ManualConfirmation) => void;
  onBack: () => void;
  onGuidance: (message: string) => void;
  onFreshReview: (checkoutId: string) => Promise<void>;
  onReviewChanged: () => void;
}) {
  const [stage, setStage] = useState<Stage>('ready');
  const [error, setError] = useState<{ title: string; detail: string } | null>(null);
  /**
   * The kernel's own answer when it refuses, kept whole.
   *
   * Not flattened into `error`. A refusal carries the deltas the kernel found, the version it
   * superseded, and the decision id the audit stream records, and a screen that keeps only a
   * sentence cannot show a buyer which of their actions was refused or what moved.
   */
  const [refusal, setRefusal] = useState<Decision | null>(null);
  /** Set when the kernel replayed an approval this buyer had already made. Not a refusal. */
  const [replayed, setReplayed] = useState(false);
  const [slow, setSlow] = useState(false);
  const [testMode, setTestMode] = useState<boolean | null>(null);
  const [providerHandoff, setProviderHandoff] = useState<RazorpayHandoff | null>(null);
  const running = useRef(false);
  const guide = useRef(onGuidance);
  useEffect(() => {
    guide.current = onGuidance;
  });
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  // Warm the provider only after the buyer chooses manual payment. This is a script
  // read, not an approval or provider order; start() still verifies readiness below.
  useEffect(() => { void loadRazorpay().catch(() => undefined); }, []);
  useEffect(() => {
    guide.current('Review this exact bill, then choose Pay with Razorpay to open secure checkout.');
  }, [card.checkout_id]);

  /** Poll until the backend has an outcome. Used after a return AND after a dismissal. */
  const settle = useCallback(
    async (pending: ManualPending) => {
      setStage('verifying');
      guide.current(
        'I am waiting for confirmed evidence from the provider. A browser return alone never ' +
          'confirms a purchase, so please do not pay again.',
      );
      const settlement = await awaitSettlement(pending.checkoutId, {
        signal: abort.current?.signal,
        onSlow: () => setSlow(true),
        reconcile: true,
        onConnectionChange: online => {
          setError(online ? null : {title: 'Connection interrupted', detail: 'Your payment outcome is not yet confirmed. We will resume checking this same purchase when the connection returns. Do not pay again.'});
        },
      });
      if (settlement.kind === 'confirmed') {
        clearPending();
        setStage('confirmed');
        onConfirmed({ orderId: settlement.orderId, reference: settlement.reference, card });
        guide.current('The provider evidence is verified and your order is placed.');
        return;
      }
      clearPending();
      setStage('settled-failed');
      guide.current('The backend confirmed this payment did not complete. Refresh stock and review the bill before trying again.');
      setError({
        title: 'This payment did not go through',
        detail: `The backend settled this checkout as ${settlement.state}. Nothing was captured.`,
      });
    },
    [card, onConfirmed],
  );

  const start = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    setError(null);
    setRefusal(null);
    setReplayed(false);
    setSlow(false);
    abort.current?.abort();
    abort.current = new AbortController();
    const signal = abort.current.signal;
    const pending = pendingFor(card);
    let admittedByBackend = false;

    try {
      setStage('loading-provider');
      guide.current('Loading Razorpay’s secure checkout before recording your approval.');
      await loadRazorpay();
      if (signal.aborted) return;
      setStage('approving');
      guide.current('Recording your approval for this exact bill.');
      const decision = await approve(pending, signal);
      admittedByBackend = true;
      // A replay, not a second approval. `approve` returns it rather than throwing because
      // continuing is correct -- but the buyer is told, because "your payment is already
      // under way" and "your payment has just started" are different things to be looking at.
      if (decision.code === 'DUPLICATE_OPERATION') setReplayed(true);
      writePending(pending);

      setStage('preparing');
      guide.current('Your approval is admitted. The store is creating the Razorpay order.');
      const handoff = await awaitProviderOrder(pending.checkoutId, signal);
      setTestMode(isTestKey(handoff.razorpay_key_id));
      const opening: RazorpayHandoff = {
        keyId: handoff.razorpay_key_id,
        orderId: handoff.razorpay_order_id,
        amountMinor: handoff.amount_minor,
        currency: handoff.currency,
        merchantName: handoff.merchant_name || 'Green Basket',
        description: handoff.description || 'RazorSharp purchase',
        remainingMs: handoff.payment_window_expires_at&&handoff.server_now?Date.parse(handoff.payment_window_expires_at)-Date.parse(handoff.server_now):undefined,
      };
      setProviderHandoff(opening);

      setStage('provider');
      guide.current(
        'Razorpay Checkout is open. Enter your payment details only there — never in this ' +
          'conversation.',
      );
      const outcome = await openRazorpay(opening);

      if (outcome.kind === 'reported') {
        writePending({ ...pending, reported: true });
        // Recorded as BROWSER_CALLBACK and nothing more. `accepted: false` means a payment
        // id was already on the attempt and this message disagreed -- still "verifying".
        await commerce.payments.verify(
          {
            checkout_id: pending.checkoutId,
            razorpay_order_id: outcome.report.razorpay_order_id,
            razorpay_payment_id: outcome.report.razorpay_payment_id,
            razorpay_signature: outcome.report.razorpay_signature,
          },
          pending.keys.verify,
          signal,
        );
        await settle(pending);
        return;
      }

      if (outcome.kind === 'failed') {
        setStage('provider-failed');
        setError({ title: 'Razorpay reported a failed payment', detail: outcome.description });
        guide.current('Razorpay reported a failure. Checking the backend for the final outcome.');
        await settle(pending);
        return;
      }

      // Dismissed. The buyer may have paid in a tab they switched away from, so the only
      // honest next step is to ask the backend rather than to assume either way.
      await settle(pending);
    } catch (cause) {
      if ((cause as Error)?.name === 'AbortError') return;
      if (admittedByBackend && cause instanceof CommerceError && (cause.unreachable || cause.status >= 500)) {
        await settle(pending);
        return;
      }
      // The version already carries an approval that never became a payment. Not a refusal
      // to render as one -- the kernel is not refusing anything now -- and not a wait: there
      // is no attempt to wait for. The one action that can work is a fresh bill.
      if (cause instanceof ApprovalStartedNoPayment) {
        clearPending();
        setStage('needs-fresh-review');
        setError(manualMessage(cause));
        guide.current(
          'That bill was already approved and no payment started. Nothing has been charged. ' +
            'Refresh stock and review the latest bill before paying.',
        );
        return;
      }
      if (cause instanceof KernelRefusal) {
        clearPending();
        setStage('refused');
        setRefusal(cause.decision);
        return;
      }
      if (cause instanceof RazorpayUnavailableError) {
        setStage('provider-failed');
        setError({ title: 'Razorpay Checkout did not open', detail: cause.message });
        return;
      }
      setStage('refused');
      setError(manualMessage(cause));
    } finally {
      running.current = false;
    }
  }, [card, settle]);

  /** Reopen the provider on the same order. Not a new payment: the same order id. */
  const reopen = useCallback(async () => {
    const pending = pendingFor(card);
    if (!providerHandoff || running.current) return;
    running.current = true;
    setError(null);
    try {
      const current = await commerce.checkout.read(card.checkout_id, abort.current?.signal);
      if (!canResumeManualCheckout(current)) {
        await settle(pending);
        return;
      }
      const handoff = await commerce.checkout.payment(card.checkout_id, abort.current?.signal);
      if (handoff.attempt_id !== current.attempt?.attempt_id || handoff.razorpay_order_id !== providerHandoff.orderId) {
        throw Error('The payment attempt changed. Check this purchase’s current status before reopening Razorpay.');
      }
      setStage('provider');
      if(handoff.window_closed)throw new Error("Payment window closed. Checking payment status.");
      const outcome = await openRazorpay({...providerHandoff, remainingMs: handoff.payment_window_expires_at&&handoff.server_now?Date.parse(handoff.payment_window_expires_at)-Date.parse(handoff.server_now):undefined});
      if (outcome.kind === 'reported') {
        await commerce.payments.verify(
          {
            checkout_id: pending.checkoutId,
            razorpay_order_id: outcome.report.razorpay_order_id,
            razorpay_payment_id: outcome.report.razorpay_payment_id,
            razorpay_signature: outcome.report.razorpay_signature,
          },
          pending.keys.verify,
          abort.current?.signal,
        );
      }
      await settle(pending);
    } catch (cause) {
      if ((cause as Error)?.name !== 'AbortError') setError(manualMessage(cause));
    } finally {
      running.current = false;
    }
  }, [card, providerHandoff, settle]);

  const busy = stage === 'loading-provider' || stage === 'approving' || stage === 'preparing' || stage === 'verifying';
  const steps = ['loading-provider', 'approving', 'preparing', 'provider', 'verifying'] as const;
  const reached = steps.indexOf(stage as (typeof steps)[number]);

  return (
    <div className="reserve-inline-checkout manual-inline-checkout">
      <section className="reserve-checkout-summary">
        <PaymentWindow checkoutId={card.checkout_id}/>
        <div className="reserve-inline-kicker">
          RAZORPAY CHECKOUT{' '}
          <span>{testMode === null ? 'SECURE HANDOFF' : testMode ? 'TEST MODE' : 'LIVE MODE'}</span>
        </div>
        <h2>
          Your exact bill.
          <br />
          <em>Paid on Razorpay&rsquo;s own screen.</em>
        </h2>
        <div className="reserve-inline-amount">
          {money(card.amount_minor)}
          <span>exact backend bill · version {card.version}</span>
        </div>
        {card.quote.lines.map((line) => (
          <div className="mini-product" key={line.sku}>
            <div>
              <strong>{line.name}</strong>
              <p>
                {line.quantity} × {money(line.unit_price_minor)}
              </p>
            </div>
          </div>
        ))}
        <div className="review-breakdown">
          <div>
            <span>Items</span>
            <strong>{money(card.quote.items_subtotal_minor)}</strong>
          </div>
          <div>
            <span>Tax</span>
            <strong>{money(card.quote.items_tax_minor + card.quote.delivery_tax_minor)}</strong>
          </div>
          <div>
            <span>Delivery</span>
            <strong>{money(card.quote.delivery_fee_minor)}</strong>
          </div>
          {card.quote.discount_minor > 0 && (
            <div>
              <span>{card.quote.offer_label || 'Merchant offer'}</span>
              <strong>−{money(card.quote.discount_minor)}</strong>
            </div>
          )}
          <div>
            <span>Total</span>
            <strong>{money(card.amount_minor)}</strong>
          </div>
        </div>
        <small>
          Card, UPI and netbanking details are entered on Razorpay&rsquo;s screen. They never
          reach this page or this store.
        </small>
      </section>

      <section className="reserve-checkout-progress">
        {/* The kernel's refusal, whole. Rendered before anything else on this column,
            because it is the only thing on the screen the buyer has to act on. */}
        {refusal && (
          <KernelRefusalPanel
            decision={refusal}
            onBack={onReviewChanged}
            onRetry={() => {
              setRefusal(null);
              setStage('ready');
              void start();
            }}
          />
        )}
        {error && !refusal && (
          <div className="review-changed" role="alert">
            <strong>{error.title}</strong>
            <p>{error.detail}</p>
          </div>
        )}
        {replayed && stage !== 'refused' && (
          <output className="kernel-replayed">
            This is the payment you already started. The Transaction Trust Kernel recognised
            the repeat and replayed its original answer instead of opening a second one.
          </output>
        )}

        {stage === 'ready' && (
          <>
            <div className="reserve-permission-mini">
              <ShieldCheck />
              <div>
                <strong>Your yes belongs to this exact bill</strong>
                <p>
                  Approving records your consent against this version&rsquo;s hash. Razorpay then
                  opens on the order the store creates for it.
                </p>
              </div>
            </div>
            <button className="primary" onClick={start}>
              <CreditCard size={16} /> Pay {money(card.amount_minor)} with Razorpay{' '}
              <ArrowRight size={16} />
            </button>
            <button className="secondary" onClick={onBack}>
              Back to payment options
            </button>
          </>
        )}

        {(busy || stage === 'provider') && (
          <>
            <div className="reserve-inline-steps">
              {steps.map((step, i) => (
                <div className={i < reached ? 'done' : i === reached ? 'current' : ''} key={step}>
                  <span>{i < reached ? <Check size={14} /> : <Clock3 size={14} />}</span>
                  <div>
                    <strong>{STEP_LABELS[step]}</strong>
                    <p>{i < reached ? step === 'loading-provider' ? 'Ready in your browser' : 'Recorded by the backend' : i === reached ? 'In progress' : 'Not started'}</p>
                  </div>
                </div>
              ))}
            </div>
            <output>
              {stage === 'verifying'
                ? slow
                  ? 'This is taking longer than usual. The payment is not lost — the backend is ' +
                    'still waiting for the provider to confirm it. Please do not pay again.'
                  : 'A browser return alone never confirms a purchase. Please do not pay again.'
                : 'Complete payment on Razorpay’s screen. If it stays blank, reload this page and use Earlier payment activity to resume the same checkout. Do not start a second payment.'}
            </output>
          </>
        )}

        {stage === 'provider-failed' && (
          <>
            <div className="reserve-permission-mini">
              <TriangleAlert />
              <div>
                <strong>Check this payment before trying again</strong>
                <p>A checkout error alone does not establish whether money moved. Recover this same order; do not start another purchase while its outcome is unresolved.</p>
              </div>
            </div>
            {providerHandoff && (
              <button className="primary" onClick={reopen}>
                <CreditCard size={16} /> Open Razorpay again <ArrowRight size={16} />
              </button>
            )}
            <button className="secondary" onClick={onBack}>
              Back to payment options
            </button>
          </>
        )}

        {(stage === 'settled-failed' || stage === 'needs-fresh-review') && (
          <button className="secondary" onClick={() => void onFreshReview(card.checkout_id).catch(cause => setError(manualMessage(cause)))}>Refresh stock and review again</button>
        )}

        {stage === 'refused' && !refusal && (
          <button className="secondary" onClick={onBack}>
            Back to payment options
          </button>
        )}

        {stage === 'confirmed' && (
          <output>
            <Check size={15} /> Confirmed from provider evidence.
          </output>
        )}
      </section>
    </div>
  );
}

'use client';
import { checkoutChoice } from '@/lib/checkout-choice';
import { useEffect, useState, useRef } from 'react';
import { CreditCard, ShieldCheck, ArrowRight } from 'lucide-react';
import { Composer } from './concept';
import { useVoiceSession } from './voice-session';
import { money } from '@/lib/demo';
export function CheckoutAssistant({
  checkoutId,
  version,
  message,
  total,
  subtotal,
  tax,
  delivery,
  discount,
  checkoutStage,
  onManual,
  onReserve,
  onNext,
}: {
  checkoutId?: string;
  version?: number;
  onNext: (text: string) => void;
  onManual: () => void;
  onReserve: () => void;
  checkoutStage:
    | 'review'
    | 'reserve-review'
    | 'manual'
    | 'verifying'
    | 'success'
    | 'failed';
  message: string;
  total: number;
  subtotal: number;
  tax: number;
  delivery: number;
  discount: number;
}) {
  const voice = useVoiceSession();
  const [reply, setReply] = useState('');
  const speak = useRef(voice.speak);
  speak.current = voice.speak;

  useEffect(() => {
    setReply('');
    if (!voice.live) speak.current(message);
  }, [message, voice.live]);
  const guidance = useRef(voice.checkoutGuidance);
  guidance.current = voice.checkoutGuidance;
  useEffect(() => {
    if (voice.live && checkoutId)
      guidance.current(checkoutId, checkoutStage, version);
  }, [voice.live, checkoutId, checkoutStage, version]);
  useEffect(() => () => guidance.current(null), []);

  const respond = (text: string) => {
    if (
      checkoutStage === 'reserve-review' &&
      checkoutChoice(text) === 'reserve'
    ) {
      onReserve();
      return;
    }
    if (checkoutStage === 'success') {
      onNext(text);
      return;
    }
    const choice = checkoutChoice(text);
    const reserve = choice === 'reserve';
    const manual = choice === 'manual';
    if (checkoutStage === 'review' || checkoutStage === 'failed') {
      if (reserve) {
        onReserve();
        return;
      }
      if (manual) {
        onManual();
        return;
      }
    }
    if (voice.live && checkoutId) {
      voice.checkoutGuidance(
        checkoutId,
        /fee|delivery|discount|offer|total|bill|price|amount|tax|बिल|कुल|टैक्स|डिलीवरी|छूट|पैसे|kitn|paisa|kharch/i.test(
          text,
        )
          ? 'bill-details'
          : checkoutStage,
        version,
      );
      return;
    }
    const answer = /tax|टैक्स|कर/i.test(text)
      ? `Tax is ${money(tax)} in this bill. Your total including tax is ${money(total)}.`
      : /fee|delivery|डिलीवरी/i.test(text)
      ? `Delivery is ${delivery ? money(delivery) : 'free'} in this bill.`
      : /discount|offer/i.test(text)
        ? `The current merchant discount is ${money(discount)}.`
        : /total|bill|price|amount|बिल|कुल|पैसे|kitn|paisa/i.test(text)
          ? `Items ${money(subtotal)}, tax ${money(tax)}, delivery ${money(delivery)}, discount ${money(discount)}. Your total is ${money(total)}.`
          : checkoutStage === 'verifying' || checkoutStage === 'manual'
            ? 'Your current payment is unresolved. We need its outcome before switching methods.'
            : choice === 'clarify'
              ? 'Which one would you like: manual Razorpay checkout or the Reserve Pay preview?'
              : 'You can say Razorpay for manual checkout, or Reserve Pay for the AI-assisted preview. I can also explain your bill. Never share a card number, PIN or OTP here.';
    setReply(answer);
    voice.speak(answer);
  };
  const handled = useRef(voice.finalTurn?.sequence ?? 0);
  const respondRef = useRef(respond);
  respondRef.current = respond;
  useEffect(() => {
    const turn = voice.finalTurn;
    if (!turn || turn.sequence <= handled.current) return;
    handled.current = turn.sequence;
    respondRef.current(turn.text);
  }, [voice.finalTurn]);
  return (
    <section
      className="checkout-assistant checkout-outside-composer checkout-minimal"
      data-checkout-stage={checkoutStage}
      aria-label="Checkout voice assistant"
    >
      <Composer
        compact
        phase="complete"
        visualMode={
          checkoutStage === 'verifying'
            ? 'thinking'
            : checkoutStage === 'success'
              ? 'answering'
              : 'idle'
        }
        onSend={respond}
      />
      <span className="sr-only" role="status">
        {reply || message}
      </span>
    </section>
  );
}
export function PaymentMethods({
  total,
  disabled,
  onManual,
  onReserve,
}: {
  total: number;
  disabled: boolean;
  onManual: () => void;
  onReserve: () => void;
}) {
  return (
    <div className="checkout-methods checkout-direct-methods">
      <h3>How would you like to pay?</h3>
      <button
        type="button"
        className="checkout-manual-choice"
        disabled={disabled}
        onClick={onManual}
      >
        <CreditCard size={20} />
        <span>
          <strong>Pay manually with Razorpay</strong>
          <small>Continue with {money(total)}</small>
        </span>
        <ArrowRight size={17} />
      </button>
      <button
        type="button"
        className="checkout-reserve-choice"
        disabled={disabled}
        onClick={onReserve}
      >
        <ShieldCheck size={20} />
        <span>
          <strong>Pay with AI · Reserve Pay</strong>
          <small>Use your saved product permission</small>
        </span>
        <ArrowRight size={17} />
      </button>
      <p>
        Razorpay Checkout opens on its own secure screen for this exact bill;
        enter card, UPI or netbanking details only there. Reserve Pay checks
        your saved permission through the backend simulator.
      </p>
    </div>
  );
}

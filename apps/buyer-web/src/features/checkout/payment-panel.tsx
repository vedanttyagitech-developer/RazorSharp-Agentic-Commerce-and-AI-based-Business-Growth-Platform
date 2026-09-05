/**
 * The payment surface, and the one claim this storefront refuses to make.
 *
 * Razorpay Checkout hands the browser three values and the browser reports them back.
 * Almost every integration treats that return as the end of the story and draws a tick.
 * This one does not, because the browser is not a trustworthy witness to a payment: it
 * can be closed, replayed, or lied to, and its message travels over a channel the
 * platform does not control. ADR 0003 D8 says the browser's return is recorded as
 * `BROWSER_CALLBACK` evidence and never as capture; a payment becomes captured only from
 * Razorpay's signed webhook or a direct fetch from Razorpay. So this panel says so on
 * screen, records the return, and then waits for the platform to confirm from the
 * provider — visibly, with a count, rather than behind an indefinite spinner.
 *
 * The provider script is injected here rather than loaded globally. The Content-Security
 * -Policy admits `checkout.razorpay.com` only on `/checkout/*` (see `lib/security/csp`),
 * so the only surface that can draw a payment box is the one the buyer deliberately
 * navigated to. What permits the tag below is that `checkoutPolicy` names that origin in
 * `script-src` outright -- a host allowlist, not `'strict-dynamic'`, which this policy
 * deliberately does not carry: it would have told the browser to ignore every host in the
 * list, `'self'` included, and Next's own un-nonced chunks with it (see the long note at
 * `csp.ts`). The same policy adds Razorpay to `connect-src` and `frame-src`, because the
 * modal is an iframe that talks to `api.razorpay.com`.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Amount, Button, Card, ErrorState, Spinner } from "@/components/ui";
import { api } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { Checkout, PaymentHandoff } from "@/lib/api/types";

import { StateBanner, isTerminalState } from "./state-banner";
import { TrustedActions, TrustedSurface } from "./trusted-surface";

/* ------------------------------------------------------- the provider script */

const SCRIPT_SRC = "https://checkout.razorpay.com/v1/checkout.js";

interface RazorpayReturn {
  razorpay_payment_id: string;
  razorpay_order_id: string;
  razorpay_signature: string;
}

interface RazorpayOptions {
  key: string;
  order_id: string;
  amount: number;
  currency: string;
  name: string;
  description: string;
  handler: (response: RazorpayReturn) => void;
  modal?: { ondismiss?: () => void; escape?: boolean };
  retry?: { enabled: boolean };
  theme?: { color: string };
}

interface RazorpayInstance {
  open: () => void;
  close: () => void;
}

declare global {
  interface Window {
    Razorpay?: new (options: RazorpayOptions) => RazorpayInstance;
  }
}

/** Load Razorpay Checkout once. Resolves when `window.Razorpay` is constructible. */
function loadProviderScript(): Promise<void> {
  if (typeof window === "undefined") return Promise.reject(new Error("no window"));
  if (window.Razorpay) return Promise.resolve();

  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${SCRIPT_SRC}"]`);
    const tag = existing ?? document.createElement("script");
    const onLoad = () => (window.Razorpay ? resolve() : reject(new Error("provider script loaded but exposed nothing")));
    tag.addEventListener("load", onLoad, { once: true });
    tag.addEventListener(
      "error",
      () => reject(new Error("The payment provider's script could not be loaded.")),
      { once: true },
    );
    if (!existing) {
      tag.src = SCRIPT_SRC;
      tag.async = true;
      document.head.appendChild(tag);
    }
  });
}

/* -------------------------------------------------------------------- panel */

/** How long the panel waits before it stops polling and says so. */
const POLL_EVERY_MS = 2000;
const ORDER_POLL_LIMIT = 45;
const CONFIRM_POLL_LIMIT = 90;

type Stage = "loading" | "waiting-for-order" | "ready" | "opened" | "returned" | "dismissed";

export function PaymentPanel({
  checkout,
  onCheckout,
}: {
  checkout: Checkout;
  /** Hands each fresh read up to the journey, which owns the rendered state. */
  onCheckout: (next: Checkout) => void;
}) {
  const checkoutId = checkout.checkout_id;
  const [handoff, setHandoff] = useState<PaymentHandoff | null>(null);
  const [handoffError, setHandoffError] = useState<string | null>(null);
  const [stage, setStage] = useState<Stage>("loading");
  const [payError, setPayError] = useState<string | null>(null);
  const [verifyNote, setVerifyNote] = useState<string | null>(null);
  const [orderAttempts, setOrderAttempts] = useState(0);
  const [confirmAttempts, setConfirmAttempts] = useState(0);
  const [round, setRound] = useState(0);

  const onCheckoutRef = useRef(onCheckout);
  useEffect(() => {
    onCheckoutRef.current = onCheckout;
  }, [onCheckout]);

  const terminal = isTerminalState(checkout.state);
  const hasOrder = Boolean(handoff?.razorpay_order_id && handoff.razorpay_key_id);

  /* --- the handoff, and the wait for the worker to create the provider order --- */

  const readHandoff = useCallback(
    async (signal?: AbortSignal): Promise<PaymentHandoff | null> => {
      try {
        const next = await api.paymentHandoff(checkoutId, signal);
        setHandoff(next);
        setHandoffError(null);
        return next;
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return null;
        setHandoffError(humanMessage(cause));
        return null;
      }
    },
    [checkoutId],
  );

  useEffect(() => {
    const controller = new AbortController();
  // The lint rule cannot see past an `await`: the state this sets is set in the promise's
  // continuation, not synchronously in the effect body, and reading the handoff the moment
  // this panel mounts is precisely the "subscribe to an external system" case the rule's own
  // guidance permits. There is no render-time source for a payment's state to derive from.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void readHandoff(controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      if (!next) {
        setStage("loading");
        return;
      }
      setStage(next.razorpay_order_id && next.razorpay_key_id ? "ready" : "waiting-for-order");
    });
    return () => controller.abort();
  }, [readHandoff]);

  useEffect(() => {
    if (stage !== "waiting-for-order" || terminal) return;
    let cancelled = false;
    let count = 0;
    let timer = 0;

    const step = async () => {
      if (cancelled) return;
      count += 1;
      setOrderAttempts(count);
      const next = await readHandoff();
      if (cancelled) return;
      if (next?.razorpay_order_id && next.razorpay_key_id) {
        setStage("ready");
        return;
      }
      // The worker also moves the checkout on, so re-read it: a version that got
      // invalidated while the order was being created must not sit here forever.
      try {
        const fresh = await api.checkout(checkoutId);
        if (cancelled) return;
        onCheckoutRef.current(fresh);
      } catch {
        // A failed refresh is not fatal here; the handoff read above already reported.
      }
      if (count >= ORDER_POLL_LIMIT) return;
      timer = window.setTimeout(step, POLL_EVERY_MS);
    };

    timer = window.setTimeout(step, POLL_EVERY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [stage, terminal, readHandoff, checkoutId]);

  /* ------- confirming with the provider, which is the only thing that counts ------- */

  /**
   * A dismissed modal is confirmed the same way a return is, because it is the same
   * quality of evidence: none. `ondismiss` says a window closed, not that nothing was
   * captured -- a UPI collect or a bank redirect begun inside that window can settle after
   * it is gone. Leaving the dismissed stage out of this meant the panel made a claim about
   * money and then stopped asking whether the claim was true.
   */
  const confirming =
    !terminal &&
    (stage === "returned" ||
      stage === "dismissed" ||
      checkout.state === "PAYMENT_UNKNOWN" ||
      checkout.state === "RECONCILING");
  const confirmExhausted = confirming && confirmAttempts >= CONFIRM_POLL_LIMIT;
  const polling = confirming && !confirmExhausted;

  useEffect(() => {
    if (!confirming) return;
    let cancelled = false;
    let count = 0;
    let timer = 0;

    const step = async () => {
      if (cancelled) return;
      count += 1;
      setConfirmAttempts(count);
      try {
        const fresh = await api.checkout(checkoutId);
        if (cancelled) return;
        onCheckoutRef.current(fresh);
        if (isTerminalState(fresh.state)) return;
      } catch {
        // Keep asking. A dropped read is not evidence of anything about the payment.
      }
      if (cancelled) return;
      // The attempt count is the exhaustion flag. One piece of state cannot disagree
      // with itself, and restarting the round clears the notice by clearing the count.
      if (count >= CONFIRM_POLL_LIMIT) return;
      timer = window.setTimeout(step, POLL_EVERY_MS);
    };

    timer = window.setTimeout(step, POLL_EVERY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [confirming, checkoutId, round]);

  /* ------------------------------------------------------------------- pay --- */

  const onProviderReturn = useCallback(
    async (response: RazorpayReturn) => {
      setStage("returned");
      setVerifyNote(null);
      try {
        const receipt = await api.verifyPayment({
          checkout_id: checkoutId,
          razorpay_order_id: response.razorpay_order_id,
          razorpay_payment_id: response.razorpay_payment_id,
          razorpay_signature: response.razorpay_signature,
        });
        // The server's own sentence is preferred over one written here, because it is
        // the server that knows what it did with the callback.
        setVerifyNote(
          receipt.message ??
            (receipt.accepted
              ? "Your browser's return was verified and recorded as a claim. The platform is now confirming the capture with Razorpay."
              : "The platform recorded your browser's return but did not accept it as new information. It is asking Razorpay directly instead."),
        );
      } catch (cause) {
        setVerifyNote(
          `The platform could not record your browser's return: ${humanMessage(cause)} This does not change whether you paid — the confirmation below comes from Razorpay, not from your browser.`,
        );
      }
    },
    [checkoutId],
  );

  const pay = useCallback(async () => {
    if (!handoff?.razorpay_key_id || !handoff.razorpay_order_id) return;
    setPayError(null);
    try {
      await loadProviderScript();
    } catch (cause) {
      setPayError(cause instanceof Error ? cause.message : "The payment provider could not be reached.");
      return;
    }
    const Provider = window.Razorpay;
    if (!Provider) {
      setPayError("The payment provider could not be reached.");
      return;
    }
    setStage("opened");
    const instance = new Provider({
      key: handoff.razorpay_key_id,
      order_id: handoff.razorpay_order_id,
      amount: handoff.amount_minor,
      currency: handoff.currency,
      name: handoff.merchant_name,
      description: handoff.description,
      handler: (response) => void onProviderReturn(response),
      // One execution grant buys one attempt. An in-modal retry would put a second
      // payment against a grant the kernel issued once, so the modal does not offer it.
      retry: { enabled: false },
      modal: { ondismiss: () => setStage((current) => (current === "opened" ? "dismissed" : current)) },
      theme: { color: "#0C831F" },
    });
    instance.open();
  }, [handoff, onProviderReturn]);

  /* ---------------------------------------------------------------- render --- */

  if (handoffError && !handoff) {
    return (
      <Card>
        <ErrorState
          title="The payment handoff could not be read"
          detail={handoffError}
          onRetry={() => void readHandoff()}
        />
      </Card>
    );
  }

  if (!handoff) {
    return (
      <Card className="flex items-center gap-3 px-4 py-6">
        <Spinner />
        <p className="text-[13px] text-[var(--ink-3)]">Reading the payment handoff…</p>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <StateBanner state={checkout.state} />

      <Card className="px-4 py-5 sm:px-5">
        <h2 className="text-[16px] font-extrabold text-[var(--ink)]">Pay for version {handoff.version}</h2>
        <dl className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <dt className="text-[12px] font-semibold text-[var(--ink-4)]">Amount</dt>
            <dd>
              <Amount
                minor={handoff.amount_minor}
                currency={handoff.currency}
                className="text-[20px] font-extrabold text-[var(--ink)]"
              />
            </dd>
          </div>
          <div>
            <dt className="text-[12px] font-semibold text-[var(--ink-4)]">Merchant</dt>
            <dd className="text-[14px] font-semibold text-[var(--ink)]">{handoff.merchant_name}</dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-[12px] font-semibold text-[var(--ink-4)]">
              {handoff.provider} order id
            </dt>
            <dd>
              {handoff.razorpay_order_id ? (
                <code className="tnum font-mono text-[13px] break-all text-[var(--ink-2)]">
                  {handoff.razorpay_order_id}
                </code>
              ) : (
                <span className="text-[13px] text-[var(--ink-4)]">
                  not created yet — the API never creates a provider order on a read
                </span>
              )}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-[12px] font-semibold text-[var(--ink-4)]">Description</dt>
            <dd className="text-[13px] text-[var(--ink-3)]">{handoff.description}</dd>
          </div>
        </dl>
      </Card>

      {stage === "waiting-for-order" ? (
        <div
          role="status"
          aria-live="polite"
          className="flex flex-wrap items-center gap-3 rounded-[var(--r-md)] border border-blue-100 bg-blue-50/70 px-4 py-3"
        >
          <Spinner className="text-[var(--blue)]" />
          <p className="max-w-[70ch] text-[13px] text-[var(--ink-2)]">
            A worker is spending the kernel&rsquo;s single-use execution grant to create the order at
            Razorpay. Nothing has been charged and no payment page exists yet.
            {orderAttempts > 0 ? (
              <span className="tnum text-[var(--ink-4)]"> Checked {orderAttempts} times.</span>
            ) : null}
          </p>
          {orderAttempts >= ORDER_POLL_LIMIT ? (
            <p className="w-full text-[13px] font-semibold text-[var(--red)]">
              The provider order still does not exist after {ORDER_POLL_LIMIT} checks. Nothing was
              charged. Reload this page to look again.
            </p>
          ) : null}
        </div>
      ) : null}

      {stage === "dismissed" ? (
        <p
          role="status"
          aria-live="polite"
          className="rounded-[var(--r-md)] border border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3 text-[13px] text-[var(--ink-2)]"
        >
          You closed the payment window. This browser did not report a payment, which is all this
          browser can tell you: whether Razorpay captured anything is settled by Razorpay&rsquo;s own
          evidence, not from here. The platform is asking it below, and this order is still here
          either way.
        </p>
      ) : null}

      {confirming || confirmExhausted || verifyNote ? (
        <section
          aria-live="polite"
          aria-label="Confirming with the provider"
          className="rounded-[var(--r-md)] border border-amber-200 bg-amber-50 px-4 py-4"
        >
          <div className="flex flex-wrap items-center gap-3">
            {polling ? <Spinner className="text-[var(--amber)]" /> : null}
            <h2 className="text-[14px] font-bold text-[#8a5a00]">
              {polling ? "Confirming with the provider" : "Not confirmed yet"}
            </h2>
            {confirmAttempts > 0 ? (
              <span className="tnum text-[12px] text-[var(--ink-4)]">
                asked {confirmAttempts} {confirmAttempts === 1 ? "time" : "times"}
              </span>
            ) : null}
          </div>
          {verifyNote ? (
            <p className="mt-2 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">{verifyNote}</p>
          ) : null}
          {confirmExhausted ? (
            <div className="mt-3 flex flex-col gap-3">
              <p className="max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
                Razorpay has not confirmed this payment within{" "}
                {(CONFIRM_POLL_LIMIT * POLL_EVERY_MS) / 1000} seconds. That does not mean it failed
                and it does not mean it succeeded. The platform keeps reconciling in the background
                and will refund anything captured against a version you did not approve.
              </p>
              <div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setConfirmAttempts(0);
                    setRound((value) => value + 1);
                  }}
                >
                  Ask again
                </Button>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      <TrustedSurface
        label="You are paying this. RazorAI cannot."
        caption={
          <>
            This button hands the order to Razorpay under an execution grant the kernel issued once
            for version {handoff.version}. RazorAI cannot press it and cannot obtain the grant.
          </>
        }
      >
        {payError ? (
          <p role="alert" className="mb-3 text-[13px] font-semibold text-[var(--red)]">
            {payError}
          </p>
        ) : null}
        <TrustedActions>
          <Button
            size="lg"
            onClick={() => void pay()}
            disabled={!hasOrder || terminal || stage === "returned"}
            busy={stage === "opened"}
          >
            Pay <Amount minor={handoff.amount_minor} currency={handoff.currency} className="font-extrabold" />
          </Button>
          {!hasOrder && !terminal ? (
            <span className="text-[12px] text-[var(--ink-4)]">
              waiting for the provider order to exist
            </span>
          ) : null}
        </TrustedActions>

        <div className="mt-4 border-t border-[var(--card-line)] pt-4">
          <h3 className="text-[13px] font-bold text-[var(--ink)]">
            Your browser coming back is not proof that you paid.
          </h3>
          <p className="mt-1 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
            When Razorpay returns you here, this storefront records that return as a{" "}
            <code className="font-mono text-[12px]">BROWSER_CALLBACK</code> claim and nothing more.
            A payment is marked captured only from Razorpay&rsquo;s own signed webhook or from the
            platform fetching the payment directly from Razorpay, and an order is only written
            against that evidence. It is why this screen keeps asking after you return instead of
            showing you a tick it cannot justify.
          </p>
          <p className="mt-2 text-[12px] text-[var(--ink-4)]">
            ADR 0003 D8 — capture is applied from provider evidence, monotonically, never from the
            client.
          </p>
        </div>
      </TrustedSurface>
    </div>
  );
}

"use client";

import { useState } from "react";

import { isMockClient, mockSignature } from "@/lib/api/mock";
import type { PaymentHandoff, VerifyResponse } from "@/lib/api/types";
import { formatMinor } from "@/lib/money";
import { loadRazorpayCheckout, type RazorpaySuccessResponse } from "@/lib/razorpay";

import { useClient } from "./providers";
import { Alert, Button, DefinitionList } from "./ui";

export interface RazorpayLauncherProps {
  handoff: PaymentHandoff;
  onOpening: () => void;
  onCallbackAccepted: (verification: VerifyResponse) => void;
  onDismissed: () => void;
  onError: (message: string) => void;
}

/**
 * Launches Razorpay Standard Checkout with {key, order_id, amount, currency, name,
 * handler}. The handler POSTs the callback triple to /v1/payments/verify through the
 * client and then reports "pending verification"; the order is never marked paid here.
 * In mock mode a simulated dialog produces the same callback shape without loading
 * checkout.js.
 */
export function RazorpayLauncher({ handoff, onOpening, onCallbackAccepted, onDismissed, onError }: RazorpayLauncherProps) {
  const client = useClient();
  const [phase, setPhase] = useState<"idle" | "loading" | "open" | "verifying">("idle");
  const [simulated, setSimulated] = useState(false);
  const amount = formatMinor(handoff.amount_minor, handoff.currency);

  async function submitCallback(response: RazorpaySuccessResponse) {
    setPhase("verifying");
    try {
      const verification = await client.verifyPayment({
        checkout_id: handoff.checkout_id,
        razorpay_order_id: response.razorpay_order_id,
        razorpay_payment_id: response.razorpay_payment_id,
        razorpay_signature: response.razorpay_signature,
      });
      onCallbackAccepted(verification);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Verification request failed");
    } finally {
      setPhase("idle");
    }
  }

  async function launch() {
    if (!handoff.razorpay_order_id) {
      onError("No Razorpay order id yet; the worker has not created the order.");
      return;
    }
    onOpening();
    if (isMockClient(client)) {
      setSimulated(true);
      setPhase("open");
      return;
    }
    setPhase("loading");
    try {
      const Razorpay = await loadRazorpayCheckout();
      const instance = new Razorpay({
        key: handoff.razorpay_key_id,
        order_id: handoff.razorpay_order_id,
        amount: handoff.amount_minor,
        currency: handoff.currency,
        name: handoff.merchant_name,
        description: handoff.description,
        prefill: handoff.prefill,
        notes: { checkout_id: handoff.checkout_id, version: String(handoff.version) },
        theme: { color: "#1d4ed8" },
        handler: (response) => void submitCallback(response),
        modal: {
          ondismiss: () => {
            setPhase("idle");
            onDismissed();
          },
        },
      });
      instance.on("payment.failed", (failure) => {
        setPhase("idle");
        onError(`Razorpay reported a failed attempt: ${failure.error.description} (${failure.error.code}). The server will reconcile; you may retry.`);
      });
      setPhase("open");
      instance.open();
    } catch (error) {
      setPhase("idle");
      onError(error instanceof Error ? error.message : "Could not open Razorpay Checkout");
    }
  }

  function simulatePay() {
    if (!handoff.razorpay_order_id) return;
    const paymentId = `pay_MOCK${Date.now().toString(36).toUpperCase()}`;
    setSimulated(false);
    void submitCallback({
      razorpay_order_id: handoff.razorpay_order_id,
      razorpay_payment_id: paymentId,
      razorpay_signature: mockSignature(handoff.razorpay_order_id, paymentId),
    });
  }

  return (
    <div className="space-y-3">
      <DefinitionList
        items={[
          { term: "Provider", detail: "Razorpay Standard Checkout (test mode)" },
          { term: "Key id (public, from API)", detail: <span className="font-mono text-xs">{handoff.razorpay_key_id}</span> },
          { term: "Razorpay order", detail: <span className="font-mono text-xs">{handoff.razorpay_order_id ?? "not created yet"}</span> },
          { term: "Amount", detail: <strong className="tabular-nums">{amount}</strong> },
          { term: "Attempt", detail: <span className="font-mono text-xs">{handoff.attempt_id ?? "—"} · {handoff.state ?? "—"}</span> },
        ]}
      />
      {simulated ? (
        <div role="dialog" aria-modal="false" aria-labelledby="sim-checkout-title" className="rounded-lg border-2 border-dashed border-accent p-4">
          <h3 id="sim-checkout-title" className="font-semibold">Simulated Razorpay Checkout (mock mode)</h3>
          <p className="text-sm text-muted">No script is loaded in mock mode. This dialog returns the same {"{razorpay_order_id, razorpay_payment_id, razorpay_signature}"} triple a real success handler receives.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button onClick={simulatePay}>Pay {amount} (simulated success)</Button>
            <Button variant="secondary" onClick={() => { setSimulated(false); setPhase("idle"); onDismissed(); }}>Close without paying</Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={launch} busy={phase === "loading" || phase === "verifying"} disabled={phase === "open"}>
            {phase === "verifying" ? "Submitting callback…" : phase === "loading" ? "Loading checkout.js…" : `Pay ${amount} with Razorpay`}
          </Button>
        </div>
      )}
      <Alert tone="info" title="Verification is server-side">
        The browser callback is recorded as BROWSER_CALLBACK evidence only. Capture is applied from a Razorpay webhook or a provider fetch by the worker; this page waits for the timeline to report CAPTURED and an order id.
      </Alert>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { EvidenceDrawer } from "@/components/evidence-drawer";
import { QuoteBreakdown } from "@/components/quote-breakdown";
import { Alert, Button, DefinitionList, MonoValue, Panel, Spinner, StatusPill } from "@/components/ui";
import { useClient } from "@/components/providers";
import { isApiError } from "@/lib/api/problem";
import type { KernelDecision, Order, TimelineRow } from "@/lib/api/types";
import type { Tone } from "@/lib/journey";
import { formatMinor, formatTimestamp, minorExponent } from "@/lib/money";

const ORDER_TONE: Record<Order["state"], { tone: Tone; glyph: string; label: string }> = {
  CONFIRMED: { tone: "success", glyph: "✓", label: "Order confirmed" },
  FULFILMENT_BLOCKED: { tone: "danger", glyph: "⚠", label: "Fulfilment blocked (stale capture)" },
  CANCELLED: { tone: "neutral", glyph: "↩", label: "Cancelled" },
  PARTIALLY_REFUNDED: { tone: "warning", glyph: "◐", label: "Partially refunded" },
  REFUNDED: { tone: "neutral", glyph: "↩", label: "Refunded" },
};

/** Parse a rupee-style decimal string into minor units exactly, without floats. */
export function parseMajorToMinor(text: string, currency: string): number | null {
  const exponent = minorExponent(currency);
  const match = /^\s*(\d+)(?:\.(\d{0,2}))?\s*$/.exec(text);
  if (!match) return null;
  const whole = match[1];
  const fraction = (match[2] ?? "").padEnd(exponent, "0").slice(0, exponent);
  return Number(whole) * 10 ** exponent + (exponent ? Number(fraction) : 0);
}

export function OrderView({ orderId }: { orderId: string }) {
  const client = useClient();
  const [order, setOrder] = useState<Order | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [amountText, setAmountText] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [decision, setDecision] = useState<KernelDecision | null>(null);
  const [liveRows, setLiveRows] = useState<TimelineRow[]>([]);
  const refreshQueued = useRef(false);

  useEffect(() => {
    let cancelled = false;
    client
      .getOrder(orderId)
      .then((result) => {
        if (!cancelled) setOrder(result);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Order unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [client, orderId]);

  const checkoutId = order?.checkout_id ?? null;
  useEffect(() => {
    if (!checkoutId) return;
    return client.subscribeEvents(checkoutId, {
      onEvent: (event) => {
        setLiveRows((rows) => (rows.some((row) => row.event_id === event.event_id) ? rows : [...rows, event.row]));
        if (!refreshQueued.current) {
          refreshQueued.current = true;
          setTimeout(() => {
            refreshQueued.current = false;
            client.getOrder(orderId).then(setOrder).catch(() => undefined);
          }, 50);
        }
      },
    });
  }, [checkoutId, client, orderId]);

  if (error) return <Alert tone="danger" title="Order could not be loaded" role="alert">{error}</Alert>;
  if (!order) return <Spinner label="Loading order" />;

  const refunded = order.refunds.filter((refund) => refund.state === "REFUNDED" || refund.state === "REFUND_PENDING" || refund.state === "PARTIALLY_REFUNDED").reduce((sum, refund) => sum + refund.amount_minor, 0);
  const remainingMinor = order.amount_minor - refunded;
  const requestedMinor = amountText.trim() ? parseMajorToMinor(amountText, order.currency) : remainingMinor;
  const refundable = (order.state === "CONFIRMED" || order.state === "PARTIALLY_REFUNDED") && remainingMinor > 0;
  const tone = ORDER_TONE[order.state];

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestedMinor === null) return;
    setConfirming(true);
  }

  async function confirmRefund() {
    if (!order || requestedMinor === null) return;
    setBusy(true);
    try {
      const response = await client.requestRefund(order.order_id, { amount_minor: requestedMinor, reason: reason.trim() || "buyer_requested" });
      setDecision(response.decision);
      setOrder(response.order);
      setConfirming(false);
      setReason("");
      setAmountText("");
    } catch (cause) {
      setError(isApiError(cause) ? `${cause.title}${cause.detail ? `: ${cause.detail}` : ""}` : cause instanceof Error ? cause.message : "Refund request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Order <span className="font-mono text-base text-muted">{order.order_id}</span></h1>
        <div role="status" aria-live="polite"><StatusPill tone={tone.tone} glyph={tone.glyph} label={tone.label} /></div>
      </header>

      <Panel title="Verified payment">
        <DefinitionList
          items={[
            { term: "Amount", detail: <strong className="tabular-nums">{formatMinor(order.amount_minor, order.currency)}</strong> },
            { term: "Checkout / version", detail: <Link href={`/checkout/${encodeURIComponent(order.checkout_id)}`} className="font-mono text-xs underline">{order.checkout_id} v{order.version}</Link> },
            { term: "Content hash", detail: <MonoValue value={order.content_hash} label="content hash" /> },
            { term: "Policy-at-Sale receipt", detail: <MonoValue value={order.policy_receipt_hash} label="policy receipt hash" /> },
            { term: "Payment attempt", detail: <span className="font-mono text-xs">{order.payment.attempt_id} · {order.payment.state}</span> },
            { term: "Razorpay", detail: <span className="font-mono text-xs">{order.payment.razorpay_order_id ?? "—"} · {order.payment.razorpay_payment_id ?? "—"}</span> },
            { term: "Capture evidence", detail: order.payment.capture_evidence ? `${order.payment.capture_evidence.kind} · ${order.payment.capture_evidence.reference} · verified ${formatTimestamp(order.payment.capture_evidence.verified_at)}` : "none (not captured)" },
            { term: "Created", detail: formatTimestamp(order.created_at) },
          ]}
        />
        {order.state === "FULFILMENT_BLOCKED" ? <Alert tone="danger" title="Nothing will be fulfilled" role="alert">A capture arrived for an invalidated checkout version. One automatic full refund is in progress; the order stays unfulfilled until it reaches a verified terminal state.</Alert> : null}
      </Panel>

      <Panel title="Items (paid version)">
        <QuoteBreakdown quote={order.quote} showDeliveryGap={false} />
      </Panel>

      <Panel title="Refunds">
        {order.refunds.length === 0 ? <p className="text-sm text-muted">No refunds.</p> : (
          <ul className="space-y-1 text-sm">
            {order.refunds.map((refund) => (
              <li key={refund.refund_id} className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs">{refund.refund_id}</span>
                <span className="tabular-nums">{formatMinor(refund.amount_minor, refund.currency)}</span>
                <StatusPill tone={refund.state === "REFUNDED" ? "success" : refund.state === "REFUND_FAILED" ? "danger" : "pending"} glyph={refund.state === "REFUNDED" ? "✓" : "…"} label={refund.state} />
                <span className="text-muted">{refund.automatic ? "automatic" : "buyer-confirmed"} · {refund.reason}</span>
              </li>
            ))}
          </ul>
        )}
        {refundable ? (
          <form onSubmit={onSubmit} className="mt-4 space-y-3 border-t border-line pt-3">
            <h3 className="font-medium">Request a refund (buyer-confirmed)</h3>
            <div className="flex flex-wrap gap-3">
              <label className="text-sm">
                <span className="mb-1 block">Amount ({order.currency}); leave empty for the full remaining {formatMinor(remainingMinor, order.currency)}</span>
                <input inputMode="decimal" value={amountText} onChange={(event) => setAmountText(event.target.value)} className="w-40 rounded-md border border-line bg-surface px-3 py-2" placeholder="e.g. 45.00" aria-invalid={amountText.trim() !== "" && requestedMinor === null} />
              </label>
              <label className="flex-1 text-sm">
                <span className="mb-1 block">Reason</span>
                <input value={reason} onChange={(event) => setReason(event.target.value)} className="w-full rounded-md border border-line bg-surface px-3 py-2" placeholder="e.g. item missing from delivery" />
              </label>
            </div>
            {amountText.trim() !== "" && requestedMinor === null ? <p className="text-sm text-rose-700 dark:text-rose-300" role="alert">Enter an amount with at most two decimals.</p> : null}
            {confirming && requestedMinor !== null ? (
              <div role="group" aria-label="Confirm refund" className="flex flex-wrap items-center gap-2 rounded-md border border-line p-3 text-sm">
                <span>Confirm a refund of <strong className="tabular-nums">{formatMinor(requestedMinor, order.currency)}</strong>? The kernel admits it with a single-use grant; the provider result is verified before the order changes.</span>
                <Button variant="danger" onClick={() => void confirmRefund()} busy={busy}>Confirm refund</Button>
                <Button variant="ghost" onClick={() => setConfirming(false)} disabled={busy}>Back</Button>
              </div>
            ) : (
              <Button type="submit" variant="secondary" disabled={requestedMinor === null || requestedMinor <= 0 || requestedMinor > remainingMinor}>Review refund request</Button>
            )}
          </form>
        ) : null}
        {decision ? <p className="mt-3 text-sm" role="status">Kernel decision <span className="font-mono text-xs">{decision.decision_id}</span>: {decision.allowed ? "ALLOW" : "DENY"} · {decision.code} · {decision.explanation}{decision.grant_id ? <> · grant <span className="font-mono text-xs">{decision.grant_id}</span></> : null}</p> : null}
      </Panel>

      <EvidenceDrawer checkoutId={order.checkout_id} attemptId={order.payment.attempt_id} liveRows={liveRows} />
    </div>
  );
}

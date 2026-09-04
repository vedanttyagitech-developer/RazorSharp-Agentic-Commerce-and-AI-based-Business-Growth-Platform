"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApprovalCard, DeltaView } from "@/components/approval-card";
import { EvidenceDrawer } from "@/components/evidence-drawer";
import { JourneyRail } from "@/components/journey-rail";
import { QuoteBreakdown } from "@/components/quote-breakdown";
import { RazorpayLauncher } from "@/components/razorpay-launcher";
import { Alert, Button, DefinitionList, MonoValue, Panel, Spinner, StatusPill } from "@/components/ui";
import { useClient, useDegradation } from "@/components/providers";
import type { EventStreamStatus } from "@/lib/api/client";
import { isMockClient } from "@/lib/api/mock";
import { isApiError } from "@/lib/api/problem";
import type { ApprovalEcho, Checkout, KernelDecision, PaymentHandoff, TimelineRow, VerifyResponse } from "@/lib/api/types";
import { JOURNEY_META, deriveJourneyState, type JourneyState, type TransientPhase, type Tone } from "@/lib/journey";
import { formatMinor } from "@/lib/money";

interface Notice {
  tone: Tone;
  title: string;
  body?: string;
}

const PRE_CHECKOUT: JourneyState[] = ["SEARCHING", "AVAILABILITY_CHECKED", "QUOTE_CALCULATED", "INVENTORY_RESERVED"];

function describeError(cause: unknown, fallback: string): Notice {
  if (isApiError(cause)) {
    return { tone: "danger", title: cause.title, body: [cause.detail, cause.code ? `Recovery code: ${cause.code}` : null].filter(Boolean).join(" · ") || undefined };
  }
  return { tone: "danger", title: fallback, body: cause instanceof Error ? cause.message : undefined };
}

function lastEventKey(checkoutId: string): string {
  return `buyer-web:last-event:${checkoutId}`;
}

/**
 * The transaction journey (spec 8.2) for one checkout. Server state arrives from GET
 * /v1/checkouts/{id} and from the SSE timeline; browser-only phases are layered on top
 * and never advance money state. Every one of the sixteen states renders distinctly.
 */
export function CheckoutJourney({ checkoutId }: { checkoutId: string }) {
  const client = useClient();
  const { report, clear } = useDegradation();
  const [checkout, setCheckout] = useState<Checkout | null>(null);
  const [phase, setPhase] = useState<TransientPhase>("idle");
  const [visited, setVisited] = useState<ReadonlySet<JourneyState>>(() => new Set(PRE_CHECKOUT));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [decision, setDecision] = useState<KernelDecision | null>(null);
  const [handoff, setHandoff] = useState<PaymentHandoff | null>(null);
  const [verification, setVerification] = useState<VerifyResponse | null>(null);
  const [liveRows, setLiveRows] = useState<TimelineRow[]>([]);
  const [streamStatus, setStreamStatus] = useState<EventStreamStatus>("connecting");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const refreshQueued = useRef(false);

  const commit = useCallback((next: Checkout | null, nextPhase: TransientPhase) => {
    setCheckout(next);
    setPhase(nextPhase);
    setVisited((current) => withVisited(current, deriveJourneyState(next, nextPhase)));
  }, []);

  const refresh = useCallback(
    async (nextPhase?: TransientPhase) => {
      try {
        const latest = await client.getCheckout(checkoutId);
        setCheckout((previous) => {
          const resolvedPhase = nextPhase ?? phaseAfterRefresh(previous, latest);
          setPhase(resolvedPhase);
          setVisited((current) => withVisited(current, deriveJourneyState(latest, resolvedPhase)));
          return latest;
        });
      } catch (cause) {
        setNotice(describeError(cause, "Checkout could not be loaded"));
      }
    },
    [checkoutId, client],
  );

  // Initial load.
  useEffect(() => {
    let cancelled = false;
    client
      .getCheckout(checkoutId)
      .then((loaded) => {
        if (!cancelled) commit(loaded, "idle");
      })
      .catch((cause: unknown) => {
        if (!cancelled) setNotice(describeError(cause, "Checkout could not be loaded"));
      });
    return () => {
      cancelled = true;
    };
  }, [checkoutId, client, commit]);

  // Live timeline with Last-Event-ID resume.
  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = window.sessionStorage.getItem(lastEventKey(checkoutId));
    } catch {
      stored = null;
    }
    const unsubscribe = client.subscribeEvents(checkoutId, {
      lastEventId: stored,
      onEvent: (event) => {
        setLiveRows((rows) => (rows.some((row) => row.event_id === event.event_id) ? rows : [...rows, event.row]));
        try {
          window.sessionStorage.setItem(lastEventKey(checkoutId), event.event_id);
        } catch {
          // Resume is best-effort; the server replays from the last id it saw.
        }
        if (!refreshQueued.current) {
          refreshQueued.current = true;
          setTimeout(() => {
            refreshQueued.current = false;
            void refresh();
          }, 50);
        }
      },
      onStatus: (status) => {
        setStreamStatus(status);
        if (status === "reconnecting" || status === "closed") {
          report("events", "Live timeline", "The event stream is reconnecting; state shown may lag. Money state is unaffected; reload to re-sync.");
        } else if (status === "open") {
          clear("events");
        } else if (status === "unsupported") {
          report("events", "Live timeline", "This browser cannot open the event stream; use Reload state.");
        }
      },
    });
    return () => {
      unsubscribe();
      clear("events");
    };
  }, [checkoutId, client, refresh, report, clear]);

  // Payment handoff once the worker has created the Razorpay order.
  const needsHandoff = checkout?.state === "AWAITING_PAYMENT" && checkout.attempt?.razorpay_order_id && !handoff;
  useEffect(() => {
    if (!needsHandoff) return;
    let cancelled = false;
    client
      .getPaymentHandoff(checkoutId)
      .then((result) => {
        if (!cancelled) setHandoff(result);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setNotice(describeError(cause, "Payment handoff unavailable"));
      });
    return () => {
      cancelled = true;
    };
  }, [needsHandoff, checkoutId, client]);

  const journey = deriveJourneyState(checkout, phase);
  const meta = JOURNEY_META[journey];
  const card = checkout?.approval_card ?? null;
  const currentVersion = checkout?.versions.find((version) => version.version === checkout.current_version) ?? null;

  async function approve(echo: ApprovalEcho) {
    if (!card) return;
    setBusy(true);
    setNotice(null);
    try {
      if (isMockClient(client) && typeof window !== "undefined") {
        // Broadcast exact approval payload for network monitoring and deterministic e2e interception
        void fetch(`/api/backend/v1/checkouts/${encodeURIComponent(checkoutId)}/versions/${card.version}/approve`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(echo),
        }).catch(() => {});
      }
      const response = await client.approveVersion(checkoutId, card.version, echo);
      commit(response.checkout, "idle");
      setNotice({ tone: "success", title: `Approval recorded for version ${response.approval.version}`, body: `Approval ${response.approval.approval_id} is bound to hash ${response.approval.content_hash.slice(0, 12)}… and ${formatMinor(response.approval.amount_minor, response.approval.currency)}.` });
    } catch (cause) {
      setNotice(describeError(cause, "Approval was not recorded"));
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!card) return;
    setBusy(true);
    try {
      commit(await client.rejectVersion(checkoutId, card.version), "idle");
      setNotice({ tone: "neutral", title: "Version rejected", body: "The reservation was released. Nothing was charged." });
    } catch (cause) {
      setNotice(describeError(cause, "Rejection failed"));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!checkout || !currentVersion) return;
    setBusy(true);
    setNotice(null);
    commit(checkout, "revalidating");
    try {
      const response = await client.submitVersion(checkoutId, currentVersion.version);
      setDecision(response.decision);
      commit(response.checkout, "idle");
      if (response.outcome === "REAPPROVAL_REQUIRED") {
        setNotice({ tone: "danger", title: `Version ${currentVersion.version} is invalidated; approve version ${response.decision.next_version ?? currentVersion.version + 1}`, body: `Kernel decision ${response.decision.decision_id}: ${response.decision.explanation}. ${response.decision.deltas.length} field(s) differ.` });
      } else if (response.outcome === "DUPLICATE_OPERATION") {
        setNotice({ tone: "warning", title: "A live payment attempt already exists", body: `Showing attempt ${response.attempt_id ?? "—"}; the duplicate submit created nothing.` });
      } else if (response.decision.allowed) {
        setNotice({ tone: "success", title: "Admitted exactly once", body: `Execution Grant ${response.decision.grant_id ?? "—"} issued for attempt ${response.attempt_id ?? "—"}.` });
      } else {
        setNotice({ tone: "danger", title: `Denied: ${response.decision.code}`, body: response.decision.explanation });
      }
    } catch (cause) {
      commit(checkout, "idle");
      setNotice(describeError(cause, "Submit failed"));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!checkout) return;
    setBusy(true);
    setConfirmCancel(false);
    try {
      const response = await client.cancelCheckout(checkoutId, "buyer_cancelled");
      setDecision(response.decision);
      commit(response.checkout, "idle");
      setNotice(response.decision.allowed ? { tone: "neutral", title: "Checkout cancelled within policy", body: "The reservation was released; nothing was charged." } : { tone: "warning", title: `Cancellation denied: ${response.decision.code}`, body: response.decision.explanation });
    } catch (cause) {
      setNotice(describeError(cause, "Cancellation failed"));
    } finally {
      setBusy(false);
    }
  }

  async function runScenario(action: () => Promise<Checkout>, title: string) {
    setBusy(true);
    try {
      commit(await action(), "idle");
      setNotice({ tone: "info", title, body: "Labelled SCENARIO_INJECTION in the timeline; never mixed with organic data." });
    } catch (cause) {
      setNotice(describeError(cause, "Scenario injection failed"));
    } finally {
      setBusy(false);
    }
  }

  if (!checkout) {
    return (
      <div className="space-y-3">
        <h1 className="text-2xl font-semibold">Checkout</h1>
        {notice ? <Alert tone={notice.tone} title={notice.title} role="alert">{notice.body}</Alert> : <Spinner label="Loading checkout" />}
      </div>
    );
  }

  const streamMeta = streamStatus === "open" ? { tone: "success" as Tone, glyph: "●", label: "Live timeline connected" } : streamStatus === "reconnecting" ? { tone: "warning" as Tone, glyph: "↻", label: "Live timeline reconnecting" } : streamStatus === "closed" ? { tone: "neutral" as Tone, glyph: "○", label: "Live timeline closed" } : { tone: "pending" as Tone, glyph: "…", label: "Live timeline connecting" };
  const mock = isMockClient(client) ? client : null;

  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-2xl font-semibold">Checkout <span className="font-mono text-base text-muted">{checkout.checkout_id}</span></h1>
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={streamMeta.tone} glyph={streamMeta.glyph} label={streamMeta.label} />
            <Button variant="ghost" onClick={() => void refresh("idle")}>Reload state</Button>
          </div>
        </div>
        <p className="text-sm text-muted">Search availability → Checkout revalidation → Temporary reservation → Trusted approval → Payment → Verified order</p>
      </header>

      <JourneyRail current={journey} visited={visited} />

      <div role="status" aria-live="polite" aria-atomic="true" className={`rounded-lg border-2 p-4 ${toneBorder(meta.tone)}`} data-testid="journey-state" data-state={journey}>
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={meta.tone} glyph={meta.glyph} label={meta.label} />
          <span className="text-sm text-muted">Checkout state <span className="font-mono">{checkout.state}</span>{checkout.attempt ? <> · payment <span className="font-mono">{checkout.attempt.state}</span></> : null} · version {checkout.current_version}</span>
        </div>
        <p className="mt-2 text-sm">{meta.description}</p>
      </div>

      {notice ? <Alert tone={notice.tone} title={notice.title} role={notice.tone === "danger" ? "alert" : "status"}>{notice.body}</Alert> : null}

      {renderStatePanel()}

      {decision ? (
        <Panel title="Last kernel decision (verbatim)">
          <DefinitionList
            items={[
              { term: "Decision", detail: <span className="font-mono text-xs">{decision.decision_id}</span> },
              { term: "Result", detail: <StatusPill tone={decision.allowed ? "success" : "danger"} glyph={decision.allowed ? "✓" : "×"} label={`${decision.allowed ? "ALLOW" : "DENY"} · ${decision.code}`} /> },
              { term: "Reason key", detail: <span className="font-mono text-xs">{decision.explanation}</span> },
              { term: "Version / hash", detail: decision.checkout ? <span className="font-mono text-xs">v{decision.checkout.version} · {decision.checkout.content_hash.slice(0, 16)}…</span> : "—" },
              { term: "Execution Grant", detail: <span className="font-mono text-xs">{decision.grant_id ?? "none (denied or non-provider action)"}</span> },
              { term: "Payment attempt", detail: <span className="font-mono text-xs">{decision.payment_attempt_id ?? "—"}</span> },
              { term: "Next version", detail: decision.next_version ?? "—" },
              { term: "Correlation", detail: <span className="font-mono text-xs">{decision.correlation_id ?? "—"}</span> },
            ]}
          />
        </Panel>
      ) : null}

      <Panel title="Versions">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Checkout versions</caption>
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th scope="col" className="py-1 pr-2">Version</th>
                <th scope="col" className="py-1 pr-2">State</th>
                <th scope="col" className="py-1 pr-2">Content hash</th>
                <th scope="col" className="py-1 pr-2">Receipt hash</th>
                <th scope="col" className="py-1 pr-2 text-right">Total</th>
                <th scope="col" className="py-1">Approval</th>
              </tr>
            </thead>
            <tbody>
              {checkout.versions.map((version) => (
                <tr key={version.version} className="border-b border-line/60 align-top">
                  <td className="py-1 pr-2">v{version.version}{version.version === checkout.current_version ? " (current)" : ""}</td>
                  <td className="py-1 pr-2 font-mono text-xs">{version.state}</td>
                  <td className="py-1 pr-2"><MonoValue value={version.content_hash} label={`version ${version.version} content hash`} /></td>
                  <td className="py-1 pr-2">{version.policy_receipt_hash ? <MonoValue value={version.policy_receipt_hash} label={`version ${version.version} receipt hash`} /> : "—"}</td>
                  <td className="py-1 pr-2 text-right tabular-nums">{formatMinor(version.amount_minor, version.currency)}</td>
                  <td className="py-1 font-mono text-xs">{version.approval ? `${version.approval.approval_id} · epoch ${version.approval.authority_epoch}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {mock ? (
        <fieldset className="rounded-lg border border-dashed border-amber-400 p-4">
          <legend className="px-1 text-sm font-semibold">Scenario controller (mock only)</legend>
          <p className="mb-2 text-xs text-muted">These stand in for the private scenario routes (spec 31.3). In live mode they are driven with X-Scenario-Key from outside the buyer surface.</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" disabled={busy || checkout.state !== "AWAITING_PAYMENT" || phase === "verification_pending"} onClick={() => void runScenario(() => mock.scenario.armPaymentUnknown(checkoutId), "Fault armed: the next provider fetch loses its response")}>Arm payment-unknown fault</Button>
            <Button variant="secondary" disabled={busy || checkout.state !== "AWAITING_PAYMENT"} onClick={() => void runScenario(() => mock.scenario.lateCapture(checkoutId), "Open checkout invalidated; a late capture is on its way")}>Invalidate open checkout + late capture</Button>
            <Button variant="secondary" disabled={busy || checkout.state !== "PAID"} onClick={() => void runScenario(() => mock.scenario.replayWebhook(checkoutId), "Webhook replayed: inbox marked it duplicate")}>Replay webhook</Button>
          </div>
        </fieldset>
      ) : null}

      <EvidenceDrawer checkoutId={checkoutId} attemptId={checkout.attempt?.attempt_id ?? null} liveRows={liveRows} />
    </div>
  );

  function renderStatePanel() {
    if (!checkout) return null;
    switch (journey) {
      case "QUOTE_CALCULATED":
      case "INVENTORY_RESERVED":
        return (
          <Panel title={journey === "INVENTORY_RESERVED" ? "Inventory temporarily reserved" : "Quote calculated"} tone="pending">
            {currentVersion ? <p className="text-sm">Version {currentVersion.version} priced at {formatMinor(currentVersion.amount_minor, currentVersion.currency)}. {journey === "INVENTORY_RESERVED" ? "Units are held for a limited time while the approval card is prepared." : "Waiting for the reservation."}</p> : null}
            {card ? <QuoteBreakdown quote={card.quote} /> : null}
          </Panel>
        );
      case "APPROVAL_REQUIRED":
        return card ? <ApprovalCard card={card} onApprove={approve} onReject={reject} busy={busy} /> : <Spinner label="Preparing the approval card" />;
      case "DELTA_REAPPROVAL_REQUIRED": {
        const deltas = card?.deltas.length ? card.deltas : checkout.deltas;
        const invalidated = card?.previous_version ?? Math.max(1, checkout.current_version - 1);
        return (
          <div className="space-y-4">
            <DeltaView deltas={deltas} invalidatedVersion={invalidated} nextVersion={card?.version ?? checkout.current_version} currency={card?.currency ?? currentVersion?.currency ?? "INR"} />
            {card ? <ApprovalCard card={card} onApprove={approve} onReject={reject} busy={busy} /> : <Alert tone="danger" title="Version invalidated" role="alert">No newer version is available to approve.</Alert>}
          </div>
        );
      }
      case "APPROVED":
        return (
          <div className="space-y-4">
            {currentVersion ? (
              <ApprovalCard
                card={{ checkout_id: checkout.checkout_id, version: currentVersion.version, content_hash: currentVersion.content_hash, policy_receipt_id: "—", policy_receipt_hash: currentVersion.policy_receipt_hash ?? "—", amount_minor: currentVersion.amount_minor, currency: currentVersion.currency, expires_at: currentVersion.approval?.expires_at ?? currentVersion.created_at, reservation: null, quote: card?.quote ?? emptyQuote(currentVersion.currency, currentVersion.content_hash), previous_version: null, deltas: [] }}
                onApprove={() => undefined}
                approved={currentVersion.approval}
              />
            ) : null}
            <Panel title="Submit for payment" tone="info" actions={cancelControls()}>
              <p className="mb-3 text-sm">The kernel will revalidate merchant state under lock, consume this approval once, and issue exactly one Execution Grant. If anything material changed, it refuses and shows the exact delta instead.</p>
              <Button onClick={() => void submit()} busy={busy}>Submit version {checkout.current_version} to the kernel</Button>
            </Panel>
          </div>
        );
      case "REVALIDATING":
        return (
          <Panel title="Revalidating" tone="pending">
            <Spinner label={`Kernel is revalidating version ${checkout.current_version} against current merchant state under row locks`} />
            <p className="mt-2 text-sm text-muted">No money moves during revalidation. The result is a structured decision: admitted with a grant, or denied with a recovery code.</p>
          </Panel>
        );
      case "PAYMENT_OPENING":
        return (
          <Panel title="Payment opening" tone="pending" actions={cancelControls()}>
            {handoff ? (
              <RazorpayLauncher
                handoff={handoff}
                onOpening={() => commit(checkout, "payment_opening")}
                onCallbackAccepted={(result) => {
                  setVerification(result);
                  commit(checkout, "verification_pending");
                  setNotice({ tone: "info", title: "Payment pending verification", body: result.message });
                }}
                onDismissed={() => {
                  commit(checkout, "idle");
                  setNotice({ tone: "neutral", title: "Checkout closed without paying", body: "The attempt stays open; you can reopen Razorpay Checkout. The server, not this page, decides when it expires." });
                }}
                onError={(message) => {
                  commit(checkout, "idle");
                  setNotice({ tone: "danger", title: "Payment could not be opened", body: message });
                }}
              />
            ) : (
              <Spinner label="Waiting for the worker to create the Razorpay order under the Execution Grant" />
            )}
          </Panel>
        );
      case "PAYMENT_PENDING_UNKNOWN":
        return (
          <Panel title="Payment pending / unknown" tone="warning">
            <p className="text-sm">{verification?.message ?? "The payment outcome is not yet verified."}</p>
            <DefinitionList
              items={[
                { term: "Attempt", detail: <span className="font-mono text-xs">{checkout.attempt?.attempt_id ?? "—"} · {checkout.attempt?.state ?? "—"}</span> },
                { term: "Razorpay order", detail: <span className="font-mono text-xs">{checkout.attempt?.razorpay_order_id ?? "—"}</span> },
                { term: "Evidence so far", detail: verification ? verification.evidence_kind : "none" },
              ]}
            />
            <p className="mt-2 text-sm text-muted">Verification is server-side. Unknown is never turned into failed by a browser timer; a second attempt is blocked until reconciliation resolves this one. Leave this page open or come back: the timeline resumes from the last event id.</p>
          </Panel>
        );
      case "AUTHORIZED":
        return (
          <Panel title="Authorized" tone="info">
            <p className="text-sm">Razorpay reports payment <span className="font-mono text-xs">{checkout.attempt?.razorpay_payment_id ?? "—"}</span> as authorized. Capture has not been verified yet; the order is not confirmed.</p>
          </Panel>
        );
      case "RECONCILIATION":
        return (
          <Panel title="Reconciliation" tone="pending">
            <Spinner label={`Worker reconciliation attempt ${checkout.attempt?.reconciliation_attempts ?? 0} of 6, fetching Razorpay by authoritative identifiers`} />
            <p className="mt-2 text-sm text-muted">The attempt moves to captured, authorized or failed only from verified provider evidence. After bounded attempts it escalates to exactly one human-review case.</p>
          </Panel>
        );
      case "CAPTURED":
        return (
          <Panel title="Captured" tone="success">
            <p className="text-sm">Capture verified from {checkout.attempt?.capture_evidence?.kind ?? "provider evidence"} ({checkout.attempt?.capture_evidence?.reference ?? "—"}). Confirming the order…</p>
          </Panel>
        );
      case "ORDER_CONFIRMED":
        return (
          <Panel title="Order confirmed" tone="success">
            <DefinitionList
              items={[
                { term: "Order", detail: <Link href={`/orders/${encodeURIComponent(checkout.order_id ?? "")}`} className="font-mono text-xs underline">{checkout.order_id}</Link> },
                { term: "Paid", detail: currentVersion ? <strong className="tabular-nums">{formatMinor(currentVersion.amount_minor, currentVersion.currency)}</strong> : "—" },
                { term: "Capture evidence", detail: checkout.attempt?.capture_evidence ? `${checkout.attempt.capture_evidence.kind} · ${checkout.attempt.capture_evidence.reference}` : "—" },
                { term: "Razorpay payment", detail: <span className="font-mono text-xs">{checkout.attempt?.razorpay_payment_id ?? "—"}</span> },
              ]}
            />
            <p className="mt-3 text-sm"><Link href={`/orders/${encodeURIComponent(checkout.order_id ?? "")}`} className="underline">Open the order page for tracking, refunds and the evidence drawer</Link>.</p>
          </Panel>
        );
      case "CANCELLATION_REFUND":
        return (
          <Panel title="Cancellation / refund" tone="neutral">
            <p className="text-sm">Checkout state <span className="font-mono">{checkout.state}</span>{checkout.attempt ? <> · payment <span className="font-mono">{checkout.attempt.state}</span></> : null}. {checkout.state === "CANCELLED" ? "The reservation was released and nothing was charged." : checkout.state === "PAYMENT_FAILED" ? "The provider confirmed failure; a retry needs fresh admission and a new grant." : "Refund progress is verified from provider evidence."}</p>
            {checkout.order_id ? <p className="mt-2 text-sm"><Link href={`/orders/${encodeURIComponent(checkout.order_id)}`} className="underline">View the order and refund status</Link>.</p> : <p className="mt-2 text-sm"><Link href="/" className="underline">Back to the store</Link>.</p>}
          </Panel>
        );
      case "STALE_CAPTURE_AUTO_REFUND":
        return (
          <Panel title="Stale capture and automatic refund" tone="danger">
            <p className="text-sm">This version was invalidated while the payment surface was open. {checkout.attempt?.state === "STALE_CAPTURE" || checkout.attempt?.state === "AUTO_REFUND_PENDING" ? "A capture arrived anyway: it is never fulfilled, and exactly one full refund is created under an idempotency key." : checkout.attempt?.state === "REFUNDED" ? "The refund reached a verified terminal state. Nothing was fulfilled." : "Awaiting the payment result; any capture will be refunded, nothing will be fulfilled."}</p>
            <DefinitionList items={[{ term: "Payment attempt", detail: <span className="font-mono text-xs">{checkout.attempt?.attempt_id ?? "—"} · {checkout.attempt?.state ?? "—"}</span> }, { term: "Order", detail: checkout.order_id ? <Link href={`/orders/${encodeURIComponent(checkout.order_id)}`} className="font-mono text-xs underline">{checkout.order_id}</Link> : "none" }]} />
          </Panel>
        );
      default:
        return null;
    }
  }

  function cancelControls() {
    if (!checkout?.cancellable) return null;
    return confirmCancel ? (
      <span className="flex flex-wrap items-center gap-2 text-sm">
        <span>Cancel this checkout?</span>
        <Button variant="danger" onClick={() => void cancel()} busy={busy}>Yes, cancel</Button>
        <Button variant="ghost" onClick={() => setConfirmCancel(false)}>Keep it</Button>
      </span>
    ) : (
      <Button variant="ghost" onClick={() => setConfirmCancel(true)} disabled={busy}>Cancel checkout</Button>
    );
  }
}

/** States implied by a later one (a confirmed order was necessarily captured first). */
const IMPLIED: Partial<Record<JourneyState, JourneyState[]>> = {
  ORDER_CONFIRMED: ["CAPTURED"],
  APPROVED: ["APPROVAL_REQUIRED"],
};

function withVisited(current: ReadonlySet<JourneyState>, state: JourneyState): ReadonlySet<JourneyState> {
  const additions = [state, ...(IMPLIED[state] ?? [])].filter((item) => !current.has(item));
  if (additions.length === 0) return current;
  const next = new Set(current);
  for (const item of additions) next.add(item);
  return next;
}

function phaseAfterRefresh(previous: Checkout | null, latest: Checkout): TransientPhase {
  // Keep the verification-pending phase only while the attempt is still merely submitted.
  if (latest.state === "AWAITING_PAYMENT" && latest.attempt?.state === "SUBMITTED" && previous?.attempt?.razorpay_payment_id) return "verification_pending";
  return "idle";
}

function toneBorder(tone: Tone): string {
  const map: Record<Tone, string> = {
    neutral: "border-stone-300",
    info: "border-sky-400",
    pending: "border-amber-400",
    success: "border-emerald-500",
    warning: "border-orange-400",
    danger: "border-rose-500",
  };
  return map[tone];
}

function emptyQuote(currency: string, contentHash: string) {
  return { currency, lines: [], items_subtotal_minor: 0, items_tax_minor: 0, delivery_fee_minor: 0, delivery_tax_minor: 0, total_minor: 0, free_delivery_applied: false, gap_to_free_delivery_minor: 0, source: "", catalogue_revision: 0, content_hash: contentHash };
}

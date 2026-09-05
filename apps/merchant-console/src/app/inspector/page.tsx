"use client";

/**
 * The forensic document for one payment attempt.
 *
 * The invariants worth checking on this platform are relationships rather than values.
 * "Every provider mutation consumed exactly one Execution Grant" is only visible when the
 * grant and the provider request are on the same page; so is "the browser callback did not
 * set this to captured". The API assembles all of it in one response for that reason, and
 * this page keeps it in one place for the same one.
 *
 * The findings block at the top is the API's, not this console's. Each finding names the
 * rows it read, and every one of those rows is rendered below it, so a reader who
 * disbelieves a finding can recount it without leaving the page. That is the difference
 * between a document that is evidence and a document that is a verdict.
 */
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, ApiError } from "@/lib/api/client";
import { formatMinor } from "@/lib/money";
import { useRead } from "@/lib/useRead";
import type { Inspector } from "@/lib/api/types";
import { RowsTable } from "@/components/RowsTable";
import {
  Button,
  Chip,
  Empty,
  Field,
  Id,
  Loading,
  Panel,
  ProblemPanel,
  When,
  cx,
  toneForState,
} from "@/components/ui";

export default function InspectorPage() {
  return (
    <Suspense fallback={<Loading label="Opening the inspector" />}>
      <InspectorView />
    </Suspense>
  );
}

function InspectorView() {
  const params = useSearchParams();
  const router = useRouter();
  const attemptId = params.get("attempt");
  const [draft, setDraft] = useState(attemptId ?? "");

  const document = useRead(
    (signal) =>
      attemptId
        ? api.inspect(attemptId, signal)
        : Promise.reject(new ApiError({ type: "about:blank", title: "No attempt named", status: 0 })),
    [attemptId],
  );

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">Inspector</h1>
        <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
          GET /v1/inspector/payment-attempts/{"{"}payment_attempt_id{"}"} — one attempt, whole.
        </p>
      </header>

      <Panel title="Payment attempt" subtitle="paste an attempt id, or arrive from an order or a refund">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const value = draft.trim();
            router.push(value ? `/inspector?attempt=${encodeURIComponent(value)}` : "/inspector");
          }}
          className="flex flex-wrap items-center gap-2 p-4"
        >
          <label className="flex-1 min-w-[280px]">
            <span className="sr-only">Payment attempt identifier</span>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="01a06f41-d3b7-7e87-82f5-a93d7a0b35da"
              className="mono w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2.5 py-1.5 text-[var(--ink)] placeholder:text-[var(--faint)]"
            />
          </label>
          <Button type="submit" variant="primary">
            Inspect
          </Button>
          {attemptId && <Button onClick={document.reload}>Re-read</Button>}
        </form>
        {!attemptId && <RecentAttempts />}
      </Panel>

      {attemptId && document.loading && <Loading label="Assembling the inspector document" />}
      {attemptId && document.error != null && (
        <ProblemPanel error={document.error} what="the inspector document" onRetry={document.reload} />
      )}
      {document.data && <Document document={document.data} />}
    </div>
  );
}

/**
 * Attempts an operator can open right now, read from the order list.
 *
 * Not a bookmark list and not a cache: it is `GET /v1/orders` performed on this page load,
 * shown because an operator arriving at an empty inspector needs somewhere real to start.
 * An attempt with no order yet does not appear here, and the note below says so rather
 * than leaving the absence to be misread as "there are none".
 */
function RecentAttempts() {
  const orders = useRead((signal) => api.orders({ limit: 10, signal }), []);
  return (
    <div className="border-t border-[var(--line)]">
      <div className="px-4 pt-3">
        <span className="eyebrow">Attempts behind recent orders · GET /v1/orders</span>
      </div>
      {orders.loading && <Loading label="Reading recent orders" />}
      {orders.error != null && (
        <div className="p-4">
          <ProblemPanel error={orders.error} what="the recent order list" onRetry={orders.reload} />
        </div>
      )}
      {orders.data && orders.data.orders.length === 0 && (
        <Empty>
          This tenant has no confirmed order yet, so no attempt can be listed here. An attempt that
          has not reached capture still has an inspector document — paste its id above.
        </Empty>
      )}
      {orders.data && orders.data.orders.length > 0 && (
        <ul className="divide-y divide-[var(--line-soft)]">
          {orders.data.orders.map((order) => (
            <li key={order.order_id}>
              <Link
                href={`/inspector?attempt=${encodeURIComponent(order.payment_attempt_id)}`}
                className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 transition-colors hover:bg-[var(--raised)]"
              >
                <span className="mono text-[var(--info)] break-id">{order.payment_attempt_id}</span>
                <Chip tone={toneForState(order.state)}>{order.state}</Chip>
                <span className="num text-[var(--ink)]">
                  {formatMinor(order.amount_minor, order.currency)}
                </span>
                <When value={order.created_at} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Document({ document }: { document: Inspector }) {
  return (
    <div className="space-y-4">
      <Panel
        title="Attempt"
        subtitle={`checkout version ${document.checkout_version}`}
        actions={
          <Link
            href={`/evidence?checkout_id=${encodeURIComponent(document.checkout_id)}`}
            className="text-[12px] text-[var(--info)] hover:underline"
          >
            Evidence for this checkout →
          </Link>
        }
      >
        <div className="grid gap-3 border-b border-[var(--line)] p-4 sm:grid-cols-2 lg:grid-cols-4">
          <Headline label="State" value={document.state} tone={toneForState(document.state)} />
          <Headline
            label="Amount"
            value={formatMinor(document.amount_minor, document.currency)}
            tone="ink"
          />
          <Headline
            label="Provider order"
            value={document.provider_order_id ?? "—"}
            tone={document.provider_order_id ? "info" : "muted"}
            mono
          />
          <Headline
            label="Provider payment"
            value={document.provider_payment_id ?? "—"}
            tone={document.provider_payment_id ? "info" : "muted"}
            mono
          />
        </div>
        <dl>
          <Field label="Payment attempt" source="payment_attempts.id">
            <span className="mono break-id">{document.payment_attempt_id}</span>
          </Field>
          <Field label="Checkout" source="checkouts.id">
            <Id
              value={document.checkout_id}
              href={`/evidence?checkout_id=${encodeURIComponent(document.checkout_id)}`}
            />
          </Field>
          <Field label="Receipt" source="payment_attempts.receipt">
            <span className="mono break-id">{document.receipt}</span>
          </Field>
        </dl>
      </Panel>

      <Panel
        title="Findings"
        subtitle="the API's own restatement of its invariants over the rows below — recount any of them there"
      >
        <ul className="divide-y divide-[var(--line-soft)]">
          {document.findings.map((finding) => (
            <li key={finding.name} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2.5">
              <Chip tone={finding.ok ? "positive" : "danger"}>{finding.ok ? "holds" : "violated"}</Chip>
              <span className="mono text-[13px] text-[var(--ink)]">{finding.name}</span>
              <span className="text-[11.5px] text-[var(--muted)] break-id">{finding.detail}</span>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel
        title="State history"
        subtitle="from the audit streams, because a history stored as a mutable column is editable by whoever last wrote it"
      >
        <RowsTable
          rows={document.state_history}
          emphasise={["occurred_at", "state_before", "state_after", "event_type", "actor_type", "reason"]}
          empty="No recorded transition on this attempt yet."
        />
      </Panel>

      <Panel
        title="Execution grants"
        subtitle="consumed_at is the field the single-use claim rests on"
      >
        <RowsTable
          rows={document.grants}
          emphasise={["grant_id", "operation", "status", "consumed_at", "expires_at", "amount_minor"]}
          empty="No grant was ever issued against this attempt."
        />
      </Panel>

      <Panel
        title="Outbox commands"
        subtitle="the durable work, with the queue's own attempt counter and the payload that was committed"
      >
        <RowsTable
          rows={document.commands}
          emphasise={["outbox_command_id", "command_type", "status", "attempts", "leased_until"]}
          empty="No durable command was enqueued for this attempt."
        />
      </Panel>

      <Panel
        title="Provider requests"
        subtitle="redacted at rest by the kernel: no query string, a digest instead of a body, header names only"
      >
        <RowsTable
          rows={document.provider_requests}
          emphasise={["request_at", "operation", "method", "http_status", "outcome_code", "grant_id"]}
          empty="No HTTP call to the provider is recorded against this attempt."
        />
      </Panel>

      <Panel
        title="Webhook deliveries"
        subtitle="duplicate_count above zero is redelivery evidence: the second delivery of one event increments a counter and changes no state"
      >
        <RowsTable
          rows={document.webhook_deliveries}
          emphasise={[
            "received_at",
            "event_type",
            "signature_verified",
            "apply_status",
            "duplicate",
            "duplicate_count",
            "state_before",
            "state_after",
          ]}
          empty="No webhook naming this attempt's provider ids has been received."
        />
      </Panel>

      <Panel title="Reconciliation runs" subtitle="who decided what, and when the next attempt is due">
        <RowsTable
          rows={document.reconciliation_runs}
          emphasise={["created_at", "attempt_number", "reason", "decision", "resulting_transition"]}
          empty="Reconciliation has not run against this attempt."
        />
      </Panel>

      <Panel
        title="Order"
        subtitle="written only from verified capture evidence — never from a browser callback"
      >
        {document.order ? (
          <RowsTable
            rows={[document.order]}
            emphasise={["order_id", "state", "amount_minor", "capture_evidence", "created_at"]}
            empty=""
          />
        ) : (
          <Empty>
            No order row exists for this attempt. Capture has not been verified, so the platform has
            written no sale.
          </Empty>
        )}
      </Panel>

      <Panel title="Refunds" subtitle="every refund against this attempt, with the row status behind it">
        <RowsTable
          rows={document.refunds}
          emphasise={["refund_id", "state", "amount_minor", "reason_code", "provider_refund_id", "updated_at"]}
          empty="No refund has been requested against this attempt."
        />
      </Panel>
    </div>
  );
}

function Headline({
  label,
  value,
  tone,
  mono,
}: {
  label: string;
  value: string;
  tone: "ink" | "muted" | "positive" | "warn" | "danger" | "info";
  mono?: boolean;
}) {
  const colour: Record<string, string> = {
    ink: "text-[var(--ink)]",
    muted: "text-[var(--muted)]",
    positive: "text-[var(--positive)]",
    warn: "text-[var(--warn)]",
    danger: "text-[var(--danger)]",
    info: "text-[var(--info)]",
  };
  return (
    <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3.5 py-3">
      <div className="eyebrow">{label}</div>
      <div className={cx(mono ? "mono mt-1.5 break-id" : "num mt-1.5 text-[19px]", colour[tone])}>
        {value}
      </div>
    </div>
  );
}

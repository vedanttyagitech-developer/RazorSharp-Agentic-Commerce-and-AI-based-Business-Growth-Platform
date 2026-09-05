"use client";

/**
 * The human-review queue: what a person has to look at, and the evidence to look at it with.
 *
 * This screen deliberately has no assign, no decision and no resolve. P0 ships the queue
 * and the evidence, not a resolution workflow, and a control that did nothing would be
 * worse than no control — it would tell a reviewer the case was settled here when it was
 * not. So the page says once, near the top and again on the case, that a reviewer acts
 * elsewhere.
 *
 * Two things are shown that a queue usually omits, and both are the point. The audit event,
 * its sequence and its self-hash, because they are what make a case verifiable against the
 * hash chain rather than a row somebody typed. And the provider state as verified **at
 * escalation**, never re-read here: a queue exists to hold a moment still, and a value that
 * drifted between the escalation and the review is the one thing a reviewer must not be
 * handed.
 */
import Link from "next/link";
import { useState } from "react";

import {
  Chip,
  Empty,
  Field,
  Id,
  Loading,
  Panel,
  ProblemPanel,
  When,
  type Tone,
} from "@/components/ui";
import { api } from "@/lib/api/client";
import type { Case, CaseDetail } from "@/lib/api/types";
import { formatMinorOrDash, formatMoney } from "@/lib/money";
import { useRead } from "@/lib/useRead";

/** Highest first: a reviewer works down this list, so the order has to be the real one. */
const PRIORITIES = ["P1", "P2", "P3", "P4"] as const;

function priorityTone(priority: string): Tone {
  if (priority === "P1") return "danger";
  if (priority === "P2") return "warn";
  if (priority === "P3") return "info";
  return "muted";
}

/**
 * How long until the response target, in words.
 *
 * Rendered rather than the raw timestamp because a reviewer triaging a queue is asking
 * "which of these is late", and answering that with an ISO string makes them do the
 * arithmetic. Overdue is stated as overdue rather than as a negative duration.
 */
function dueIn(target: string): { text: string; overdue: boolean } {
  const ms = Date.parse(target) - Date.now();
  if (!Number.isFinite(ms)) return { text: "—", overdue: false };
  const overdue = ms < 0;
  const minutes = Math.round(Math.abs(ms) / 60_000);
  const text = minutes < 60 ? `${minutes}m` : `${Math.round(minutes / 60)}h`;
  return { text: overdue ? `${text} overdue` : `in ${text}`, overdue };
}

function CaseRow({ row, selected, onSelect }: { row: Case; selected: boolean; onSelect: () => void }) {
  const due = dueIn(row.target_response_by);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      className={`w-full rounded-[var(--r-md)] border p-3 text-left transition ${
        selected
          ? "border-[var(--info)] bg-[var(--raised)]"
          : "border-[var(--line)] bg-[var(--surface)] hover:bg-[var(--raised)]"
      }`}
    >
      <div className="flex items-center gap-2">
        <Chip tone={priorityTone(row.priority)}>{row.priority}</Chip>
        <span className="font-mono text-[12px] text-[var(--ink)]">{row.reason_code}</span>
        <span
          className={`ml-auto text-[11px] ${due.overdue ? "text-[var(--danger)]" : "text-[var(--muted)]"}`}
        >
          {due.text}
        </span>
      </div>
      <p className="mt-2 text-[12px] text-[var(--muted)]">
        {row.reason_family} · opened by {row.opened_by} · {row.detections} detection
        {row.detections === 1 ? "" : "s"}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <Id value={row.checkout_id} />
        {row.monetary_exposure ? (
          <span className="tnum text-[12px] font-semibold text-[var(--ink)]">
            {formatMoney(row.monetary_exposure)} at stake
          </span>
        ) : (
          <span className="text-[11px] text-[var(--muted)]">no monetary exposure recorded</span>
        )}
      </div>
    </button>
  );
}

function CasePanel({ caseKey }: { caseKey: string }) {
  const { data, error, loading, reload } = useRead<CaseDetail>(
    (signal) => api.reviewCase(caseKey, signal),
    [caseKey],
  );

  if (loading) return <Loading label="Reading the case" />;
  if (error) return <ProblemPanel error={error} what="this case" onRetry={reload} />;
  if (!data) return null;

  const { case: row, verified_provider_state: verified, refused_evidence: refused } = data;

  return (
    <div className="space-y-4">
      <Panel
        title={
          <span className="flex flex-wrap items-center gap-2">
            <Chip tone={priorityTone(row.priority)}>{row.priority}</Chip>
            <Chip tone="muted">{row.state}</Chip>
            <span className="font-mono text-[13px]">{row.reason_code}</span>
          </span>
        }
        subtitle="Blocked on a person. Nothing on this screen resolves it, assigns it or records a decision, because in P0 nothing in the product does. A reviewer acts outside this surface; this page exists to hand them the evidence."
      >
        <Field label="Reason family">{row.reason_family}</Field>
        <Field label="Opened">
          <When value={row.opened_at} /> by {row.opened_by}
        </Field>
        <Field label="Response target">
          <When value={row.target_response_by} />{" "}
          <span className="text-[var(--muted)]">({row.target_response_seconds}s budget)</span>
        </Field>
        <Field label="Attempts">
          {row.attempts_used ?? "—"} used, {row.attempts_bound} bound
        </Field>
      </Panel>

      <Panel
        title="What the provider said, at the moment of escalation"
        subtitle="Verified when the case opened and never re-read here. A queue exists to hold a moment still; a value that drifted between the escalation and the review is the one thing a reviewer must not be shown."
      >
        {verified.present ? (
          <>
            <Field label="Status" source="provider">
              {verified.status ?? "—"}
              {verified.provider_status ? (
                <span className="ml-2 text-[var(--muted)]">provider says {verified.provider_status}</span>
              ) : null}
            </Field>
            <Field label="Amount" source="provider">
              <span className="tnum">{formatMinorOrDash(verified.amount_minor)}</span>
            </Field>
            <Field label="Payment reference" source="provider">
              {verified.provider_payment_id ? <Id value={verified.provider_payment_id} /> : "—"}
            </Field>
            <Field label="Order reference" source="provider">
              {verified.provider_order_id ? <Id value={verified.provider_order_id} /> : "—"}
            </Field>
            <Field label="Learned from">{verified.source ?? "—"}</Field>
          </>
        ) : (
          <p className="text-[13px] leading-relaxed text-[var(--warn)]">
            The provider has told this platform nothing about this payment. That is the state
            under review rather than a failed read — it is why the case exists.
          </p>
        )}
      </Panel>

      <Panel
        title="Provenance"
        subtitle="A case that names its own audit event is a case whose existence can be checked against the hash chain, rather than a row somebody typed."
      >
        <Field label="Audit event">
          <Id value={row.audit_event_id} />
        </Field>
        <Field label="Stream position">
          {row.audit_aggregate_type} · sequence {row.audit_seq}
        </Field>
        <Field label="Self hash">
          <Id value={row.audit_self_hash} />
        </Field>
        <Field label="Correlation">
          <Id value={row.correlation_id} />
        </Field>
        <Field label="Proof chain">
          {row.proof_chain.payment_attempt_id ? (
            <Link
              className="text-[var(--info)] underline"
              href={`/inspector?attempt=${encodeURIComponent(row.proof_chain.payment_attempt_id)}`}
            >
              open this payment attempt in the inspector
            </Link>
          ) : (
            <span className="text-[var(--muted)]">no payment attempt is attached to this case</span>
          )}
        </Field>
      </Panel>

      {refused ? (
        <Panel
          title="Evidence the platform refused"
          subtitle="What arrived and was not applied. A refusal recorded is a refusal a reviewer can check, which is the difference between the platform declining something and the platform losing it."
        >
          <pre className="overflow-x-auto rounded-[var(--r-sm)] bg-[var(--raised)] p-3 font-mono text-[11px] leading-relaxed text-[var(--ink)]">
            {JSON.stringify(refused, null, 2)}
          </pre>
        </Panel>
      ) : null}
    </div>
  );
}

export default function ReviewPage() {
  const [selected, setSelected] = useState<string | null>(null);
  const { data, error, loading, reload } = useRead((signal) => api.queue({ limit: 50, signal }), []);

  return (
    <main className="mx-auto max-w-[1280px] px-6 py-8">
      <header className="mb-6">
        <h1 className="text-[20px] font-semibold text-[var(--ink)]">Human review</h1>
        <p className="mt-1 max-w-3xl text-[13px] leading-relaxed text-[var(--muted)]">
          Cases the platform escalated because a person has to decide. Each carries the reason
          it is blocked, the provider state verified when it opened, and its position in the
          audit chain. Nothing here resolves a case: a reviewer acts elsewhere, and this
          surface exists to hand them what they need.
        </p>
      </header>

      {loading ? <Loading label="Reading the review queue" /> : null}
      {error ? <ProblemPanel error={error} what="the review queue" onRetry={reload} /> : null}

      {data ? (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            {PRIORITIES.map((priority) => (
              <Chip key={priority} tone={priorityTone(priority)}>
                {priority} · {data.priority_counts[priority] ?? 0}
              </Chip>
            ))}
            <span className="ml-auto text-[11px] text-[var(--muted)]">
              scope {data.scope} · showing up to {data.limit}
            </span>
          </div>

          {data.cases.length === 0 ? (
            <Empty>
              Nothing has been escalated for a person to decide. Reconciliation opens a case
              only when it cannot settle a difference on its own, so an empty queue is the
              platform reporting that it settled everything it saw.
            </Empty>
          ) : (
            <div className="grid gap-6 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
              <div className="space-y-2">
                {data.cases.map((row) => (
                  <CaseRow
                    key={row.case_key}
                    row={row}
                    selected={selected === row.case_key}
                    onSelect={() => setSelected(row.case_key)}
                  />
                ))}
              </div>
              <div>
                {selected ? <CasePanel caseKey={selected} /> : <Empty>Select a case to see its evidence.</Empty>}
              </div>
            </div>
          )}
        </>
      ) : null}
    </main>
  );
}

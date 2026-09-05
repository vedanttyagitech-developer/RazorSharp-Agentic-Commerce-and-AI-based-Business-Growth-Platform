"use client";

/**
 * Retained revenue, presented as the arithmetic it is.
 *
 * The claim this page makes is narrow and checkable: version N was approved at one total,
 * the merchant changed something, version N+1 carries a different total, and what the
 * buyer actually paid is whichever of those the kernel admitted. Each of those four
 * figures is a committed row, and each is labelled with the row it came from, because a
 * headline number with no provenance is a marketing claim rather than evidence.
 *
 * Then the page verifies its own evidence. The audit streams behind that checkout are
 * walked by `tk.verify_chain` server-side -- every hash recomputed from the stored
 * columns, never trusted as stored -- and the verdict is rendered link by link. An
 * evidence page that cannot verify its own evidence is decoration, so a broken chain is
 * shown as loudly as an intact one.
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { api, ApiError } from "@/lib/api/client";
import { deltaMinor, formatDelta, formatMinorOrDash } from "@/lib/money";
import { useRead, type ReadState } from "@/lib/useRead";
import type { AuditVerification, RetainedRevenue } from "@/lib/api/types";
import {
  Chip,
  Field,
  Id,
  Loading,
  Panel,
  ProblemPanel,
  When,
  cx,
} from "@/components/ui";

export default function EvidencePage() {
  return (
    <Suspense fallback={<Loading label="Opening the evidence page" />}>
      <Evidence />
    </Suspense>
  );
}

function Evidence() {
  const params = useSearchParams();
  const requested = params.get("checkout_id");

  const session = useRead((signal) => api.session(signal), []);
  const merchantId = session.data?.merchant_id ?? null;

  const retained = useRead(
    (signal) =>
      merchantId
        ? api.retainedRevenue(merchantId, { checkoutId: requested ?? undefined, signal })
        : Promise.reject(new ApiError({ type: "about:blank", title: "No operator session yet", status: 0 })),
    [merchantId, requested],
  );

  const checkoutId = retained.data?.checkout_id ?? null;

  // The proof chain is read only to learn which payment attempt this checkout produced,
  // so the attempt's own audit stream can be verified beside the checkout's and every
  // figure can link onward to the inspector document for it.
  const proof = useRead(
    (signal) =>
      checkoutId
        ? api.proof(checkoutId, signal)
        : Promise.reject(new ApiError({ type: "about:blank", title: "No checkout yet", status: 0 })),
    [checkoutId],
  );

  const checkoutChain = useRead(
    (signal) =>
      checkoutId
        ? api.verifyStream("checkout", checkoutId, signal)
        : Promise.reject(new ApiError({ type: "about:blank", title: "No checkout yet", status: 0 })),
    [checkoutId],
  );

  // The timeline is the narrative behind the four figures above: which actor did what, in
  // order, with the scenario injection labelled rather than filtered out. Hiding the demo
  // apparatus would make an injected price change indistinguishable from an inventory
  // failure, which is the one thing the specification forbids.
  const timeline = useRead(
    (signal) =>
      checkoutId
        ? api.timeline(checkoutId, signal)
        : Promise.reject(new ApiError({ type: "about:blank", title: "No checkout yet", status: 0 })),
    [checkoutId],
  );

  const attemptId = proof.data?.payment_attempt_id ?? null;
  const attemptChain = useRead(
    (signal) =>
      attemptId
        ? api.verifyStream("payment_attempt", attemptId, signal)
        : Promise.reject(new ApiError({ type: "about:blank", title: "No attempt yet", status: 0 })),
    [attemptId],
  );

  const onward = attemptId
    ? `/inspector?attempt=${encodeURIComponent(attemptId)}`
    : checkoutId
      ? `/evidence?checkout_id=${encodeURIComponent(checkoutId)}`
      : "/evidence";

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">Evidence</h1>
        <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
          What the platform retained by refusing a stale approval, and the verification of the audit
          chains that carry it.
        </p>
      </header>

      {session.error != null && <ProblemPanel error={session.error} what="the operator session" onRetry={session.reload} />}
      {(session.loading || retained.loading) && !retained.error && (
        <Loading label="Reading the retained-revenue evidence" />
      )}
      {retained.error != null && merchantId && <RetainedFailure error={retained.error} onRetry={retained.reload} />}

      {retained.data && (
        <>
          <Arithmetic evidence={retained.data} onward={onward} />

          <Panel
            title="Provenance"
            subtitle="where each figure above was read from"
          >
            <dl>
              <Field label="Checkout" source="retained_revenue.checkout_id">
                <Id value={retained.data.checkout_id} href={onward} />
              </Field>
              <Field label="Merchant" source="retained_revenue.merchant_id">
                <Id value={retained.data.merchant_id} />
              </Field>
              <Field label="Version N invalidated at" source="checkout_versions.invalidated_at">
                <When value={retained.data.stale_invalidated_at} />
              </Field>
              <Field label="Capture evidence" source="orders.capture_evidence.source">
                {retained.data.captured_from ? (
                  <Chip tone="positive">{retained.data.captured_from}</Chip>
                ) : (
                  <span className="text-[var(--muted)]">
                    none yet — no order row, so no captured amount is stated
                  </span>
                )}
              </Field>
              <Field label="Direction" source="retained_revenue.direction">
                <Chip tone={retained.data.direction === "UNSETTLED" ? "warn" : "positive"}>
                  {retained.data.direction}
                </Chip>
              </Field>
              <Field label="Refunded since capture" source="SUM(refunds.amount_minor) settled">
                <span className="num">
                  {formatMinorOrDash(retained.data.refunded_minor, retained.data.currency)}
                </span>
              </Field>
              <Field label="Controlled scenario" source="scenario_runs">
                {retained.data.controlled_scenario ? (
                  <span className="flex flex-wrap items-center gap-2">
                    <Chip tone="warn">INJECTED</Chip>
                    <span className="text-[var(--muted)]">
                      the price change was a scenario injection, labelled rather than hidden
                    </span>
                  </span>
                ) : (
                  <Chip tone="muted">organic</Chip>
                )}
              </Field>
              <Field label="The API&rsquo;s own account" source="retained_revenue.explanation">
                {retained.data.explanation}
              </Field>
            </dl>
          </Panel>
        </>
      )}

      <Panel
        title="Chain verification"
        subtitle="GET /v1/audit/streams/{aggregate_type}/{aggregate_id}/verify — hashes recomputed, never trusted"
      >
        <div className="grid gap-3 p-4 lg:grid-cols-2">
          <ChainCard
            heading="checkout"
            id={checkoutId}
            state={checkoutChain}
            waiting={!checkoutId}
            waitingNote="No checkout to verify until the retained-revenue read succeeds."
          />
          <ChainCard
            heading="payment_attempt"
            id={attemptId}
            state={attemptChain}
            waiting={!attemptId}
            waitingNote={
              proof.error
                ? "The proof chain could not be read, so this attempt's stream cannot be named."
                : "No payment attempt on this checkout yet."
            }
          />
        </div>
        {proof.data && (
          <div className="border-t border-[var(--line)] px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="eyebrow">Proof chain verdict</span>
              <Chip tone={proof.data.verdict.ok ? "positive" : "danger"}>
                {proof.data.verdict.ok ? "HOLDS" : "BROKEN"}
              </Chip>
              <Chip tone="muted">tier {proof.data.verdict.tier}</Chip>
              <span className="text-[11.5px] text-[var(--faint)]">
                GET /v1/checkouts/{"{"}checkout_id{"}"}/proof
              </span>
            </div>
            <ul className="mt-2 space-y-1">
              {proof.data.verdict.checks.map((check) => (
                <li key={check.name} className="flex flex-wrap items-baseline gap-2">
                  <Chip
                    tone={!check.applicable ? "muted" : check.ok ? "positive" : "danger"}
                  >
                    {!check.applicable ? "n/a" : check.ok ? "ok" : "failed"}
                  </Chip>
                  <span className="mono text-[var(--ink)]">{check.name}</span>
                  <span className="text-[11.5px] text-[var(--muted)] break-id">{check.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Panel>

      <Panel
        title="Action timeline"
        subtitle="GET /v1/checkouts/{checkout_id}/timeline — merged, oldest first, injections labelled"
        actions={
          timeline.data && (
            <span className="mono text-[var(--faint)]">
              {timeline.data.scenario_injections} injection
              {timeline.data.scenario_injections === 1 ? "" : "s"}
            </span>
          )
        }
      >
        {!checkoutId && (
          <p className="px-4 py-6 text-center text-[12px] text-[var(--muted)]">
            No checkout to trace until the retained-revenue read succeeds.
          </p>
        )}
        {checkoutId && timeline.loading && <Loading label="Reading the timeline" />}
        {checkoutId && timeline.error != null && (
          <div className="p-4">
            <ProblemPanel error={timeline.error} what="the action timeline" onRetry={timeline.reload} />
          </div>
        )}
        {timeline.data && (
          <ol className="divide-y divide-[var(--line-soft)]">
            {timeline.data.entries.map((entry) => (
              <li
                key={entry.id}
                className={cx(
                  "grid gap-x-4 gap-y-1 px-4 py-2.5 sm:grid-cols-[150px_1fr]",
                  entry.scenario_injection && "bg-[color-mix(in_srgb,var(--warn)_7%,transparent)]",
                )}
              >
                <div>
                  <When value={entry.occurred_at} />
                  <div className="mono mt-0.5 text-[var(--faint)]">{entry.source}</div>
                </div>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Chip tone="muted">{entry.actor}</Chip>
                    <span className="mono text-[var(--ink)]">{entry.action}</span>
                    {entry.checkout_version !== null && (
                      <Chip tone="info">v{entry.checkout_version}</Chip>
                    )}
                    {entry.scenario_injection && <Chip tone="warn">INJECTED</Chip>}
                  </div>
                  <p className="mt-0.5 text-[12px] text-[var(--muted)] break-id">{entry.summary}</p>
                  {entry.content_hash && (
                    <div className="mono mt-0.5 text-[var(--faint)] break-id">
                      hash {entry.content_hash}
                      {entry.policy_receipt_hash && ` · receipt ${entry.policy_receipt_hash}`}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </div>
  );
}

/**
 * The four figures and the one subtraction, laid out as a sum rather than as a dashboard.
 *
 * `difference_minor` and `net_retained_minor` come from the API. The only arithmetic this
 * component performs is the approved-to-corrected movement, which the API does not send as
 * a field and which is a subtraction of two integers it did send. It is labelled by the
 * two fields it subtracts rather than as "the difference", because `difference_minor` is
 * that name already and is measured from the captured amount instead of the corrected
 * one -- two figures under one word would make the page's own arithmetic unfollowable.
 */
function Arithmetic({ evidence, onward }: { evidence: RetainedRevenue; onward: string }) {
  // Named for the two fields it subtracts, not for the word "difference", because the
  // API already sends a `difference_minor` and it is a different subtraction: captured
  // minus approved rather than corrected minus approved. On a checkout that was refused
  // and never paid, the server's difference is null while this one is ₹161.70, and a
  // browser figure wearing the server's name there would be the console asserting a
  // settlement fact out of two quotes.
  const requoteMovement =
    evidence.stale_approved_minor !== null && evidence.corrected_total_minor !== null
      ? deltaMinor(evidence.stale_approved_minor, evidence.corrected_total_minor)
      : null;

  const rows: Array<{
    label: string;
    source: string;
    value: string;
    tone?: string;
    note: string;
  }> = [
    {
      label: `Version ${evidence.stale_version ?? "N"} — approved total`,
      source: "checkout_versions + approvals, the version whose approval was invalidated",
      value: formatMinorOrDash(evidence.stale_approved_minor, evidence.currency),
      note: "the exact bytes and amount the buyer consented to",
    },
    {
      label: `Version ${evidence.corrected_version ?? "N+1"} — total after the merchant's change`,
      source: "checkout_versions, the version the kernel built when it refused",
      value: formatMinorOrDash(evidence.corrected_total_minor, evidence.currency),
      note: "re-quoted against merchant state as it stood at admission",
    },
    {
      label: "Captured — what the buyer actually paid",
      source: "orders.total_minor, written only from verified capture evidence",
      value: formatMinorOrDash(evidence.captured_minor, evidence.currency),
      tone: evidence.captured_minor === null ? "muted" : "positive",
      note: evidence.captured_from
        ? `capture evidence: ${evidence.captured_from}`
        : "no order row yet, so the platform states no captured amount",
    },
    {
      label: "Difference — retained by the refusal",
      source: "retained_revenue.difference_minor — captured minus approved, over committed rows",
      value: formatMinorOrDash(evidence.difference_minor, evidence.currency),
      tone: evidence.difference_minor === null ? "muted" : "positive",
      note: `direction ${evidence.direction}`,
    },
  ];

  return (
    <Panel
      title="The arithmetic"
      subtitle="four committed rows and the difference between them"
      actions={
        <Link href={onward} className="text-[12px] text-[var(--info)] hover:underline">
          Inspect this checkout →
        </Link>
      }
    >
      <ol className="divide-y divide-[var(--line-soft)]">
        {rows.map((row) => (
          <li key={row.label}>
            <Link
              href={onward}
              className="grid gap-1 px-4 py-3 transition-colors hover:bg-[var(--raised)] sm:grid-cols-[1fr_auto] sm:items-center sm:gap-4"
            >
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-[var(--ink)]">{row.label}</div>
                <div className="mono mt-0.5 text-[var(--faint)] break-id">{row.source}</div>
                <div className="mt-0.5 text-[11.5px] text-[var(--muted)]">{row.note}</div>
              </div>
              <div
                className={cx(
                  "num text-[22px] leading-none sm:text-right",
                  row.tone === "positive"
                    ? "text-[var(--positive)]"
                    : row.tone === "muted"
                      ? "text-[var(--muted)]"
                      : "text-[var(--ink)]",
                )}
              >
                {row.value}
              </div>
            </Link>
          </li>
        ))}
      </ol>
      <div className="grid gap-3 border-t border-[var(--line)] p-4 sm:grid-cols-2">
        <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3.5 py-3">
          <div className="eyebrow">Re-quote movement · subtracted in this browser</div>
          <div className="num mt-1.5 text-[19px] text-[var(--warn)]">
            {requoteMovement === null ? "—" : formatDelta(requoteMovement, evidence.currency)}
          </div>
          <p className="mt-1 text-[11px] text-[var(--faint)]">
            corrected_total_minor − stale_approved_minor. It is not the difference above:{" "}
            <span className="text-[var(--muted)]">difference_minor</span> is captured minus approved,
            so it answers what the buyer paid, and this answers what the re-quote moved.{" "}
            {evidence.difference_minor === null
              ? "Nothing has been captured on this checkout, so the server states no difference at all."
              : "The two agree only once the corrected version is the one that was captured."}
          </p>
        </div>
        <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3.5 py-3">
          <div className="eyebrow">Net retained, after refunds</div>
          <div
            className={cx(
              "num mt-1.5 text-[19px]",
              evidence.net_retained_minor === null ? "text-[var(--muted)]" : "text-[var(--positive)]",
            )}
          >
            {formatMinorOrDash(evidence.net_retained_minor, evidence.currency)}
          </div>
          <p className="mt-1 text-[11px] text-[var(--faint)]">
            retained_revenue.net_retained_minor — null while nothing has been captured, rather than
            zero.
          </p>
        </div>
      </div>
    </Panel>
  );
}

function ChainCard({
  heading,
  id,
  state,
  waiting,
  waitingNote,
}: {
  heading: string;
  id: string | null;
  state: ReadState<AuditVerification>;
  waiting: boolean;
  waitingNote: string;
}) {
  if (waiting) {
    return (
      <div className="rounded-[var(--r-md)] border border-dashed border-[var(--line)] bg-[var(--raised)] p-4">
        <span className="eyebrow">{heading}</span>
        <p className="mt-2 text-[12px] text-[var(--muted)]">{waitingNote}</p>
      </div>
    );
  }
  if (state.loading) {
    return (
      <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] p-4">
        <span className="eyebrow">{heading}</span>
        <div className="mt-2">
          <Loading label={`Verifying the ${heading} chain`} />
        </div>
      </div>
    );
  }
  if (state.error) {
    return <ProblemPanel error={state.error} what={`the ${heading} audit chain`} onRetry={state.reload} />;
  }
  if (!state.data) return null;

  const verification = state.data;
  return (
    <div
      className={cx(
        "rounded-[var(--r-md)] border bg-[var(--raised)] p-4",
        verification.intact
          ? "border-[color-mix(in_srgb,var(--positive)_40%,transparent)]"
          : "border-[color-mix(in_srgb,var(--danger)_55%,transparent)]",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow">{heading}</span>
        <Chip tone={verification.intact ? "positive" : "danger"}>
          {verification.intact ? "INTACT" : "BROKEN"}
        </Chip>
        <Chip tone="muted">{verification.code}</Chip>
      </div>
      {id && (
        <div className="mono mt-1.5 text-[var(--faint)] break-id">{id}</div>
      )}
      <dl className="mt-3 grid grid-cols-3 gap-2">
        <Stat label="chain length" value={String(verification.length)} />
        <Stat label="events verified" value={String(verification.events_verified)} />
        <Stat label="head seq" value={verification.head_seq === null ? "—" : String(verification.head_seq)} />
      </dl>
      <div className="mono mt-2 text-[var(--faint)] break-id">
        head hash {verification.head_hash ?? "—"}
      </div>
      {verification.first_break && (
        <pre className="mono mt-2 overflow-x-auto rounded-[var(--r-sm)] border border-[color-mix(in_srgb,var(--danger)_45%,transparent)] bg-[var(--surface)] p-2 text-[var(--danger)]">
          {JSON.stringify(verification.first_break, null, 2)}
        </pre>
      )}
      {verification.empty && (
        <p className="mt-2 text-[11.5px] text-[var(--warn)]">
          This stream is empty. An empty chain verifies trivially and proves nothing.
        </p>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="eyebrow">{label}</dt>
      <dd className="num mt-0.5 text-[15px] text-[var(--ink)]">{value}</dd>
    </div>
  );
}

function RetainedFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--surface)] p-4">
        <Chip tone="muted">NOTHING TO ACCOUNT FOR</Chip>
        <p className="mt-2 text-[13px] text-[var(--ink)]">{error.problem.detail ?? error.problem.title}</p>
        <p className="mt-1 text-[11.5px] text-[var(--faint)]">
          The endpoint answers 404 rather than a zero, because &ldquo;nothing was retained&rdquo; and
          &ldquo;nothing has happened yet&rdquo; are different claims and only one of them is true here.
        </p>
      </div>
    );
  }
  return <ProblemPanel error={error} what="the retained-revenue evidence" onRetry={onRetry} />;
}

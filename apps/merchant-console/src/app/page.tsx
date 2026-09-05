"use client";

/**
 * The operator's first screen.
 *
 * Five reads, each rendered independently: safe mode, the outbox, the order counts, the
 * refund counts, and the retained-revenue headline. Independently, because a console
 * whose whole screen goes blank when one endpoint is slow is a console nobody opens
 * during an incident -- and because "the outbox read failed" and "the outbox is empty"
 * must never look the same.
 *
 * Every tile links to the page that can act on it. A number an operator cannot follow is
 * a number they have to go and find somewhere else.
 */
import Link from "next/link";
import { api, ApiError } from "@/lib/api/client";
import { formatCount, formatMinorOrDash } from "@/lib/money";
import { useRead } from "@/lib/useRead";
import { Chip, Empty, Figure, Loading, Panel, ProblemPanel, toneForState } from "@/components/ui";

export default function OverviewPage() {
  const session = useRead((signal) => api.session(signal), []);
  const merchantId = session.data?.merchant_id ?? null;

  const safeMode = useRead((signal) => api.safeMode(signal), []);
  const outbox = useRead((signal) => api.outbox({ limit: 1, signal }), []);
  const orders = useRead((signal) => api.orders({ limit: 1, signal }), []);
  const refunds = useRead((signal) => api.refunds({ limit: 1, signal }), []);
  const retained = useRead(
    (signal) =>
      merchantId
        ? api.retainedRevenue(merchantId, { signal })
        : Promise.reject(new ApiError({ type: "about:blank", title: "No operator session yet", status: 0 })),
    [merchantId],
  );

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">Overview</h1>
        <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
          The state of the tenant right now, read from the API on this page load.
        </p>
      </header>

      {/* ------------------------------------------------------------------ safe mode */}
      <Panel
        title="Operating mode"
        subtitle="GET /v1/ops/safe-mode"
        actions={
          <Link href="/operations?tab=safe-mode" className="text-[12px] text-[var(--info)] hover:underline">
            Open the switch →
          </Link>
        }
      >
        {safeMode.loading && <Loading label="Reading the operating mode" />}
        {safeMode.error != null && (
          <div className="p-4">
            <ProblemPanel error={safeMode.error} what="the operating mode" onRetry={safeMode.reload} />
          </div>
        )}
        {safeMode.data && (
          <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
            <Figure
              label="Mode"
              value={safeMode.data.mode}
              tone={safeMode.data.safe_mode ? "warn" : "positive"}
              hint={`scope ${safeMode.data.scope}`}
              href="/operations?tab=safe-mode"
            />
            <Figure
              label="Delegated debit"
              value={safeMode.data.permitted.DELEGATED_DEBIT ? "permitted" : "blocked"}
              tone={safeMode.data.permitted.DELEGATED_DEBIT ? "positive" : "danger"}
              hint="asked of the kernel, not inferred"
            />
            <Figure
              label="Refund execute"
              value={safeMode.data.permitted.REFUND_EXECUTE ? "permitted" : "blocked"}
              tone={safeMode.data.permitted.REFUND_EXECUTE ? "positive" : "danger"}
              hint="a kill switch that stopped refunds would harm the buyer it protects"
            />
            <Figure
              label="Reconciliation"
              value={safeMode.data.permitted.RECONCILIATION ? "permitted" : "blocked"}
              tone={safeMode.data.permitted.RECONCILIATION ? "positive" : "danger"}
              hint="never stopped by safe mode"
            />
          </div>
        )}
      </Panel>

      {/* --------------------------------------------------------------------- outbox */}
      <Panel
        title="Durable outbox"
        subtitle="GET /v1/ops/outbox — counts across the whole tenant, not this page"
        actions={
          <Link href="/operations?tab=outbox" className="text-[12px] text-[var(--info)] hover:underline">
            Open the queue →
          </Link>
        }
      >
        {outbox.loading && <Loading label="Reading the outbox" />}
        {outbox.error != null && (
          <div className="p-4">
            <ProblemPanel error={outbox.error} what="the durable outbox" onRetry={outbox.reload} />
          </div>
        )}
        {outbox.data && (
          <CountRow counts={outbox.data.counts} href="/operations?tab=outbox" param="status" />
        )}
      </Panel>

      {/* --------------------------------------------------------------------- orders */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Orders by state"
          subtitle="GET /v1/orders — every state, zeros included"
          actions={
            <Link href="/operations?tab=orders" className="text-[12px] text-[var(--info)] hover:underline">
              Open →
            </Link>
          }
        >
          {orders.loading && <Loading label="Reading orders" />}
          {orders.error != null && (
            <div className="p-4">
              <ProblemPanel error={orders.error} what="the order counts" onRetry={orders.reload} />
            </div>
          )}
          {orders.data && (
            <>
              <CountRow counts={orders.data.counts} href="/operations?tab=orders" param="status" />
              <p className="border-t border-[var(--line-soft)] px-4 py-2 text-[11.5px] text-[var(--faint)]">
                scope <span className="mono text-[var(--muted)]">{orders.data.scope}</span> — the whole
                tenant, because this console holds the scenario key.
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="Refunds by state"
          subtitle="GET /v1/refunds — pending, unknown and failed are three different facts"
          actions={
            <Link href="/operations?tab=refunds" className="text-[12px] text-[var(--info)] hover:underline">
              Open →
            </Link>
          }
        >
          {refunds.loading && <Loading label="Reading refunds" />}
          {refunds.error != null && (
            <div className="p-4">
              <ProblemPanel error={refunds.error} what="the refund counts" onRetry={refunds.reload} />
            </div>
          )}
          {refunds.data && (
            <>
              <CountRow counts={refunds.data.counts} href="/operations?tab=refunds" param="state" />
              <p className="border-t border-[var(--line-soft)] px-4 py-2 text-[11.5px] text-[var(--faint)]">
                scope <span className="mono text-[var(--muted)]">{refunds.data.scope}</span>
              </p>
            </>
          )}
        </Panel>
      </div>

      {/* ---------------------------------------------------------- retained revenue */}
      <Panel
        title="Retained revenue"
        subtitle="GET /v1/merchants/{merchant_id}/evidence/retained-revenue"
        actions={
          <Link href="/evidence" className="text-[12px] text-[var(--info)] hover:underline">
            Open the arithmetic →
          </Link>
        }
      >
        {(session.loading || (retained.loading && merchantId)) && (
          <Loading label="Reading the retained-revenue evidence" />
        )}
        {session.error != null && (
          <div className="p-4">
            <ProblemPanel error={session.error} what="the operator session" onRetry={session.reload} />
          </div>
        )}
        {retained.error != null && merchantId && <RetainedFailure error={retained.error} onRetry={retained.reload} />}
        {retained.data && (
          <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
            <Figure
              label={`Version ${retained.data.stale_version ?? "—"} approved`}
              value={formatMinorOrDash(retained.data.stale_approved_minor, retained.data.currency)}
              hint="the total the buyer consented to, from the invalidated version"
              href={`/evidence?checkout_id=${encodeURIComponent(retained.data.checkout_id)}`}
            />
            <Figure
              label={`Version ${retained.data.corrected_version ?? "—"} total`}
              value={formatMinorOrDash(retained.data.corrected_total_minor, retained.data.currency)}
              hint="after the merchant's change"
              href={`/evidence?checkout_id=${encodeURIComponent(retained.data.checkout_id)}`}
            />
            <Figure
              label="Captured"
              value={formatMinorOrDash(retained.data.captured_minor, retained.data.currency)}
              tone={retained.data.captured_minor === null ? "muted" : "positive"}
              hint={retained.data.captured_from ?? "no verified capture evidence yet"}
              href={`/evidence?checkout_id=${encodeURIComponent(retained.data.checkout_id)}`}
            />
            <Figure
              label="Net retained"
              value={formatMinorOrDash(retained.data.net_retained_minor, retained.data.currency)}
              tone={retained.data.net_retained_minor === null ? "muted" : "positive"}
              hint={`direction ${retained.data.direction}`}
              href={`/evidence?checkout_id=${encodeURIComponent(retained.data.checkout_id)}`}
            />
            <p className="text-[12px] text-[var(--muted)] sm:col-span-2 lg:col-span-4">
              {retained.data.explanation}
            </p>
          </div>
        )}
      </Panel>
    </div>
  );
}

/**
 * A 404 here is not a failure: it is the API saying this merchant has no refused approval
 * and no confirmed order yet. Rendering that as a read error would teach an operator to
 * ignore red panels, so it is separated out and carries the API's own wording.
 */
function RetainedFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      <div className="p-4">
        <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] p-4">
          <Chip tone="muted">NOTHING TO ACCOUNT FOR</Chip>
          <p className="mt-2 text-[12.5px] text-[var(--ink)]">
            {error.problem.detail ?? error.problem.title}
          </p>
          <p className="mt-1 text-[11.5px] text-[var(--faint)]">
            Run the demonstration through a refused approval and this figure appears. It is not
            estimated in the meantime.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="p-4">
      <ProblemPanel error={error} what="the retained-revenue evidence" onRetry={onRetry} />
    </div>
  );
}

/** Counts across a whole scope, every key the API sent, zeros included and clickable. */
function CountRow({
  counts,
  href,
  param,
}: {
  counts: Record<string, number>;
  href: string;
  param: string;
}) {
  const entries = Object.entries(counts);
  if (entries.length === 0) return <Empty>The API returned no counts for this collection.</Empty>;
  return (
    <ul className="flex flex-wrap gap-2 p-4">
      {entries.map(([state, count]) => (
        <li key={state}>
          <Link
            href={`${href}&${param}=${encodeURIComponent(state)}`}
            className="flex min-w-[112px] flex-col gap-1 rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3 py-2 transition-colors hover:border-[var(--info)]"
          >
            <Chip tone={count === 0 ? "muted" : toneForState(state)}>{state}</Chip>
            <span className="num text-[17px] text-[var(--ink)]">{formatCount(count)}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

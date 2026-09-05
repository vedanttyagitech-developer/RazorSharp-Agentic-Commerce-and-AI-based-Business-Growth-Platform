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
 *
 * The outbox panel leads with what is stalled rather than with the status counts, because
 * the status counts cannot say it: PENDING covers both a command about to run and a
 * command parked a week out, and a summary that renders those identically tells a
 * merchant a refund is in hand when it is going nowhere.
 */
import Link from "next/link";
import { api, ApiError } from "@/lib/api/client";
import { formatCount, formatMinorOrDash } from "@/lib/money";
import { useRead } from "@/lib/useRead";
import { Chip, Empty, Figure, Id, Loading, Panel, ProblemPanel, When, type Tone, toneForState } from "@/components/ui";
import type { OutboxWaiting } from "@/lib/api/types";

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
              value={permittedWord(safeMode.data.permitted, "DELEGATED_DEBIT")}
              tone={permittedTone(safeMode.data.permitted, "DELEGATED_DEBIT")}
              hint="asked of the kernel, not inferred"
            />
            <Figure
              label="Refund execute"
              value={permittedWord(safeMode.data.permitted, "REFUND_EXECUTE")}
              tone={permittedTone(safeMode.data.permitted, "REFUND_EXECUTE")}
              hint="a kill switch that stopped refunds would harm the buyer it protects"
            />
            <Figure
              label="Reconciliation"
              value={permittedWord(safeMode.data.permitted, "RECONCILIATION")}
              tone={permittedTone(safeMode.data.permitted, "RECONCILIATION")}
              hint="never stopped by safe mode"
            />
          </div>
        )}
      </Panel>

      {/* --------------------------------------------------------------------- outbox */}
      <Panel
        title="Durable outbox"
        subtitle="GET /v1/ops/outbox — what is stalled, then the counts, both across the whole tenant rather than one page"
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
          <>
            <Waiting waiting={outbox.data.waiting} />
            <CountRow counts={outbox.data.counts} href="/operations?tab=outbox" param="status" />
          </>
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

/**
 * The commands that are still owed a delivery and are not about to get one.
 *
 * This sits above the status counts because it corrects them. `PENDING` covers a command
 * the worker will lease in the next second and a command whose `available_at` is a week
 * away, so a tenant with a refund parked past the weekend renders as a small tidy number
 * and a merchant reads it as "in hand". Money owed to a buyer that is going nowhere is
 * not a queue depth, and it does not belong buried in a timestamp column three clicks
 * away on another tab.
 *
 * Zero is rendered rather than hidden, for the same reason every other count here shows
 * its zeros: a merchant must be able to tell "checked, and nothing is stalled" from "not
 * checked". A warning that only ever appears when it is bad teaches nobody what its
 * absence means. Both numbers come from the same `GET /v1/ops/outbox` read as the counts
 * below, and if that read fails this whole panel is a problem document — there is no
 * fixture behind it and no zero is invented in its place.
 */
function Waiting({ waiting }: { waiting: OutboxWaiting }) {
  const stalled = waiting.parked + waiting.overdue;
  const href = "/operations?tab=outbox&status=PENDING";

  if (stalled === 0) {
    return (
      <p className="border-b border-[var(--line-soft)] px-4 py-2.5 text-[11.5px] text-[var(--faint)]">
        <span className="text-[var(--muted)]">Nothing parked and nothing overdue.</span> No queued
        command is scheduled beyond{" "}
        <span className="num text-[var(--muted)]">{describe(waiting.parked_beyond_seconds)}</span> out
        or is more than{" "}
        <span className="num text-[var(--muted)]">{describe(waiting.overdue_beyond_seconds)}</span>{" "}
        past due — counted across the tenant by the same read as the states below.
      </p>
    );
  }

  return (
    <div className="border-b border-[var(--line-soft)] bg-[var(--raised)] px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="danger">MONEY WORK IS STALLED</Chip>
        <span className="text-[12.5px] text-[var(--ink)]">
          <span className="num">{formatCount(stalled)}</span>{" "}
          {stalled === 1 ? "command is" : "commands are"} queued and not about to run.
        </span>
        {/* Filtered to the statuses these were counted from, so the queue opens on the
            rows this line is about rather than on all of them. Labelled differently from
            the panel's own unfiltered link, which sits a few pixels above it. */}
        <Link href={href} className="ml-auto text-[12px] text-[var(--info)] hover:underline">
          Find these in the queue →
        </Link>
      </div>

      <ul className="mt-2 space-y-1 text-[12px] text-[var(--muted)]">
        {/* Two lines, never one total, because the remedies are opposite: a parked
            command is a decision somebody made and an overdue one is a worker that is
            not running. A merchant told only "2 stalled" cannot tell which to chase. */}
        <li>
          <span className="num text-[var(--ink)]">{formatCount(waiting.parked)}</span> parked —
          scheduled further out than{" "}
          <span className="num">{describe(waiting.parked_beyond_seconds)}</span>, which is past every
          retry delay this platform can produce, so something set these deliberately.
        </li>
        <li>
          <span className="num text-[var(--ink)]">{formatCount(waiting.overdue)}</span> overdue — due
          more than <span className="num">{describe(waiting.overdue_beyond_seconds)}</span> ago and
          still unclaimed, which is a worker that is not keeping up.
        </li>
      </ul>

      {waiting.oldest && (
        <p className="mt-2 border-t border-[var(--line-soft)] pt-2 text-[11.5px] text-[var(--muted)]">
          Oldest: <span className="mono text-[var(--ink)]">{waiting.oldest.command_type}</span>{" "}
          <Id value={waiting.oldest.command_id} /> — queued{" "}
          <When value={waiting.oldest.created_at} />, not due until{" "}
          <When value={waiting.oldest.available_at} /> (
          <span className="num">{waiting.oldest.attempts}</span>{" "}
          {waiting.oldest.attempts === 1 ? "attempt" : "attempts"} so far).
        </p>
      )}

      <p className="mt-1.5 text-[11px] text-[var(--faint)]">
        Counted across the whole tenant by <span className="mono">GET /v1/ops/outbox</span>, the same
        read as the states below, and not narrowed by any filter set on the queue.
      </p>
    </div>
  );
}

/**
 * A threshold in words.
 *
 * The API sends seconds because seconds are what the retry policy is written in, and a
 * merchant reading "3600" has to do arithmetic to find out whether that is a long time.
 * Only the two shapes this actually receives are handled; anything else falls back to the
 * number it was given rather than being rounded into a sentence that misstates it.
 */
function describe(seconds: number): string {
  if (seconds >= 3600 && seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return hours === 1 ? "an hour" : `${hours} hours`;
  }
  if (seconds >= 60 && seconds % 60 === 0) {
    const minutes = seconds / 60;
    return minutes === 1 ? "a minute" : `${minutes} minutes`;
  }
  return `${seconds}s`;
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

// ------------------------------------------------------- the kernel's capability answers

/**
 * Three answers, not two.
 *
 * `permitted` is typed as `Record<string, boolean>` because the API declares an open map
 * of capability names, so reading a key the response never carried yields `undefined` and
 * a plain ternary collapses that into the same word as an explicit `false`. On a screen
 * whose whole job is to say what the kernel decided, painting a red "blocked" tile over a
 * question the kernel was never asked is the worst of the three outcomes: it is a
 * confident claim about a refusal that did not happen. Absence gets its own word and its
 * own neutral tone, so an operator can tell a shut capability from a silent one.
 */
function permittedWord(permitted: Record<string, boolean>, capability: string): string {
  const answer = permitted[capability];
  if (answer === undefined) return "not stated";
  return answer ? "permitted" : "blocked";
}

function permittedTone(permitted: Record<string, boolean>, capability: string): Tone {
  const answer = permitted[capability];
  if (answer === undefined) return "muted";
  return answer ? "positive" : "danger";
}

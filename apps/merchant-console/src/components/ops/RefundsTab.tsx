"use client";

/**
 * Refunds across the whole tenant, and the one screen in this console that must not be
 * skimmed.
 *
 * REFUND_PENDING, REFUND_UNKNOWN and REFUND_FAILED are three different facts about money
 * and they are rendered as three different things: different colour, different label, and
 * a plain sentence on every row saying what the state means and who owns it. An operator
 * who reads a pending refund as a failed one retries it, and a buyer refunded twice is how
 * that ends. Colour alone would not be enough even if it were reliable, so the sentence is
 * always there.
 *
 * `row_status` is shown beside `state` because they are different columns: `state` is the
 * wire state, `row_status` is `refunds.status` verbatim, and an operator reconciling
 * against the database needs the second one.
 */
import { useState } from "react";
import { api } from "@/lib/api/client";
import { formatCount, formatMinorOrDash, formatMoney } from "@/lib/money";
import { useRead } from "@/lib/useRead";
import {
  Chip,
  Empty,
  Flag,
  Id,
  Loading,
  NotWired,
  Panel,
  ProblemPanel,
  TableWrap,
  Td,
  Th,
  When,
  toneForState,
} from "@/components/ui";
import { FilterChip } from "./Filters";
import { Pager, useCursors } from "./Pager";

/** Every refund state the API counts, in the order an operator triages them. */
const REFUND_STATES = [
  "REFUND_PENDING",
  "REFUND_UNKNOWN",
  "REFUND_FAILED",
  "RECONCILING",
  "ESCALATED",
  "PARTIALLY_REFUNDED",
  "REFUNDED",
] as const;

/**
 * What each state means, in a sentence, from `commerce_api.routers.refunds`.
 *
 * Written out rather than inferred, because the distinction between "the provider has not
 * answered" and "the answer was lost" is the whole reason these are separate states.
 */
const MEANING: Readonly<Record<string, string>> = {
  REFUND_PENDING: "the provider was asked and has not answered — in flight, do not retry",
  REFUND_UNKNOWN: "the answer was lost; reconciliation owns this row, not an operator",
  REFUND_FAILED: "the provider said no — this refund did not happen",
  RECONCILING: "a reconciliation run is deciding this row now",
  ESCALATED: "reconciliation exhausted its attempts and handed this to a human",
  PARTIALLY_REFUNDED: "settled for less than the captured amount",
  REFUNDED: "settled in full against the capture",
};

export function RefundsTab({ initialState }: { initialState: string | null }) {
  const [state, setState] = useState<string>(initialState ?? "");
  const cursors = useCursors();
  const page = useRead(
    (signal) => api.refunds({ state: state || undefined, limit: 25, cursor: cursors.cursor, signal }),
    [state, cursors.cursor],
  );

  function choose(next: string) {
    setState(next);
    cursors.reset();
  }

  return (
    <Panel
      title="Refunds"
      subtitle="GET /v1/refunds?state=&limit=&cursor= — counts across the scope, every state included"
      actions={page.data && <span className="mono text-[var(--faint)]">scope {page.data.scope}</span>}
    >
      <div className="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-4 py-2.5">
        <FilterChip
          active={state === ""}
          onClick={() => choose("")}
          label="all"
          count={page.data ? Object.values(page.data.counts).reduce((a, b) => a + b, 0) : undefined}
        />
        {REFUND_STATES.map((value) => (
          <FilterChip
            key={value}
            active={state === value}
            onClick={() => choose(value)}
            label={value}
            count={page.data?.counts[value]}
            tone={toneForState(value)}
          />
        ))}
      </div>

      <div className="grid gap-2 border-b border-[var(--line)] px-4 py-3 sm:grid-cols-3">
        {(["REFUND_PENDING", "REFUND_UNKNOWN", "REFUND_FAILED"] as const).map((value) => (
          <div
            key={value}
            className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3 py-2"
          >
            <Chip tone={toneForState(value)}>{value}</Chip>
            <p className="mt-1.5 text-[11.5px] text-[var(--muted)]">{MEANING[value]}</p>
            <p className="num mt-1 text-[16px] text-[var(--ink)]">
              {page.data ? formatCount(page.data.counts[value] ?? 0) : "—"}
            </p>
          </div>
        ))}
      </div>

      {page.loading && <Loading label="Reading refunds" />}
      {page.error != null && (
        <div className="p-4">
          <ProblemPanel error={page.error} what="the refund list" onRetry={page.reload} />
        </div>
      )}
      {page.data && page.data.refunds.length === 0 && (
        <Empty>
          No refund matches this filter. The API answered with counts, so this is an empty result and
          not a failed read.
        </Empty>
      )}
      {page.data && page.data.refunds.length > 0 && (
        <TableWrap>
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                <Th>Refund</Th>
                <Th>State</Th>
                <Th align="right">Amount</Th>
                <Th align="right">Of captured</Th>
                <Th>Reason</Th>
                <Th>Origin</Th>
                <Th>Provider refund</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {page.data.refunds.map((refund) => (
                <tr key={refund.refund_id} className="hover:bg-[var(--raised)]">
                  <Td>
                    <Id value={refund.refund_id} />
                    <div className="mono mt-0.5 text-[var(--faint)]">
                      order {refund.order_id ? <Id value={refund.order_id} /> : "—"}
                    </div>
                    <div className="mono text-[var(--faint)]">
                      attempt{" "}
                      <Id
                        value={refund.payment_attempt_id}
                        href={`/inspector?attempt=${encodeURIComponent(refund.payment_attempt_id)}`}
                      />
                    </div>
                  </Td>
                  <Td>
                    <Chip tone={toneForState(refund.state)}>{refund.state}</Chip>
                    <div className="mt-1 max-w-[240px] text-[11px] text-[var(--muted)]">
                      {MEANING[refund.state] ?? "state not in this console's vocabulary"}
                    </div>
                    <div className="mono mt-1 text-[var(--faint)]">row_status {refund.row_status}</div>
                  </Td>
                  <Td align="right" className="num text-[var(--ink)]">
                    {formatMoney(refund.amount)}
                  </Td>
                  <Td align="right" className="num text-[var(--muted)]">
                    {formatMinorOrDash(refund.captured_minor, refund.currency)}
                    {refund.captured_minor === null && (
                      <div className="mono mt-0.5 text-[var(--warn)]">no order row</div>
                    )}
                  </Td>
                  <Td className="mono text-[var(--muted)] break-id">{refund.reason}</Td>
                  <Td>
                    <Flag
                      value={refund.automatic}
                      yes="automatic"
                      no="buyer-requested"
                      yesTone="warn"
                      noTone="muted"
                    />
                  </Td>
                  <Td className="mono text-[var(--muted)] break-id">
                    {refund.provider_refund_id ?? "—"}
                  </Td>
                  <Td>
                    <When value={refund.updated_at} />
                    <div className="mono mt-0.5 text-[var(--faint)]">
                      {formatCount(refund.age_seconds)}s old
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {page.data && (
        <Pager
          cursors={cursors}
          nextCursor={page.data.next_cursor}
          shown={page.data.refunds.length}
          noun="refunds"
        />
      )}

      <div className="border-t border-[var(--line)] p-4">
        <NotWired
          what="This console reads refunds. It cannot start one, and that is a decision rather than an omission: a refund is consent about money, and the capability to ask for one belongs to the buyer's session. The operator session this console holds carries catalogue.read and order.read, and POST /v1/orders/{order_id}/refunds requires refund.request."
          missing="refund.request on the OPERATOR capability registry (commerce_api.deps.CAPABILITIES_BY_ACTOR)"
        />
      </div>
    </Panel>
  );
}

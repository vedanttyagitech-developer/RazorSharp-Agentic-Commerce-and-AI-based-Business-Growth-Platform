"use client";

/**
 * Orders across the whole tenant, newest first, with the counts beside them.
 *
 * Two things this table is careful about. `refunded_minor` is the database's sum over
 * *settled* refunds only, so an order with a refund in flight shows a refund count above
 * zero and a refunded amount that has not moved -- which is correct, because money the
 * provider has not confirmed returning has not returned. And `capture_evidence.kind` is
 * rendered on every row rather than tucked into a detail view: it is never
 * BROWSER_CALLBACK, and a column that is always WEBHOOK or PROVIDER_FETCH is a standing
 * demonstration of the rule rather than a paragraph asserting it.
 */
import { useState } from "react";
import { api } from "@/lib/api/client";
import { formatCount, formatMinor, formatMoney } from "@/lib/money";
import { useRead } from "@/lib/useRead";
import {
  Chip,
  Empty,
  Id,
  Loading,
  Panel,
  ProblemPanel,
  TableWrap,
  Td,
  Th,
  When,
  cx,
  toneForState,
} from "@/components/ui";
import { FilterChip } from "./Filters";
import { Pager, useCursors } from "./Pager";

const ORDER_STATES = [
  "CONFIRMED",
  "FULFILMENT_BLOCKED",
  "CANCELLED",
  "PARTIALLY_REFUNDED",
  "REFUNDED",
] as const;

export function OrdersTab({ initialStatus }: { initialStatus: string | null }) {
  const [status, setStatus] = useState<string>(initialStatus ?? "");
  const cursors = useCursors();
  const page = useRead(
    (signal) => api.orders({ status: status || undefined, limit: 25, cursor: cursors.cursor, signal }),
    [status, cursors.cursor],
  );

  function choose(next: string) {
    setStatus(next);
    cursors.reset();
  }

  return (
    <Panel
      title="Orders"
      subtitle="GET /v1/orders?status=&limit=&cursor= — keyset paginated on (created_at, id)"
      actions={
        page.data && (
          <span className="mono text-[var(--faint)]">scope {page.data.scope}</span>
        )
      }
    >
      <div className="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-4 py-2.5">
        <FilterChip active={status === ""} onClick={() => choose("")} label="all" count={page.data ? Object.values(page.data.counts).reduce((a, b) => a + b, 0) : undefined} />
        {ORDER_STATES.map((state) => (
          <FilterChip
            key={state}
            active={status === state}
            onClick={() => choose(state)}
            label={state}
            count={page.data?.counts[state]}
            tone={toneForState(state)}
          />
        ))}
      </div>

      {page.loading && <Loading label="Reading orders" />}
      {page.error != null && (
        <div className="p-4">
          <ProblemPanel error={page.error} what="the order list" onRetry={page.reload} />
        </div>
      )}
      {page.data && page.data.orders.length === 0 && (
        <Empty>
          No order matches this filter. The API answered with counts, so this is an empty result and
          not a failed read.
        </Empty>
      )}
      {page.data && page.data.orders.length > 0 && (
        <TableWrap>
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                <Th>Order</Th>
                <Th>State</Th>
                <Th align="right">Amount</Th>
                <Th align="right">Refunded</Th>
                <Th>Capture evidence</Th>
                <Th>Provider ids</Th>
                <Th>Attempt</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody>
              {page.data.orders.map((order) => (
                <tr key={order.order_id} className="hover:bg-[var(--raised)]">
                  <Td>
                    <Id value={order.order_id} />
                    <div className="mono mt-0.5 text-[var(--faint)]">
                      checkout <Id value={order.checkout_id} href={`/evidence?checkout_id=${encodeURIComponent(order.checkout_id)}`} /> · v{order.version}
                    </div>
                  </Td>
                  <Td>
                    <Chip tone={toneForState(order.state)}>{order.state}</Chip>
                  </Td>
                  <Td align="right" className="num text-[var(--ink)]">
                    {formatMoney(order.amount)}
                  </Td>
                  <Td align="right">
                    <span
                      className={cx(
                        "num",
                        order.refunded_minor > 0 ? "text-[var(--warn)]" : "text-[var(--faint)]",
                      )}
                    >
                      {formatMinor(order.refunded_minor, order.currency)}
                    </span>
                    <div className="mono mt-0.5 text-[var(--faint)]">
                      {order.refund_count} refund{order.refund_count === 1 ? "" : "s"} (settled only)
                    </div>
                  </Td>
                  <Td>
                    {order.capture_evidence ? (
                      <>
                        <Chip tone="positive">{order.capture_evidence.kind}</Chip>
                        <div className="mono mt-0.5 text-[var(--faint)] break-id">
                          {order.capture_evidence.reference}
                        </div>
                      </>
                    ) : (
                      <Chip tone="warn">none recorded</Chip>
                    )}
                  </Td>
                  <Td>
                    <div className="mono text-[var(--muted)] break-id">
                      {order.razorpay_order_id ?? "—"}
                    </div>
                    <div className="mono text-[var(--faint)] break-id">
                      {order.razorpay_payment_id ?? "—"}
                    </div>
                  </Td>
                  <Td>
                    <Id
                      value={order.payment_attempt_id}
                      href={`/inspector?attempt=${encodeURIComponent(order.payment_attempt_id)}`}
                    />
                  </Td>
                  <Td>
                    <When value={order.created_at} />
                    <div className="mono mt-0.5 text-[var(--faint)]">
                      {formatCount(order.age_seconds)}s old
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
          shown={page.data.orders.length}
          noun="orders"
        />
      )}
    </Panel>
  );
}

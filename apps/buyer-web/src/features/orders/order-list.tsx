/**
 * Every confirmed sale in this session's scope, newest first.
 *
 * Two things this screen is careful about. The state filter is built from the `counts`
 * the server sends rather than from a list hard-coded here, because the server sends
 * every state it measures with a zero where there are none: a zero next to CANCELLED
 * means "no cancelled orders", while a state absent from that map means "this deployment
 * did not measure it", and a filter invented in the browser erases that difference.
 *
 * The other is paging. `next_cursor` is a keyset cursor over `(created_at, id)`, so a
 * page fetched with it never repeats or skips a row however many orders land in between.
 * Nothing here computes an offset, and nothing here totals an amount: `refunded_minor`
 * and `amount` arrive already summed by the database that owns them.
 */
"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Amount, Badge, Button, Card, EmptyState, ErrorState, Skeleton, cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { OrderSummary } from "@/lib/api/types";

import { CaptureEvidenceTag, MONO, formatDuration, formatTimestamp } from "./capture-evidence";

const PAGE_SIZE = 25;

/**
 * The lifecycle order a reader expects, used only to sort the chips.
 *
 * A state the server reports that is missing from this list is still rendered, at the
 * end. The server owns the vocabulary; this is a preference about sequence, not a filter.
 */
const STATE_ORDER = [
  "CONFIRMED",
  "FULFILMENT_BLOCKED",
  "PARTIALLY_REFUNDED",
  "REFUNDED",
  "CANCELLED",
] as const;

const STATE_TONE: Readonly<Record<string, "neutral" | "green" | "blue" | "amber" | "red">> = {
  CONFIRMED: "green",
  FULFILMENT_BLOCKED: "amber",
  PARTIALLY_REFUNDED: "blue",
  REFUNDED: "neutral",
  CANCELLED: "red",
};

/** A UUID a reader scans rather than reads. The full value stays in the row's title. */
function shortId(id: string): string {
  return id.length <= 13 ? id : `${id.slice(0, 8)}…${id.slice(-4)}`;
}

/**
 * `age_seconds` as the database clock measured it, in words.
 *
 * Integer division on a duration, which is the one kind of arithmetic this storefront
 * does: a duration is not money and no approval was ever hashed against it.
 */
function formatAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "just now";
  if (seconds < 60) return "moments ago";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function orderedStates(counts: Record<string, number>): string[] {
  const preferred: readonly string[] = STATE_ORDER;
  const known = preferred.filter((name) => name in counts);
  const extra = Object.keys(counts).filter((name) => !preferred.includes(name));
  return [...known, ...extra.sort()];
}

/* --------------------------------------------------------------------- a row */

/**
 * Exported for the identity test, on the same reasoning as `sentenceFor` in
 * `order-actions`: the property worth pinning is which identifier this screen calls the
 * order's name, and that is only checkable if a test can render it directly.
 */
export function OrderRow({ order }: { order: OrderSummary }) {
  const tone = STATE_TONE[order.state] ?? "neutral";
  return (
    <li>
      <Link
        href={`/orders/${encodeURIComponent(order.order_id)}`}
        className="block rounded-[var(--r-md)] focus-visible:outline-2"
        aria-label={`Order ${order.reference ?? order.order_id}, ${order.state.toLowerCase().replace(/_/g, " ")}, ${order.amount.display} ${order.amount.currency}`}
      >
        <Card className="px-4 py-3 transition hover:border-[var(--ink-6)] hover:brightness-[0.995]">
          <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
            <div className="min-w-0">
              {/*
                The reference in the row, the whole id on hover.

                A list wants one name per row, and `shortId` was not one: `01a0786d…7565`
                is a truncated UUID, which is neither sayable nor unique-looking, and it
                was read out in full by the aria-label above. The reference is already on
                this payload. The id stays in `title` because that is where a row keeps
                what it joins on.
              */}
              <p className={cx(MONO, "text-[13px] text-[var(--ink)]")} title={order.order_id}>
                {order.reference ?? shortId(order.order_id)}
              </p>
              <p className="mt-0.5 text-[12px] text-[var(--ink-4)]" title={order.created_at}>
                {formatAge(order.age_seconds)} · {formatTimestamp(order.created_at)}
                {/*
                  And how long it took to become an order. "Checkout", not "cart": the
                  span starts at the kernel's first freeze, and a cart is browsing.
                */}
                {formatDuration(order.duration_seconds)
                  ? ` · checkout to confirmed in ${formatDuration(order.duration_seconds)}`
                  : ""}
              </p>
            </div>
            <div className="text-right">
              <Amount money={order.amount} className="text-[16px] font-bold text-[var(--ink)]" />
              {order.refunded_minor > 0 ? (
                <p className="mt-0.5 text-[12px] text-[var(--blue)]">
                  <Amount minor={order.refunded_minor} currency={order.currency} /> settled back
                </p>
              ) : null}
            </div>
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Badge tone={tone}>{order.state.replace(/_/g, " ")}</Badge>
            <CaptureEvidenceTag evidence={order.capture_evidence} />
            <Badge tone={order.refund_count > 0 ? "blue" : "neutral"}>
              {order.refund_count === 0
                ? "No refunds"
                : `${order.refund_count} refund${order.refund_count === 1 ? "" : "s"}`}
            </Badge>
            <span className="text-[12px] text-[var(--ink-5)]">version {order.version}</span>
          </div>
        </Card>
      </Link>
    </li>
  );
}

/* ------------------------------------------------------------------- the list */

/**
 * One filter's rows, held with the exact inputs that produced them.
 *
 * Keeping the key alongside the rows is what stops a slow answer from landing under a
 * heading it does not describe: a result whose key no longer matches the filter being
 * asked about is simply not rendered, and the screen reads as loading again.
 */
interface LoadedPage {
  key: string;
  rows: OrderSummary[];
  counts: Record<string, number> | null;
  scope: string | null;
  cursor: string | null;
  problem: string | null;
}

export function OrderList() {
  const [state, setState] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState<LoadedPage | null>(null);
  const [pagingBusy, setPagingBusy] = useState(false);
  const [pagingProblem, setPagingProblem] = useState<string | null>(null);

  const key = `${attempt} ${state ?? ""}`;
  const current = loaded && loaded.key === key ? loaded : null;
  const rows = current?.rows ?? [];
  const counts = current?.counts ?? null;
  const scope = current?.scope ?? null;
  const cursor = current?.cursor ?? null;
  const problem = current?.problem ?? null;
  const phase: "loading" | "ready" | "error" =
    current === null ? "loading" : current.problem !== null ? "error" : "ready";

  const retry = useCallback(() => setAttempt((previous) => previous + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    api
      .orders({ status: state ?? undefined, limit: PAGE_SIZE, signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setLoaded({
          key,
          rows: page.orders,
          counts: page.counts,
          scope: page.scope,
          cursor: page.next_cursor,
          problem: null,
        });
        setPagingProblem(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          key,
          rows: [],
          counts: null,
          scope: null,
          cursor: null,
          problem: humanMessage(cause),
        });
      });
    return () => controller.abort();
  }, [key, state]);

  /**
   * The next keyset page, appended. A failure here keeps the rows already on screen and
   * says so inline; only a failed first page has nothing honest to show. The append is
   * dropped outright if the filter moved while the page was in flight, which is the same
   * rule the key enforces above.
   */
  async function loadMore(): Promise<void> {
    if (!current || current.cursor === null) return;
    const from = current;
    setPagingBusy(true);
    setPagingProblem(null);
    try {
      const page = await api.orders({
        status: state ?? undefined,
        limit: PAGE_SIZE,
        cursor: from.cursor ?? undefined,
      });
      setLoaded((previous) =>
        previous && previous.key === from.key
          ? {
              ...previous,
              rows: [...previous.rows, ...page.orders],
              counts: page.counts,
              scope: page.scope,
              cursor: page.next_cursor,
            }
          : previous,
      );
    } catch (cause) {
      setPagingProblem(humanMessage(cause));
    } finally {
      setPagingBusy(false);
    }
  }

  const stateNames = counts ? orderedStates(counts) : [];
  // A count of rows, not an amount. `counts` is exhaustive over the states this scope
  // measures, so its sum is exactly the number of orders the unfiltered view holds.
  const total = counts ? Object.values(counts).reduce((sum, n) => sum + n, 0) : null;

  return (
    <section className="column py-6" aria-labelledby="orders-heading">
      <header className="mb-4">
        <h1 id="orders-heading" className="text-[20px] font-bold text-[var(--ink)]">
          Orders
        </h1>
        <p className="mt-1 text-[13px] text-[var(--ink-4)]">
          {scope === "tenant"
            ? "Every order in this merchant's tenant. Each one exists because verified provider evidence put it there."
            : "Your confirmed orders. Each one exists because verified provider evidence put it there."}
        </p>
      </header>

      {counts ? (
        <div
          role="group"
          aria-label="Filter orders by state"
          className="mb-4 flex flex-wrap items-center gap-2"
        >
          <FilterChip
            label="All"
            count={total}
            active={state === null}
            onSelect={() => setState(null)}
          />
          {stateNames.map((name) => (
            <FilterChip
              key={name}
              label={name.replace(/_/g, " ").toLowerCase()}
              count={counts[name]}
              active={state === name}
              onSelect={() => setState(name)}
            />
          ))}
        </div>
      ) : null}

      <p aria-live="polite" className="sr-only">
        {phase === "loading"
          ? "Loading orders"
          : phase === "error"
            ? `Could not load orders. ${problem ?? ""}`
            : `${rows.length} order${rows.length === 1 ? "" : "s"} shown`}
      </p>

      {phase === "loading" ? (
        <ul className="grid gap-2">
          {Array.from({ length: 5 }, (_, index) => (
            <li key={index}>
              <Skeleton className="h-[92px] w-full" />
            </li>
          ))}
        </ul>
      ) : phase === "error" ? (
        <ErrorState
          title="Could not load your orders"
          detail={problem ?? undefined}
          onRetry={retry}
        />
      ) : rows.length === 0 ? (
        <EmptyState
          title={state === null ? "No orders yet" : `No orders in ${state.replace(/_/g, " ").toLowerCase()}`}
          detail={
            state === null
              ? "An order appears here only after the platform verifies the capture with the provider."
              : "Nothing is in this state right now. Clear the filter to see the rest."
          }
          action={
            state === null ? (
              <Link
                href="/"
                className="inline-flex h-10 items-center rounded-[var(--r-md)] bg-[var(--green)] px-4 text-[14px] font-semibold text-white transition hover:brightness-95"
              >
                Browse the store
              </Link>
            ) : (
              <Button variant="ghost" size="sm" onClick={() => setState(null)}>
                Show all orders
              </Button>
            )
          }
        />
      ) : (
        <>
          <ul className="grid gap-2">
            {rows.map((order) => (
              <OrderRow key={order.order_id} order={order} />
            ))}
          </ul>

          {pagingProblem ? (
            <p role="alert" className="mt-3 text-[13px] text-[var(--red)]">
              {pagingProblem}
            </p>
          ) : null}

          <div className="mt-4 flex justify-center">
            {cursor ? (
              <Button
                variant="ghost"
                busy={pagingBusy}
                onClick={() => void loadMore()}
              >
                Load more
              </Button>
            ) : (
              <p className="text-[12px] text-[var(--ink-5)]">
                That is every order in this view.
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function FilterChip({
  label,
  count,
  active,
  onSelect,
}: {
  label: string;
  count: number | null;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[13px] font-semibold capitalize transition",
        active
          ? "border-[var(--green)] bg-[var(--green-add-bg)] text-[var(--green)]"
          : "border-[var(--card-line)] bg-white text-[var(--ink-3)] hover:bg-[var(--tint-2)]",
      )}
    >
      {label}
      <span className={cx("tnum text-[12px]", active ? "text-[var(--green)]" : "text-[var(--ink-5)]")}>
        {count ?? "—"}
      </span>
    </button>
  );
}

/**
 * One confirmed sale, and the evidence that it is the sale the buyer agreed to.
 *
 * The order carries its own `content_hash` and `policy_receipt_hash`, copied onto the row
 * at capture, so "what were the terms of this sale" is answerable from this screen alone
 * without trusting that the checkout version still says what it said. Both are rendered
 * in full, in monospace, because a hash truncated for tidiness is a hash nobody can check.
 *
 * The refund states are the other reason this screen is careful. REFUND_PENDING and
 * REFUND_UNKNOWN look alike in a database and mean opposite things to an operator: a
 * pending refund is one the provider accepted and has not finished, while an unknown one
 * is a refund whose existence the platform cannot yet assert. Collapsing the two is how a
 * buyer gets refunded twice, so each state is rendered as a visibly different thing, with
 * a sentence saying what it licenses and a glyph so the difference survives without colour.
 */
"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { Amount, Badge, Button, Card, ErrorState, Skeleton, cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { Order, Quote, Refund } from "@/lib/api/types";

import { CaptureEvidencePanel, MONO, SectionCard, formatTimestamp } from "./capture-evidence";
import { DeliveryProgress } from "./delivery-progress";
import { OrderActions } from "./order-actions";

/* ------------------------------------------------------------------- vocabulary */

const ORDER_STATES: Readonly<
  Record<string, { tone: "neutral" | "green" | "blue" | "amber" | "red"; meaning: string }>
> = {
  CONFIRMED: {
    tone: "green",
    meaning: "Captured against verified provider evidence and bound to the approved version.",
  },
  FULFILMENT_BLOCKED: {
    tone: "amber",
    meaning: "The money is captured, but fulfilment is held pending an operator decision.",
  },
  CANCELLED: {
    tone: "red",
    meaning: "This sale was withdrawn. Any money that moved is accounted for in the refunds below.",
  },
  PARTIALLY_REFUNDED: {
    tone: "blue",
    meaning: "Part of the captured amount has settled back to the buyer.",
  },
  REFUNDED: {
    tone: "neutral",
    meaning: "The whole captured amount has settled back to the buyer.",
  },
};

/**
 * The refund vocabulary, spelled out. `licence` is the operational consequence: what the
 * platform may and may not do next while a refund sits in this state.
 */
const REFUND_STATES: Readonly<
  Record<
    string,
    {
      label: string;
      tone: "neutral" | "green" | "blue" | "amber" | "red";
      accent: string;
      glyph: "clock" | "question" | "cross" | "sync" | "part" | "tick";
      licence: string;
    }
  >
> = {
  REFUND_PENDING: {
    label: "Pending at the provider",
    tone: "amber",
    accent: "var(--amber)",
    glyph: "clock",
    licence:
      "The provider accepted this refund and has not finished it. The money has not returned yet, and nothing further is sent.",
  },
  REFUND_UNKNOWN: {
    label: "Outcome unknown",
    tone: "red",
    accent: "var(--red)",
    glyph: "question",
    licence:
      "The provider did not answer in time, so the platform cannot say whether this refund exists. It reconciles against the provider's own record and never re-sends, because a blind retry is how a buyer is refunded twice.",
  },
  REFUND_FAILED: {
    label: "Rejected by the provider",
    tone: "red",
    accent: "var(--red)",
    glyph: "cross",
    licence:
      "The provider refused this refund and no money moved. A fresh refund may be admitted under a new grant.",
  },
  RECONCILING: {
    label: "Reconciling",
    tone: "blue",
    accent: "var(--blue)",
    glyph: "sync",
    licence:
      "The platform is matching this refund against the provider's record before it does anything else with it.",
  },
  ESCALATED: {
    label: "Escalated to an operator",
    tone: "amber",
    accent: "var(--amber)",
    glyph: "question",
    licence:
      "Automatic reconciliation gave up. A human decides what happens next; nothing is retried in the meantime.",
  },
  PARTIALLY_REFUNDED: {
    label: "Settled, part of the capture",
    tone: "blue",
    accent: "var(--blue)",
    glyph: "part",
    licence:
      "This refund has settled at the provider for less than the amount captured. The rest of the capture is still held.",
  },
  REFUNDED: {
    label: "Settled in full",
    tone: "green",
    accent: "var(--green)",
    glyph: "tick",
    licence: "The provider has confirmed the money returned to the buyer.",
  },
};

function RefundGlyph({ kind }: { kind: "clock" | "question" | "cross" | "sync" | "part" | "tick" }) {
  const common = { viewBox: "0 0 16 16", width: 14, height: 14, fill: "none", "aria-hidden": true } as const;
  if (kind === "clock") {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
        <path d="M8 4.8V8.3l2.2 1.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    );
  }
  if (kind === "question") {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
        <path
          d="M6.4 6.3c0-.9.7-1.6 1.6-1.6s1.6.7 1.6 1.6c0 1.1-1.6 1.2-1.6 2.4"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinecap="round"
        />
        <circle cx="8" cy="11.1" r="0.8" fill="currentColor" />
      </svg>
    );
  }
  if (kind === "cross") {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
        <path d="M5.9 5.9 10.1 10.1M10.1 5.9 5.9 10.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    );
  }
  if (kind === "sync") {
    return (
      <svg {...common}>
        <path
          d="M13 8a5 5 0 0 1-8.6 3.4M3 8a5 5 0 0 1 8.6-3.4"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
        />
        <path d="M3 4.4V7.6h3.2M13 11.6V8.4H9.8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (kind === "part") {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
        <path d="M8 2.2A5.8 5.8 0 0 1 8 13.8Z" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
      <path d="M5.5 8.2 7.3 10l3.3-3.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/* ------------------------------------------------------------------- fragments */

function Field({
  label,
  children,
  wide = false,
}: {
  label: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={cx("min-w-0", wide && "sm:col-span-2")}>
      <dt className="text-[12px] font-semibold text-[var(--ink-4)]">{label}</dt>
      <dd className="mt-0.5 min-w-0 text-[13px] text-[var(--ink)]">{children}</dd>
    </div>
  );
}

/**
 * A hash the reader may want to paste into a ledger. Copying is offered because the whole
 * point of showing the hash is that someone can compare it to another system's copy.
 */
function Hash({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState<"idle" | "done" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied("done");
    } catch {
      setCopied("failed");
    }
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied("idle"), 2000);
  }, [value]);

  return (
    <Field label={label} wide>
      <div className="flex items-start gap-2">
        <code className={cx(MONO, "min-w-0 flex-1 rounded-[var(--r-sm)] bg-[var(--tint-2)] px-2 py-1")}>
          {value}
        </code>
        <Button variant="ghost" size="sm" onClick={() => void copy()} aria-label={`Copy ${label}`}>
          {copied === "done" ? "Copied" : copied === "failed" ? "Copy failed" : "Copy"}
        </Button>
      </div>
      <span aria-live="polite" className="sr-only">
        {copied === "done" ? `${label} copied` : copied === "failed" ? `${label} could not be copied` : ""}
      </span>
    </Field>
  );
}

/* ----------------------------------------------------------------------- quote */

function QuoteTable({ quote }: { quote: Quote }) {
  return (
    <div>
      <div className="-mx-4 overflow-x-auto px-4">
        <table className="w-full min-w-[520px] border-collapse text-[13px]">
          <caption className="sr-only">Lines and taxes in the quote this order was priced from</caption>
          <thead>
            <tr className="border-b-[0.5px] border-[var(--card-line)] text-left text-[12px] text-[var(--ink-4)]">
              <th scope="col" className="py-2 pr-3 font-semibold">Item</th>
              <th scope="col" className="py-2 pr-3 text-right font-semibold">Qty</th>
              <th scope="col" className="py-2 pr-3 text-right font-semibold">Unit</th>
              <th scope="col" className="py-2 pr-3 text-right font-semibold">Tax</th>
              <th scope="col" className="py-2 text-right font-semibold">Subtotal</th>
            </tr>
          </thead>
          <tbody>
            {quote.lines.map((line) => (
              <tr key={line.sku} className="border-b-[0.5px] border-[var(--card-line)] align-top">
                <td className="py-2 pr-3">
                  <span className="font-semibold text-[var(--ink)]">{line.name}</span>
                  <span className={cx(MONO, "mt-0.5 block text-[var(--ink-5)]")}>{line.sku}</span>
                </td>
                <td className="tnum py-2 pr-3 text-right text-[var(--ink-2)]">{line.quantity}</td>
                <td className="py-2 pr-3 text-right text-[var(--ink-2)]">
                  <Amount minor={line.unit_price_minor} currency={quote.currency} />
                </td>
                <td className="py-2 pr-3 text-right text-[var(--ink-3)]">
                  <Amount minor={line.tax_minor} currency={quote.currency} />
                </td>
                <td className="py-2 text-right font-semibold text-[var(--ink)]">
                  <Amount minor={line.subtotal_minor} currency={quote.currency} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <dl className="mt-3 grid gap-1.5 text-[13px]">
        <Total label="Items" minor={quote.items_subtotal_minor} currency={quote.currency} />
        <Total label="Tax on items" minor={quote.items_tax_minor} currency={quote.currency} />
        <Total label="Delivery" minor={quote.delivery_fee_minor} currency={quote.currency} />
        <Total label="Tax on delivery" minor={quote.delivery_tax_minor} currency={quote.currency} />
        {quote.discount_minor > 0 ? (
          <Total
            label={quote.offer_label ?? "Offer"}
            minor={-quote.discount_minor}
            currency={quote.currency}
          />
        ) : null}
        <div className="mt-1 flex items-baseline justify-between border-t-[0.5px] border-[var(--card-line)] pt-2">
          <dt className="text-[14px] font-bold text-[var(--ink)]">Total</dt>
          <dd>
            <Amount money={quote.total} className="text-[16px] font-bold text-[var(--ink)]" />
          </dd>
        </div>
      </dl>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {quote.discount_minor > 0 ? <Badge tone="green">Offer applied</Badge> : null}
        {quote.free_delivery_applied ? (
          <Badge tone="green">Free delivery applied</Badge>
        ) : quote.gap_to_free_delivery_minor !== null ? (
          <Badge tone="neutral">
            <Amount minor={quote.gap_to_free_delivery_minor} currency={quote.currency} /> short of free
            delivery
          </Badge>
        ) : null}
        <Badge tone="neutral">catalogue revision {quote.catalogue_revision}</Badge>
        <Badge tone="neutral">priced by {quote.source}</Badge>
      </div>

      <p className="mt-3 text-[12px] text-[var(--ink-4)]">
        Quote hash <code className={MONO}>{quote.content_hash}</code>
      </p>
    </div>
  );
}

function Total({ label, minor, currency }: { label: string; minor: number; currency: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <dt className="text-[var(--ink-3)]">{label}</dt>
      <dd className="text-[var(--ink-2)]">
        <Amount minor={minor} currency={currency} />
      </dd>
    </div>
  );
}

/* --------------------------------------------------------------------- refunds */

function RefundRow({ refund }: { refund: Refund }) {
  const known = REFUND_STATES[refund.state];
  const accent = known?.accent ?? "var(--ink-5)";
  return (
    <li
      className="rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-white"
      style={{ borderLeft: `3px solid ${accent}` }}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-3 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={known?.tone ?? "neutral"}>
              <RefundGlyph kind={known?.glyph ?? "question"} />
              {known ? known.label : refund.state}
            </Badge>
            <code className={cx(MONO, "text-[var(--ink-5)]")}>{refund.state}</code>
            {refund.automatic ? <Badge tone="amber">Issued automatically</Badge> : null}
          </div>
          <p className="mt-1.5 max-w-prose text-[13px] text-[var(--ink-3)]">
            {known
              ? known.licence
              : "The storefront does not recognise this refund state and shows it exactly as the server sent it."}
          </p>
          {refund.automatic ? (
            <p className="mt-1 max-w-prose text-[12px] text-[var(--ink-4)]">
              Nobody asked for this refund. The platform issued it because the capture landed on a
              checkout that was no longer valid, and returning the money is the only correct answer
              to that.
            </p>
          ) : null}
          <p className="mt-1.5 text-[12px] text-[var(--ink-4)]" title={refund.created_at}>
            {refund.reason} · {formatTimestamp(refund.created_at)}
          </p>
          <code className={cx(MONO, "mt-1 block text-[var(--ink-5)]")}>{refund.refund_id}</code>
        </div>
        <Amount
          minor={refund.amount_minor}
          currency={refund.currency}
          className="text-[16px] font-bold text-[var(--ink)]"
        />
      </div>
    </li>
  );
}

/* ------------------------------------------------------------------ the screen */

export function OrderDetail({ orderId }: { orderId: string }) {
  /**
   * One read is held with the exact inputs that produced it, and the screen's phase is
   * derived by comparing those inputs to the ones being asked about now. That is why
   * nothing here resets a phase before fetching: a result whose key no longer matches is
   * already not being rendered, so a stale order can never appear under a new order's
   * heading while its own read is in flight.
   */
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{
    key: string;
    order: Order | null;
    problem: string | null;
  } | null>(null);

  const key = `${attempt} ${orderId}`;
  const current = result && result.key === key ? result : null;
  const order = current?.order ?? null;
  const problem = current?.problem ?? null;
  const phase: "loading" | "ready" | "error" =
    current === null ? "loading" : current.problem !== null ? "error" : "ready";

  const retry = useCallback(() => setAttempt((previous) => previous + 1), []);

  /**
   * A re-read that failed after the screen already had an order.
   *
   * Kept beside the order rather than replacing it. The re-read is asked for by the action
   * panel once the platform has answered a cancellation, and swapping the whole screen for
   * an error at that moment would take away the verdict the buyer has just been given --
   * the one thing on the page they were waiting for. So the order stays, and this says
   * plainly that what is on screen may now be behind.
   */
  const [rereadFailed, setRereadFailed] = useState<string | null>(null);

  /**
   * Adopt an order the server has already sent back.
   *
   * The refund route answers with the order re-read inside the transaction that admitted
   * the refund, so there is a fresher copy in hand than any follow-up GET could return.
   * Written under the current key so it lands on the screen that asked for it, and never
   * on a screen that has since moved to a different order.
   */
  const applyOrder = useCallback(
    (fresh: Order) => {
      setRereadFailed(null);
      setResult({ key, order: fresh, problem: null });
    },
    [key],
  );

  /**
   * Read the order again in place, without dropping to skeletons.
   *
   * Deliberately not `retry`: bumping the attempt changes the key, which unmounts the body
   * and with it the panel holding the platform's answer. This keeps the key and replaces
   * only the data underneath, so a buyer who has just been told why their cancellation was
   * refused can still read that sentence while the fresh state arrives under it.
   */
  const refresh = useCallback(() => {
    api
      .order(orderId)
      .then((found) => {
        setRereadFailed(null);
        setResult({ key, order: found, problem: null });
      })
      .catch((cause: unknown) => setRereadFailed(humanMessage(cause)));
  }, [key, orderId]);

  useEffect(() => {
    const controller = new AbortController();
    api
      .order(orderId, controller.signal)
      .then((found) => {
        if (controller.signal.aborted) return;
        setResult({ key, order: found, problem: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ key, order: null, problem: humanMessage(cause) });
      });
    return () => controller.abort();
  }, [key, orderId]);

  return (
    <section className="column py-6">
      <Link
        href="/orders"
        className="inline-flex items-center gap-1 text-[13px] font-semibold text-[var(--green)]"
      >
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
          <path d="M9.6 3.4 5 8l4.6 4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        All orders
      </Link>

      <p aria-live="polite" className="sr-only">
        {phase === "loading" ? "Loading this order" : phase === "error" ? `Could not load this order. ${problem ?? ""}` : "Order loaded"}
      </p>

      {phase === "loading" ? (
        <div className="mt-4 grid gap-3">
          <Skeleton className="h-[120px] w-full" />
          <Skeleton className="h-[180px] w-full" />
          <Skeleton className="h-[160px] w-full" />
        </div>
      ) : phase === "error" || !order ? (
        <ErrorState
          title="Could not load this order"
          detail={problem ?? undefined}
          onRetry={retry}
        />
      ) : (
        <OrderBody
          order={order}
          onOrder={applyOrder}
          onChanged={refresh}
          rereadFailed={rereadFailed}
        />
      )}
    </section>
  );
}

function OrderBody({
  order,
  onOrder,
  onChanged,
  rereadFailed,
}: {
  order: Order;
  onOrder: (order: Order) => void;
  onChanged: () => void;
  rereadFailed: string | null;
}) {
  const state = ORDER_STATES[order.state];
  return (
    <div className="mt-4 grid gap-3">
      <Card className="px-4 py-4">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-[18px] font-bold text-[var(--ink)]">Order</h1>
            <code className={cx(MONO, "mt-0.5 block text-[13px] text-[var(--ink-2)]")}>
              {order.order_id}
            </code>
            <p className="mt-1 text-[12px] text-[var(--ink-4)]" title={order.created_at}>
              Confirmed {formatTimestamp(order.created_at)}
            </p>
          </div>
          <div className="text-right">
            <Amount money={order.amount} className="text-[28px] font-bold text-[var(--ink)]" />
            <div className="mt-1 flex justify-end">
              <Badge tone={state?.tone ?? "neutral"}>{order.state.replace(/_/g, " ")}</Badge>
            </div>
          </div>
        </div>
        {state ? <p className="mt-3 max-w-prose text-[13px] text-[var(--ink-3)]">{state.meaning}</p> : null}
      </Card>

      {rereadFailed ? (
        <div
          role="status"
          className="rounded-[var(--r-md)] border-[0.5px] border-[var(--amber)] bg-amber-50/40 px-4 py-3"
        >
          <p className="max-w-prose text-[13px] text-[var(--ink-2)]">
            The platform answered, but reading this order again straight afterwards did not
            work: {rereadFailed} What is shown below is the last copy that arrived, so it may
            not yet include what just happened.
          </p>
        </div>
      ) : null}

      {/*
        The controls sit here, directly under the amount, and not at the foot of the page.
        Everything below them is evidence -- hashes, the quote, the attempt, the capture --
        and it is the right material for somebody auditing a sale and the wrong material to
        make a buyer scroll past before they are allowed to ask for their money back. A
        cancel control that can only be found by reading four cards of forensics first is
        hard to find in the way that matters, whatever the sitemap says.
      */}
      <OrderActions order={order} onOrder={onOrder} onChanged={onChanged} />

      {/*
        The track sits above the evidence and below the controls. A buyer who has just paid
        looks for "where is it" before anything else, so it goes high; but it is drawn from
        two real fields and three the platform cannot report, so it must not sit above the
        refund controls and imply the sale is progressing when it may need withdrawing.
      */}
      <DeliveryProgress order={order} />

      <SectionCard
        title="What this sale is bound to"
        subtitle="Copied onto the order at capture, so the terms are answerable from this row alone."
      >
        <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2">
          <Field label="Approved version">
            <span className="tnum font-semibold">{order.version}</span>
            <span className="ml-2 text-[12px] text-[var(--ink-4)]">
              of checkout <code className={MONO}>{order.checkout_id}</code>
            </span>
          </Field>
          <Field label="Currency">
            <span className="tnum">{order.currency}</span>
          </Field>
          <Hash label="Content hash" value={order.content_hash} />
          <Hash label="Policy receipt hash" value={order.policy_receipt_hash} />
        </dl>
        <p className="mt-3 max-w-prose text-[12px] text-[var(--ink-4)]">
          The content hash covers the exact bytes the buyer approved; the policy receipt hash covers
          the policy in force at that moment. A capture that did not match both would not have
          produced this order.
        </p>
      </SectionCard>

      {order.quote ? (
        <SectionCard title="The quote this order was priced from" subtitle="Every figure is the server's own.">
          <QuoteTable quote={order.quote} />
        </SectionCard>
      ) : (
        <SectionCard title="The quote this order was priced from">
          <p className="text-[13px] text-[var(--ink-3)]">
            No quote is retained on this order. The hashes above still bind it to the version the
            buyer approved.
          </p>
        </SectionCard>
      )}

      <SectionCard title="Payment attempt" subtitle="One live attempt per checkout, enforced by the database.">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-4 sm:grid-cols-2">
          <Field label="Attempt state">
            <span className="font-semibold">{order.payment.state.replace(/_/g, " ")}</span>
            <code className={cx(MONO, "ml-2 text-[var(--ink-5)]")}>{order.payment.state}</code>
          </Field>
          <Field label="Attempt version">
            <span className="tnum">{order.payment.version}</span>
          </Field>
          <Field label="Attempt id" wide>
            <code className={MONO}>{order.payment.attempt_id}</code>
          </Field>
          <Field label="Razorpay order id">
            <code className={MONO}>{order.payment.razorpay_order_id ?? "—"}</code>
          </Field>
          <Field label="Razorpay payment id">
            <code className={MONO}>{order.payment.razorpay_payment_id ?? "—"}</code>
          </Field>
          <Field label="Execution grant">
            <code className={MONO}>{order.payment.grant_id ?? "—"}</code>
          </Field>
          <Field label="Reconciliation attempts">
            <span className="tnum">{order.payment.reconciliation_attempts}</span>
            <span className="ml-2 text-[12px] text-[var(--ink-4)]">
              {order.payment.reconciliation_attempts === 0
                ? "never needed reconciling"
                : "times the platform went back to the provider to settle what happened"}
            </span>
          </Field>
        </dl>
      </SectionCard>

      <CaptureEvidencePanel evidence={order.payment.capture_evidence} />

      <SectionCard
        title={`Refunds (${order.refunds.length})`}
        subtitle="Each refund is a separate kernel admission under its own single-use grant."
      >
        {order.refunds.length === 0 ? (
          <p className="text-[13px] text-[var(--ink-3)]">
            No refund has been raised against this order.
          </p>
        ) : (
          <ul className="grid gap-2">
            {order.refunds.map((refund) => (
              <RefundRow key={refund.refund_id} refund={refund} />
            ))}
          </ul>
        )}
      </SectionCard>
    </div>
  );
}

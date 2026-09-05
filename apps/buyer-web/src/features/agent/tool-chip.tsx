/**
 * One chip per entry in a turn's `tool_calls`.
 *
 * The list is the harness's own ledger, recorded by the executor before each tool ran, so
 * a runner cannot report a call it did not make. This component's whole discipline is to
 * add nothing to it: the label is a translation of the registered tool name into English,
 * the sentence beside it is the server's `summary` verbatim, and the mark on the right is
 * `ok`. Nothing here infers what a tool "probably" did.
 *
 * A refused call is drawn differently from a failed one. Both have `ok: false`, but they
 * are opposite facts: a failure is the store not answering, and a refusal is the
 * capability gate doing its job, which is the thing this submission is about.
 */
"use client";

import { cx } from "@/components/ui";
import type { ToolCall } from "@/lib/api/types";

/**
 * The closed tool table of `commerce_api.services.agent_service.TOOLS`, in English.
 *
 * Written out rather than derived, because "catalog.get_product" reads as "read one
 * product" to a buyer and as nothing at all when mechanically de-dotted. A name absent
 * here still renders -- the server owns this vocabulary and an unknown tool must appear
 * as itself rather than be swallowed.
 */
const READABLE_TOOLS: Readonly<Record<string, string>> = {
  "catalog.search": "searched the catalogue",
  "catalog.get_product": "read one product",
  "basket.read": "read the basket",
  "checkout.read": "read the checkout",
  "order.track": "tracked the order",
  "support.case.read": "read the support case",
  "merchant.catalogue_health.read": "read catalogue health",
  "merchant.inventory_anomalies.read": "read inventory anomalies",
  "merchant.checkout_metrics.read": "read checkout metrics",
};

/**
 * Capabilities the agent surface does not contain. A denial is recorded in the ledger
 * under the capability rather than a tool, because there is no tool: these are buyer
 * consent, and the absence is structural.
 */
const READABLE_REFUSALS: Readonly<Record<string, string>> = {
  "checkout.approve": "asked to approve",
  "checkout.reject": "asked to reject",
  "checkout.cancel": "asked to cancel",
  "payment.verify": "asked to confirm a payment",
  "refund.request": "asked to start a refund",
  "grant.revoke": "asked to revoke a grant",
};

function readableName(call: ToolCall, refused: boolean): string {
  const table = refused ? READABLE_REFUSALS : READABLE_TOOLS;
  return table[call.name] ?? call.name.replace(/[._]/g, " ");
}

/**
 * Read an extension member the schema accepts but does not type.
 *
 * `ToolCallSchema` is loose so that a server that starts sending more about a call does
 * not fail the parse at the boundary. What TypeScript makes of a loose object is a
 * detail of zod's inference, and this component should not depend on it, so the two
 * extras it actually uses -- `denied` and `reason_key` -- are read through here.
 */
function extra(call: ToolCall, key: string): unknown {
  return (call as unknown as Record<string, unknown>)[key];
}

function Tick() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none">
      <path
        d="M3.5 8.5 L6.5 11.5 L12.5 4.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Cross() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none">
      <path
        d="M4.5 4.5 L11.5 11.5 M11.5 4.5 L4.5 11.5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** A barred circle: the gate turned this away, rather than the call going wrong. */
function Barred() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none">
      <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="1.6" />
      <path d="M4.2 11.8 L11.8 4.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

export function ToolChip({ call }: { call: ToolCall }) {
  const refused = extra(call, "denied") === true;
  const reasonKey = extra(call, "reason_key");
  const outcome = refused ? "refused" : call.ok ? "succeeded" : "failed";

  return (
    <li
      className={cx(
        "inline-flex max-w-full items-start gap-1.5 rounded-full px-2.5 py-1 text-[12px] leading-4",
        refused
          ? "border border-amber-400/30 bg-amber-400/10 text-amber-300"
          : call.ok
            ? "border border-white/10 bg-white/[0.04] text-slate-200"
            : "border border-rose-400/30 bg-rose-500/10 text-rose-300",
      )}
      title={typeof reasonKey === "string" && reasonKey ? `${call.name} — ${reasonKey}` : call.name}
    >
      <span className="mt-0.5 shrink-0">
        {refused ? <Barred /> : call.ok ? <Tick /> : <Cross />}
      </span>
      <span className="min-w-0">
        <span className="font-semibold">{readableName(call, refused)}</span>
        <span className="sr-only"> — {outcome}. </span>
        {call.summary ? <span className="text-slate-400"> · {call.summary}</span> : null}
      </span>
    </li>
  );
}

/** The whole tool log for one turn. Renders nothing when the turn called no tool. */
export function ToolChips({ calls }: { calls: readonly ToolCall[] }) {
  if (calls.length === 0) return null;
  return (
    <div className="mt-2">
      <p className="mb-1 font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-500">
        What it actually did
      </p>
      <ul className="flex flex-wrap gap-1.5">
        {calls.map((call, index) => (
          <ToolChip key={`${call.name}-${index}`} call={call} />
        ))}
      </ul>
    </div>
  );
}

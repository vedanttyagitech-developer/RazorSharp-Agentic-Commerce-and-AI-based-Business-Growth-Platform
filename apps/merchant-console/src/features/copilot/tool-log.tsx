"use client";

/**
 * The turn's tool ledger, and the refusals in it.
 *
 * Both are drawn from `tool_calls` and `denials` exactly as the server sent them. The
 * executor records each call before it runs, so this is what the harness actually did
 * rather than what the reply says it did, and the whole discipline of this file is to add
 * nothing to it: the label is a translation of a registered tool name, the sentence beside
 * it is the server's own `summary`, and the mark is `ok`.
 *
 * A refusal and a failure both arrive with `ok: false` and they are opposite facts. A
 * failure is the platform not answering. A refusal is the capability gate doing its job,
 * which is the property this whole submission is arguing for -- so it is drawn as a panel
 * with its own heading rather than as an error, and never in the red this console reserves
 * for a read that broke.
 */
import type { Denial, ToolCall } from "@/lib/api/types";

import { Chip } from "@/components/ui";

import { reasonSentence, toolName } from "./payloads";

/** Extension members the loose schema accepts but does not type. Read, never inferred. */
function extra(call: ToolCall, key: string): unknown {
  return (call as unknown as Record<string, unknown>)[key];
}

/**
 * The server's summary, unless it merely repeats the label beside it.
 *
 * `read inventory anomalies · read inventory anomalies (3)` is what the ledger and the
 * translation table produce together, and printing both reads as a rendering fault rather
 * than as evidence. Only an exact repetition of the label is dropped, and only its prefix:
 * the count in the tail is the part that carries information and it always survives.
 */
function summaryBeyond(label: string, summary: string): string {
  const trimmed = summary.trim();
  if (!trimmed.toLowerCase().startsWith(label.toLowerCase())) return trimmed;
  return trimmed.slice(label.length).replace(/^[\s:·—-]+/, "");
}

export function ToolLog({ calls }: { calls: readonly ToolCall[] }) {
  if (calls.length === 0) return null;
  return (
    <div className="mt-2.5">
      <p className="eyebrow">What it actually called</p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {calls.map((call, index) => {
          const refused = extra(call, "denied") === true;
          const reasonKey = extra(call, "reason_key");
          const outcome = refused ? "refused" : call.ok ? "succeeded" : "failed";
          const label = toolName(call.name);
          const summary = call.summary ? summaryBeyond(label, call.summary) : "";
          return (
            <li key={`${call.name}-${index}`}>
              <Chip tone={refused ? "warn" : call.ok ? "positive" : "danger"}>
                <span aria-hidden="true">{refused ? "⊘" : call.ok ? "✓" : "✕"}</span>
                <span className="font-semibold">{label}</span>
                <span className="sr-only"> — {outcome}.</span>
                {summary ? <span className="opacity-80">{summary}</span> : null}
              </Chip>
              {typeof reasonKey === "string" && reasonKey ? (
                <span className="sr-only"> Reason {reasonKey}.</span>
              ) : null}
            </li>
          );
        })}
      </ul>
      <p className="mono mt-1 text-[var(--faint)]">
        {calls.map((call) => call.name).join(" · ")}
      </p>
    </div>
  );
}

/**
 * Every capability the harness asked for and was refused this turn.
 *
 * Renders nothing when there were none, so a caller can mount it under each reply without
 * first asking whether it will show. The raw capability and reason key stay on the card
 * because they are the strings a reviewer greps the server logs for; the sentence above
 * them is for the merchant standing in front of the screen.
 */
export function DenialPanel({ denials }: { denials: readonly Denial[] }) {
  if (denials.length === 0) return null;
  return (
    <section
      aria-label="Refused by the capability gate"
      className="mt-2.5 rounded-[var(--r-md)] border border-[color-mix(in_srgb,var(--warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--warn)_10%,transparent)] p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="warn">REFUSED</Chip>
        <span className="text-[12px] font-semibold text-[var(--ink)]">
          The copilot asked for something it does not hold
        </span>
      </div>
      <p className="mt-1.5 text-[11.5px] text-[var(--muted)]">
        The gate refused before any tool ran. Nothing was attempted and nothing changed. This is
        the system working, not a fault.
      </p>
      <ul className="mt-2 space-y-1.5">
        {denials.map((denial, index) => (
          <li
            key={`${denial.capability}-${denial.reason_key}-${index}`}
            className="rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2.5 py-2"
          >
            <p className="text-[12px] font-semibold text-[var(--ink)] break-id">
              {denial.capability}
            </p>
            <p className="mt-0.5 text-[11.5px] leading-[1.45] text-[var(--muted)]">
              {reasonSentence(denial.reason_key)}
            </p>
            <p className="mono mt-1 text-[var(--faint)] break-id">{denial.reason_key}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

"use client";

/**
 * The durable outbox: pending, leased, done, failed and dead, with the revive action.
 *
 * The counts across the tenant sit above the page, and they are the numbers worth reading
 * during an incident: a rising FAILED count is a provider problem and a rising PENDING
 * count is a worker shortage, and those have opposite remedies. Reading either off a
 * truncated list would point an operator the wrong way, so the API sends both and this
 * panel keeps them apart.
 *
 * Revive answers HTTP 200 even when it did nothing -- `code: CONCURRENT_OPERATION` with
 * the status actually found, because only a DEAD row may be revived. This component
 * renders that answer verbatim rather than treating a 200 as success: telling an operator
 * a completed money command was re-queued when it was not is precisely the kind of
 * comfortable lie this console exists without.
 */
import { useState } from "react";
import { api } from "@/lib/api/client";
import { problemOf } from "@/lib/api/problem";
import { useRead } from "@/lib/useRead";
import type { ReviveResult } from "@/lib/api/types";
import {
  Button,
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
  toneForState,
} from "@/components/ui";
import { FilterChip } from "./Filters";

const OUTBOX_STATUSES = ["PENDING", "LEASED", "DONE", "FAILED", "DEAD"] as const;

/** One revive outcome, kept per command so a row shows what its own button did. */
interface Outcome {
  result?: ReviveResult;
  error?: unknown;
  running?: boolean;
}

export function OutboxTab({ initialStatus }: { initialStatus: string | null }) {
  const [status, setStatus] = useState<string>(initialStatus ?? "");
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({});
  const page = useRead(
    (signal) => api.outbox({ status: status || undefined, limit: 100, signal }),
    [status],
  );

  async function revive(commandId: string) {
    setOutcomes((current) => ({ ...current, [commandId]: { running: true } }));
    try {
      const result = await api.reviveCommand(commandId);
      setOutcomes((current) => ({ ...current, [commandId]: { result } }));
      // The row's status has moved if the revive took, so re-read rather than patch it here.
      page.reload();
    } catch (error) {
      setOutcomes((current) => ({ ...current, [commandId]: { error } }));
    }
  }

  return (
    <Panel
      title="Durable outbox"
      subtitle="GET /v1/ops/outbox?status= — newest first, because the rows you want during an incident are the ones that just failed"
      actions={<Button onClick={page.reload}>Re-read</Button>}
    >
      <div className="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-4 py-2.5">
        <FilterChip
          active={status === ""}
          onClick={() => setStatus("")}
          label="all"
          count={page.data ? Object.values(page.data.counts).reduce((a, b) => a + b, 0) : undefined}
        />
        {OUTBOX_STATUSES.map((value) => (
          <FilterChip
            key={value}
            active={status === value}
            onClick={() => setStatus(value)}
            label={value}
            count={page.data?.counts[value]}
            tone={toneForState(value)}
          />
        ))}
        <span className="ml-1 text-[11px] text-[var(--faint)]">
          counts across the tenant
        </span>
      </div>

      {page.loading && <Loading label="Reading the outbox" />}
      {page.error != null && (
        <div className="p-4">
          <ProblemPanel error={page.error} what="the durable outbox" onRetry={page.reload} />
        </div>
      )}
      {page.data && page.data.commands.length === 0 && (
        <Empty>No command matches this filter. The queue counts above came from the same read.</Empty>
      )}
      {page.data && page.data.commands.length > 0 && (
        <TableWrap>
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                <Th>Command</Th>
                <Th>Type</Th>
                <Th>Status</Th>
                <Th align="right">Attempts</Th>
                <Th>Available at</Th>
                <Th>Leased until</Th>
                <Th>Created</Th>
                <Th>Revive</Th>
              </tr>
            </thead>
            <tbody>
              {page.data.commands.map((command) => {
                const outcome = outcomes[command.command_id];
                return (
                  <tr key={command.command_id} className="hover:bg-[var(--raised)]">
                    <Td>
                      <Id value={command.command_id} />
                      <div className="mono mt-0.5 text-[var(--faint)]">
                        correlation <Id value={command.correlation_id} />
                      </div>
                    </Td>
                    <Td className="mono text-[var(--ink)]">{command.command_type}</Td>
                    <Td>
                      <Chip tone={toneForState(command.status)}>{command.status}</Chip>
                    </Td>
                    <Td align="right" className="num text-[var(--ink)]">
                      {command.attempts}
                    </Td>
                    <Td>
                      <When value={command.available_at} />
                    </Td>
                    <Td>
                      <When value={command.leased_until} />
                    </Td>
                    <Td>
                      <When value={command.created_at} />
                    </Td>
                    <Td>
                      <Button
                        variant={command.status === "DEAD" ? "primary" : "default"}
                        onClick={() => revive(command.command_id)}
                        disabled={outcome?.running}
                        title={
                          command.status === "DEAD"
                            ? "Re-queue this dead letter with a fresh attempt budget"
                            : "Only a DEAD command is revived; the API will say what it found"
                        }
                      >
                        {outcome?.running ? "Reviving…" : "Revive"}
                      </Button>
                      {outcome?.result && (
                        <div className="mt-1.5" aria-live="polite">
                          <Chip tone={outcome.result.code === "OK" ? "positive" : "warn"}>
                            {outcome.result.code}
                          </Chip>
                          <div className="mono mt-0.5 text-[var(--muted)]">
                            now {outcome.result.status}
                            {outcome.result.retry_at && (
                              <>
                                {" "}
                                · retry <When value={outcome.result.retry_at} />
                              </>
                            )}
                          </div>
                        </div>
                      )}
                      {outcome?.error != null && (
                        <div className="mt-1.5" aria-live="assertive">
                          <Chip tone="danger">REVIVE FAILED</Chip>
                          <div className="mono mt-0.5 max-w-[220px] text-[var(--danger)] break-id">
                            {problemOf(outcome.error).status} {problemOf(outcome.error).detail ?? problemOf(outcome.error).title}
                          </div>
                        </div>
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableWrap>
      )}
      {page.data && (
        <p className="border-t border-[var(--line)] px-4 py-2.5 text-[11.5px] text-[var(--faint)]">
          Showing <span className="num text-[var(--ink)]">{page.data.commands.length}</span> of a{" "}
          <span className="num text-[var(--ink)]">{page.data.limit}</span> row limit. The counts above
          are across the whole tenant and are not derived from this page.
        </p>
      )}
    </Panel>
  );
}

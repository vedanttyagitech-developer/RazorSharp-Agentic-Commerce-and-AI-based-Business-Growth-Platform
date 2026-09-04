"use client";

import { useState } from "react";

import type { ProofChain, TimelineRow } from "@/lib/api/types";
import { formatTimestamp } from "@/lib/money";

import { useClient } from "./providers";
import { Alert, Button, Spinner, StatusPill } from "./ui";

export interface EvidenceDrawerProps {
  checkoutId: string;
  attemptId: string | null;
  /** Live rows already received over SSE; the drawer merges them with GET /timeline. */
  liveRows?: TimelineRow[];
}

/**
 * Evidence drawer (spec 26, 28): the action timeline, the Money Action Proof Chain
 * verdict, and links to the inspector JSON. Hashes and references only; nothing secret.
 */
export function EvidenceDrawer({ checkoutId, attemptId, liveRows = [] }: EvidenceDrawerProps) {
  const client = useClient();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<TimelineRow[] | null>(null);
  const [proof, setProof] = useState<ProofChain | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [timeline, chain] = await Promise.all([client.getTimeline(checkoutId), client.getProof(checkoutId)]);
      setRows(timeline.rows);
      setProof(chain);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load evidence");
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && rows === null && !loading) void load();
  }

  const merged = mergeRows(rows ?? [], liveRows);
  const inspectorHref = attemptId ? client.inspectorUrl(attemptId) : null;
  const proofHref = client.proofUrl(checkoutId);
  const auditHref = client.auditVerifyUrl("checkout", checkoutId);

  return (
    <section aria-labelledby="evidence-heading" className="rounded-lg border border-line bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 p-4">
        <h2 id="evidence-heading" className="text-base font-semibold">Evidence</h2>
        <Button variant="secondary" onClick={toggle} aria-expanded={open} aria-controls="evidence-body">
          {open ? "Hide evidence" : "Show timeline and proof chain"}
        </Button>
      </div>
      <div id="evidence-body" hidden={!open} className="space-y-4 border-t border-line p-4">
        {loading ? <Spinner label="Loading timeline and proof chain" /> : null}
        {error ? <Alert tone="danger" title="Evidence unavailable" role="alert">{error}</Alert> : null}

        {proof ? (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-medium">Money Action Proof Chain</h3>
              <StatusPill tone={proof.verdict.ok ? "success" : "warning"} glyph={proof.verdict.ok ? "✓" : "!"} label={proof.verdict.ok ? "Verifier: complete" : "Verifier: incomplete"} />
              <span className="text-sm text-muted">{proof.verdict.summary}</span>
            </div>
            <ol className="grid gap-1 text-sm sm:grid-cols-2">
              {proof.links.map((link) => (
                <li key={link.step} className="flex items-start gap-2 rounded border border-line px-2 py-1">
                  <span aria-hidden="true" className="w-4 font-mono">{link.present ? (link.verified ? "✓" : "!") : "·"}</span>
                  <span>
                    <span className="font-medium">{link.step}. {link.name}</span>
                    <span className="sr-only">{link.present ? (link.verified ? " present and verified" : " present, not verified") : " missing"}</span>
                    {link.reference ? <span className="block font-mono text-xs text-muted">{link.reference}</span> : null}
                    {link.hash ? <span className="block font-mono text-xs text-muted">{link.hash}</span> : null}
                    <span className="block text-xs text-muted">{link.detail}</span>
                  </span>
                </li>
              ))}
            </ol>
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
              {proof.verdict.checks.map((check) => (
                <li key={check.name}><span aria-hidden="true" className="font-mono">{check.ok ? "✓" : "×"}</span> {check.name}: {check.detail}{check.ok ? "" : " (failed)"}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div>
          <h3 className="font-medium">Action timeline</h3>
          {merged.length === 0 && !loading ? <p className="text-sm text-muted">No rows yet.</p> : null}
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <caption className="sr-only">Action timeline rows</caption>
              <thead>
                <tr className="border-b border-line text-left text-muted">
                  <th scope="col" className="py-1 pr-2">Time</th>
                  <th scope="col" className="py-1 pr-2">Actor</th>
                  <th scope="col" className="py-1 pr-2">Action</th>
                  <th scope="col" className="py-1 pr-2">Version / hash</th>
                  <th scope="col" className="py-1 pr-2">Policy / receipt</th>
                  <th scope="col" className="py-1 pr-2">Decision · grant</th>
                  <th scope="col" className="py-1">Provider / reconciliation</th>
                </tr>
              </thead>
              <tbody>
                {merged.map((row) => (
                  <tr key={row.event_id} className={`border-b border-line/60 align-top ${row.scenario_injection ? "bg-amber-50 dark:bg-amber-950" : ""}`}>
                    <td className="py-1 pr-2 whitespace-nowrap font-mono">{formatTimestamp(row.occurred_at)}<span className="block text-muted">{row.event_id}</span></td>
                    <td className="py-1 pr-2">{row.actor}</td>
                    <td className="py-1 pr-2 font-mono">{row.action}{row.scenario_injection ? <span className="block text-amber-800 dark:text-amber-200">SCENARIO_INJECTION</span> : null}</td>
                    <td className="py-1 pr-2 font-mono">v{row.version ?? "—"}{row.hash_short ? <span className="block text-muted">{row.hash_short}</span> : null}</td>
                    <td className="py-1 pr-2">{row.policy_evaluated ?? "—"}{row.policy_receipt_hash ? <span className="block font-mono text-muted">{row.policy_receipt_hash.slice(0, 12)}…</span> : null}</td>
                    <td className="py-1 pr-2">
                      {row.decision ? <span className="font-mono">{row.decision.allowed ? "ALLOW" : "DENY"} {row.decision.code}<span className="block text-muted">{row.decision.reason}</span></span> : "—"}
                      {row.grant ? <span className="block font-mono text-muted">{row.grant.grant_id} · {row.grant.status}</span> : null}
                      {row.authority_ref ? <span className="block font-mono text-muted">{row.authority_ref}</span> : null}
                    </td>
                    <td className="py-1">{row.provider_result ?? ""}{row.reconciliation_result ? <span className="block text-muted">{row.reconciliation_result}</span> : null}<span className="block font-mono text-muted">corr {row.correlation_id}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-2 text-sm">
          <h3 className="font-medium">Inspector JSON</h3>
          {inspectorHref || proofHref || auditHref ? (
            <ul className="flex flex-wrap gap-3">
              {inspectorHref ? <li><a className="underline" href={inspectorHref} target="_blank" rel="noreferrer">Payment attempt inspector</a></li> : null}
              {proofHref ? <li><a className="underline" href={proofHref} target="_blank" rel="noreferrer">Proof chain JSON</a></li> : null}
              {auditHref ? <li><a className="underline" href={auditHref} target="_blank" rel="noreferrer">Audit hash-chain verification</a></li> : null}
            </ul>
          ) : (
            <details>
              <summary className="cursor-pointer">Mock mode has no HTTP inspector; view the proof-chain JSON inline</summary>
              <pre className="mt-2 max-h-80 overflow-auto rounded bg-stone-100 p-2 font-mono text-xs dark:bg-stone-800">{JSON.stringify(proof, null, 2)}</pre>
            </details>
          )}
        </div>
      </div>
    </section>
  );
}

function mergeRows(fetched: TimelineRow[], live: TimelineRow[]): TimelineRow[] {
  const byId = new Map<string, TimelineRow>();
  for (const row of fetched) byId.set(row.event_id, row);
  for (const row of live) byId.set(row.event_id, row);
  return Array.from(byId.values()).sort((a, b) => a.occurred_at.localeCompare(b.occurred_at) || a.event_id.localeCompare(b.event_id));
}

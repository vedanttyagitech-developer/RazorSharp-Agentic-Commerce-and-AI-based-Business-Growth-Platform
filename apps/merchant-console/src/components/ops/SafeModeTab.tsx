"use client";

/**
 * The kill switch, and the one control in this console where a lie would be dangerous.
 *
 * The rule, stated plainly: **the state shown is always the state the API last returned.**
 * Pressing the switch does not move anything on screen. The POST returns the mode the API
 * re-read inside the same transaction that wrote it -- what admission will decide a moment
 * later -- and that answer replaces the displayed state. If the POST fails, the failure is
 * rendered and the previously-read state stays exactly as it was, still labelled with when
 * it was read. A console that painted an engaged banner over a request that never landed
 * would tell an operator the platform is protected when it is not, and that is the precise
 * dishonesty this console was rebuilt to remove.
 *
 * The banner names both halves for the same reason the API sends both. A kill switch that
 * stopped everything would be an outage, and an outage harms the buyer it was meant to
 * protect; so "what is still available" is as prominent as "what is blocked".
 */
import { useState } from "react";
import { api } from "@/lib/api/client";
import { useRead } from "@/lib/useRead";
import type { SafeMode } from "@/lib/api/types";
import {
  Button,
  Chip,
  Field,
  Loading,
  Panel,
  ProblemPanel,
  When,
  cx,
} from "@/components/ui";

/** `transaction_kernel.safe_mode.ModeChangeReason`, split as the kernel splits it. */
const ENTRY_REASONS = [
  "OPERATOR_DECLARED_INCIDENT",
  "KEY_COMPROMISE_EVIDENCE",
  "ABNORMAL_DUPLICATE_ATTEMPTS",
  "UNRESOLVED_RECONCILIATION_BACKLOG",
  "SIGNATURE_VERIFICATION_ANOMALY",
  "PROVIDER_INCIDENT_DECLARED",
] as const;

const EXIT_REASONS = ["INCIDENT_RESOLVED", "OPERATOR_STOOD_DOWN", "TENANT_EXEMPTED"] as const;

export function SafeModeTab() {
  const read = useRead((signal) => api.safeMode(signal), []);

  // The state written by a successful POST, which supersedes the read once it exists.
  const [written, setWritten] = useState<SafeMode | null>(null);
  const [writeError, setWriteError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [entryReason, setEntryReason] = useState<string>(ENTRY_REASONS[0]);
  const [exitReason, setExitReason] = useState<string>(EXIT_REASONS[0]);

  const mode = written ?? read.data;
  const origin = written ? "POST /v1/ops/safe-mode" : "GET /v1/ops/safe-mode";

  async function toggle(enabled: boolean) {
    setPending(true);
    setWriteError(null);
    try {
      const result = await api.setSafeMode(enabled, enabled ? entryReason : exitReason);
      setWritten(result);
    } catch (error) {
      // Nothing is painted. The failure is shown and the displayed state stays as read.
      setWriteError(error);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-4">
      <Panel
        title="Safe Mode"
        subtitle={`current state from ${origin}`}
        actions={
          <Button
            onClick={() => {
              setWritten(null);
              setWriteError(null);
              read.reload();
            }}
          >
            Re-read
          </Button>
        }
      >
        {read.loading && !mode && <Loading label="Reading the operating mode" />}
        {read.error != null && !mode && (
          <div className="p-4">
            <ProblemPanel error={read.error} what="the operating mode" onRetry={read.reload} />
          </div>
        )}

        {mode && (
          <>
            <div
              className={cx(
                "flex flex-wrap items-center gap-3 border-b px-4 py-4",
                mode.safe_mode
                  ? "border-[color-mix(in_srgb,var(--warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--warn)_10%,transparent)]"
                  : "border-[var(--line)]",
              )}
            >
              <span
                className={cx(
                  "num text-[26px] leading-none",
                  mode.safe_mode ? "text-[var(--warn)]" : "text-[var(--positive)]",
                )}
              >
                {mode.mode}
              </span>
              <Chip tone={mode.safe_mode ? "warn" : "positive"}>
                {mode.safe_mode ? "delegated payment stopped" : "delegated payment permitted"}
              </Chip>
              <Chip tone="muted">scope {mode.scope}</Chip>
              {mode.since && (
                <span className="text-[11.5px] text-[var(--muted)]">
                  since <When value={mode.since} />
                </span>
              )}
            </div>

            <dl>
              <Field label="Reason on record" source="operating_modes.reason_code">
                {mode.reason_code ? <Chip tone="warn">{mode.reason_code}</Chip> : <span className="text-[var(--muted)]">none — the tenant is in NORMAL</span>}
              </Field>
              <Field label="Actor" source="operating_modes.actor">
                <span className="mono break-id">{mode.actor ?? "—"}</span>
              </Field>
              <Field label="Blocked while engaged" source="safe_mode.SAFE_MODE_BLOCKED">
                <ul className="flex flex-wrap gap-1.5">
                  {mode.blocked.map((operation) => (
                    <li key={operation}>
                      <Chip tone="danger">{operation}</Chip>
                    </li>
                  ))}
                </ul>
              </Field>
              <Field label="Still available" source="safe_mode.SAFE_MODE_PERMITTED">
                <ul className="flex flex-wrap gap-1.5">
                  {mode.still_available.map((operation) => (
                    <li key={operation}>
                      <Chip tone="positive">{operation}</Chip>
                    </li>
                  ))}
                </ul>
                <p className="mt-1.5 text-[11.5px] text-[var(--faint)]">
                  A kill switch that stopped refunds and reconciliation would be an outage, and an
                  outage harms the buyer it was meant to protect.
                </p>
              </Field>
              <Field label="Asked of the kernel now" source="tk.is_permitted, inside this request">
                <ul className="flex flex-wrap gap-1.5">
                  {Object.entries(mode.permitted).map(([operation, allowed]) => (
                    <li key={operation}>
                      <Chip tone={allowed ? "positive" : "danger"}>
                        {operation} {allowed ? "permitted" : "blocked"}
                      </Chip>
                    </li>
                  ))}
                </ul>
              </Field>
              {mode.revoked_grant_ids.length > 0 && (
                <Field label="Grants revoked by this activation" source="execution_grants swept on entry">
                  <ul className="space-y-0.5">
                    {mode.revoked_grant_ids.map((grantId) => (
                      <li key={grantId} className="mono text-[var(--warn)] break-id">
                        {grantId}
                      </li>
                    ))}
                  </ul>
                  <p className="mt-1 text-[11.5px] text-[var(--faint)]">
                    Refund grants are never swept: a switch thrown to protect a buyer must not cancel
                    money that buyer is already owed.
                  </p>
                </Field>
              )}
            </dl>
          </>
        )}
      </Panel>

      <Panel
        title="Throw or stand down the switch"
        subtitle="POST /v1/ops/safe-mode — an authenticated, audited operator action"
      >
        {writeError != null && (
          <div className="p-4">
            <ProblemPanel
              error={writeError}
              what="the safe-mode write — the mode above is still the last state the API reported, unchanged"
            />
          </div>
        )}

        <div className="grid gap-4 p-4 md:grid-cols-2">
          <div className="rounded-[var(--r-md)] border border-[color-mix(in_srgb,var(--warn)_40%,transparent)] bg-[var(--raised)] p-3.5">
            <h3 className="text-[13px] font-semibold text-[var(--warn)]">Enter Safe Mode</h3>
            <p className="mt-1 text-[11.5px] text-[var(--muted)]">
              Stops new delegated payments for this tenant and withdraws its unused delegated grants.
              Human-present checkout, refunds, reconciliation and support continue.
            </p>
            <label className="mt-3 block">
              <span className="eyebrow">Reason</span>
              <select
                value={entryReason}
                onChange={(event) => setEntryReason(event.target.value)}
                className="mono mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2 py-1.5 text-[var(--ink)]"
              >
                {ENTRY_REASONS.map((reason) => (
                  <option key={reason} value={reason}>
                    {reason}
                  </option>
                ))}
              </select>
            </label>
            <p className="mt-1 text-[11px] text-[var(--faint)]">
              A closed vocabulary from the kernel, not free text: an incident record that says
              &ldquo;because&rdquo; is not an audited administrative action.
            </p>
            <div className="mt-3">
              <Button variant="danger" onClick={() => toggle(true)} disabled={pending || mode?.safe_mode === true}>
                {pending ? "Writing…" : "Enter Safe Mode"}
              </Button>
            </div>
          </div>

          <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] p-3.5">
            <h3 className="text-[13px] font-semibold text-[var(--positive)]">Stand down</h3>
            <p className="mt-1 text-[11.5px] text-[var(--muted)]">
              Returns this tenant to NORMAL. Grants revoked on entry are not reissued; a new checkout
              earns a new one.
            </p>
            <label className="mt-3 block">
              <span className="eyebrow">Reason</span>
              <select
                value={exitReason}
                onChange={(event) => setExitReason(event.target.value)}
                className="mono mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2 py-1.5 text-[var(--ink)]"
              >
                {EXIT_REASONS.map((reason) => (
                  <option key={reason} value={reason}>
                    {reason}
                  </option>
                ))}
              </select>
            </label>
            <div className="mt-3">
              <Button variant="primary" onClick={() => toggle(false)} disabled={pending || mode?.safe_mode === false}>
                {pending ? "Writing…" : "Stand down"}
              </Button>
            </div>
          </div>
        </div>

        <p className="border-t border-[var(--line)] px-4 py-2.5 text-[11.5px] text-[var(--faint)]" aria-live="polite">
          {written
            ? "The state above is the mode the API re-read inside the transaction that wrote it."
            : "The state above is the mode the API reported on the last read. Nothing on this screen is optimistic."}
        </p>
      </Panel>
    </div>
  );
}

/**
 * Changes to the shop: proposed, agreed to, and carried out.
 *
 * A merchant raises a price, adjusts stock, delists a product. Every one of those is
 * drafted, put to somebody, approved and only then performed -- and the screen exists to
 * make the middle step real rather than ceremonial.
 *
 * The digest is the reason this is not a status dropdown
 * -----------------------------------------------------
 * Every proposal has a content hash, and the approve button sends it back. If somebody
 * edited the proposal after this page drew it, the hash will not match and the server
 * refuses -- so an approval can never attach to a document its approver never read. The
 * refusal comes back with both digests and is rendered as an answer, because it is one:
 * two people on one worklist is ordinary, and being told "this changed" is more useful
 * than being told to try again.
 *
 * That is the same guarantee the buyer's approval card gives on the storefront, arrived at
 * the same way. It is worth the symmetry: a platform that binds a buyer's consent to exact
 * bytes and lets a merchant approve "whatever the row says now" is only half serious.
 *
 * What this page shows and does not
 * ---------------------------------
 * It shows the digest, shortened, because an approver should be able to see that it moved.
 * It shows who proposed and who approved as two separate facts, because a change a model
 * drafted and a change a person typed deserve different scrutiny.
 *
 * It decides nothing. Buttons are drawn from the action's state and the server decides
 * anyway -- this page can be seconds stale, and the refusal that comes back is the correct
 * answer rather than a fault.
 */
"use client";

import { useCallback, useState } from "react";

import {
  Button,
  Chip,
  Empty,
  Field,
  Id,
  Loading,
  Panel,
  ProblemPanel,
  TableWrap,
  Td,
  Th,
  When,
  cx,
  type Tone,
} from "@/components/ui";
import { api } from "@/lib/api/client";
import type { MerchantAction } from "@/lib/api/types";
import { useRead } from "@/lib/useRead";

const FILTERS: ReadonlyArray<{ key: string; label: string }> = [
  { key: "", label: "Everything" },
  { key: "DRAFT", label: "Drafts" },
  { key: "AWAITING_APPROVAL", label: "Waiting on you" },
  { key: "APPROVED", label: "Approved" },
  { key: "SUCCEEDED", label: "Done" },
];

/** What each kind changes, in words rather than in the enum's. */
const KINDS: ReadonlyArray<{ key: string; label: string; field: string; hint: string }> = [
  {
    key: "PRICE_CHANGE",
    label: "Change a price",
    field: "unit_price_minor",
    hint: "In paise. ₹28.00 is 2800 — integers only, because a price that hashes as a float is a price two verifiers can disagree about.",
  },
  {
    key: "STOCK_ADJUSTMENT",
    label: "Set stock",
    field: "units",
    hint: "The number of units on the shelf after this change, not the difference.",
  },
];

/**
 * A tone per state.
 *
 * `AWAITING_APPROVAL` is the one that should catch the eye: it is the only state in which
 * the screen is waiting on the person reading it. `STALE` and `FAILED` are warnings rather
 * than errors -- both are the platform refusing correctly.
 */
function toneFor(state: string): Tone {
  if (state === "AWAITING_APPROVAL") return "warn";
  if (state === "APPROVED" || state === "EXECUTING") return "info";
  if (state === "SUCCEEDED") return "positive";
  if (state === "FAILED" || state === "STALE") return "danger";
  return "muted";
}

/** A digest, short enough to read and long enough to see change. */
function digest(value: string): string {
  return `${value.slice(0, 10)}…`;
}

export default function ActionsPage() {
  const [filter, setFilter] = useState("");
  const [nonce, setNonce] = useState(0);
  const list = useRead(
    (signal) => api.merchantActions({ state: filter || undefined, signal }),
    [filter, nonce],
  );
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return (
    <main className="mx-auto flex min-h-dvh max-w-5xl flex-col gap-5 px-6 py-10">
      <header>
        <p className="mono text-[11px] uppercase tracking-[0.14em] text-[var(--faint)]">
          Merchant workspace
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-[var(--ink)]">
          Changes to the shop
        </h1>
        <p className="mt-2 max-w-[70ch] text-[13px] leading-relaxed text-[var(--muted)]">
          Every change is drafted, agreed to and only then carried out. Approving names the
          exact proposal you read: if somebody edits it after this page draws it, the
          approval is refused rather than applied to something else.
        </p>
      </header>

      <Propose onProposed={reload} />

      <Panel
        title="Your changes"
        subtitle="Newest first — this is a record of what you have been doing, not a queue somebody is waiting on."
        actions={
          <div className="flex flex-wrap gap-1.5">
            {FILTERS.map((option) => (
              <button
                key={option.key || "all"}
                type="button"
                onClick={() => setFilter(option.key)}
                className={cx(
                  "rounded-[var(--r-sm)] border px-2 py-1 text-[11.5px] transition-colors",
                  option.key === filter
                    ? "border-[var(--faint)] bg-[var(--raised)] text-[var(--ink)]"
                    : "border-transparent text-[var(--muted)] hover:text-[var(--ink)]",
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
        }
      >
        {list.loading ? (
          <Loading label="Reading your changes" />
        ) : list.error ? (
          <div className="p-4">
            <ProblemPanel error={list.error} what="GET /v1/merchant/actions" onRetry={reload} />
          </div>
        ) : !list.data || list.data.actions.length === 0 ? (
          <Empty>
            {filter ? "Nothing is in this state." : "You have not proposed any changes yet."}
          </Empty>
        ) : (
          <TableWrap>
            <table className="w-full border-collapse text-[12.5px]">
              <thead>
                <tr>
                  <Th>Change</Th>
                  <Th>Proposal</Th>
                  <Th>Digest</Th>
                  <Th>State</Th>
                  <Th>Raised</Th>
                  <Th>
                    <span className="sr-only">Act on it</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {list.data.actions.map((action) => (
                  <Row key={action.action_id} action={action} onChanged={reload} />
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Panel>
    </main>
  );
}

function Row({ action, onChanged }: { action: MerchantAction; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const [said, setSaid] = useState<string | null>(null);

  const run = useCallback(
    (work: () => Promise<unknown>) => {
      setBusy(true);
      setProblem(null);
      setSaid(null);
      work()
        .then((result) => {
          setBusy(false);
          // `execute` answers 200 either way, so a refusal arrives here rather than in the
          // catch. Reading `ok` is the only way to tell them apart, and printing the
          // server's own reason is better than inventing a sentence for it.
          const outcome = result as { ok?: boolean; reason?: string };
          if (outcome && outcome.ok === false) setSaid(outcome.reason ?? "refused");
          onChanged();
        })
        .catch((cause: unknown) => {
          setBusy(false);
          setProblem(cause);
        });
    },
    [onChanged],
  );

  const kind = KINDS.find((k) => k.key === action.kind);
  const value = kind ? action.proposal[kind.field] : undefined;

  return (
    <>
      <tr>
        <Td className="whitespace-nowrap">
          <span className="text-[var(--ink)]">{kind?.label ?? action.kind}</span>
          <span className="mono mt-0.5 block text-[11px] text-[var(--faint)]">
            {action.target}
          </span>
        </Td>
        <Td>
          {value !== undefined ? (
            <span className="num text-[var(--ink)]">{String(value)}</span>
          ) : (
            <span className="mono text-[11px] text-[var(--muted)]">
              {JSON.stringify(action.proposal)}
            </span>
          )}
          <span className="mt-0.5 block text-[11px] text-[var(--faint)]">
            against revision {action.expected_revision}
          </span>
        </Td>
        <Td>
          <span title={action.content_hash} className="mono text-[11px] text-[var(--muted)]">
            {digest(action.content_hash)}
          </span>
        </Td>
        <Td>
          <Chip tone={toneFor(action.state)}>{action.state}</Chip>
          {action.outcome_note && (
            <span className="mt-1 block max-w-[34ch] text-[11px] text-[var(--muted)]">
              {action.outcome_note}
            </span>
          )}
        </Td>
        <Td>
          <When value={action.created_at} />
          <span className="mt-0.5 block text-[11px] text-[var(--faint)]">
            by <Id value={action.proposed_by} />
          </span>
        </Td>
        <Td align="right">
          <div className="flex justify-end gap-1.5">
            {action.state === "DRAFT" && (
              <Button
                variant="default"
                disabled={busy}
                onClick={() => run(() => api.submitAction(action.action_id))}
              >
                Put it up
              </Button>
            )}
            {action.state === "AWAITING_APPROVAL" && (
              <>
                <Button
                  variant="primary"
                  disabled={busy}
                  // The digest travels with the press. This is the whole guarantee: the
                  // hash sent is the hash rendered in this row, so approving a row that
                  // moved since it was drawn is refused by the server.
                  onClick={() => run(() => api.approveAction(action.action_id, action.content_hash))}
                >
                  Approve this exact change
                </Button>
                <Button
                  variant="default"
                  disabled={busy}
                  onClick={() => run(() => api.rejectAction(action.action_id, "not now"))}
                >
                  Decline
                </Button>
              </>
            )}
            {action.state === "APPROVED" && (
              <Button
                variant="primary"
                disabled={busy}
                onClick={() => run(() => api.executeAction(action.action_id))}
              >
                Make the change
              </Button>
            )}
          </div>
        </Td>
      </tr>
      {(problem || said) && (
        <tr>
          <Td className="!pt-0">
            <div className="col-span-6">
              {problem ? (
                <ProblemPanel error={problem} what={`Action ${digest(action.content_hash)}`} />
              ) : (
                <p role="status" className="text-[12px] text-[var(--warn)]">
                  The platform did not make this change: {said}. Its state above says where
                  that leaves it.
                </p>
              )}
            </div>
          </Td>
        </tr>
      )}
    </>
  );
}

function Propose({ onProposed }: { onProposed: () => void }) {
  const [kind, setKind] = useState(KINDS[0].key);
  const [target, setTarget] = useState("AMUL-DAIRY-001");
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);

  const chosen = KINDS.find((k) => k.key === kind) ?? KINDS[0];
  // Integer or nothing. A price that hashes as a float is a price two verifiers can
  // disagree about, and the server refuses one by name -- so the button is off rather than
  // the request being sent to be told so.
  const parsed = /^\d{1,9}$/.test(value.trim()) ? Number(value.trim()) : null;

  const propose = useCallback(() => {
    if (parsed === null) return;
    setBusy(true);
    setProblem(null);
    api
      .proposeAction({
        kind,
        target: target.trim(),
        proposal: reason.trim()
          ? { [chosen.field]: parsed, reason: reason.trim() }
          : { [chosen.field]: parsed },
      })
      .then(() => {
        setBusy(false);
        setValue("");
        setReason("");
        onProposed();
      })
      .catch((cause: unknown) => {
        setBusy(false);
        setProblem(cause);
      });
  }, [chosen.field, kind, onProposed, parsed, reason, target]);

  return (
    <Panel title="Propose a change" subtitle="It is drafted, not applied. Nothing moves yet.">
      <div className="grid gap-3 p-4 sm:grid-cols-[200px_180px_1fr]">
        <label className="block">
          <span className="block text-[11.5px] font-medium text-[var(--muted)]">What</span>
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] px-2 py-1.5 text-[12.5px] text-[var(--ink)]"
          >
            {KINDS.map((option) => (
              <option key={option.key} value={option.key}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="block text-[11.5px] font-medium text-[var(--muted)]">Which product</span>
          <input
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            className="mono mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] px-2 py-1.5 text-[12px] text-[var(--ink)]"
          />
        </label>
        <label className="block">
          <span className="block text-[11.5px] font-medium text-[var(--muted)]">
            New value
          </span>
          <input
            value={value}
            onChange={(event) => setValue(event.target.value)}
            inputMode="numeric"
            placeholder="2800"
            className="num mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] px-2 py-1.5 text-[12.5px] text-[var(--ink)]"
          />
          <span className="mt-1 block text-[11px] text-[var(--faint)]">{chosen.hint}</span>
        </label>
      </div>
      <div className="grid gap-3 px-4 pb-4">
        <label className="block">
          <span className="block text-[11.5px] font-medium text-[var(--muted)]">
            Why (stored on the proposal, read by whoever approves)
          </span>
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Supplier raised the wholesale price."
            className="mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] px-2 py-1.5 text-[12.5px] text-[var(--ink)]"
          />
        </label>
        <div>
          <Button variant="primary" disabled={busy || parsed === null} onClick={propose}>
            Draft this change
          </Button>
        </div>
        {problem ? <ProblemPanel error={problem} what="POST /v1/merchant/actions" /> : null}
      </div>
    </Panel>
  );
}

/** Kept for the detail view a later screen will want; unused fields are not rendered. */
export function ActionDetail({ action }: { action: MerchantAction }) {
  return (
    <Panel title={`${action.kind} on ${action.target}`}>
      <dl>
        <Field label="Digest" source="merchant_actions.content_hash">
          <span className="mono">{action.content_hash}</span>
        </Field>
        <Field label="Proposed by" source="merchant_actions.proposed_by">
          <Id value={action.proposed_by} />
        </Field>
        <Field label="Approved by" source="merchant_actions.approved_by">
          {action.approved_by ? (
            <Id value={action.approved_by} />
          ) : (
            <span className="text-[var(--faint)]">Nobody has agreed to it.</span>
          )}
        </Field>
      </dl>
    </Panel>
  );
}

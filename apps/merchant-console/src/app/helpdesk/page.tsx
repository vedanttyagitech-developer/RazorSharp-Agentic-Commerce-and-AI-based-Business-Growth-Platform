/**
 * The helpdesk: what buyers have raised, and the place a person answers them.
 *
 * The buyer's order screen says, in words, that a disputed order goes to somebody who
 * decides what they are owed. This is that somebody's screen. Until it existed the case
 * was written to a table nobody read, and the promise was true about intent and false
 * about machinery -- which is the kind of false that survives a demonstration.
 *
 * **No amount, anywhere on this page.** Not shown, not typed, not computed. The API this
 * reads carries none in either direction, and that is deliberate rather than pending: a
 * figure attached to a case is a figure that was true once, and a refund admitted in the
 * meantime moves it. What is still refundable is a question for the kernel at the moment
 * somebody asks, through the same route the buyer's own screen reads, so the two people
 * looking at one order cannot be shown different numbers.
 *
 * **And no refund from here.** Answering a case records that a person dealt with it. The
 * money goes back through the refund route and the kernel's admission against its own
 * ledger, under a capability this console does not hold. A queue that could also pay out
 * would be a second way to move money with none of the ledger's arithmetic behind it.
 *
 * What this page decides: nothing. Every control sends a real request and renders the real
 * answer, including "no". A case that cannot make the move is refused by the server with
 * the moves it could have made, and that refusal is drawn as an answer rather than as an
 * error -- because it is one. Two people on one queue is ordinary, and the 409 is what
 * stops the second press quietly overwriting the first person's work.
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
import type { SupportCase } from "@/lib/api/types";
import { useRead } from "@/lib/useRead";

/**
 * The states a case can be in, in the order it moves through them.
 *
 * `ALL` is first because a helpdesk that opens on a filter is a helpdesk that hides work.
 * The counts beside each are not shown, and that absence is on purpose: this page reads one
 * filtered list, so any number it printed beside the others would be invented.
 */
const FILTERS: ReadonlyArray<{ key: string; label: string }> = [
  { key: "", label: "Everything" },
  { key: "OPEN", label: "Waiting" },
  { key: "ACKNOWLEDGED", label: "Being handled" },
  { key: "RESOLVED", label: "Answered" },
  { key: "CLOSED", label: "Closed" },
];

/**
 * What each reason key says in a sentence a person reads.
 *
 * The same five keys the buyer's own screen offers. A key with no entry renders as itself
 * rather than as a blank: an unknown reason is a fact about a case, and a helpdesk that
 * silently drops it shows the person answering less than the buyer typed.
 */
const REASONS: Readonly<Record<string, string>> = {
  buyer_requested: "Asked for a refund",
  item_not_delivered: "Never arrived",
  item_damaged: "Arrived damaged",
  wrong_item: "Not what they ordered",
  ordered_by_mistake: "Ordered by mistake",
};

/**
 * The moves the server permits from each state. Mirrored here to label the buttons only.
 *
 * `back: true` marks the ones that take a move back. They are labelled as corrections
 * rather than as steps, drawn quietly rather than as the obvious next thing, and the
 * server refuses them without a reason -- because a case that is ACKNOWLEDGED again looks
 * exactly like one nobody ever answered, and the note is the only thing that says which.
 */
const MOVES: Readonly<
  Record<string, ReadonlyArray<{ to: string; label: string; back?: boolean }>>
> = {
  OPEN: [
    { to: "ACKNOWLEDGED", label: "Pick this up" },
    { to: "CLOSED", label: "Close without answering" },
  ],
  ACKNOWLEDGED: [
    { to: "RESOLVED", label: "Answer it" },
    { to: "CLOSED", label: "Close without answering" },
    { to: "OPEN", label: "Put it back in the queue", back: true },
  ],
  RESOLVED: [
    { to: "CLOSED", label: "Close" },
    { to: "ACKNOWLEDGED", label: "Reopen — it was not answered", back: true },
  ],
  CLOSED: [{ to: "ACKNOWLEDGED", label: "Reopen — closed by mistake", back: true }],
};

/**
 * A tone per state, and not `toneForState` from the kit.
 *
 * That one maps the platform's own money states, where `CLOSED` is not a word and `OPEN`
 * would fall through to neutral. Here `OPEN` is the one that should catch the eye, because
 * it is the only state in which somebody is waiting.
 */
function toneFor(status: string): Tone {
  if (status === "OPEN") return "warn";
  if (status === "ACKNOWLEDGED") return "info";
  if (status === "RESOLVED") return "positive";
  return "muted";
}

export default function HelpdeskPage() {
  const [filter, setFilter] = useState("OPEN");
  const queue = useRead(
    (signal) => api.supportQueue({ status: filter || undefined, signal }),
    [filter],
  );
  const [openCase, setOpenCase] = useState<string | null>(null);

  return (
    <main className="mx-auto flex min-h-dvh max-w-5xl flex-col gap-5 px-6 py-10">
      <header>
        <p className="mono text-[11px] uppercase tracking-[0.14em] text-[var(--faint)]">
          Merchant workspace
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight text-[var(--ink)]">
          Helpdesk
        </h1>
        <p className="mt-2 max-w-[70ch] text-[13px] leading-relaxed text-[var(--muted)]">
          What buyers have raised about their orders, oldest first. This is where a case
          from the storefront arrives and where somebody answers it. It carries no amount
          and it moves no money: what is still refundable is the platform&rsquo;s figure,
          asked for when it is needed, and a refund goes back through the refund path.
        </p>
      </header>

      <Panel
        title="The queue"
        subtitle="Oldest first, because the case that has waited longest is the one somebody is owed an answer on."
        actions={
          <div className="flex flex-wrap gap-1.5">
            {FILTERS.map((option) => (
              <button
                key={option.key || "all"}
                type="button"
                onClick={() => {
                  setFilter(option.key);
                  setOpenCase(null);
                }}
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
        {queue.loading ? (
          <Loading label="Reading the support queue" />
        ) : queue.error ? (
          <div className="p-4">
            <ProblemPanel
              error={queue.error}
              what="GET /v1/support/cases"
              onRetry={queue.reload}
            />
          </div>
        ) : !queue.data || queue.data.cases.length === 0 ? (
          <Empty>
            Nothing here.{" "}
            {filter
              ? "No case is in this state right now."
              : "No buyer has raised a case on this tenant."}
          </Empty>
        ) : (
          <>
            <TableWrap>
              <table className="w-full border-collapse text-[12.5px]">
                <thead>
                  <tr>
                    <Th>Order</Th>
                    <Th>What they said</Th>
                    <Th>Raised</Th>
                    <Th>State</Th>
                    <Th>With</Th>
                    <Th>
                      <span className="sr-only">Open the case</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {queue.data.cases.map((entry) => (
                    <tr key={entry.case_id}>
                      <Td className="whitespace-nowrap">
                        <span className="mono text-[var(--ink)]">{entry.order_reference}</span>
                      </Td>
                      <Td>
                        <span className="text-[var(--ink)]">
                          {REASONS[entry.reason] ?? entry.reason}
                        </span>
                        {entry.opened_by === "AGENT" && (
                          <span className="ml-2 align-middle">
                            {/* Who wrote it changes how much weight it carries. */}
                            <Chip tone="muted">VIA COPILOT</Chip>
                          </span>
                        )}
                        {entry.note && (
                          <p className="mt-1 max-w-[52ch] text-[11.5px] text-[var(--muted)]">
                            &ldquo;{entry.note}&rdquo;
                          </p>
                        )}
                      </Td>
                      <Td>
                        <When value={entry.created_at} />
                      </Td>
                      <Td>
                        <Chip tone={toneFor(entry.status)}>{entry.status}</Chip>
                      </Td>
                      <Td>
                        {entry.handled_by ? (
                          <Id value={entry.handled_by} />
                        ) : (
                          <span className="text-[var(--faint)]">nobody yet</span>
                        )}
                      </Td>
                      <Td align="right">
                        <Button
                          variant="ghost"
                          onClick={() =>
                            setOpenCase(openCase === entry.case_id ? null : entry.case_id)
                          }
                        >
                          {openCase === entry.case_id ? "Close" : "Open"}
                        </Button>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
            {queue.data.may_have_more && (
              <p className="border-t border-[var(--line)] px-4 py-2 text-[11.5px] text-[var(--faint)]">
                This page is full, so there may be more behind it. The count above is what
                came back, not how many exist.
              </p>
            )}
          </>
        )}
      </Panel>

      {openCase && (
        <CasePanel
          caseId={openCase}
          onChanged={() => {
            queue.reload();
          }}
        />
      )}
    </main>
  );
}

type Outcome =
  | { kind: "none" }
  | { kind: "moved"; status: string }
  | { kind: "refused"; error: unknown };

/**
 * One case, and the moves a person can make on it.
 *
 * The buttons are drawn from the case's own state, and the server decides anyway. That is
 * not redundancy: this page can be a few seconds stale, somebody else may have picked the
 * case up in the meantime, and the 409 that comes back is the correct answer rather than a
 * fault. It is rendered as an answer, with the moves that *were* possible, so the person
 * reading learns what happened instead of being told to try again.
 */
function CasePanel({ caseId, onChanged }: { caseId: string; onChanged: () => void }) {
  const detail = useRead((signal) => api.supportCase(caseId, signal), [caseId]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>({ kind: "none" });

  const move = useCallback(
    (to: string) => {
      setBusy(true);
      setOutcome({ kind: "none" });
      api
        .advanceCase(caseId, { status: to, note })
        .then((moved) => {
          setBusy(false);
          setNote("");
          setOutcome({ kind: "moved", status: moved.status });
          detail.reload();
          onChanged();
        })
        .catch((cause: unknown) => {
          setBusy(false);
          // Refused, not failed. The panel below prints the server's own words.
          setOutcome({ kind: "refused", error: cause });
          detail.reload();
        });
    },
    [caseId, note, detail, onChanged],
  );

  if (detail.loading) return <Loading label="Reading the case" />;
  if (detail.error)
    return (
      <ProblemPanel
        error={detail.error}
        what={`GET /v1/support/cases/${caseId}`}
        onRetry={detail.reload}
      />
    );
  if (!detail.data) return null;

  const one: SupportCase = detail.data;
  const moves = MOVES[one.status] ?? [];

  return (
    <Panel
      title={`Case on ${one.order_reference}`}
      subtitle="Everything the buyer said, and nothing about what they are owed."
      actions={<Chip tone={toneFor(one.status)}>{one.status}</Chip>}
    >
      <dl>
        <Field label="Order" source="orders.id">
          <Id value={one.order_id} />
        </Field>
        <Field label="Reason" source="support_cases.reason_code">
          {REASONS[one.reason] ?? one.reason}
        </Field>
        <Field label="What the buyer said" source="support_cases.note">
          {one.note ? (
            <span className="whitespace-pre-wrap">{one.note}</span>
          ) : (
            <span className="text-[var(--faint)]">They wrote nothing.</span>
          )}
        </Field>
        <Field label="Raised by" source="support_cases.opened_by">
          {one.opened_by === "AGENT"
            ? "The copilot, on the buyer's behalf"
            : "The buyer, on the order screen"}
        </Field>
        <Field label="Raised" source="support_cases.created_at">
          <When value={one.created_at} />
        </Field>
        <Field label="With" source="support_cases.handled_by">
          {one.handled_by ? (
            <Id value={one.handled_by} />
          ) : (
            <span className="text-[var(--faint)]">Nobody has picked it up.</span>
          )}
        </Field>
        <Field label="The answer given" source="support_cases.resolution_note">
          {one.resolution_note ? (
            <span className="whitespace-pre-wrap">{one.resolution_note}</span>
          ) : (
            <span className="text-[var(--faint)]">Nothing recorded yet.</span>
          )}
        </Field>
      </dl>

      <div className="border-t border-[var(--line)] p-4">
        {moves.length === 0 ? (
          <p className="text-[12px] text-[var(--muted)]">
            This case is closed. There is nothing further to do on it here, and reopening it
            is not something this screen can do quietly &mdash; the server refuses the move.
          </p>
        ) : (
          <>
            <label
              htmlFor="case-note"
              className="block text-[11.5px] font-medium text-[var(--muted)]"
            >
              What you decided
            </label>
            <p className="mt-0.5 text-[11px] text-[var(--faint)]">
              Stored on the case and shown to whoever reads it next. Words, not figures:
              there is no amount field here and the server would refuse one.
            </p>
            <textarea
              id="case-note"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              rows={3}
              maxLength={1000}
              placeholder="Both bottles refunded and collection arranged."
              className="mt-2 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] px-2.5 py-2 text-[12.5px] text-[var(--ink)]"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {moves.map((option) => (
                <Button
                  key={option.to}
                  // A correction is drawn quietly. Putting "reopen" in the same weight as
                  // "answer it" would make undoing look like the next step rather than
                  // what it is.
                  variant={option.back ? "ghost" : option.to === "CLOSED" ? "default" : "primary"}
                  disabled={busy || (option.back === true && note.trim() === "")}
                  title={
                    option.back && note.trim() === ""
                      ? "Say why in the box above. A reversal with no reason cannot be told from a mistake."
                      : undefined
                  }
                  onClick={() => move(option.to)}
                >
                  {option.label}
                </Button>
              ))}
            </div>
          </>
        )}

        {outcome.kind === "moved" && (
          <p role="status" className="mt-3 text-[12px] text-[var(--positive)]">
            Recorded. The case is now {outcome.status}, and your name is on it.
          </p>
        )}
        {outcome.kind === "refused" && (
          <div className="mt-3">
            <ProblemPanel
              error={outcome.error}
              what={`POST /v1/support/cases/${caseId}/advance`}
            />
          </div>
        )}
      </div>
    </Panel>
  );
}

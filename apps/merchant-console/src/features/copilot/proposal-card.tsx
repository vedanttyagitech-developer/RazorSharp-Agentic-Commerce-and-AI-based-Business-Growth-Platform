"use client";

/**
 * A growth proposal, and the press that applies it.
 *
 * This card is the whole argument of the merchant copilot in one object. The agent named a
 * change; it did not make one. Specification 6.6 is flat about that -- "an agent proposal
 * cannot directly change price, stock, discount, fee, campaign budget, refund rule or
 * financial authority" -- and the reason is not ceremony: an agent that could change a
 * price is an agent that can lose a merchant money without being asked.
 *
 * So the change lives on this card as data until a person presses apply, and three rules
 * hold the press honest:
 *
 *  - **The request is shown before it is sent.** The exact endpoint and the exact bytes.
 *    A merchant pressing a button is entitled to know what the button sends, and the
 *    client method behind it (`api.applyProposedChange`) composes nothing of its own so
 *    that what is drawn here is what goes on the wire.
 *  - **The applied state comes from the server's answer.** Not from the press. The card
 *    reads applied only while it is holding the injection the API returned, with its
 *    revision, its deltas and its audit event id. A press that fails renders the failure
 *    and leaves the card unapplied, because a card that looked applied over a request that
 *    never landed would tell a merchant their shelf is restocked when it is not.
 *  - **One endpoint, and only one.** The proposal's change goes to the scenario controller,
 *    which applies it under the merchant simulator's lock and audits it in the same
 *    transaction. This console never writes a merchant table, and a proposal naming any
 *    other route gets no button at all -- see `applyBlockedReason`.
 *
 * Nothing here computes a figure. The four amounts a discount proposal must carry arrive
 * as four server-supplied integers and are rendered as four; a margin this card derived
 * would be a number the platform never stood behind.
 */
import { useRef, useState } from "react";

import { Button, Chip, Field, Panel, Spinner, When } from "@/components/ui";
import { api } from "@/lib/api/client";
import { extensionsOf, problemOf } from "@/lib/api/problem";
import { formatMinor } from "@/lib/money";
import type { Injection } from "@/lib/api/types";

import {
  APPLY_ENDPOINT,
  amountLabel,
  applyBlockedReason,
  leverName,
  type Proposal,
} from "./payloads";

/** Where the press has got to. `applied` exists only while it holds the API's answer. */
type Press =
  | { state: "idle" }
  | { state: "sending" }
  | { state: "applied"; injection: Injection }
  | { state: "failed"; error: unknown };

function StatedOr({ value }: { value: string | number | null | undefined }) {
  if (value === null || value === undefined || value === "") {
    return <span className="mono text-[var(--warn)]">not stated</span>;
  }
  return <span className="break-id">{value}</span>;
}

/**
 * A press that did not land, drawn as the problem document it was.
 *
 * `ProblemPanel` is the console's own rendering of a failed read and says so in its
 * heading, which is the right words on every other screen and the wrong ones here: this
 * was a write, and telling a merchant a read failed while their change may or may not have
 * been applied is a worse sentence than saying nothing. So the same information -- status,
 * title, detail and every extension member the API attached -- is drawn under a heading
 * that describes what actually happened.
 */
function ApplyFailure({ error }: { error: unknown }) {
  const problem = problemOf(error);
  const extensions = extensionsOf(problem);
  return (
    <div
      role="alert"
      aria-live="assertive"
      className="border-t border-[var(--line)] bg-[color-mix(in_srgb,var(--danger)_8%,transparent)] p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="danger">NOT APPLIED</Chip>
        <span className="text-[12px] text-[var(--muted)]">the platform refused this change</span>
      </div>
      <p className="mt-2 text-[13px] font-semibold text-[var(--ink)]">
        <span className="num mr-2 text-[var(--danger)]">{problem.status}</span>
        {problem.title}
      </p>
      {problem.detail && (
        <p className="mt-1 text-[12px] text-[var(--muted)] break-id">{problem.detail}</p>
      )}
      {extensions.length > 0 && (
        <dl className="mono mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[var(--faint)]">
          {extensions.map(([key, value]) => (
            <div key={key} className="contents">
              <dt>{key}</dt>
              <dd className="text-[var(--muted)] break-id">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="mt-3 text-[11.5px] text-[var(--muted)]">
        Nothing above has moved and this is still a proposal. The change is unapplied until the
        platform answers a press with an injection, and it has not.
      </p>
    </div>
  );
}

/**
 * The injection the API returned, drawn as the evidence that the change actually happened.
 *
 * The deltas are the platform's own before/after pairs and the revision pair is how any
 * other page in this console knows its catalogue read has gone stale. The audit event id
 * is here so the merchant can leave this panel and go verify the write against the hash
 * chain rather than taking the card's word for it.
 */
function AppliedEvidence({ injection }: { injection: Injection }) {
  return (
    <div className="border-t border-[var(--line)] bg-[color-mix(in_srgb,var(--positive)_7%,transparent)]">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <Chip tone="positive">APPLIED BY YOU</Chip>
        <span className="text-[12px] text-[var(--muted)]">
          the platform answered, and this is its answer
        </span>
      </div>
      <dl>
        <Field label="Injection" source="injection_id">
          <span className="mono break-id">{injection.injection_id}</span>
        </Field>
        <Field label="Label" source="label">
          {injection.label}
        </Field>
        <Field label="Catalogue revision" source="revision_before → revision_after">
          <span className="num">
            {injection.revision_before} → {injection.revision_after}
          </span>
        </Field>
        <Field label="What moved" source="deltas">
          {injection.deltas.length === 0 ? (
            <span className="text-[var(--muted)]">The platform reported no change.</span>
          ) : (
            <ul className="space-y-0.5">
              {injection.deltas.map((delta) => (
                <li key={delta.field} className="mono">
                  {delta.field}: {String(delta.before)} → {String(delta.after)}
                </li>
              ))}
            </ul>
          )}
        </Field>
        <Field label="Applied at" source="injected_at">
          <When value={injection.injected_at} />
        </Field>
        <Field label="Audit event" source="audit_event_id">
          <span className="mono break-id">{injection.audit_event_id}</span>
        </Field>
      </dl>
      <p className="px-4 py-2 text-[11.5px] text-[var(--faint)]">
        Every page in this console reading the catalogue is now behind by one revision. Re-read
        them to see this change.
      </p>
    </div>
  );
}

export function ProposalCard({ proposal }: { proposal: Proposal }) {
  const [press, setPress] = useState<Press>({ state: "idle" });
  const inFlight = useRef<AbortController | null>(null);
  const blocked = applyBlockedReason(proposal);
  const body = proposal.change.body;
  const request = JSON.stringify(body, null, 2);
  const money = Object.entries(proposal.money ?? {});

  async function apply() {
    if (blocked !== null || press.state === "sending") return;
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    setPress({ state: "sending" });
    try {
      const injection = await api.applyProposedChange(body, controller.signal);
      setPress({ state: "applied", injection });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      // Nothing is painted applied. The failure is what the merchant sees, and the change
      // above it is still a proposal, because that is what it still is.
      setPress({ state: "failed", error });
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
    }
  }

  return (
    <Panel
      className="mt-2.5 border-l-2 border-l-[var(--brand)]"
      title={
        <span className="flex flex-wrap items-center gap-2">
          <Chip tone="info">PROPOSAL</Chip>
          <span>{proposal.title}</span>
        </span>
      }
      subtitle={
        <>
          The copilot proposes. It changed nothing, and nothing changes until you press apply.
        </>
      }
      actions={<span className="mono text-[var(--faint)] break-id">{proposal.proposal_id}</span>}
    >
      <p className="px-4 py-3 text-[12.5px] leading-[1.55] text-[var(--ink)]">
        {proposal.rationale}
      </p>

      <dl className="border-t border-[var(--line)]">
        <Field label="Revenue lever" source="specification 9.1">
          <span className="flex flex-wrap items-center gap-2">
            <Chip tone="info">{leverName(proposal.lever)}</Chip>
            <span className="mono text-[var(--faint)]">{proposal.lever}</span>
          </span>
        </Field>
        <Field label="What would move" source="9.1 measurement">
          <StatedOr value={proposal.metric} />
        </Field>
        <Field label="Deterministic gate" source="9.1 gate">
          <StatedOr value={proposal.gate} />
        </Field>
        <Field label="Evidence" source="6.6 source, window, sample">
          <span className="flex flex-wrap items-center gap-1.5">
            <Chip tone={proposal.evidence.synthetic ? "warn" : "muted"}>
              {proposal.evidence.synthetic ? "synthetic data" : "live data"}
            </Chip>
            <Chip tone="muted">source {proposal.evidence.source}</Chip>
            <Chip tone="muted">window {proposal.evidence.window ?? "not stated"}</Chip>
            <Chip tone="muted">
              sample{" "}
              {proposal.evidence.sample_size === null || proposal.evidence.sample_size === undefined
                ? "not stated"
                : proposal.evidence.sample_size}
            </Chip>
            {proposal.evidence.catalogue_revision !== null &&
              proposal.evidence.catalogue_revision !== undefined && (
                <Chip tone="muted">catalogue revision {proposal.evidence.catalogue_revision}</Chip>
              )}
          </span>
        </Field>
        <Field label="Read by" source="evidence.read_by">
          {proposal.evidence.read_by.length === 0 ? (
            <span className="text-[var(--warn)]">
              No tool is named. There is no evidence behind this proposal.
            </span>
          ) : (
            <span className="flex flex-wrap gap-1.5">
              {proposal.evidence.read_by.map((tool) => (
                <Chip key={tool} tone="muted">
                  {tool}
                </Chip>
              ))}
            </span>
          )}
        </Field>
        <Field label="Reversible" source="change.reversible">
          {proposal.change.reversible === null || proposal.change.reversible === undefined ? (
            <Chip tone="warn">not stated</Chip>
          ) : (
            <Chip tone={proposal.change.reversible ? "positive" : "warn"}>
              {proposal.change.reversible ? "yes" : "no"}
            </Chip>
          )}
        </Field>
        <Field label="Applied where" source="where">
          <StatedOr value={proposal.where} />
        </Field>
      </dl>

      {money.length > 0 && (
        <div className="border-t border-[var(--line)] px-4 py-3">
          <p className="eyebrow">Amounts the platform derived</p>
          <dl className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {money.map(([key, amount]) => (
              <div
                key={key}
                className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3 py-2"
              >
                <dt className="eyebrow">{amountLabel(key)}</dt>
                <dd className="num mt-1 text-[15px] text-[var(--ink)]">
                  {amount.display
                    ? `${amount.currency} ${amount.display}`
                    : formatMinor(amount.minor, amount.currency)}
                </dd>
                <dd className="mono mt-0.5 text-[var(--faint)]">
                  {amount.minor} minor · {amount.currency}
                </dd>
              </div>
            ))}
          </dl>
          {/*
            Four figures, four server integers. 6.6 asks for gross revenue, discount cost
            and net captured and retained revenue separately for a reason a console is well
            placed to break: the moment one of them is a subtraction performed here, it is
            a number the platform never stood behind and would not reproduce.
          */}
          <p className="mt-2 text-[11.5px] text-[var(--muted)]">
            Each figure is a separate integer the platform supplied, in minor units. Nothing on
            this card is a difference or a rate computed in your browser.
          </p>
        </div>
      )}

      <div className="border-t border-[var(--line)] px-4 py-3">
        <p className="eyebrow">The request this will send</p>
        <pre className="mono mt-1.5 overflow-x-auto rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] p-3 text-[var(--ink)]">
          {proposal.change.endpoint}
          {"\n"}
          {request}
        </pre>
        <p className="mt-1.5 text-[11.5px] text-[var(--muted)]">
          That body is sent exactly as written, and nothing else is sent with it. The scenario
          controller applies it under the merchant simulator&rsquo;s lock and audits it as a
          labelled injection in the same transaction. This console never writes a merchant table.
        </p>
        {proposal.change.reverses_to && (
          <>
            <p className="eyebrow mt-3">What reverses it</p>
            <pre className="mono mt-1.5 overflow-x-auto rounded-[var(--r-md)] border border-dashed border-[var(--line)] bg-[var(--raised)] p-3 text-[var(--muted)]">
              {JSON.stringify(proposal.change.reverses_to, null, 2)}
            </pre>
            <p className="mt-1.5 text-[11.5px] text-[var(--muted)]">
              Carried as data so you can undo this yourself through the same endpoint. There is no
              button for it here: an undo the copilot could press is a change the copilot can make.
            </p>
          </>
        )}
      </div>

      {/*
        The proposal envelope carries `applied`, and the agent must always send it false --
        only a person changes it. A true arriving from the agent is a contract violation
        rather than a state, so it is named as one and never allowed to colour the card.
      */}
      {proposal.applied === true && press.state !== "applied" && (
        <div className="border-t border-[var(--line)] bg-[color-mix(in_srgb,var(--warn)_10%,transparent)] px-4 py-3">
          <Chip tone="warn">IGNORED CLAIM</Chip>
          <p className="mt-1.5 text-[12px] text-[var(--ink)]">
            This proposal arrived with <span className="mono">applied: true</span>. Only a person
            applies a proposal, so the claim is ignored: nothing on this card is drawn as applied
            unless the platform answered a press with an injection.
          </p>
        </div>
      )}

      <div className="border-t border-[var(--line)] px-4 py-3">
        {blocked !== null ? (
          <div className="rounded-[var(--r-md)] border border-[color-mix(in_srgb,var(--warn)_45%,transparent)] bg-[color-mix(in_srgb,var(--warn)_10%,transparent)] p-3">
            <Chip tone="warn">NOT APPLICABLE HERE</Chip>
            <p className="mt-1.5 text-[12px] leading-[1.5] text-[var(--ink)]">{blocked}</p>
            <p className="mono mt-1 text-[var(--faint)] break-id">
              this console applies only {APPLY_ENDPOINT}
            </p>
          </div>
        ) : press.state === "applied" ? (
          <p className="text-[12px] text-[var(--muted)]">
            This proposal has been applied. It is a record of what you did, not a control any more.
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={() => void apply()} disabled={press.state === "sending"}>
              Apply this change
            </Button>
            {press.state === "sending" ? (
              <Spinner label="Sending to the scenario controller" />
            ) : (
              <span className="text-[11.5px] text-[var(--muted)]">
                You are the one applying it. The copilot cannot.
              </span>
            )}
          </div>
        )}
      </div>

      {press.state === "failed" && <ApplyFailure error={press.error} />}

      {press.state === "applied" && <AppliedEvidence injection={press.injection} />}
    </Panel>
  );
}

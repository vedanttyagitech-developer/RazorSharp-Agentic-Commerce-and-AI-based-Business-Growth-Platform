"use client";

/**
 * The screen the whole studio exists for: what this assistant can do, and what it cannot.
 *
 * Every list, every count and the sentence at the top are computed in `boundary.ts` from
 * `GET /v1/agent/capabilities` and a composition that can only narrow it. Nothing on this
 * screen is written in advance. That is the point: a paragraph promising a merchant that
 * their assistant cannot take a payment is worth very little, and the same claim rendered
 * from the capability set the gate is built from is worth a great deal, because it goes
 * wrong the moment the platform changes and the merchant sees it go wrong.
 *
 * Four rings, drawn in order of how far each one is from the merchant's control, because
 * that is the honest order to read them in: what you chose, what you can still choose,
 * what the roster chose, and what nobody chooses.
 *
 * One thing this screen refuses to do is overclaim. `absent_by_construction` is the API's
 * list of consent verbs, not a complete catalogue of everything an agent may not do, and
 * the last section says so. A screen that presented three verbs as the whole of the
 * boundary would be inviting exactly the objection it exists to answer.
 */
import { Chip, Panel, cx } from "@/components/ui";

import type { Boundary, CapabilityRow } from "./boundary";
import { capabilityLabel, capabilityVerb, roleTitle } from "./vocabulary";

/** English for a list, with the Oxford comma this codebase's prose already uses. */
function sentenceList(items: readonly string[]): string {
  if (items.length === 0) return "";
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

function CapabilityLine({ row, muted }: { row: CapabilityRow; muted?: boolean }) {
  return (
    <li className="border-b border-[var(--line-soft)] px-4 py-2.5 last:border-0">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span
          className={cx(
            "text-[12.5px] font-semibold",
            muted ? "text-[var(--muted)]" : "text-[var(--ink)]",
          )}
        >
          {row.label}
        </span>
        <span className="mono text-[var(--faint)] break-id">{row.capability}</span>
        {!row.known && <Chip tone="warn">not described here</Chip>}
        {row.known && !row.toolNamed && <Chip tone="muted">no tool under this name</Chip>}
      </div>
      {row.detail && (
        <p className="mt-1 max-w-[70ch] text-[11.5px] leading-[1.5] text-[var(--muted)]">
          {row.detail}
        </p>
      )}
    </li>
  );
}

export function BoundaryPanel({
  boundary,
  name,
}: {
  boundary: Boundary;
  /** What the merchant called this assistant, or empty. */
  name: string;
}) {
  const subject = name.trim() === "" ? `Your ${roleTitle(boundary.role.specialist)} assistant` : name.trim();
  const reads = boundary.granted.filter((row) => row.kind === "read");
  const proposes = boundary.granted.filter((row) => row.kind === "propose");
  const undescribed = boundary.granted.filter((row) => row.kind === "unclassified");

  // The refusals in the sentence are the server's own `absent_by_construction`, put into
  // English by the same table the lists below use. Writing the verbs out here instead
  // would have read identically today and stopped being true the moment the API changed
  // its mind, which is the one failure this screen must not have.
  const never = boundary.neverOnAnyAgent.map(capabilityVerb);
  const can = [...reads, ...proposes].map((row) => capabilityVerb(row.capability));

  return (
    <Panel
      title="What this assistant can and cannot do"
      subtitle="Every line below is a set operation over the capability document the gate is built from."
      actions={
        <span className="mono text-[var(--faint)] break-id">
          {boundary.counts.read} read · {boundary.counts.propose} propose ·{" "}
          {boundary.counts.unclassified} undescribed
        </span>
      }
    >
      {/* ---------------------------------------------------------------- the sentence */}
      <div className="border-b border-[var(--line)] bg-[var(--ink)] px-4 py-4">
        <p className="text-[14px] leading-[1.6] text-[#f0f4f6]">
          <span className="font-semibold">{subject}</span>{" "}
          {can.length === 0 ? (
            <>holds no capability at all: every question it is asked would be answered from nothing.</>
          ) : (
            <>can {sentenceList(can)}.</>
          )}{" "}
          {undescribed.length > 0 && (
            <>
              It also holds {sentenceList(undescribed.map((row) => row.capability))}, which this
              console has no description for.{" "}
            </>
          )}
          <span className="text-[#c4d2e0]">
            {never.length > 0 ? (
              <>It can never {sentenceList(never)}</>
            ) : (
              <>Its limits are the capabilities above and nothing else</>
            )}
            , and there is no control on this page, or on any page of this console, that changes
            that.
          </span>
        </p>
        <p className="mono mt-2 text-[#7b8a9a] break-id">
          principal {boundary.role.principal_id}
        </p>
      </div>

      {/* ------------------------------------------------------------------- ring one */}
      <section aria-label="What it can do">
        <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-[var(--line)] bg-[var(--raised)] px-4 py-2">
          <h3 className="text-[12px] font-semibold text-[var(--ink)]">What it can do</h3>
          <p className="text-[11px] text-[var(--muted)]">you switched these on</p>
        </header>
        {boundary.granted.length === 0 ? (
          <p className="px-4 py-4 text-[12px] text-[var(--warn)]">
            Nothing. Every capability the platform offers this role is switched off, so the
            assistant would answer every question without reading anything.
          </p>
        ) : (
          <ul>
            {boundary.granted.map((row) => (
              <CapabilityLine key={row.capability} row={row} />
            ))}
          </ul>
        )}
      </section>

      {/* ------------------------------------------------------------------- ring two */}
      {boundary.disabled.length > 0 && (
        <section aria-label="What you switched off">
          <header className="flex flex-wrap items-baseline justify-between gap-2 border-y border-[var(--line)] bg-[var(--raised)] px-4 py-2">
            <h3 className="text-[12px] font-semibold text-[var(--ink)]">What you switched off</h3>
            <p className="text-[11px] text-[var(--muted)]">yours to switch back on</p>
          </header>
          <ul>
            {boundary.disabled.map((row) => (
              <CapabilityLine key={row.capability} row={row} muted />
            ))}
          </ul>
        </section>
      )}

      {/* ----------------------------------------------------------------- ring three */}
      {boundary.elsewhereOnTheSurface.length > 0 && (
        <section aria-label="What this role never carries">
          <header className="border-y border-[var(--line)] bg-[var(--raised)] px-4 py-2">
            <h3 className="text-[12px] font-semibold text-[var(--ink)]">
              What {roleTitle(boundary.role.specialist)} never carries
            </h3>
            <p className="mt-0.5 text-[11px] text-[var(--muted)]">
              The platform binds these to a different specialist. Not a setting — the roster
              decides which specialist holds what, and one cannot borrow from another.
            </p>
          </header>
          <ul className="px-4 py-3">
            {boundary.elsewhereOnTheSurface.map((entry) => (
              <li key={entry.role} className="mb-2 last:mb-0">
                <p className="eyebrow">{roleTitle(entry.role)} holds</p>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {entry.capabilities.map((row) => (
                    <Chip key={row.capability} tone="muted">
                      {row.label}
                    </Chip>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ------------------------------------------------------------------ ring four */}
      <section aria-label="What no agent may ever hold">
        <header className="border-y border-[var(--line)] bg-[color-mix(in_srgb,var(--brand)_7%,transparent)] px-4 py-2">
          <h3 className="text-[12px] font-semibold text-[var(--ink)]">
            What no agent may ever hold
          </h3>
          <p className="mt-0.5 text-[11px] text-[var(--muted)]">
            Named by the server, in <span className="mono">absent_by_construction</span>. No
            merchant setting reaches this list, and no message to the assistant does either.
          </p>
        </header>
        <ul>
          {boundary.neverOnAnyAgent.map((capability) => (
            <li
              key={capability}
              className="flex flex-wrap items-baseline gap-x-2 gap-y-1 border-b border-[var(--line-soft)] px-4 py-2.5 last:border-0"
            >
              <span aria-hidden="true" className="text-[var(--faint)]">
                ⊘
              </span>
              <span className="text-[12.5px] font-semibold text-[var(--ink)]">
                {capabilityLabel(capability)}
              </span>
              <span className="mono text-[var(--faint)] break-id">{capability}</span>
            </li>
          ))}
        </ul>

        {boundary.withheldFromEveryAgent.length > 0 && (
          <div className="border-t border-[var(--line)] px-4 py-3">
            <p className="eyebrow">Your session holds these; no agent gets them</p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {boundary.withheldFromEveryAgent.map((capability) => (
                <Chip key={capability} tone="warn">
                  {capabilityLabel(capability)}
                </Chip>
              ))}
            </div>
            <p className="mt-1.5 text-[11.5px] leading-[1.5] text-[var(--muted)]">
              The harness intersects your operator session with the agent surface before a
              specialist is bound. These did not survive it.
            </p>
          </div>
        )}

        {/*
          The caveat is part of the argument rather than a hedge against it. A merchant who
          reads three verbs and concludes that is the whole boundary has been misled by a
          screen that was trying to reassure them.
        */}
        <p className="border-t border-[var(--line)] px-4 py-3 text-[11.5px] leading-[1.5] text-[var(--muted)]">
          This is the list the API names, not a complete catalogue. The platform holds many
          operations no agent may perform — issuing an execution grant, creating a payment order,
          executing a refund, applying a webhook — that never appear in an agent&rsquo;s vocabulary
          at all, so there is nothing for this document to list. A capability an agent cannot
          name is a stronger guarantee than one it is refused, and it is also a quieter one.
        </p>
      </section>
    </Panel>
  );
}

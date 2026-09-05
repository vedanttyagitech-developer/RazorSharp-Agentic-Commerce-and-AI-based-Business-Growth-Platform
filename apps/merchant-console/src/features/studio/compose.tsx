"use client";

/**
 * Composing an assistant: pick the role, switch capabilities off, name it, brief it.
 *
 * Every control here narrows. The roles are the ones the server listed in `specialists`,
 * the capabilities are the ones it listed for the role that is selected, and there is no
 * field anywhere on this screen into which a capability name can be typed. That is not a
 * convenience decision. A capability string this console accepted from a merchant would
 * be a capability string the platform had never declared, and the whole claim the studio
 * makes -- that what is on screen is what the gate will enforce -- rests on there being
 * no such string.
 *
 * The briefing is merchant-authored free text and the screen says what becomes of it.
 * Specification 6.5 is the governing sentence: "Merchant instructions remain
 * lower-precedence data and cannot change system invariants." It is not a system prompt,
 * it is not a persona, and this console will not call it either, because a field named
 * that would be describing a boundary violation in friendly words.
 */
import { Chip, Field, Panel, cx } from "@/components/ui";
import type { AgentCapabilities, SpecialistCapabilities } from "@/lib/api/types";

import {
  BRIEFING_MAX_CHARS,
  NAME_MAX_CHARS,
  narrow,
  type Composition,
} from "./composition";
import { capabilityKind, glossOf, roleSummary, roleTitle } from "./vocabulary";

/** The tone a capability's kind is drawn in. Neither of the real kinds is alarming. */
function kindTone(kind: string): "positive" | "info" | "warn" {
  if (kind === "read") return "positive";
  if (kind === "propose") return "info";
  return "warn";
}

function CapabilityToggle({
  capability,
  role,
  checked,
  onChange,
}: {
  capability: string;
  role: SpecialistCapabilities;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  const gloss = glossOf(capability);
  const kind = capabilityKind(capability);
  const inputId = `cap-${role.specialist}-${capability.replace(/[^a-z]+/gi, "-")}`;
  return (
    <li
      className={cx(
        "rounded-[var(--r-md)] border p-3 transition-colors",
        checked
          ? "border-[color-mix(in_srgb,var(--brand)_45%,transparent)] bg-[color-mix(in_srgb,var(--brand)_6%,transparent)]"
          : "border-[var(--line)] bg-[var(--raised)]",
      )}
    >
      <div className="flex items-start gap-2.5">
        <input
          id={inputId}
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
          className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[var(--brand)]"
        />
        <div className="min-w-0 flex-1">
          <label htmlFor={inputId} className="block text-[12.5px] font-semibold text-[var(--ink)]">
            {gloss ? gloss.label : capability}
          </label>
          <p className="mono mt-0.5 text-[var(--faint)] break-id">{capability}</p>
          {gloss ? (
            <p className="mt-1.5 text-[11.5px] leading-[1.5] text-[var(--muted)]">{gloss.detail}</p>
          ) : (
            /*
              A capability the server declared and this console has no sentence for. It is
              offered anyway -- the platform decides what exists -- and it is drawn as
              undescribed rather than quietly listed among the reads, because a merchant
              should not switch on something nobody explained to them.
            */
            <p className="mt-1.5 text-[11.5px] leading-[1.5] text-[var(--warn)]">
              The platform declares this capability for this role and this console has no
              description of it. Read it as its name and nothing more.
            </p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <Chip tone={kindTone(kind)}>
              {kind === "unclassified" ? "not described here" : kind === "read" ? "reads" : "proposes"}
            </Chip>
            {!role.tools.includes(capability) && (
              <Chip tone="muted">no tool under this name</Chip>
            )}
          </div>
        </div>
      </div>
    </li>
  );
}

export function ComposePanel({
  caps,
  role,
  composition,
  onChange,
  saved,
}: {
  caps: AgentCapabilities;
  role: SpecialistCapabilities;
  composition: Composition;
  onChange: (next: Composition) => void;
  /** Whether this browser accepted the last write. `null` before anything was written. */
  saved: boolean | null;
}) {
  const chosen = new Set(composition.capabilities);

  function pickRole(next: SpecialistCapabilities) {
    // Switching role starts from that role's whole declared set rather than carrying the
    // previous role's ticks across. The two rosters overlap only by accident, and an
    // intersection would leave a merchant with an agent nobody chose.
    onChange({ ...composition, role: next.specialist, capabilities: [...next.capabilities] });
  }

  function toggle(capability: string, on: boolean) {
    const wanted = new Set(chosen);
    if (on) wanted.add(capability);
    else wanted.delete(capability);
    onChange({ ...composition, capabilities: narrow(role, wanted) });
  }

  return (
    <Panel
      title="Compose"
      subtitle="Roles and capabilities are read from GET /v1/agent/capabilities. Nothing here can add one."
      actions={
        saved === false ? (
          <Chip tone="warn">this browser refused to save</Chip>
        ) : saved === true ? (
          <Chip tone="muted">saved in this browser</Chip>
        ) : null
      }
    >
      <div className="space-y-4 p-4">
        {/* ------------------------------------------------------------------- the role */}
        <fieldset>
          <legend className="eyebrow">Which built-in specialist</legend>
          <p className="mt-1 text-[11.5px] text-[var(--muted)]">
            The merchant copilot reaches {caps.specialists.length}{" "}
            {caps.specialists.length === 1 ? "specialist" : "specialists"} on this session. You are
            configuring one of them for your shop, not creating one.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            {caps.specialists.map((entry) => {
              const active = entry.specialist === role.specialist;
              return (
                <button
                  key={entry.specialist}
                  type="button"
                  onClick={() => pickRole(entry)}
                  aria-pressed={active}
                  className={cx(
                    "min-w-[200px] flex-1 rounded-[var(--r-md)] border px-3 py-2.5 text-left transition-colors",
                    active
                      ? "border-[var(--brand)] bg-[color-mix(in_srgb,var(--brand)_8%,transparent)]"
                      : "border-[var(--line)] bg-[var(--raised)] hover:border-[var(--faint)]",
                  )}
                >
                  <span className="block text-[12.5px] font-semibold text-[var(--ink)]">
                    {roleTitle(entry.specialist)}
                  </span>
                  <span className="mono mt-0.5 block text-[var(--faint)]">
                    {entry.specialist} · {entry.capabilities.length} capabilities
                  </span>
                  {roleSummary(entry.specialist) && (
                    <span className="mt-1.5 block text-[11.5px] leading-[1.45] text-[var(--muted)]">
                      {roleSummary(entry.specialist)}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </fieldset>

        {/* ----------------------------------------------------------- the capabilities */}
        <fieldset>
          <legend className="eyebrow">What it may do</legend>
          <p className="mt-1 text-[11.5px] text-[var(--muted)]">
            These {role.capabilities.length} are everything the platform binds to{" "}
            {roleTitle(role.specialist)} for this session. Switching one off narrows your
            assistant; there is no switch that widens it past this list, here or anywhere.
          </p>
          <ul className="mt-2 grid gap-2 sm:grid-cols-2">
            {role.capabilities.map((capability) => (
              <CapabilityToggle
                key={capability}
                capability={capability}
                role={role}
                checked={chosen.has(capability)}
                onChange={(next) => toggle(capability, next)}
              />
            ))}
          </ul>
          {composition.capabilities.length === 0 && (
            <p className="mt-2 text-[11.5px] text-[var(--warn)]">
              Nothing is switched on. An assistant with no capability can still hold a
              conversation and can read nothing, so every answer it gives would be ungrounded.
            </p>
          )}
        </fieldset>
      </div>

      {/* -------------------------------------------------------- name and the briefing */}
      <dl className="border-t border-[var(--line)]">
        <Field label="What your shop calls it" source="a label, stored in this browser">
          <input
            value={composition.name}
            maxLength={NAME_MAX_CHARS}
            onChange={(event) => onChange({ ...composition, name: event.target.value })}
            placeholder="Weekend stock assistant"
            aria-label="What your shop calls this assistant"
            className="w-full max-w-[420px] rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2.5 py-1.5 text-[12.5px] text-[var(--ink)] placeholder:text-[var(--faint)]"
          />
        </Field>
        <Field label="Briefing" source="specification 6.5 — lower-precedence data">
          <textarea
            value={composition.briefing}
            maxLength={BRIEFING_MAX_CHARS}
            rows={4}
            onChange={(event) => onChange({ ...composition, briefing: event.target.value })}
            placeholder="What this shop wants the assistant to pay attention to."
            aria-label="Briefing for this assistant"
            className="w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2.5 py-1.5 text-[12.5px] leading-[1.5] text-[var(--ink)] placeholder:text-[var(--faint)]"
          />
          <p className="mono mt-1 text-[var(--faint)]">
            {composition.briefing.length} / {BRIEFING_MAX_CHARS}
          </p>
          <p className="mt-1.5 text-[11.5px] leading-[1.5] text-[var(--muted)]">
            This is not a system prompt and this console will not call it one. Specification 6.5
            holds merchant instructions to lower-precedence data that cannot change system
            invariants, so a briefing can say what your shop cares about and cannot grant a
            capability, remove a guardrail, or ask the assistant for a verb it does not hold.
          </p>
        </Field>
      </dl>
    </Panel>
  );
}

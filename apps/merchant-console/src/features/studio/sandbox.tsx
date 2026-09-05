"use client";

/**
 * Ask the assistant something and watch what it actually did.
 *
 * The renderer is the copilot's, not a second one. `Transcript` draws the reply, then the
 * tools that were really called, then anything the gate refused, then the card, then the
 * proposal -- in that fixed order, because the order is the argument -- and this panel
 * mounts it rather than restating it. A studio with its own transcript would be a second
 * place for the two to disagree about what a denial looks like.
 *
 * Two honesty problems this panel exists to not have.
 *
 * **The turn runs under your operator session, not under your draft.** The platform has
 * no endpoint that accepts a composition and runs a turn narrowed to it, so a capability
 * you switched off is still bound for the session that answers here. Rather than fake the
 * refusal that publishing would produce, the panel measures the difference: every tool the
 * turn called is reconciled against the composition, and a call outside it is named as the
 * distance between the session and the draft. It is never called a denial, because it is
 * not one.
 *
 * **A proposal here applies nothing.** `Transcript` is mounted read-only, so a growth
 * proposal drafted in a sandbox renders with its evidence and without the press. A screen
 * labelled "try it" that could change a live shelf would be the worst kind of surprise,
 * and a control that mutates merchant state does not belong in this page's landmark
 * anyway.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { Chip, NotWired, Panel, cx } from "@/components/ui";
import { Transcript, type Message } from "@/features/copilot/transcript";
import { api } from "@/lib/api/client";
import type { Turn } from "@/lib/api/types";

import { reconcileCalls } from "./boundary";
import type { Composition } from "./composition";
import { capabilityLabel, questionsFor, roleTitle } from "./vocabulary";

/** The endpoint that would run a turn under a published composition. It does not exist. */
const NARROWED_TURN_ENDPOINT = "POST /v1/merchant/agent/definitions/{id}/turn";

function Reconciliation({
  composition,
  declared,
  turn,
}: {
  composition: Composition;
  declared: readonly string[];
  turn: Turn;
}) {
  const { inside, outside } = reconcileCalls(composition, turn.tool_calls, declared);
  if (inside.length === 0 && outside.length === 0) return null;
  return (
    <div className="mt-3 rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] p-3">
      <p className="eyebrow">That answer, measured against your draft</p>
      <ul className="mt-1.5 space-y-1">
        {inside.map((capability) => (
          <li key={`in-${capability}`} className="flex flex-wrap items-baseline gap-2">
            <Chip tone="positive">within your grant</Chip>
            <span className="text-[11.5px] text-[var(--ink)]">{capabilityLabel(capability)}</span>
            <span className="mono text-[var(--faint)] break-id">{capability}</span>
          </li>
        ))}
        {outside.map((capability) => (
          <li key={`out-${capability}`} className="flex flex-wrap items-baseline gap-2">
            <Chip tone="warn">outside your draft</Chip>
            <span className="text-[11.5px] text-[var(--ink)]">{capabilityLabel(capability)}</span>
            <span className="mono text-[var(--faint)] break-id">{capability}</span>
          </li>
        ))}
      </ul>
      {outside.length > 0 && (
        <p className="mt-2 text-[11.5px] leading-[1.5] text-[var(--muted)]">
          You switched {outside.length === 1 ? "that one" : "those"} off, and the call still
          ran. It ran under your operator session, which holds the role&rsquo;s whole declared
          set; your draft lives in this browser and the platform has never seen it. Publishing is
          what would close the gap, and the endpoint that would run a turn narrowed to a draft
          does not exist yet.
        </p>
      )}
    </div>
  );
}

export function SandboxPanel({
  composition,
  declared,
}: {
  composition: Composition;
  /** Everything the server declared for this role, from `specialists[].capabilities`. */
  declared: readonly string[];
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [lastTurn, setLastTurn] = useState<Turn | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const inFlight = useRef<AbortController | null>(null);
  // A counter, not a random value, so the server and the client agree on every key through
  // hydration. The dock keeps its own for the same reason; neither is exported, and a
  // shared one would be a module-level mutable the two panels raced on.
  const sequence = useRef(0);

  const nextId = useCallback(() => {
    sequence.current += 1;
    return `s${sequence.current}`;
  }, []);

  // The dock owns its own autoscroll on its own wrapper, so mounting `Transcript` here
  // brings none with it.
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, pending]);

  useEffect(() => () => inFlight.current?.abort(), []);

  const send = useCallback(
    async (raw: string) => {
      const message = raw.trim();
      if (!message || pending) return;
      setMessages((previous) => [...previous, { id: nextId(), role: "merchant", text: message }]);
      setDraft("");
      setPending(true);

      const controller = new AbortController();
      inFlight.current = controller;
      try {
        const turn = await api.merchantTurn({ message }, controller.signal);
        setLastTurn(turn);
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "copilot", text: turn.reply, turn },
        ]);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setLastTurn(null);
        setMessages((previous) => [...previous, { id: nextId(), role: "problem", error }]);
      } finally {
        if (inFlight.current === controller) inFlight.current = null;
        setPending(false);
      }
    },
    [nextId, pending],
  );

  const suggestions = questionsFor(composition.capabilities);

  return (
    <Panel
      title="Try it"
      subtitle="POST /v1/merchant/agent/turn — a real turn, against your real data, moving no money."
      actions={<Chip tone="info">proposes only</Chip>}
    >
      <div className="border-b border-[var(--line)] px-4 py-3">
        <p className="max-w-[80ch] text-[12px] leading-[1.55] text-[var(--muted)]">
          This runs the {roleTitle(composition.role)} specialist under your operator session, so
          the answer is real and so is every figure in it. Your draft is not part of the request:
          a capability you switched off above is still bound here, and the strip under each
          answer says which calls fell outside your draft rather than pretending they were
          refused.
        </p>
      </div>

      <div className="p-4">
        <div
          ref={scrollRef}
          className="max-h-[440px] min-h-[120px] overflow-y-auto rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] p-3"
        >
          {messages.length === 0 && !pending ? (
            <div>
              <p className="max-w-[70ch] text-[12.5px] leading-[1.55] text-[var(--ink)]">
                Ask it something. Every figure comes back from a tool call this panel shows you,
                and any change to stock or price arrives as a proposal — which you apply from the
                copilot, never from here.
              </p>
              {suggestions.length > 0 ? (
                <>
                  <p className="eyebrow mt-3">
                    Questions your {suggestions.length === 1 ? "capability" : "capabilities"} can
                    answer
                  </p>
                  <ul className="mt-1.5 flex flex-col gap-1.5">
                    {suggestions.map((question) => (
                      <li key={question}>
                        <button
                          type="button"
                          onClick={() => void send(question)}
                          className="w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-3 py-2 text-left text-[12.5px] text-[var(--ink)] transition-colors hover:border-[var(--info)]"
                        >
                          {question}
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="mt-3 text-[11.5px] leading-[1.5] text-[var(--warn)]">
                  Nothing you have switched on maps to a question this console knows how to
                  suggest. You can still type one.
                </p>
              )}
            </div>
          ) : (
            <Transcript
              messages={messages}
              pending={pending}
              readOnly
              label={`Sandbox conversation with the ${roleTitle(composition.role)} specialist`}
            />
          )}
        </div>

        {lastTurn && (
          <Reconciliation composition={composition} declared={declared} turn={lastTurn} />
        )}

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send(draft);
          }}
          className="mt-3 flex items-center gap-2"
        >
          <label htmlFor="studio-composer" className="sr-only">
            Ask the assistant you are composing
          </label>
          <input
            id="studio-composer"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask about the catalogue, inventory or checkouts"
            autoComplete="off"
            className="h-9 min-w-0 flex-1 rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2.5 text-[12.5px] text-[var(--ink)] placeholder:text-[var(--faint)]"
          />
          <button
            type="submit"
            disabled={pending || draft.trim().length === 0}
            className={cx(
              "inline-flex items-center gap-1.5 rounded-[var(--r-sm)] border px-2.5 py-1.5 text-[12px] font-medium transition-colors",
              "border-[color-mix(in_srgb,var(--brand)_60%,transparent)] bg-[color-mix(in_srgb,var(--brand)_18%,transparent)] text-[var(--brand)]",
              "hover:bg-[color-mix(in_srgb,var(--brand)_26%,transparent)] disabled:cursor-not-allowed disabled:opacity-45",
            )}
          >
            Ask
          </button>
        </form>
      </div>

      <div className="border-t border-[var(--line)] p-4">
        <NotWired
          what="Running a turn narrowed to the assistant you composed, so a capability you switched off is refused by the gate rather than measured against afterwards."
          missing={NARROWED_TURN_ENDPOINT}
        />
      </div>
    </Panel>
  );
}

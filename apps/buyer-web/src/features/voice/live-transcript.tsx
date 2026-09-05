/**
 * The conversation, on screen, before it is anywhere else.
 *
 * The assistant's reply is drawn the moment the `agent_reply` frame lands and never waits
 * for a byte of audio. That is not a nicety, it is the reason the pipeline is split at all
 * (19.1): the text is authored once, shown immediately, and spoken afterwards by a
 * separate synthesiser. When the voice fails, the reply is already here in full and the
 * only thing missing is the sound of it.
 *
 * Three things are drawn differently on purpose, and all three are distinctions a buyer
 * would otherwise have to be told about:
 *
 *  - **Interim text is not what you said yet.** A streaming recognizer revises its own
 *    hypothesis mid-sentence, so what is on screen while you speak is a guess. It is drawn
 *    unfinished -- dashed, grey, labelled -- and it is never treated as intent.
 *  - **A stale final was heard and not acted on.** It aged past the freshness window, so
 *    the agent never saw it. Hiding that would teach a buyer that the assistant is deaf at
 *    random; showing it as a normal turn would be a lie about what happened.
 *  - **A deterministic reply is the server's words, not a model's** (19.10). Every money
 *    fact -- the total, the fee, the refund, whether a payment went through -- is rendered
 *    from a versioned locale template out of server-confirmed fields, and this component
 *    shows which template and which fields, because "trust us, it is deterministic" is not
 *    checkable and a template id is.
 *
 * Nothing in this file computes an amount. The fields under a deterministic reply are the
 * strings the server put on the wire, rendered verbatim; there is no `parseFloat` here and
 * no arithmetic on anything that could be money.
 *
 * And nothing in this file approves anything. A transcript is intent evidence, never
 * authority evidence (19.11): a spoken "yes" lands here as a sentence and stops. There is
 * deliberately no approve, pay, cancel or refund control anywhere in this component, and a
 * reply that proposes one says where the buyer actually does it instead.
 */
"use client";

import { cx } from "@/components/ui";

import type { HeldTurn, TranscriptEntry } from "./transcript";

export interface LiveTranscriptProps {
  entries: readonly TranscriptEntry[];
  /** The turn currently being spoken: revisable, unconfirmed, not yet intent. */
  held: HeldTurn | null;
  /** True while the assistant's audio is playing, for the speaking indicator. */
  speaking: boolean;
  className?: string;
}

function BuyerTurn({ entry }: { entry: Extract<TranscriptEntry, { kind: "buyer" }> }) {
  return (
    <li className="flex flex-col items-end gap-1">
      <p
        className={cx(
          "max-w-[88%] rounded-[var(--r-lg)] rounded-br-[var(--r-sm)] px-3 py-2 text-[13px] leading-[1.5] whitespace-pre-wrap",
          entry.stale
            ? "border border-dashed border-[var(--amber)] bg-amber-50/50 text-[var(--ink-3)]"
            : "bg-[var(--tint-1)] text-[var(--ink)]",
        )}
      >
        <span className="sr-only">You said: </span>
        {entry.text}
      </p>
      {entry.stale ? (
        <p className="max-w-[88%] text-right text-[12px] leading-[1.4] text-[var(--ink-4)]">
          <span className="font-semibold text-[var(--amber)]">Heard, not acted on.</span> This
          reached the assistant too late to be current, so it was not used. Say it again if you
          still need it.
        </p>
      ) : null}
      {entry.source === "text" ? (
        <p className="text-[12px] text-[var(--ink-5)]">typed</p>
      ) : null}
    </li>
  );
}

/**
 * The audit line under a deterministic reply.
 *
 * Template id and version first, because that pair is what someone reading over the
 * buyer's shoulder greps the server's audit trail for. The fields are the exact values the
 * server confirmed and then spoke, printed as they arrived.
 */
function DeterministicFacts({
  entry,
}: {
  entry: Extract<TranscriptEntry, { kind: "assistant" }>;
}) {
  const fields = entry.fields ? Object.entries(entry.fields) : [];
  return (
    <div className="mt-1.5 rounded-[var(--r-sm)] bg-[var(--tint-2)] px-2.5 py-2">
      <p className="text-[12px] font-semibold text-[var(--ink-2)]">
        Spoken from a fixed template, not written by the model.
      </p>
      <p className="mt-0.5 text-[12px] leading-[1.45] text-[var(--ink-3)]">
        Totals, fees, refunds and payment outcomes are rendered by the server from values it
        has confirmed. Approving and paying still happen on the store&rsquo;s own pages, never
        here and never by voice.
      </p>
      {fields.length > 0 ? (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          {fields.map(([name, value]) => (
            <div key={name} className="contents">
              <dt className="text-[12px] text-[var(--ink-5)]">{name}</dt>
              {/* The server's own string. Never parsed, never re-formatted, never added up. */}
              <dd className="tnum text-[12px] text-[var(--ink-2)]">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <p className="tnum mt-1.5 text-[9px] tracking-[0.04em] text-[var(--ink-5)]">
        {entry.templateId ?? "template unrecorded"}
        {entry.templateVersion === null ? "" : ` · v${entry.templateVersion}`} · {entry.locale}
      </p>
    </div>
  );
}

function AssistantTurn({ entry }: { entry: Extract<TranscriptEntry, { kind: "assistant" }> }) {
  return (
    <li className="flex flex-col items-start">
      <p className="mb-1 text-[9px] font-bold tracking-[0.08em] text-[var(--blue)] uppercase">
        RazorAI{entry.deterministic ? " · server-rendered" : ""}
      </p>
      <div className="w-[94%] max-w-full">
        <p
          className={cx(
            "rounded-[var(--r-lg)] rounded-tl-[var(--r-sm)] px-3 py-2 text-[13px] leading-[1.5] whitespace-pre-wrap",
            entry.deterministic
              ? "border-[0.5px] border-[var(--blue)] bg-blue-50/40 text-[var(--ink)]"
              : "border-[0.5px] border-[var(--card-line)] bg-white text-[var(--ink-2)]",
          )}
        >
          {entry.text}
        </p>
        {entry.deterministic ? <DeterministicFacts entry={entry} /> : null}
      </div>
    </li>
  );
}

/**
 * The unfinished turn.
 *
 * Outside the live region on purpose: a partial that revises itself five times a second
 * inside `aria-live` is a screen reader talking over the person it is transcribing. The
 * final settles into the log below and is announced there, once.
 */
function Interim({ held }: { held: HeldTurn }) {
  return (
    <div aria-live="off" className="flex justify-end">
      <p className="max-w-[88%] rounded-[var(--r-lg)] rounded-br-[var(--r-sm)] border border-dashed border-[var(--ink-6)] bg-[var(--tint-3)] px-3 py-2 text-[13px] leading-[1.5] whitespace-pre-wrap text-[var(--ink-4)] italic">
        {held.text}
        <span className="mt-1 block text-[12px] not-italic">still hearing you — not confirmed</span>
      </p>
    </div>
  );
}

export function LiveTranscript({ entries, held, speaking, className }: LiveTranscriptProps) {
  return (
    <div className={cx("flex flex-col gap-3", className)}>
      <ol
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Voice conversation"
        className="flex flex-col gap-4"
      >
        {entries.map((entry) =>
          entry.kind === "buyer" ? (
            <BuyerTurn key={entry.id} entry={entry} />
          ) : (
            <AssistantTurn key={entry.id} entry={entry} />
          ),
        )}
      </ol>

      {held && held.text.length > 0 ? <Interim held={held} /> : null}

      {speaking ? (
        <p className="flex items-center gap-1.5 text-[12px] text-[var(--ink-4)]">
          <span aria-hidden="true" className="flex gap-1">
            {[0, 1, 2].map((index) => (
              <span
                key={index}
                className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--blue)]"
                style={{ animationDelay: `${index * 160}ms` }}
              />
            ))}
          </span>
          Speaking the reply above. It was on screen before it was spoken.
        </p>
      ) : null}

      {entries.length === 0 && !held ? (
        <p className="text-[13px] leading-[1.5] text-[var(--ink-4)]">
          Hold the button below and say what you need. Everything the assistant says appears
          here in writing first, and is spoken afterwards.
        </p>
      ) : null}
    </div>
  );
}

/**
 * The conversation, on screen, before it is anywhere else.
 *
 * The assistant's reply is drawn the moment the `agent_reply` frame lands and never waits
 * for a byte of audio. That is not a nicety, it is the reason the pipeline is split at all
 * (19.1): the text is authored once, shown immediately, and spoken afterwards by a
 * separate synthesiser. When the voice fails, the reply is already here in full and the
 * only thing missing is the sound of it.
 *
 * Drawn as the AgentFlow live view draws its chat: the buyer's turns on the right in solid
 * indigo, the assistant's on the left as a hairline panel on the dark scene, both at most
 * 85% wide with one tight corner on the speaker's side. Three things are drawn differently
 * on purpose, and all three are distinctions a buyer would otherwise have to be told about:
 *
 *  - **Interim text is not what you said yet.** A streaming recognizer revises its own
 *    hypothesis mid-sentence, so what is on screen while you speak is a guess. It is drawn
 *    unfinished -- dashed, dim, labelled -- and it is never treated as intent.
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
import { ProductCards } from "@/features/agent/product-cards";
import { renderInline } from "@/lib/inline-markdown";

import type { HeldTurn, TranscriptEntry } from "./transcript";

export interface LiveTranscriptProps {
  entries: readonly TranscriptEntry[];
  /** The turn currently being spoken: revisable, unconfirmed, not yet intent. */
  held: HeldTurn | null;
  /** True while the assistant's audio is playing, for the speaking indicator. */
  speaking: boolean;
  /**
   * The shelf's own basket write, for the product cards a reply drew.
   *
   * Absent means the products are shown without a press -- which is what a transcript
   * rendered outside the panel should do, since a card that writes needs a basket to write
   * into and this component holds none.
   */
  onAdd?: (sku: string) => void;
  /** The sku a basket write is in flight for, so a card cannot be pressed twice. */
  busySku?: string | null;
  className?: string;
}

/**
 * The reference bubble: at most 85% wide, rounded, one tight corner on the speaker's side.
 *
 * Exported because the written chat (`features/agent/message-list`) draws its turns on the
 * same scene and must draw them the same way. One conversation in two registers -- voice
 * and text -- is still one conversation, and a buyer should not see the bubbles change
 * shape because the socket dropped.
 */
export const BUBBLE =
  "max-w-[85%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap break-words";
export const BUYER = "self-end rounded-br-[4px] bg-primary text-white";
export const ASSISTANT =
  "self-start rounded-bl-[4px] border border-white/10 bg-white/[0.06] text-slate-100";

function BuyerTurn({ entry }: { entry: Extract<TranscriptEntry, { kind: "buyer" }> }) {
  return (
    <li className="flex flex-col items-end gap-1">
      <p
        className={cx(
          BUBBLE,
          entry.stale
            ? "self-end rounded-br-[4px] border border-dashed border-amber-400/60 bg-amber-400/10 text-slate-300"
            : BUYER,
        )}
      >
        <span className="sr-only">You said: </span>
        {entry.text}
      </p>
      {entry.stale ? (
        <p className="max-w-[85%] text-right text-xs leading-relaxed text-slate-500">
          <span className="font-semibold text-amber-300">Heard, not acted on.</span> This
          reached the assistant too late to be current, so it was not used. Say it again if you
          still need it.
        </p>
      ) : null}
      {entry.source === "text" ? (
        <p className="font-mono text-[9px] uppercase tracking-[0.14em] text-slate-500">typed</p>
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
    <div className="max-w-[85%] self-start rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2">
      <p className="text-xs font-semibold text-slate-200">
        Spoken from a fixed template, not written by the model.
      </p>
      <p className="mt-0.5 text-xs leading-relaxed text-slate-400">
        Totals, fees, refunds and payment outcomes are rendered by the server from values it
        has confirmed. Approving and paying still happen on the store&rsquo;s own pages, never
        here and never by voice.
      </p>
      {fields.length > 0 ? (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          {fields.map(([name, value]) => (
            <div key={name} className="contents">
              <dt className="text-xs text-slate-500">{name}</dt>
              {/* The server's own string. Never parsed, never re-formatted, never added up. */}
              <dd className="tnum text-xs text-slate-200">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <p className="tnum mt-1.5 font-mono text-[9px] tracking-[0.04em] text-slate-500">
        {entry.templateId ?? "template unrecorded"}
        {entry.templateVersion === null ? "" : ` · v${entry.templateVersion}`} · {entry.locale}
      </p>
    </div>
  );
}

function AssistantTurn({
  entry,
  onAdd,
  busySku = null,
}: {
  entry: Extract<TranscriptEntry, { kind: "assistant" }>;
  onAdd?: (sku: string) => void;
  busySku?: string | null;
}) {
  return (
    <li className="flex flex-col items-start gap-1">
      <p className="font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-500">
        RazorAI{entry.deterministic ? " · server-rendered" : ""}
      </p>
      <p
        className={cx(
          BUBBLE,
          ASSISTANT,
          entry.deterministic && "border-indigo-400/40 bg-indigo-500/10",
        )}
      >
        {renderInline(entry.text)}
      </p>
      {entry.deterministic ? <DeterministicFacts entry={entry} /> : null}
      {/*
        The shelf belongs to the sentence that named it, so it is drawn inside this turn's
        own list item rather than appended to the conversation. `ProductCards` renders
        nothing for an empty list, which is what a reply that named no product sends.
      */}
      {entry.items && entry.items.length > 0 ? (
        <ProductCards items={entry.items} onAdd={onAdd} busySku={busySku} />
      ) : null}
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
      <p className="max-w-[85%] rounded-2xl rounded-br-[4px] border border-dashed border-white/20 bg-white/[0.04] px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap text-slate-400 italic">
        {held.text}
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block text-slate-500 not-italic motion-safe:animate-pulse"
        >
          ▍
        </span>
        <span className="mt-1 block font-mono text-[9px] tracking-[0.14em] uppercase not-italic text-slate-500">
          still hearing you — not confirmed
        </span>
      </p>
    </div>
  );
}

export function LiveTranscript({
  entries,
  held,
  speaking,
  onAdd,
  busySku = null,
  className,
}: LiveTranscriptProps) {
  return (
    <div className={cx("flex flex-col gap-3", className)}>
      <ol
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Voice conversation"
        className="flex flex-col gap-3.5"
      >
        {entries.map((entry) =>
          entry.kind === "buyer" ? (
            <BuyerTurn key={entry.id} entry={entry} />
          ) : (
            <AssistantTurn key={entry.id} entry={entry} onAdd={onAdd} busySku={busySku} />
          ),
        )}
      </ol>

      {held && held.text.length > 0 ? <Interim held={held} /> : null}

      {speaking ? (
        <p className="flex items-center gap-2 text-xs text-slate-500">
          <span aria-hidden="true" className="flex gap-1">
            {[0, 1, 2].map((index) => (
              <span
                key={index}
                className="breathing-dot h-1.5 w-1.5 rounded-full bg-[#FFA14D]"
                style={{ animationDelay: `${index * 160}ms` }}
              />
            ))}
          </span>
          Speaking the reply above. It was on screen before it was spoken.
        </p>
      ) : null}

      {entries.length === 0 && !held ? (
        <p className="mt-6 text-center text-xs leading-relaxed text-slate-500">
          Just talk. The microphone is on. Everything the assistant says appears here in
          writing first, and is spoken afterwards. You can type below instead at any time.
        </p>
      ) : null}
    </div>
  );
}

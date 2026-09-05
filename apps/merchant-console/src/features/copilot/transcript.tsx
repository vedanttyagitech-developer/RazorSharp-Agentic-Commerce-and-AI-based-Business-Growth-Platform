"use client";

/**
 * The conversation, and the evidence under each answer.
 *
 * Three things appear here and a glance tells them apart: what the merchant typed, what
 * the copilot answered, and what went wrong when a turn never reached the platform. The
 * last of those is never dressed up as an answer. A copilot that composes a sentence while
 * the API is unreachable is a copilot that will one day compose a figure, and this console
 * exists because a previous one did exactly that.
 *
 * Under every answer the order is fixed: the claim, then the tools that were actually
 * called, then anything the gate refused, then the card, then the proposal. That order is
 * the argument. The sentence comes first because it is what a person reads; everything
 * under it is what they check it against.
 */
import type { Turn } from "@/lib/api/types";

import { Chip, ProblemPanel, cx } from "@/components/ui";

import { StructuredCard, UndrawnPayload } from "./cards";
import { ProposalCard } from "./proposal-card";
import { readPayload } from "./payloads";
import { DenialPanel, ToolLog } from "./tool-log";

/**
 * One line of the transcript.
 *
 * A copilot message carries the whole `Turn` rather than a flattened copy, so the chips,
 * the refusals and the cards under it are read from the server's own record instead of
 * from something this component decided to keep.
 */
export type Message =
  | { id: string; role: "merchant"; text: string }
  | { id: string; role: "copilot"; text: string; turn: Turn | null }
  | { id: string; role: "problem"; error: unknown };

const SPECIALIST_NAMES: Readonly<Record<string, string>> = {
  shopping: "Shopping",
  checkout: "Checkout",
  support: "Support",
  growth: "Growth",
  case: "Case",
};

export function specialistName(specialist: string): string {
  return SPECIALIST_NAMES[specialist] ?? specialist;
}

/**
 * The routing reason, as a sentence, with the raw key kept beside it.
 *
 * Routing on this endpoint is a lexicon over the message -- it calls no model -- and that
 * is exactly the sort of claim a payments reviewer will not take on trust. The key is what
 * they would grep the server logs for, so it stays visible rather than being translated
 * away.
 */
export function routingSentence(reason: string): string {
  const separator = reason.indexOf(":");
  const key = separator === -1 ? reason : reason.slice(0, separator);
  const cue = separator === -1 ? "" : reason.slice(separator + 1);
  switch (key) {
    case "default_growth":
      return "the merchant copilot routes here by default";
    case "default_shopping":
      return "nothing else was in context";
    case "order_in_context":
      return "an order is in context, and an identifier beats a word";
    case "checkout_in_context":
      return "a checkout is in context, and an identifier beats a word";
    case "case_cue":
    case "support_cue":
    case "checkout_cue":
      return cue ? `the word "${cue}" in the message` : reason;
    default:
      return reason;
  }
}

function MerchantMessage({ text }: { text: string }) {
  return (
    <li className="flex justify-end">
      <p className="max-w-[85%] rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--raised)] px-3 py-2 text-[12.5px] leading-[1.5] break-words whitespace-pre-wrap text-[var(--ink)]">
        <span className="sr-only">You asked: </span>
        {text}
      </p>
    </li>
  );
}

function CopilotMessage({
  text,
  turn,
  readOnly,
}: {
  text: string;
  turn: Turn | null;
  readOnly: boolean;
}) {
  const reading = turn ? readPayload(turn.structured) : null;
  return (
    <li className="flex flex-col items-start">
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <span className="eyebrow text-[var(--brand)]">
          {turn ? `${specialistName(turn.specialist)} specialist` : "Merchant Copilot"}
        </span>
        {turn && (
          <span
            className="mono text-[var(--faint)]"
            title={`specialist=${turn.specialist} routing_reason=${turn.routing_reason}`}
          >
            routed because {routingSentence(turn.routing_reason)}
          </span>
        )}
      </div>
      <div className="w-full">
        <p className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--surface)] px-3 py-2 text-[12.5px] leading-[1.55] break-words whitespace-pre-wrap text-[var(--ink)]">
          {text}
        </p>
        {turn && (
          <>
            <ToolLog calls={turn.tool_calls} />
            <DenialPanel denials={turn.denials} />
            {reading?.card && <StructuredCard card={reading.card} />}
            {reading && (
              <UndrawnPayload undrawn={reading.undrawn} malformed={reading.malformed} />
            )}
            {reading?.proposal && (
              <ProposalCard proposal={reading.proposal} readOnly={readOnly} />
            )}
          </>
        )}
      </div>
    </li>
  );
}

/**
 * A turn that failed in transport or at the boundary.
 *
 * Rendered as the problem document it was, with its status and every extension member,
 * exactly as every failed read in this console is. The person reading this screen is the
 * one who has to tell an unreachable API from an expired session, and those two look
 * identical behind a reassuring sentence.
 */
function ProblemMessage({ error }: { error: unknown }) {
  return (
    <li className="flex flex-col items-start">
      <div className="w-full">
        <ProblemPanel error={error} what="that turn of the copilot" />
        <p className="mt-1.5 text-[11.5px] text-[var(--muted)]">
          Nothing was sent to the platform and nothing changed. Ask again.
        </p>
      </div>
    </li>
  );
}

/** Three dots while a turn is in flight, labelled, because dots say nothing aloud. */
function Working() {
  return (
    <li className="flex items-center gap-2">
      <span className="sr-only">The copilot is working on your question</span>
      <span aria-hidden="true" className="flex gap-1">
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--faint)]"
            style={{ animationDelay: `${index * 160}ms` }}
          />
        ))}
      </span>
      <span aria-hidden="true" className="text-[11.5px] text-[var(--muted)]">
        routing, then calling tools
      </span>
    </li>
  );
}

export function Transcript({
  messages,
  pending,
  className,
  label = "Conversation with the Merchant Copilot",
  readOnly = false,
}: {
  messages: readonly Message[];
  pending: boolean;
  className?: string;
  /**
   * What a screen reader calls this log.
   *
   * The dock is mounted on every page of this console, so a second transcript on the same
   * screen would otherwise give a reader two live regions with one name and no way to
   * tell which one just spoke.
   */
  label?: string;
  /** Draw proposals without the press that applies them. See `ProposalCard`. */
  readOnly?: boolean;
}) {
  return (
    <ol
      // `log` announces each addition politely and in order, which is what a transcript is.
      // Assertive would interrupt the merchant mid-sentence in the composer below it.
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label={label}
      className={cx("flex flex-col gap-4", className)}
    >
      {messages.map((message) => {
        if (message.role === "merchant")
          return <MerchantMessage key={message.id} text={message.text} />;
        if (message.role === "problem")
          return <ProblemMessage key={message.id} error={message.error} />;
        return (
          <CopilotMessage
            key={message.id}
            text={message.text}
            turn={message.turn}
            readOnly={readOnly}
          />
        );
      })}
      {pending ? <Working /> : null}
    </ol>
  );
}

/**
 * What the panel says before anyone has asked anything.
 *
 * The four suggestions are drawn from what the Growth Specialist can answer today, and
 * each one was checked against the running platform rather than imagined: catalogue
 * health, inventory anomalies and checkout counts are the three tools it holds, and the
 * fourth asks it to propose over the second. A suggestion that produced "I am not allowed
 * to do that" would be worse than no suggestion at all -- it would teach a merchant that
 * the box is decorative.
 */
export const SUGGESTIONS: readonly string[] = [
  "How is my catalogue health?",
  "Which products are out of stock?",
  "How are my checkouts and orders doing?",
  "Propose what to do about the stock anomalies",
];

export function Opening({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className="px-1">
      <p className="text-[12.5px] leading-[1.55] text-[var(--ink)]">
        Ask about the catalogue, inventory or the checkout funnel. Every figure comes back from a
        tool call this panel shows you, and any change to stock or price arrives as a proposal you
        apply yourself.
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <Chip tone="muted">reads only</Chip>
        <Chip tone="muted">no price change</Chip>
        <Chip tone="muted">no refund</Chip>
        <Chip tone="muted">no payment authority</Chip>
      </div>
      <p className="eyebrow mt-4">Try one of these</p>
      <ul className="mt-1.5 flex flex-col gap-1.5">
        {SUGGESTIONS.map((question) => (
          <li key={question}>
            <button
              type="button"
              onClick={() => onPick(question)}
              className="w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--raised)] px-3 py-2 text-left text-[12.5px] text-[var(--ink)] transition-colors hover:border-[var(--info)]"
            >
              {question}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The transcript.
 *
 * Three kinds of thing appear in it and they are drawn so that a glance tells them apart:
 * what the buyer typed, what RazorAI answered, and what went wrong when a turn never
 * reached the server. The last of those is never dressed up as an answer -- a copilot that
 * invents a sentence when the API is unreachable is a copilot that will one day invent a
 * price.
 *
 * Under every RazorAI answer sits the evidence for it: the tool log, then any refusal,
 * then any proposal. That order is the argument. The claim comes first, what was actually
 * called comes second, what the agent was not permitted to do comes third, and the only
 * way to act on any of it is a link out of this panel.
 */
"use client";

import { cx } from "@/components/ui";
import type { ApprovalCard, Basket, Turn } from "@/lib/api/types";
import { renderInline } from "@/lib/inline-markdown";

import type { LineConfirmation } from "./basket-proposal-card";
import type { CheckoutConfirmation } from "./checkout-proposal-card";
import { DenialCard } from "./denial-card";
import { ProposalCard } from "./proposal-card";
import { ToolChips } from "./tool-chip";

/**
 * One line of the transcript.
 *
 * A RazorAI message carries the whole `Turn` it came from rather than a flattened copy,
 * so the chips, denials and proposal under it are read from the server's own record. The
 * opening introduction is the one message with no turn: it is this app speaking about
 * itself, and it makes no claim about the store.
 */
export type Message =
  | { id: string; role: "buyer"; text: string }
  | { id: string; role: "razorai"; text: string; turn: Turn | null }
  | { id: string; role: "problem"; text: string };

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

function BuyerMessage({ text }: { text: string }) {
  return (
    <li className="flex justify-end">
      <p className="max-w-[85%] rounded-[var(--r-lg)] rounded-br-[var(--r-sm)] bg-[var(--tint-1)] px-3 py-2 text-[13px] leading-[1.5] whitespace-pre-wrap text-[var(--ink)]">
        <span className="sr-only">You said: </span>
        {text}
      </p>
    </li>
  );
}

function RazorAIMessage({
  text,
  turn,
  onAsk,
  onConfirmLine,
  onConfirmCheckout,
}: {
  text: string;
  turn: Turn | null;
  onAsk?: (message: string) => void;
  onConfirmLine?: (confirmation: LineConfirmation) => Promise<Basket>;
  onConfirmCheckout?: (confirmation: CheckoutConfirmation) => Promise<ApprovalCard>;
}) {
  return (
    <li className="flex flex-col items-start">
      <p className="mb-1 text-[9px] font-bold tracking-[0.08em] text-[var(--blue)] uppercase">
        {turn ? `RazorAI · ${specialistName(turn.specialist)}` : "RazorAI"}
      </p>
      <div className="w-[92%] max-w-full">
        <p className="rounded-[var(--r-lg)] rounded-tl-[var(--r-sm)] border-[0.5px] border-[var(--card-line)] bg-white px-3 py-2 text-[13px] leading-[1.5] whitespace-pre-wrap text-[var(--ink-2)]">
          {renderInline(text)}
        </p>
        {turn ? (
          <>
            <ToolChips calls={turn.tool_calls} />
            <DenialCard denials={turn.denials} />
            <ProposalCard
              structured={turn.structured}
              onAsk={onAsk}
              onConfirmLine={onConfirmLine}
              onConfirmCheckout={onConfirmCheckout}
            />
          </>
        ) : null}
      </div>
    </li>
  );
}

/**
 * A turn that failed in transport or at the boundary.
 *
 * The problem detail is shown as the client parsed it. Saying "the server sent a shape
 * this app does not understand" is more use to a buyer, and far more use to whoever is
 * watching the demo, than a cheerful sentence covering for it.
 */
function ProblemMessage({ text }: { text: string }) {
  return (
    <li className="flex flex-col items-start">
      <div
        role="alert"
        className="w-[92%] max-w-full rounded-[var(--r-md)] border-[0.5px] border-[var(--red)] bg-red-50/60 px-3 py-2"
      >
        <p className="text-[12px] font-semibold text-[var(--red)]">That turn did not go through</p>
        <p className="mt-0.5 text-[12px] leading-[1.45] text-[var(--ink-3)]">{text}</p>
        <p className="mt-1 text-[12px] text-[var(--ink-4)]">
          Nothing was sent to the store and nothing changed. Ask again.
        </p>
      </div>
    </li>
  );
}

/** Three dots while a turn is in flight. Labelled, because the dots say nothing aloud. */
function Thinking() {
  return (
    <li className="flex items-center gap-1.5 pl-1">
      <span className="sr-only">RazorAI is working on your message</span>
      <span aria-hidden="true" className="flex gap-1">
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--ink-6)]"
            style={{ animationDelay: `${index * 160}ms` }}
          />
        ))}
      </span>
      <span aria-hidden="true" className="text-[12px] text-[var(--ink-5)]">
        routing and calling tools
      </span>
    </li>
  );
}

export function MessageList({
  messages,
  pending,
  className,
  onAsk,
  onConfirmLine,
  onConfirmCheckout,
}: {
  messages: readonly Message[];
  pending: boolean;
  className?: string;
  /**
   * Ask RazorAI something else, from inside a card. The only card that uses it is the "which
   * of these did you mean?" question, whose rows send a message naming one SKU; a press
   * writes nothing anywhere. Left undefined while a turn is in flight, so those rows draw
   * themselves as unpressable instead of swallowing a click.
   */
  onAsk?: (message: string) => void;
  /**
   * Execute a bound line proposal. Threaded from the panel, which holds the basket context,
   * down to the one card that draws a press: the priced line proposal. A press sends the
   * proposal's own binding back to the server, which refuses it if anything moved.
   */
  onConfirmLine?: (confirmation: LineConfirmation) => Promise<Basket>;
  /**
   * Open a checkout for a bound `checkout.create` proposal. Threaded the same way to the
   * checkout card, which sends the buyer to the checkout's approval page once the server
   * has opened it.
   */
  onConfirmCheckout?: (confirmation: CheckoutConfirmation) => Promise<ApprovalCard>;
}) {
  return (
    <ol
      // `log` announces each addition politely and in order, which is what a transcript
      // is. Assertive would interrupt a buyer mid-sentence in the composer.
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Conversation with RazorAI"
      className={cx("flex flex-col gap-4", className)}
    >
      {messages.map((message) => {
        if (message.role === "buyer") return <BuyerMessage key={message.id} text={message.text} />;
        if (message.role === "problem")
          return <ProblemMessage key={message.id} text={message.text} />;
        return (
          <RazorAIMessage
            key={message.id}
            text={message.text}
            turn={message.turn}
            onAsk={onAsk}
            onConfirmLine={onConfirmLine}
            onConfirmCheckout={onConfirmCheckout}
          />
        );
      })}
      {pending ? <Thinking /> : null}
    </ol>
  );
}

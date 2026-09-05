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
 *
 * Drawn with the voice transcript's own bubbles, on the same dark scene. This list is what
 * the RazorAI box shows whenever the socket is not carrying the conversation -- which, with
 * the voice gateway down, is the whole visit -- so it cannot be the one surface in the box
 * still wearing the storefront's white cards.
 */
"use client";

import { cx } from "@/components/ui";
import { ASSISTANT, BUBBLE, BUYER } from "@/features/voice/live-transcript";
import type { ApprovalCard, Basket, Turn } from "@/lib/api/types";
import { renderInline } from "@/lib/inline-markdown";

import type { LineConfirmation } from "./basket-proposal-card";
import type { CheckoutConfirmation } from "./checkout-proposal-card";
import { DenialCard } from "./denial-card";
import { ProductCards, itemsFromStructured } from "./product-cards";
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

/** The mono label over an assistant turn, the same as the transcript's. */
const LABEL = "font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-500";

function BuyerMessage({ text }: { text: string }) {
  return (
    <li className="flex flex-col items-end">
      <p className={cx(BUBBLE, BUYER)}>
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
  onAdd,
  busySku = null,
  onOpened,
}: {
  text: string;
  turn: Turn | null;
  onAsk?: (message: string) => void;
  onConfirmLine?: (confirmation: LineConfirmation) => Promise<Basket>;
  onConfirmCheckout?: (confirmation: CheckoutConfirmation) => Promise<ApprovalCard>;
  onAdd?: (sku: string) => void;
  busySku?: string | null;
  onOpened?: (checkoutId: string) => void;
}) {
  // The same shelf the spoken path draws, from the same adapter: a search page's hits or
  // one product's own fields. Above the proposal card, because choosing which product comes
  // before confirming a line of it.
  const items = turn ? itemsFromStructured(turn.structured) : [];
  return (
    <li className="flex flex-col items-start gap-1">
      <p className={LABEL}>{turn ? `RazorAI · ${specialistName(turn.specialist)}` : "RazorAI"}</p>
      <p className={cx(BUBBLE, ASSISTANT)}>{renderInline(text)}</p>
      {turn ? (
        <div className="w-[92%] max-w-full">
          <ToolChips calls={turn.tool_calls} />
          <DenialCard denials={turn.denials} />
          <ProductCards items={items} onAdd={onAdd} busySku={busySku} />
          <ProposalCard
            structured={turn.structured}
            onAsk={onAsk}
            onConfirmLine={onConfirmLine}
            onConfirmCheckout={onConfirmCheckout}
            onOpened={onOpened}
          />
        </div>
      ) : null}
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
        className="w-[92%] max-w-full rounded-2xl rounded-bl-[4px] border border-rose-400/40 bg-rose-500/10 px-3.5 py-2 text-xs leading-relaxed text-slate-300"
      >
        <p className="font-semibold text-rose-300">That turn did not go through</p>
        <p className="mt-0.5">{text}</p>
        <p className="mt-1 text-slate-500">
          Nothing was sent to the store and nothing changed. Ask again.
        </p>
      </div>
    </li>
  );
}

/**
 * Three dots while a turn is in flight, in the thinking violet the pill breathes in.
 * Labelled, because the dots say nothing aloud.
 */
function Thinking() {
  return (
    <li className="flex items-center gap-2 pl-1 text-xs text-slate-500">
      <span className="sr-only">RazorAI is working on your message</span>
      <span aria-hidden="true" className="flex gap-1">
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="breathing-dot h-1.5 w-1.5 rounded-full bg-[#B08CFF]"
            style={{ animationDelay: `${index * 160}ms` }}
          />
        ))}
      </span>
      <span aria-hidden="true">routing and calling tools</span>
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
  onAdd,
  busySku = null,
  onOpened,
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
  /**
   * Add a product to the basket from the shelf a turn drew. The panel's own `basket.add`,
   * so the written path writes through exactly the request the storefront's grid sends.
   */
  onAdd?: (sku: string) => void;
  /** The sku that write is in flight for, so a card cannot be pressed twice. */
  busySku?: string | null;
  /** Given, a checkout the card opens is shown in place instead of navigated to. */
  onOpened?: (checkoutId: string) => void;
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
            onAdd={onAdd}
            busySku={busySku}
            onOpened={onOpened}
          />
        );
      })}
      {pending ? <Thinking /> : null}
    </ol>
  );
}

/**
 * The conversation itself: what was said, what it put on screen, and where money is asked for.
 *
 * Three kinds of turn and no more. The buyer's own words, so they can see they were heard
 * correctly -- which matters most when they were spoken and transcribed. The copilot's
 * reply, with any products it named drawn as cards underneath, because a product mentioned
 * in a sentence is not a thing the buyer can act on. And a problem, said plainly, because
 * an assistant that swallows a failure leaves the buyer believing something happened that
 * did not.
 *
 * The trusted surface is rendered at the end of the stream rather than in a panel of its
 * own. The approval card is part of this conversation -- it is what the last few sentences
 * were leading to -- and putting it somewhere else would ask the buyer to leave the thing
 * they are talking to in order to answer it.
 */

"use client";

import { useEffect, useRef } from "react";

import { ProductCards, itemsFromStructured } from "@/features/agent/product-cards";
import type { Product, Turn } from "@/lib/api/types";

export interface Message {
  id: string;
  role: "buyer" | "copilot" | "problem";
  text: string;
  turn?: Turn | null;
  /** Products this reply is asking the buyer to choose between. */
  products?: readonly Product[];
}

function Bubble({ message, busySku, onAdd }: {
  message: Message;
  busySku: string | null;
  onAdd: (sku: string) => void;
}) {
  if (message.role === "buyer") {
    return (
      <li className="bubble-enter flex justify-end">
        <p className="max-w-[80%] rounded-2xl rounded-br-md bg-[var(--rzp-blue-strong)] px-3.5 py-2 text-[13.5px] leading-relaxed text-slate-100">
          {message.text}
        </p>
      </li>
    );
  }
  if (message.role === "problem") {
    return (
      <li className="bubble-enter">
        <p className="max-w-[85%] rounded-2xl rounded-bl-md border border-amber-400/25 bg-amber-400/[0.07] px-3.5 py-2 text-[13px] leading-relaxed text-amber-200">
          {message.text}
        </p>
      </li>
    );
  }

  // A reply's products come either from the turn the model answered with, or from a
  // choice this component was handed directly. Both draw the same cards.
  const fromTurn = message.turn ? itemsFromStructured(message.turn.structured) : [];
  const chosen = message.products ?? [];
  const items =
    chosen.length > 0
      ? chosen.map((product) => ({
          sku: product.sku,
          name: product.display_name,
          unit_price: product.unit_price,
          stock_units: product.stock_units,
          available: product.is_available && product.stock_units > 0,
        }))
      : fromTurn;

  return (
    <li className="bubble-enter max-w-[85%] space-y-2">
      <p className="rounded-2xl rounded-bl-md bg-white/[0.06] px-3.5 py-2 text-[13.5px] leading-relaxed text-slate-100">
        {message.text}
      </p>
      {items.length > 0 ? <ProductCards items={items} busySku={busySku} onAdd={onAdd} /> : null}
    </li>
  );
}

function Thinking() {
  return (
    <li className="flex items-center gap-1.5 px-1" aria-label="Working on it">
      {[0, 1, 2].map((dot) => (
        <span
          key={dot}
          className="size-1.5 animate-pulse rounded-full bg-slate-500"
          style={{ animationDelay: `${dot * 160}ms` }}
        />
      ))}
    </li>
  );
}

export function ChatStream({
  messages,
  pending,
  busySku,
  onAdd,
  checkout,
}: {
  messages: readonly Message[];
  pending: boolean;
  busySku: string | null;
  onAdd: (sku: string) => void;
  /** The embedded checkout journey, when one is open. Rendered as part of the stream. */
  checkout: React.ReactNode;
}) {
  const end = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending, checkout]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
      <ol
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Conversation with your copilot"
        className="mx-auto flex max-w-3xl flex-col gap-3"
      >
        {messages.map((message) => (
          <Bubble key={message.id} message={message} busySku={busySku} onAdd={onAdd} />
        ))}
        {pending ? <Thinking /> : null}
        {checkout !== null ? (
          <li>
            {/* Labelled, because the buyer should be able to tell at a glance which part of
                this conversation is the shop's own surface and which part is the assistant
                talking. The assistant cannot approve or pay; this region is where those
                happen, and it says so. */}
            <div className="card-enter overflow-hidden rounded-2xl border border-[var(--rzp-blue)]/35 bg-[var(--rzp-navy-raised)] shadow-[0_10px_40px_-16px_rgba(0,0,0,0.9)]">
              <p className="flex items-center gap-1.5 border-b border-white/[0.07] px-3 py-2 font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-[var(--rzp-blue)]">
                <svg viewBox="0 0 16 16" className="size-3" fill="none" aria-hidden="true">
                  <path
                    d="M8 1.8l5 2v4.1c0 2.9-2 5.4-5 6.3-3-0.9-5-3.4-5-6.3V3.8l5-2z"
                    stroke="currentColor"
                    strokeWidth="1.3"
                    strokeLinejoin="round"
                  />
                  <path
                    d="M5.8 8.1l1.6 1.6 3-3.2"
                    stroke="currentColor"
                    strokeWidth="1.3"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
                Powered by Razorpay Payments
              </p>
              {/* `rzp-trusted` re-points the storefront's light tokens at dark values for this
                  subtree. The card is the same component the checkout page renders, with the
                  same figures and the same evidence; only the palette it resolves to changes,
                  because a stack of white boxes dropped into a navy conversation reads as a
                  different application rather than as this shop's own screen. */}
              <div className="rzp-trusted p-3">{checkout}</div>
            </div>
          </li>
        ) : null}
        <div ref={end} />
      </ol>
    </div>
  );
}

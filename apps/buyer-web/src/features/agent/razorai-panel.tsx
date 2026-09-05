/**
 * RazorAI, in a drawer on the right.
 *
 * The panel is a mouth, not a hand. It sends one message to `POST /v1/agent/turn` and
 * renders what comes back; it holds no capability of its own, and the only controls in it
 * that change anything are links to the trusted surface. That constraint is not a policy
 * this component enforces at runtime -- it is enforced by the absence of the capability on
 * the server -- but the drawing has to make it legible, or a buyer will not believe it.
 *
 * The header carries the part most demos hide: which of the five specialists answered and
 * why. Routing is `agent_service.route`, a lexicon over the message and the identifiers
 * the tab is looking at, and it calls no model. Showing the reason turns "trust us, it is
 * deterministic" into something a person can check twice and see the same answer.
 *
 * Nothing in this file computes an amount. Nothing in it retries a turn on the buyer's
 * behalf. A turn that fails is reported as having failed.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useBasketContext } from "@/components/providers";
import { cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { Basket, Turn } from "@/lib/api/types";

import type { LineConfirmation } from "./basket-proposal-card";
import { MessageList, specialistName, type Message } from "./message-list";

/**
 * The RazorAI mark: a four-point spark with a smaller one beside it.
 *
 * Drawn here rather than imported, because the panel and its launcher must carry the same
 * mark and this module is the one they both already depend on. Blue throughout, never the
 * storefront's green: green on this storefront means a control that commits something,
 * and nothing the agent draws ever does.
 */
export function RazorAIMark({ size = 18 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" fill="currentColor">
      <path d="M10.2 2.4 L11.7 8.1 L17.4 9.6 L11.7 11.1 L10.2 16.8 L8.7 11.1 L3 9.6 L8.7 8.1 Z" />
      <path d="M17.8 13.2 L18.6 16.1 L21.5 16.9 L18.6 17.7 L17.8 20.6 L17 17.7 L14.1 16.9 L17 16.1 Z" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none">
      <path
        d="M5 5 L15 15 M15 5 L5 15"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true" fill="none">
      <path
        d="M10 16.5 V4 M5 9 L10 4 L15 9"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * The routing reason, as a sentence.
 *
 * The raw key stays visible in the `title`, because that string is what a reviewer will
 * grep the server logs for; the sentence is for the buyer standing in front of it.
 */
function routingSentence(reason: string): string {
  const separator = reason.indexOf(":");
  const key = separator === -1 ? reason : reason.slice(0, separator);
  const cue = separator === -1 ? "" : reason.slice(separator + 1);
  switch (key) {
    case "default_shopping":
      return "nothing else was in context, so shopping took it";
    case "default_growth":
      return "the merchant harness routes here by default";
    case "order_in_context":
      return "an order is open in this tab, and an identifier beats a word";
    case "checkout_in_context":
      return "a checkout is open in this tab, and an identifier beats a word";
    case "support_cue":
    case "checkout_cue":
    case "case_cue":
      return cue ? `the word "${cue}" in your message` : reason;
    default:
      return reason;
  }
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

const INTRO: Message = {
  id: "intro",
  role: "razorai",
  turn: null,
  text:
    "I am RazorAI. I can search the catalogue, read your basket and explain a checkout or an " +
    "order, and I can propose an order for you. I cannot approve it and I cannot pay: that is " +
    "yours, and it happens on the store's own pages.",
};

export function RazorAIPanel({
  open,
  onClose,
  basketId,
  checkoutId,
}: {
  open: boolean;
  onClose: () => void;
  /** Overrides the basket in context, for a page that already knows which one it means. */
  basketId?: string | null;
  checkoutId?: string | null;
}) {
  // A prop wins over the context so a page that already knows which basket it is about --
  // the basket screen itself -- does not depend on the context having caught up.
  const basket = useBasketContext();
  const activeBasketId = basketId ?? basket.basketId;

  const [messages, setMessages] = useState<Message[]>([INTRO]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);

  // The one write this panel performs, and it is the buyer's, not RazorAI's: the press on a
  // priced line proposal. It goes to the same basket route the basket page uses, carrying
  // the binding the proposal was prepared against, and the provider is asked to re-read
  // afterwards because it owns the item count in the header and has no setter for it.
  const confirmLine = useCallback(
    async (confirmation: LineConfirmation): Promise<Basket> => {
      const next = await api.setLine(
        confirmation.basket_id,
        confirmation.sku,
        confirmation.quantity,
        confirmation.idempotency_key,
        confirmation.expected,
      );
      await basket.refresh();
      return next;
    },
    [basket],
  );
  const [lastTurn, setLastTurn] = useState<Turn | null>(null);

  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const inFlight = useRef<AbortController | null>(null);
  // Ids are a counter rather than a random value so that the server and the client agree
  // on every key through hydration.
  const sequence = useRef(0);

  const nextId = useCallback(() => {
    sequence.current += 1;
    return `m${sequence.current}`;
  }, []);

  // Escape closes, and Tab is kept inside the drawer while it is open. Captured on the
  // document so a keystroke inside the composer reaches it before anything else.
  useEffect(() => {
    if (!open) return;
    const node = panelRef.current;
    if (!node) return;
    const restoreTo = document.activeElement as HTMLElement | null;
    inputRef.current?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(node!.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !node!.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      restoreTo?.focus?.();
    };
  }, [open, onClose]);

  // On a phone the drawer is the whole screen, so the page behind it must not scroll
  // under it. On a desktop it sits beside the storefront and locking the page would take
  // browsing away from a buyer who opened a shopping assistant.
  useEffect(() => {
    if (!open) return;
    if (!window.matchMedia("(max-width: 639px)").matches) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  useEffect(() => {
    const node = transcriptRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, pending]);

  // A turn still in flight when the panel closes is abandoned rather than left to land in
  // a transcript nobody is looking at.
  useEffect(() => {
    if (open) return;
    inFlight.current?.abort();
    inFlight.current = null;
  }, [open]);

  useEffect(() => () => inFlight.current?.abort(), []);

  const send = useCallback(
    async (raw: string) => {
      const message = raw.trim();
      if (!message || pending) return;

      setMessages((previous) => [...previous, { id: nextId(), role: "buyer", text: message }]);
      setDraft("");
      setPending(true);

      const controller = new AbortController();
      inFlight.current = controller;
      try {
        const turn = await api.agentTurn(
          {
            message,
            basket_id: activeBasketId ?? undefined,
            checkout_id: checkoutId ?? undefined,
          },
          controller.signal,
        );
        setLastTurn(turn);
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "razorai", text: turn.reply, turn },
        ]);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "problem", text: humanMessage(error) },
        ]);
      } finally {
        if (inFlight.current === controller) inFlight.current = null;
        setPending(false);
      }
    },
    [activeBasketId, checkoutId, nextId, pending],
  );

  if (!open) return null;

  return (
    <>
      {/* The scrim belongs to the phone layout, where the drawer covers the storefront. */}
      <div
        className="fixed inset-0 z-40 bg-black/25 sm:hidden"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="razorai-title"
        className="fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l-[0.5px] border-[var(--card-line)] bg-[var(--surface)] sm:w-[400px]"
        style={{ boxShadow: "-8px 0 24px rgba(0,0,0,0.08)" }}
      >
        <header className="shrink-0 border-b border-[var(--header-line)] px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p
                id="razorai-title"
                className="flex items-center gap-1.5 text-[16px] font-bold text-[var(--ink)]"
              >
                <span className="text-[var(--blue)]">
                  <RazorAIMark />
                </span>
                RazorAI
              </p>
              {lastTurn ? (
                <p
                  className="mt-0.5 text-[12px] leading-[1.4] text-[var(--ink-4)]"
                  title={`specialist=${lastTurn.specialist} routing_reason=${lastTurn.routing_reason}`}
                >
                  <span className="font-semibold text-[var(--ink-3)]">
                    {specialistName(lastTurn.specialist)} specialist
                  </span>{" "}
                  answered — {routingSentence(lastTurn.routing_reason)}
                </p>
              ) : (
                <p className="mt-0.5 text-[12px] leading-[1.4] text-[var(--ink-4)]">
                  Routing is a lexicon, not a model. The specialist that answers, and why,
                  appears here.
                </p>
              )}
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close RazorAI"
              className="-mr-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[var(--ink-4)] transition hover:bg-[var(--tint-1)] hover:text-[var(--ink)]"
            >
              <CloseIcon />
            </button>
          </div>
        </header>

        <div ref={transcriptRef} className="flex-1 overflow-y-auto px-4 py-4">
          <MessageList
            messages={messages}
            pending={pending}
            // A card may ask RazorAI something else — the disambiguation rows do — and it
            // goes through the same `send` a typed message does, so the buyer's own choice
            // lands in the transcript above the answer to it. Withheld while a turn is in
            // flight: `send` already refuses then, and a row that looks pressable and is
            // not is a control that lies about itself.
            onAsk={pending ? undefined : (message) => void send(message)}
            onConfirmLine={confirmLine}
          />
        </div>

        <form
          className="shrink-0 border-t border-[var(--header-line)] px-4 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            void send(draft);
          }}
        >
          <div className="flex items-center gap-2">
            <label htmlFor="razorai-composer" className="sr-only">
              Message RazorAI
            </label>
            <input
              id="razorai-composer"
              ref={inputRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ask for something, or say what you need"
              autoComplete="off"
              className="h-10 min-w-0 flex-1 rounded-[var(--r-md)] bg-[var(--tint-2)] px-3 text-[14px] text-[var(--ink)] placeholder:text-[var(--ink-5)]"
            />
            <button
              type="submit"
              disabled={pending || draft.trim().length === 0}
              aria-label="Send to RazorAI"
              className={cx(
                "flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--r-md)] transition",
                "bg-[var(--blue)] text-white disabled:cursor-not-allowed disabled:opacity-40",
              )}
            >
              <SendIcon />
            </button>
          </div>
          <p className="mt-2 text-[12px] leading-[1.4] text-[var(--ink-5)]">
            RazorAI proposes. Approving and paying happen on the store&rsquo;s own pages, never in
            this panel.
          </p>
        </form>
      </div>
    </>
  );
}

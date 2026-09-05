/**
 * RazorAI, in a box over the shelf.
 *
 * The panel is a mouth, not a hand. It sends one message to `POST /v1/agent/turn` (or one
 * `text_input` frame down the voice socket) and renders what comes back; it holds no
 * capability of its own, and the only controls in it that change anything are links to the
 * trusted surface. That constraint is not a policy this component enforces at runtime -- it
 * is enforced by the absence of the capability on the server -- but the drawing has to make
 * it legible, or a buyer will not believe it.
 *
 * The drawing is the AgentFlow live view's: a near-black scene with a deep indigo ground
 * glow and a faint vignette, a mono pill cluster top-left naming the surface and what it
 * is doing right now, ghost controls top-right, and one conversation with one composer
 * centred beneath -- the voice transcript while the socket carries it, the written chat
 * with its proposal cards when it does not. The composer's edge animates from the same
 * state the pill reads, so neither can claim something the session is not doing.
 *
 * The routing reason stays on the panel, quietly: which of the five specialists answered
 * and why. Routing is `agent_service.route`, a lexicon over the message and the identifiers
 * the tab is looking at, and it calls no model. Showing the reason turns "trust us, it is
 * deterministic" into something a person can check twice and see the same answer.
 *
 * Nothing in this file computes an amount. Nothing in it retries a turn on the buyer's
 * behalf. A turn that fails is reported as having failed.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";

import { useBasketContext } from "@/components/providers";
import { useBasket } from "@/features/basket/use-basket";
import { cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { ApprovalCard, Basket, Turn } from "@/lib/api/types";
import type { UseVoiceSessionOptions } from "@/features/voice/use-voice-session";
import {
  PHASE_COLOUR,
  PHASE_LABEL,
  VoicePanel,
  type VoiceSurfaceState,
} from "@/features/voice/voice-panel";
import type { Offer } from "@/features/voice/wire";

import type { LineConfirmation } from "./basket-proposal-card";
import { CartStrip } from "./cart-strip";
import type { CheckoutConfirmation } from "./checkout-proposal-card";
import { MessageList, specialistName, type Message } from "./message-list";
import { StageRail } from "./stage-rail";
import { StageScene } from "./stage-scene";
import { useOrderStage } from "./use-order-stage";

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
    <svg
      viewBox="0 0 16 16"
      className="size-3.5"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
    >
      <path d="M4 4 L12 12 M12 4 L4 12" />
    </svg>
  );
}

function DockIcon({ layout }: { layout: "centre" | "side" }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className="size-3.5"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinejoin="round"
    >
      {layout === "centre" ? (
        <path d="M2.5 3.5h11v9h-11zM9.5 3.5v9" />
      ) : (
        <path d="M2.5 3.5h11v9h-11zM5.5 6h5v4h-5z" />
      )}
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

/** The reference's quiet ghost control: a hairline pill that brightens under the pointer. */
const GHOST =
  "rounded-full border border-white/15 bg-white/[0.04] text-slate-300 transition-colors hover:border-white/30 hover:bg-white/[0.08] hover:text-white";

/** The reference's mono pill: ten-pixel uppercase, wide-tracked. */
const MONO = "font-mono text-[10px] font-medium uppercase tracking-[0.14em]";

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
  voiceOptions,
}: {
  open: boolean;
  onClose: () => void;
  /** Overrides the basket in context, for a page that already knows which one it means. */
  basketId?: string | null;
  checkoutId?: string | null;
  /** Injected in tests: the socket and audio the voice session should use. */
  voiceOptions?: UseVoiceSessionOptions;
}) {
  // A prop wins over the context so a page that already knows which basket it is about --
  // the basket screen itself -- does not depend on the context having caught up.
  const basket = useBasketContext();
  // The shelf's own write, for the product cards a turn draws. Deliberately the same hook
  // the grid and the basket screen use rather than a second `api.setLine` here: it carries
  // the quantity bookkeeping, the bounded recovery for a basket the server has dropped, and
  // the `busySku` a card needs to refuse a second press. The hook is built for exactly this
  // -- several surfaces writing one basket, reconciling through the provider's count -- so
  // holding one here beside the context is its intended use, not a second source of truth.
  const shelf = useBasket();
  const pathname = usePathname() ?? "/";
  // Which step of the order this screen is on. Derived in one place and read by both the
  // rail and the scene, so the two cannot disagree about where the buyer is.
  const { stage, checkout: stageCheckout } = useOrderStage({
    hasLines: (shelf.basket?.lines.length ?? 0) > 0,
  });
  const activeBasketId = basketId ?? basket.basketId;

  const [messages, setMessages] = useState<Message[]>([INTRO]);
  const [pending, setPending] = useState(false);
  const [lastTurn, setLastTurn] = useState<Turn | null>(null);
  // What the voice surface says it is doing. Drawn in the pill; the composer's own edge is
  // drawn by the surface from the same facts, so the two cannot disagree.
  const [voiceState, setVoiceState] = useState<VoiceSurfaceState>({ live: false, phase: "text" });
  const onVoiceState = useCallback((next: VoiceSurfaceState) => {
    setVoiceState((current) =>
      current.live === next.live && current.phase === next.phase ? current : next,
    );
  }, []);
  // Where the box sits: over the shelf in the centre, or docked to the side so the shelf
  // stays usable beside it. Remembered per browser; nothing about it reaches the server.
  const [layout, setLayout] = useState<"centre" | "side">("centre");
  // Docked to the side wherever the PAGE has trusted controls of its own. On a checkout and
  // on the Reserve Pay simulator the box must never cover the surface the buyer is being
  // asked to approve -- covering a consent control with a conversation about it is the one
  // layout this product cannot ship.
  //
  // The stored preference is deliberately NOT overwritten: it is read back the moment the
  // buyer leaves those routes, and the manual toggle still wins afterwards, so this forces
  // the arrival rather than the whole visit.
  const forceSide = pathname.startsWith("/checkout/") || pathname === "/reserve-pay";
  useEffect(() => {
    // Deferred through a zero timeout rather than set straight from the effect body: the
    // lint rule `react-hooks/set-state-in-effect` refuses a synchronous setter here, and the
    // deferral is what lets a route change and the stored preference settle in one pass
    // instead of two renders disagreeing about where the box belongs.
    const id = window.setTimeout(() => {
      if (forceSide) {
        setLayout("side");
        return;
      }
      try {
        setLayout(window.localStorage.getItem("razorai.layout") === "side" ? "side" : "centre");
      } catch {
        /* storage may be unavailable; the default stands */
      }
    }, 0);
    return () => window.clearTimeout(id);
  }, [forceSide]);
  const toggleLayout = useCallback(() => {
    setLayout((current) => {
      const next = current === "centre" ? "side" : "centre";
      try {
        window.localStorage.setItem("razorai.layout", next);
      } catch {
        /* same */
      }
      return next;
    });
  }, []);

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

  // The buyer's other press: forming a checkout from a `checkout.create` proposal. It calls
  // the same `POST /v1/baskets/{id}/checkout` the basket page's "Proceed to checkout" uses,
  // and answers with version 1's approval card. Opening a checkout closes the basket, so the
  // context's basket id is dropped here exactly as `BasketView` drops it — a later add then
  // opens a fresh basket instead of writing to one that can only answer 409, and the header
  // count zeroes. The card owns the navigation to the checkout's own page; this only makes
  // the write and returns what the server said.
  const confirmCheckout = useCallback(
    async (confirmation: CheckoutConfirmation): Promise<ApprovalCard> => {
      const card = await api.openCheckout(confirmation.basket_id, confirmation.idempotency_key);
      basket.setBasketId(null);
      return card;
    },
    [basket],
  );

  // The spoken "yes": the buyer took the product RazorAI put forward. This is the buyer's
  // own press, made with their voice -- the same PUT the shelf's ADD sends and the same
  // POST the checkout card sends -- so the platform still executes and the kernel still
  // decides. The checkout opens in its own document, as the card does, and is told it was
  // reached by voice so it reads the card aloud and carries the next yes through.
  const takeOffer = useCallback(
    async (offer: Offer) => {
      const id = basket.basketId ?? (await api.createBasket()).basket_id;
      await api.setLine(id, offer.sku, offer.quantity);
      await basket.refresh();
      const card = await api.openCheckout(id);
      basket.setBasketId(null);
      window.location.assign(`/checkout/${encodeURIComponent(card.checkout_id)}?voice=1`);
    },
    [basket],
  );

  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const inFlight = useRef<AbortController | null>(null);
  // Ids are a counter rather than a random value so that the server and the client agree
  // on every key through hydration.
  const sequence = useRef(0);

  const nextId = useCallback(() => {
    sequence.current += 1;
    return `m${sequence.current}`;
  }, []);

  // Escape closes, and Tab is kept inside the box while it is open. Captured on the
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

  // On a phone the box is the whole screen, so the page behind it must not scroll under
  // it. On a desktop it sits over the storefront and locking the page would take browsing
  // away from a buyer who opened a shopping assistant.
  useEffect(() => {
    if (!open) return;
    if (!window.matchMedia("(max-width: 639px)").matches) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

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

  // The Checkout press on the cart strip. Until the inline journey lands it makes the same
  // POST the basket page's button makes and then opens the checkout's own page, carrying
  // `?voice=1` when the session is live so that page reads the card aloud and carries the
  // next spoken yes. The basket id is dropped exactly as `confirmCheckout` drops it: opening
  // a checkout closes the basket, and a later add must open a fresh one rather than write to
  // one that can only answer 409.
  //
  // `stripBusy` is deliberately left true on success. The navigation is already in flight and
  // a second press during it would open a second checkout for a basket that is now closed.
  const [stripBusy, setStripBusy] = useState(false);
  const checkoutFromStrip = useCallback(() => {
    const id = activeBasketId;
    if (!id) return;
    setStripBusy(true);
    void (async () => {
      try {
        const card = await api.openCheckout(id);
        basket.setBasketId(null);
        const spoken = voiceState.live ? "?voice=1" : "";
        window.location.assign(`/checkout/${encodeURIComponent(card.checkout_id)}${spoken}`);
      } catch (cause) {
        setStripBusy(false);
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "problem", text: humanMessage(cause) },
        ]);
      }
    })();
  }, [activeBasketId, basket, nextId, voiceState.live]);

  if (!open) return null;

  const { phase } = voiceState;
  const breathing = phase === "connecting" || phase === "thinking";

  return (
    <>
      {/* The scrim belongs to the phone layout, where the box covers the storefront. */}
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
        data-ai-state={phase}
        data-ai-layout={layout}
        className={cx(
          "fixed top-[92px] bottom-4 z-50 flex flex-col overflow-hidden rounded-[24px] border border-white/10 bg-[#05070E]/88 text-slate-400 backdrop-blur-xl selection:bg-indigo-500/30 selection:text-white",
          layout === "centre"
            ? "left-1/2 w-[calc(100%-1.5rem)] -translate-x-1/2 sm:w-[min(960px,calc(100%-3rem))]"
            : "right-3 w-[calc(100%-1.5rem)] sm:w-[440px]",
        )}
        style={{ boxShadow: "0 18px 48px rgba(0,0,0,0.45)" }}
      >
        {/* dark-scene ground: a deep indigo glow behind the conversation + a faint, wide
            slate vignette at the edges */}
        <div
          aria-hidden="true"
          data-testid="razorai-ground"
          className="pointer-events-none absolute inset-0"
          style={{
            background: [
              "radial-gradient(circle 520px at 50% 190px, rgb(58 74 178 / 0.20) 0%, rgb(30 41 110 / 0.10) 45%, rgb(5 7 14 / 0) 74%)",
              "radial-gradient(ellipse 130% 100% at 50% 45%, rgb(5 7 14 / 0) 55%, rgb(0 0 0 / 0.55) 100%)",
            ].join(", "),
          }}
        />

        <header className="relative z-10 flex shrink-0 items-center justify-between gap-3 px-4 pt-4 pb-2">
          {/* top-left cluster: the surface, and what it is doing right now */}
          <div className="flex min-w-0 items-center gap-2">
            <div
              role="group"
              aria-label="RazorAI status"
              className={cx(
                "flex overflow-hidden rounded-full border border-white/15 bg-white/[0.04]",
                MONO,
              )}
            >
              <span
                id="razorai-title"
                className="flex items-center gap-1.5 bg-primary px-3 py-1.5 text-white"
              >
                <RazorAIMark size={11} />
                RazorAI
              </span>
              <span className="flex items-center gap-1.5 px-3 py-1.5 text-slate-300">
                <span
                  aria-hidden="true"
                  className={cx("h-1.5 w-1.5 shrink-0 rounded-full", breathing && "breathing-dot")}
                  style={{ backgroundColor: PHASE_COLOUR[phase] }}
                />
                {PHASE_LABEL[phase]}
              </span>
            </div>
            <span
              className={cx(
                "hidden items-center rounded-full border border-white/15 bg-white/[0.04] px-3 py-1.5 text-slate-300 sm:inline-flex",
                MONO,
              )}
            >
              Customer Copilot
            </span>
          </div>

          {/* quiet ghost controls: dock to the side, and close */}
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={toggleLayout}
              aria-label={layout === "centre" ? "Dock RazorAI to the side" : "Bring RazorAI to the centre"}
              title={layout === "centre" ? "Dock to the side" : "Bring to the centre"}
              className={cx(GHOST, "p-1.5")}
            >
              <DockIcon layout={layout} />
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close RazorAI"
              title="Close"
              className={cx(GHOST, "p-1.5")}
            >
              <CloseIcon />
            </button>
          </div>
        </header>

        {/* The rail, then the scene: where the buyer is in the order, and one line about
            what happens next. Both sit above the conversation and outside it, because they
            describe the whole box rather than any one turn in it. */}
        <StageRail stage={stage} className="relative z-10 shrink-0 px-6 pt-1" />
        <StageScene
          stage={stage}
          checkout={stageCheckout}
          orderId={stageCheckout?.order_id ?? null}
          pendingNote={null}
          className="relative z-10 shrink-0 px-6 pt-1"
        />

        {/* the routing reason, quietly: which specialist answered the last written turn, and why */}
        {lastTurn ? (
          <p
            className="relative z-10 shrink-0 px-6 pb-1 text-center font-mono text-[10px] leading-relaxed tracking-[0.04em] text-slate-500"
            title={`specialist=${lastTurn.specialist} routing_reason=${lastTurn.routing_reason}`}
          >
            <span className="text-slate-300">
              {specialistName(lastTurn.specialist)} specialist
            </span>{" "}
            answered — {routingSentence(lastTurn.routing_reason)}
          </p>
        ) : null}

        {/* the conversation owns the rest of the box */}
        <div className="relative z-10 flex min-h-0 flex-1 justify-center px-4 pb-4 pt-1 sm:pb-5">
          <div className="min-h-0 w-full max-w-[640px]">
            <VoicePanel
              {...voiceOptions}
              onAffirmed={(offer) => void takeOffer(offer)}
              onStateChange={onVoiceState}
              onSendText={(text) => void send(text)}
              textPending={pending}
              // The product cards a spoken reply draws press the shelf's own write, not a
              // second path of their own: one basket, one request, whichever surface the
              // buyer happened to be looking at.
              onAdd={(sku) => void shelf.add(sku)}
              busySku={shelf.busySku}
              // The live cart, held out of the scrolling transcript so it is still there
              // when the buyer decides to check out. Every press on it is the shelf's own
              // write, and Checkout is the same POST the basket page's button sends.
              beforeComposer={
                <CartStrip
                  lines={shelf.basket?.lines ?? []}
                  names={shelf.names}
                  total={shelf.basket?.quote?.total ?? null}
                  busySku={shelf.busySku}
                  onSetQuantity={(sku, quantity) => void shelf.setQuantity(sku, quantity)}
                  onCheckout={checkoutFromStrip}
                  checkoutBusy={stripBusy}
                  className="relative z-10 shrink-0 pt-2"
                />
              }
              inputRef={inputRef}
            >
              <MessageList
                messages={messages}
                pending={pending}
                // A card may ask RazorAI something else — the disambiguation rows do — and
                // it goes through the same `send` a typed message does, so the buyer's own
                // choice lands in the transcript above the answer to it. Withheld while a
                // turn is in flight: `send` already refuses then, and a row that looks
                // pressable and is not is a control that lies about itself.
                onAsk={pending ? undefined : (message) => void send(message)}
                onConfirmLine={confirmLine}
                onConfirmCheckout={confirmCheckout}
                onAdd={(sku) => void shelf.add(sku)}
                busySku={shelf.busySku}
              />
            </VoicePanel>
          </div>
        </div>
      </div>
    </>
  );
}

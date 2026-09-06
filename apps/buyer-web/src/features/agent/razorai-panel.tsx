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
import { z } from "zod";

import { useBasketContext } from "@/components/providers";
import { useBasket } from "@/features/basket/use-basket";
import { cx } from "@/components/ui";
import { api, newIdempotencyKey } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { ApprovalCard, Basket, Checkout, Turn } from "@/lib/api/types";
import type {
  UseVoiceSessionOptions,
  VoiceSessionController,
} from "@/features/voice/use-voice-session";
import {
  PHASE_COLOUR,
  PHASE_LABEL,
  VoicePanel,
  type VoiceSurfaceState,
} from "@/features/voice/voice-panel";
import type { Offer, ReplyItem } from "@/features/voice/wire";
import { isAffirmative, isNegative } from "@/features/voice/transcript";

import { LineProposalSchema, type LineConfirmation } from "./basket-proposal-card";
import { CartStrip } from "./cart-strip";
import { itemsFromStructured } from "./product-cards";
import type { CheckoutConfirmation } from "./checkout-proposal-card";
import { CheckoutJourney } from "@/features/checkout/checkout-journey";
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

/**
 * What the box is currently asking the buyer's permission for.
 *
 * Three kinds because there are exactly three writes a conversation can reach: a line into
 * the basket, a checkout over that basket, and a payment against that checkout. Each carries
 * only what its sentence needs to name -- no request, no handler, nothing executable. A slip
 * is a question; the answer is the only thing that calls the API.
 */
/**
 * What a TYPED reply offered, so a written "yes" reaches the same slip a spoken one does.
 *
 * The spoken path never needed this: the gateway derives an `offer` in `_offer_of` and puts
 * it on the frame, so `transcript.ts` already holds one by the time the buyer answers. A
 * turn taken over plain HTTP carries no such field -- only `structured` -- so a written
 * "yes" had nothing to accept and fell through to the API, which answered it
 * conversationally and wrote NOTHING. That was a dead end rather than a slow path: the
 * basket stayed empty, so the next "checkout" hit the `need_checkout` refusal ("open a
 * checkout from your basket first") and the buyer could not get out of it by typing. It
 * only ever affected buyers who type, which is why it survived a spoken demonstration --
 * and typing is all that is left when the microphone cannot reach a model.
 *
 * The rule is the gateway's, not a second opinion: `itemsFromStructured` is already the one
 * definition of "what this reply put on the shelf" (its own docstring says two adapters
 * would be two definitions), and taking the FIRST item mirrors `_offer_of(rows[0])`
 * exactly. Availability is not re-litigated here either -- `ReplyItem.available` is the
 * gateway's `_available` verdict, and an unavailable row yields no offer, so a written
 * "yes" can never put a sold-out line in a basket that a spoken one would have refused.
 *
 * Quantity is 1 for the same reason `_offer_of` hardcodes it: this infers an offer from a
 * reply that named products, and a reply that named a quantity carries it in a line
 * proposal the slip reads instead. Nothing here multiplies anything.
 */
/**
 * Did the buyer just tell the store to put something in the basket?
 *
 * The model decides its own tool calls, and on the same sentence it sometimes calls
 * `basket_propose_line` and sometimes only reads the product and answers in words. That is
 * a reasonable thing for a model to do and a terrible thing for a shop to do: "add amul
 * gold" filled the basket on one turn and left it empty on the next, with the same reply
 * on screen both times, so the buyer had no way to tell which had happened. The add is
 * therefore the panel's decision, taken from the buyer's own sentence, and the model is
 * left to supply the words and the identity of the product.
 *
 * Deliberately narrow. It matches an instruction to add, in the three languages this shop
 * is spoken to in, and nothing else -- a question ("do you have milk?") is not an add, and
 * the products it could act on are only ever the single unambiguous one the same turn read.
 */
const ADD_INTENT: RegExp =
  /\b(add|buy|order|put|get me|i (?:want|need)|take)\b|\b(chahiye|chaahiye|de do|dedo|dalo|daal do|daldo|le lo|lelo|add kar|add kro|add karo)\b|(चाहिए|दे दो|डाल दो|ले लो|जोड़)/i;

function offerFromTurn(turn: Turn | null): Offer | null {
  if (turn === null) return null;
  const items = itemsFromStructured(turn.structured);
  // EXACTLY one, never the first of several. A search that returned five milks is a
  // disambiguation, and the reply says so in as many words -- "I will not guess which of
  // these 5 you meant. Pick one." Taking `[0]` there would do the guessing the runner just
  // refused to do, and a yes meant as "yes, milk" would add whichever row happened to sort
  // first. One candidate is an offer; several are a question, and a question is answered by
  // naming the product or pressing its own +, not by a bare yes.
  if (items.length !== 1) return null;
  const [first] = items;
  if (first === undefined || !first.available) return null;
  return { sku: first.sku, name: first.name, quantity: 1, unit_price: first.unit_price };
}

function DockIcon({ layout }: { layout: "centre" | "side" }) {  return (
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
    "I am RazorAI. I can search the catalogue, read your cart and explain a checkout or an " +
    "order, and I can propose an order for you. I cannot approve it and I cannot pay: that is " +
    "yours, and it happens on the store's own pages.",
};

/**
 * States that mean the buyer has approved and money is the next thing to happen. The
 * provider's sheet may open from any of them; it waits for the Razorpay order regardless.
 */
/** What RazorAI says when the approval card comes up: the order, then the question. */
function approvalSentence(checkout: Checkout): string {
  const card = checkout.approval_card;
  const lines = card?.quote?.lines ?? [];
  const items =
    lines.length > 0
      ? lines.map((line) => `${line.quantity} × ${line.name}`).join(", ")
      : "your cart";
  const total = card?.total?.display ?? "";
  const currency = card?.total?.currency ?? "INR";
  return (
    `Here is your order: ${items}. The total is ${currency} ${total}, ` +
    `including delivery and tax. Say yes to approve and pay, or no to hold off.`
  );
}

/** What RazorAI says when the buyer declines at the approval card. */
const HELD_OFF =
  "No problem, nothing has been charged and nothing is reserved against you. " +
  "Your cart is still here. Say yes when you are ready to pay, or press Reject to close " +
  "this checkout and keep shopping.";

const PAY_NEXT: ReadonlySet<string> = new Set([
  "APPROVED",
  "EXECUTION_PENDING",
  "AWAITING_PAYMENT",
  "PAYMENT_UNKNOWN",
]);

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
  // True while a cart write or a checkout open is in flight.
  const [writeBusy, setWriteBusy] = useState(false);
  /** The checkout being approved inside the box, instead of on its own page. */
  const [inlineCheckoutId, setInlineCheckoutId] = useState<string | null>(null);
  const [inlineCheckout, setInlineCheckout] = useState<Checkout | null>(null);
  /** The buyer allowed the payment; the embedded sheet may open. Reset with the checkout. */
  const [payAllowed, setPayAllowed] = useState(false);
  // Bumped when the buyer TYPES a yes at an approval card, so the trusted surface
  // approves the card it is displaying. See `CheckoutJourney`'s `approveNonce`.
  const [approveNonce, setApproveNonce] = useState(0);
  /** The live session, so an embedded approval card speaks through this socket and not a new one. */
  const [session, setSession] = useState<VoiceSessionController | null>(null);

  const { stage, checkout: polledCheckout } = useOrderStage({
    hasLines: (shelf.basket?.lines.length ?? 0) > 0,
    checkoutId: inlineCheckoutId,
  });
  // The embedded journey's own reading wins when there is one: it is the component actually
  // driving that checkout, and the poll exists for a checkout this box did not open.
  const stageCheckout = inlineCheckout ?? polledCheckout;

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

  /**
   * The buyer's own "add this" instruction, executed.
   *
   * A line proposal the runner built for an explicit add is no longer something the buyer
   * confirms twice: the instruction WAS the confirmation, so this performs the same write
   * the proposal card's press performed -- `api.setLine` against the proposal's basket,
   * carrying the binding the proposal was prepared against -- and opens a basket first
   * when none exists. The server still re-checks the binding under the basket's lock, so
   * a price or catalogue that moved since the proposal was built is refused exactly as it
   * was refused to a press; the refusal surfaces as a problem bubble, never as a silent
   * success.
   *
   * Quantity: a bound proposal carries the absolute the write will send (`quantity`); an
   * unbound one (`no_basket`) has none because there is no line to be absolute against,
   * and on an empty basket the absolute equals the delta the buyer asked for. A basket
   * that could not be read this turn auto-adds nothing -- the card keeps that case, since
   * writing into a cart nobody could read is the one guess here.
   */
  const runDirectAdd = useCallback(
    async (proposal: z.infer<typeof LineProposalSchema>): Promise<Basket> => {
      if (proposal.basket_id !== null && proposal.quantity !== null) {
        const next = await api.setLine(
          proposal.basket_id,
          proposal.sku,
          proposal.quantity,
          newIdempotencyKey(),
          proposal.binding ?? undefined,
        );
        await basket.refresh();
        await shelf.reload();
        return next;
      }
      if (proposal.blocked_by !== "no_basket" || proposal.delta < 1) {
        throw new Error("This add could not be prepared against a readable basket.");
      }
      const created = await api.createBasket();
      basket.setBasketId(created.basket_id);
      const next = await api.setLine(
        created.basket_id,
        proposal.sku,
        proposal.delta,
        newIdempotencyKey(),
      );
      await basket.refresh();
      await shelf.reload();
      return next;
    },
    [basket, shelf],
  );

  const proceedToCheckout = useCallback(
    async (buyerSentence?: string) => {
      const targetBasketId = activeBasketId ?? shelf.basket?.basket_id;
      if (!targetBasketId || (shelf.basket?.lines?.length ?? 0) === 0) {
        return false;
      }
      setPending(true);
      try {
        const card = await api.openCheckout(targetBasketId, newIdempotencyKey());
        basket.setBasketId(null);
        setInlineCheckoutId(card.checkout_id);
        setPayAllowed(false);
        await shelf.reload();

        const lines = card.quote?.lines ?? [];
        const count = lines.length;
        const itemsSummary =
          lines.map((l) => `${l.quantity}× ${l.name}`).join(", ") || `${count} items`;
        const total = card.total.display;
        const currency = card.total.currency;
        const msg = `Here is your order: ${itemsSummary} (Total: ${currency} ${total}). Please confirm your order to proceed to payment.`;

        setMessages((previous) => [
          ...previous,
          ...(buyerSentence
            ? [{ id: nextId(), role: "buyer" as const, text: buyerSentence }]
            : []),
          { id: nextId(), role: "razorai" as const, text: msg, turn: null },
        ]);
        return true;
      } catch (err) {
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "problem" as const, text: humanMessage(err) },
        ]);
        return false;
      } finally {
        setPending(false);
      }
    },
    [activeBasketId, basket, nextId, shelf],
  );

  const addOffer = useCallback(
    async (offer: Offer) => {
      // Opening a checkout consumes the basket, so the id this panel is holding stops
      // accepting lines the moment the buyer reaches an approval card -- and the next "add
      // one more" then failed against a basket that had become a checkout. A shop that
      // cannot take a second order is not a shop, so a refused write opens a fresh basket
      // and lands there. Retried once and only once: a second refusal is a real one.
      let id = activeBasketId ?? (await api.createBasket()).basket_id;
      try {
        await api.setLine(id, offer.sku, offer.quantity);
      } catch {
        id = (await api.createBasket()).basket_id;
        await api.setLine(id, offer.sku, offer.quantity);
      }
      basket.setBasketId(id);
      await basket.refresh();
      await shelf.reload();
      return id;
    },
    [activeBasketId, basket, shelf],
  );

  const send = useCallback(
    async (raw: string) => {
      const message = raw.trim();
      if (!message || pending) return;

      setMessages((previous) => [...previous, { id: nextId(), role: "buyer", text: message }]);
      setPending(true);

      const controller = new AbortController();
      inFlight.current = controller;
      try {
        let currentBasketId = activeBasketId;
        if (!currentBasketId) {
          try {
            const created = await api.createBasket();
            currentBasketId = created.basket_id;
            basket.setBasketId(created.basket_id);
          } catch {
            // fallback if creating basket fails
          }
        }
        const turn = await api.agentTurn(
          {
            message,
            basket_id: currentBasketId ?? undefined,
            checkout_id: checkoutId ?? undefined,
          },
          controller.signal,
        );
        setLastTurn(turn);
        // An explicit add needs no second confirmation: the instruction was one. When the
        // turn carries a line proposal, the panel performs the write it describes -- the
        // same `api.setLine` the card's press performed, binding included -- and reports
        // the outcome beside the reply. The card is suppressed for this turn; the basket
        // header is the receipt. Every other kind of turn renders exactly as before.
        const parsed = LineProposalSchema.safeParse(
          turn.structured !== null && typeof turn.structured === "object"
            ? (turn.structured as Record<string, unknown>).proposal
            : undefined,
        );
        const replyId = nextId();
        setMessages((previous) => [
          ...previous,
          { id: replyId, role: "razorai", text: turn.reply, turn },
        ]);
        // The proposal when the model built one; otherwise the single product this turn
        // read, on the buyer's own instruction to add it. Both paths perform the same
        // write, so "add amul gold" fills the basket whichever way the model answered.
        const offer = parsed.success ? null : ADD_INTENT.test(message) ? offerFromTurn(turn) : null;
        if (parsed.success || offer !== null) {
          try {
            if (parsed.success) await runDirectAdd(parsed.data);
            else if (offer !== null) await addOffer(offer);
            setMessages((previous) =>
              previous.map((entry) =>
                entry.id === replyId && entry.role === "razorai"
                  ? { ...entry, directAdd: { status: "added" as const } }
                  : entry,
              ),
            );
          } catch (error) {
            setMessages((previous) =>
              previous.map((entry) =>
                entry.id === replyId && entry.role === "razorai"
                  ? {
                      ...entry,
                      directAdd: { status: "failed" as const, detail: humanMessage(error) },
                    }
                  : entry,
              ),
            );
          }
        }
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
    [activeBasketId, addOffer, basket, checkoutId, nextId, pending, runDirectAdd],
  );

  /*
   * Permission before every write, and the writes themselves unchanged underneath.
   *
   * Nothing below invents a request. Allow on a slip calls exactly the call the button or the
   * card already called -- `api.setLine`, `api.openCheckout`, the payment panel's own `pay()`
   * -- so the kernel still decides and the platform still executes. What changed is only that
   * the buyer is asked first, in words, with the amount in front of them.
   *
   * The chain is add -> checkout -> pay, and each Allow raises the next question rather than
   * running ahead: a buyer who wanted one bottle of milk should not discover they have opened
   * a checkout because they said yes once.
   */

  /**
   * Open the checkout for a cart, without asking first.
   *
   * Opening one quotes and reserves; it charges nothing and can be cancelled. The question
   * that matters comes a step later and is a better question, because the approval card
   * names every line, the fees and the exact total, where a slip could only name a figure.
   * So there is one confirmation on the way to money, and it is the one that shows the order.
   */
  const goToCheckout = useCallback(
    async (basketId: string) => {
      const card = await api.openCheckout(basketId);
      basket.setBasketId(null);
      setPayAllowed(false);
      setInlineCheckoutId(card.checkout_id);
    },
    [basket],
  );

  /**
   * The cart write itself, shared by the press and by the word.
   *
   * Extracted because a yes used to cost two answers for one intention: RazorAI asked ("Amul
   * Taaza Toned Milk 500 ml is 28.00. Say yes and I'll put it in your cart"), the buyer said
   * yes, and a slip then asked the SAME question again with Add it / Deny. Two confirmations
   * for one add, where the first already named the item and its price.
   *
   * Adding to a cart is not the money step: nothing is charged, nothing is reserved, and a
   * line can be changed or removed afterwards. So the reply IS the question, and a yes
   * answers it. The confirmations that remain are the ones that matter -- confirming the
   * checkout, and paying -- and neither of those was touched.
   */


  // A spoken yes answers the slip on screen when there is one, and otherwise takes the offer
  // by raising the slip for it. The one thing it must never do is both: a buyer answering
  // "yes" to "add two litres?" is not also accepting whatever the last reply happened to
  // offer, and routing it to both would write twice for one word.
  const onAffirmed = useCallback(
    (offer: Offer) => {
      // One question, one answer -- the same rule the typed path follows. RazorAI already
      // named the item and its price and asked; a slip repeating it with buttons was a second
      // confirmation for a reversible, unpriced-to-the-buyer step. Money still asks twice:
      // confirming the checkout and paying are each their own permission.
      setWriteBusy(true);
      void (async () => {
        try {
          await addOffer(offer);
        } catch (error) {
          setMessages((previous) => [
            ...previous,
            { id: nextId(), role: "problem", text: humanMessage(error) },
          ]);
        } finally {
          setWriteBusy(false);
        }
      })();
    },
    [addOffer, nextId],
  );

  const onDenied = useCallback(() => {
    // At the approval card a spoken no declines to pay, and says so; it closes nothing.
    if (inlineCheckout?.state === "APPROVAL_REQUIRED") {
      setMessages((previous) => [
        ...previous,
        { id: nextId(), role: "razorai", text: HELD_OFF, turn: null },
      ]);
      return;
    }
    const hasCartItems = (shelf.basket?.lines?.length ?? 0) > 0;
    if (inlineCheckoutId === null && hasCartItems) {
      void proceedToCheckout("no");
    }
  }, [
    inlineCheckout?.state,
    inlineCheckoutId,
    shelf.basket?.lines?.length,
    proceedToCheckout,
    nextId,
  ]);

  /**
   * Everything the buyer TYPES, routed the way the same words spoken would be.
   *
   * `send` posts a turn, which is the right thing for "milk" and the wrong thing for "yes":
   * a yes is an answer to a question this panel asked, not a new question for the store. The
   * spoken path has always known that -- `transcript.ts` turns a spoken yes into `affirmed`
   * and the panel raises a slip -- while a typed yes went to `POST /v1/agent/turn`, came
   * back as "Would you like to add anything else?", and added nothing. Same word, same
   * offer on screen, two different outcomes depending on which way it was entered.
   *
   * The order matters. A slip already on screen is answered FIRST, because the buyer is
   * replying to the question in front of them rather than re-accepting the reply behind it
   * -- the same reason `onAffirmed` refuses to do both, where doing both would write twice
   * for one word. Only with no slip open does a yes reach back to what the last reply
   * offered.
   *
   * A yes with nothing to accept, and a no with nothing to refuse, both fall through to
   * `send` and get whatever the store says -- this intercepts words that have a referent,
   * and invents no local reply for words that do not.
   */
  const ask = useCallback(
    (raw: string) => {
      const text = raw.trim();
      if (!text || pending) return;

      const isCartCreateOnly = /^(create|make|start|open)\s+(a\s+)?(cart|basket)$/i.test(text);
      if (isCartCreateOnly) {
        void (async () => {
          try {
            setPending(true);
            const created = await api.createBasket();
            basket.setBasketId(created.basket_id);
            await basket.refresh();
            const msg = "I've created a new cart for you! What would you like to add?";
            setMessages((prev) => [
              ...prev,
              { id: nextId(), role: "buyer", text: raw },
              { id: nextId(), role: "razorai", text: msg, turn: null },
            ]);
          } catch (error) {
            setMessages((prev) => [
              ...prev,
              { id: nextId(), role: "problem", text: humanMessage(error) },
            ]);
          } finally {
            setPending(false);
          }
        })();
        return;
      }

      const hasCartItems = (shelf.basket?.lines?.length ?? 0) > 0;
      if (inlineCheckoutId === null && hasCartItems) {
        const isNoMore =
          isNegative(text) ||
          /^(no|nah|nope|nothing|nothing else|no thanks|nahi|kuch nahi)$/i.test(text);
        const isCheckoutIntent =
          /\b(proceed|checkout|pay|place order|done|buy now|order now)\b/i.test(text);
        if (isNoMore || isCheckoutIntent) {
          void proceedToCheckout(raw);
          return;
        }
      }

      // A no while the approval card is up is an answer to THIS question, not a new one for
      // the store. It declines to pay and does nothing else: rejecting the version is the
      // buyer's own press on the card, and one typed word should not close a checkout.
      if (isNegative(text) && inlineCheckout?.state === "APPROVAL_REQUIRED") {
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "razorai", text: HELD_OFF, turn: null },
        ]);
        return;
      }

      if (isAffirmative(text)) {
        // An approval card on screen owns the word. `VoiceConsent` already gives a spoken yes
        // this path; without the typed one the box said "say yes to approve" to a buyer who
        // could only type, and meant it for nobody. The nonce is a bump, not a card: the
        // journey approves what it is DISPLAYING, so nothing here can name a version, a hash
        // or an amount. Restricted to `APPROVAL_REQUIRED` -- once it is approved a yes is not
        // consent to pay, which is its own permission.
        if (inlineCheckout?.state === "APPROVAL_REQUIRED" && inlineCheckout.approval_card) {
          setApproveNonce((n) => n + 1);
          return;
        }
        // Only reach back to what the last reply offered while the conversation is still
        // ABOUT the shelf. Once a checkout is open the box is asking about money, and
        // inferring an add from the search that happened three turns ago would answer a
        // question the buyer is not being asked.
        if (inlineCheckoutId === null) {
          const offer = offerFromTurn(lastTurn);
          if (offer !== null) {
            // The reply already asked, naming the item and its price. This answers it, rather
            // than asking the same thing again with buttons on it.
            setWriteBusy(true);
            void (async () => {
              try {
                await addOffer(offer);
              } catch (error) {
                setMessages((previous) => [
                  ...previous,
                  { id: nextId(), role: "problem", text: humanMessage(error) },
                ]);
              } finally {
                setWriteBusy(false);
              }
            })();
            return;
          }
        }
      }
      void send(text);
    },
    [
      pending,
      inlineCheckoutId,
      shelf.basket?.lines?.length,
      proceedToCheckout,
      inlineCheckout,
      lastTurn,
      send,
      basket,
      nextId,
      addOffer,
    ],
  );

  // The cart strip's Checkout press opens the checkout, and the approval card it lands on
  // is the confirmation: every line, the fees, the total, and the bytes they hash to.
  const checkoutFromStrip = useCallback(() => {
    const id = activeBasketId;
    if (!id) return;
    void goToCheckout(id).catch((error: unknown) => {
      setMessages((previous) => [
        ...previous,
        { id: nextId(), role: "problem", text: humanMessage(error) },
      ]);
    });
  }, [activeBasketId, goToCheckout, nextId]);

  // An Add press on a product card asks the same question a spoken yes does. The name comes
  // from the basket's own quoted names when it has one; the sku is an honest fallback and
  // never a guess at a product's title.
  // An Add press on a product card is the buyer's own press. Asking again with buttons was
  // a second confirmation for a step that charges nothing and can be undone from the cart.
  const askToAdd = useCallback(
    (sku: string, item?: ReplyItem) => {
      void addOffer({
        sku,
        name: item?.name ?? shelf.names[sku] ?? sku,
        quantity: 1,
        unit_price: item?.unit_price ?? null,
      }).catch((error: unknown) => {
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "problem", text: humanMessage(error) },
        ]);
      });
    },
    [addOffer, shelf.names, nextId],
  );

  // The pay question, raised from the embedded checkout's own state rather than guessed: the
  // kernel has admitted the submit and the provider order exists, so there is a real amount
  // to put in front of the buyer. Raised once per checkout.
  const payAsked = useRef<string | null>(null);
  const approvalAnnounced = useRef<string | null>(null);
  const paidAnnounced = useRef<string | null>(null);

  // The card shows the order; RazorAI says it too, because a buyer who arrived here by
  // talking should be told what they are approving without having to read a table. Driven
  // from the checkout the box is showing rather than from the callback that receives it:
  // the callback runs during the journey's own render, and a message appended there was
  // lost to the render it interrupted.
  useEffect(() => {
    const card = inlineCheckout;
    if (!card || card.state !== "APPROVAL_REQUIRED" || !card.approval_card) return;
    const key = `${card.checkout_id}:${card.current_version}`;
    if (approvalAnnounced.current === key) return;
    approvalAnnounced.current = key;
    const sentence = approvalSentence(card);
    setMessages((previous) => [
      ...previous,
      { id: nextId(), role: "razorai", text: sentence, turn: null },
    ]);
  }, [inlineCheckout, nextId]);

  const onInlineState = useCallback(
    (next: Checkout | null) => {
      setInlineCheckout(next);
      if (!next) return;
      // A checkout that ended is no longer the box's business, and leaving its card up would
      // invite an approval of something already closed.
      if (next.state === "CANCELLED" || next.state === "REJECTED" || next.state === "EXPIRED") {
        setInlineCheckoutId(null);
        setInlineCheckout(null);
        setPayAllowed(false);
        return;
      }
      // "Approve to pay" is the permission. Once the kernel has admitted the version, the
      // provider's sheet is the buyer's next act and needs no second press: the button they
      // pressed said what would follow. Waiting for AWAITING_PAYMENT specifically left the
      // sheet closed through EXECUTION_PENDING, which is where the order is actually being
      // created -- so the buyer saw a Pay button and an already-loaded provider doing
      // nothing. The panel still opens nothing until the Razorpay order exists.
      if (PAY_NEXT.has(next.state) && payAsked.current !== next.checkout_id) {
        payAsked.current = next.checkout_id;
        setPayAllowed(true);
      }
      if (next.state === "PAID" && paidAnnounced.current !== next.checkout_id) {
        paidAnnounced.current = next.checkout_id;
        const total = next.approval_card?.total?.display ?? "";
        const currency = next.approval_card?.total?.currency ?? "INR";
        const lines = next.approval_card?.quote?.lines ?? [];
        const itemSummary =
          lines.map((l) => `${l.quantity}× ${l.name}`).join(", ") || "your items";
        const orderId = next.order_id ?? "";
        // RazorAI's own line, drawn in her transcript like every other reply. The browser's
        // speech synthesiser used to say it too, in a different voice from the one the
        // gateway speaks with, so the buyer heard two assistants. Only the gateway speaks.
        const announcement = `Payment of ${currency} ${total} for ${itemSummary} was successful and your order is confirmed!${orderId ? ` Order ID: ${orderId}.` : ""}`;
        setMessages((previous) => [
          ...previous,
          {
            id: nextId(),
            role: "razorai",
            text: announcement,
            turn: null,
          },
        ]);
      }
    },
    [nextId],
  );

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
              onAffirmed={onAffirmed}
              onDenied={onDenied}
              onSession={setSession}
              onStateChange={onVoiceState}
              onSendText={(text) => ask(text)}
              textPending={pending}
              // The product cards a spoken reply draws press the shelf's own write, not a
              // second path of their own: one basket, one request, whichever surface the
              // buyer happened to be looking at.
              onAdd={askToAdd}
              busySku={shelf.busySku}
              // The live cart, held out of the scrolling transcript so it is still there
              // when the buyer decides to check out. Every press on it is the shelf's own
              // write, and Checkout is the same POST the basket page's button sends.
              beforeComposer={
                <div className="relative z-10 flex shrink-0 flex-col gap-2 pt-2">
                  {/* The question, first: while one is on screen it is the only thing the
                      buyer needs to answer, and it sits above the cart it is about. */}
                  {/* The approval and the payment, inside the box, on this session's own
                      microphone. Framed as what it is: the trusted surface, not a card the
                      agent drew. `CheckoutJourney` is the SAME component the checkout page
                      renders, so a spoken yes here matches the same hash, amount, currency
                      and version it matches there. */}
                  {inlineCheckoutId ? (
                    /* NOT `edge-glow-ring` -- that class is a decorative overlay
                        (`position: absolute; inset: -2px; pointer-events: none`) meant for a
                        ring drawn OVER a relatively-positioned parent, and putting it on a
                        content container broke the money step twice over. Absolutely
                        positioned, this block left normal flow: its parent collapsed to 7px
                        of padding and the approval card was laid out at y=1339 in a 950px
                        viewport. `pointer-events: none` then made the whole surface
                        click-transparent, so the Approve button was visible, enabled, and
                        every press went through it to the composer underneath. Approving was
                        impossible with a mouse on either path, spoken or typed. The border
                        below is the same ring, drawn in flow. */
                    <div className="rounded-2xl border border-[#B08CFF]/40 p-2">
                      <p className="px-1 pb-1 font-mono text-[9px] font-semibold uppercase tracking-[0.14em] text-[#B08CFF]">
                        Trusted surface
                      </p>
                      <div className="max-h-[46vh] overflow-y-auto">
                        <CheckoutJourney
                          checkoutId={inlineCheckoutId}
                          embedded
                          voiceFlow={voiceState.live}
                          session={session ?? undefined}
                          onState={onInlineState}
                          payAllowed={payAllowed}
                          approveNonce={approveNonce}
                        />
                      </div>
                    </div>
                  ) : null}

                  {/* The cart, but only while there IS one. Opening a checkout consumes the
                      basket -- `openCheckout` clears the id -- so from that moment the strip
                      has nothing true left to say, and what it actually said was "Your cart
                      is empty" directly underneath an approval card quoting the total of the
                      very items it claimed were gone. It also cost the buyer the approval:
                      this region is `shrink-0`, so the journey's 46vh plus the strip plus the
                      composer overflowed a fixed-height panel, and the thing pushed under the
                      strip was the Approve button -- present, and unpressable, on the one step
                      that has to be pressed for money to move. */}
                  {inlineCheckoutId === null ? (
                    <CartStrip
                      lines={shelf.basket?.lines ?? []}
                      names={shelf.names}
                      total={shelf.basket?.quote?.total ?? null}
                      busySku={shelf.busySku}
                      onSetQuantity={(sku, quantity) => void shelf.setQuantity(sku, quantity)}
                      onCheckout={checkoutFromStrip}
                      checkoutBusy={writeBusy}
                    />
                  ) : null}
                </div>
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
                onAsk={pending ? undefined : (message) => ask(message)}
                onConfirmLine={confirmLine}
                onConfirmCheckout={confirmCheckout}
                onAdd={askToAdd}
                busySku={shelf.busySku}
                // A checkout the card opens is taken in place, so the approval happens
                // in this box rather than on another page.
                onOpened={(id) => {
                  setPayAllowed(false);
                  setInlineCheckoutId(id);
                }}
              />
            </VoicePanel>
          </div>
        </div>
      </div>
    </>
  );
}

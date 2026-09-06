/**
 * The shop, as a conversation.
 *
 * This replaces a storefront with an assistant floating over it. The conversation is now
 * the product: the buyer talks, the cart stands open beside them, and the shelf is a press
 * away for when pointing is easier than describing. Everything that moves money still
 * happens on the surfaces the kernel binds -- the approval card and the payment sheet are
 * the same components the checkout page renders, embedded here rather than reimplemented,
 * because a second implementation of a consent screen is the last thing this project
 * should own.
 *
 * The flow is this component's, not the model's. Earlier the assistant decided whether to
 * call the tool that adds a line, so the same sentence filled the cart on one turn and did
 * nothing on the next -- and the buyer had no way to tell which had happened. Now the
 * buyer's own words decide: `readIntent` says what was asked for, the catalogue resolves
 * which product that is, and `useBasket` performs the write. The model is asked for
 * language and for what to offer next, and it can be slow or wrong without the shop
 * breaking.
 *
 * One writer. Every add, every quantity change, every removal goes through the `useBasket`
 * hook, which reads the current line before writing an absolute quantity. Writing to
 * `api.setLine` directly from here is what made "add milk" twice leave one bottle in the
 * cart: the server assigns the quantity it is given, it does not accumulate, so a second
 * add of the same SKU was a write of 1 over a 1.
 */

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useBasketContext } from "@/components/providers";
import { cx } from "@/components/ui";
import { useBasket } from "@/features/basket/use-basket";
import { CheckoutJourney } from "@/features/checkout/checkout-journey";
import { useVoiceSession } from "@/features/voice/use-voice-session";
import { api, newIdempotencyKey } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { humanMessage } from "@/lib/api/problem";
import { formatMinor } from "@/lib/money";
import type { Checkout, Product, Turn } from "@/lib/api/types";

import { CartRail } from "./cart-rail";
import { ChatStream, type Message } from "./chat-stream";
import { Composer } from "./composer";
import {
  HELD_OFF,
  QUANTITY_CHIPS,
  STAGES,
  chipsFor,
  orderSentence,
  paidSentence,
  readCount,
  readIntent,
  stageOf,
} from "./flow";
import { OrdersSheet } from "./orders-sheet";
import { ShopNav, type Place } from "./shop-nav";
import { StoreSheet } from "./store-sheet";

/**
 * How long the shop waits for the assistant to find words for something already done.
 *
 * Measured against real turns on this stack: a grounded suggestion costs four model calls
 * and lands in about twenty seconds. That is far too long to hold a confirmation the cart
 * has already made true, so the plain sentence goes up instead and the offer is lost for
 * that turn. Better a shop that sometimes does not upsell than one that seems to hang.
 */
const WORDS_TIMEOUT_MS = 9_000;

/** Checkout states from which the buyer's next act is the provider's sheet, not a press. */
const PAY_NEXT: ReadonlySet<string> = new Set([
  "APPROVED",
  "EXECUTION_PENDING",
  "AWAITING_PAYMENT",
  "PAYMENT_UNKNOWN",
]);

/**
 * Where the buyer is, along the bottom.
 *
 * It sat across the top, taking the position that belongs to the shelf, the orders and the
 * cart. Down here it is beside the composer, where the flow it describes is actually
 * happening -- but it is still the buyer's map of a purchase, so it is legible rather than
 * decorative. The first attempt at "small" made it grey on navy at nine pixels, which is
 * not small, it is gone.
 *
 * Numbered, because these are steps in an order and a buyer counting them should not have
 * to. A step behind you carries a tick; the step you are on is the only one in the action
 * colour; the ones ahead are dim but readable.
 */
function StageRail({ stage }: { stage: string }) {
  const index = STAGES.findIndex((entry) => entry.key === stage);
  return (
    <ol
      role="list"
      aria-label="Where you are"
      className="flex shrink-0 items-center justify-center gap-1.5 pb-2"
    >
      {STAGES.map((entry, position) => {
        const done = position < index;
        const here = position === index;
        return (
          <li key={entry.key} className="flex items-center gap-1.5">
            <span
              aria-current={here ? "step" : undefined}
              className={cx(
                "flex items-center gap-1.5 rounded-full px-2 py-1 transition-colors",
                here && "rzp-stage-in bg-[var(--rzp-blue-soft)]",
              )}
            >
              <span
                className={cx(
                  "flex size-4 shrink-0 items-center justify-center rounded-full font-mono text-[9px] font-bold",
                  here
                    ? "bg-[var(--rzp-blue)] text-white"
                    : done
                      ? "bg-emerald-500/20 text-emerald-300"
                      : "bg-white/[0.07] text-slate-500",
                )}
              >
                {done ? (
                  <svg viewBox="0 0 12 12" className="size-2.5" fill="none" aria-hidden="true">
                    <path
                      d="M2.5 6.2l2.3 2.3L9.5 3.8"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                ) : (
                  position + 1
                )}
              </span>
              <span
                className={cx(
                  "text-[11px] font-medium leading-none",
                  here ? "text-white" : done ? "text-emerald-300/80" : "text-slate-500",
                )}
              >
                {entry.label}
              </span>
            </span>
            {position < STAGES.length - 1 ? (
              <span
                aria-hidden="true"
                className={cx("h-px w-3", done ? "bg-emerald-400/40" : "bg-white/10")}
              />
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

let counter = 0;
const nextId = () => `m${++counter}`;

export function CopilotApp() {
  const shelf = useBasket();
  const cart = useBasketContext();
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);
  const [writing, setWriting] = useState(false);

  const [checkoutId, setCheckoutId] = useState<string | null>(null);
  const [checkout, setCheckout] = useState<Checkout | null>(null);
  const [payAllowed, setPayAllowed] = useState(false);
  // The card this checkout is about, kept until the checkout itself changes. Admission
  // spends the approval, so the checkout read stops carrying one from EXECUTION_PENDING
  // onwards -- and the cart column, reading it from there, fell back to an empty cart at
  // the very moment the provider's sheet was asking for money.
  const [card, setCard] = useState<Checkout["approval_card"]>(null);
  const [approveNonce, setApproveNonce] = useState(0);

  const [storeOpen, setStoreOpen] = useState(false);
  const [ordersOpen, setOrdersOpen] = useState(false);
  const [cartOpen, setCartOpen] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  // A product the buyer named without saying how many. The shop asks rather than assumes:
  // "add milk" is a request for milk, not a request for exactly one of it, and a cart that
  // fills itself with quantities nobody chose is a cart the buyer has to audit.
  const [awaitingCount, setAwaitingCount] = useState<Product | null>(null);
  const [hasOrders, setHasOrders] = useState(false);

  // The microphone. Started from a press because browsers will not open a capture device
  // without a gesture, and left on afterwards: this shop is meant to be talked to, and a
  // buyer who has to hold a button down to finish a sentence is using a walkie-talkie.
  const voice = useVoiceSession();
  const spokenUpTo = useRef(0);

  const announced = useRef<string | null>(null);
  const paidAnnounced = useRef<string | null>(null);
  const payAsked = useRef<string | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  const cartLines = shelf.basket?.quote?.lines.length ?? 0;
  const cartCount = useMemo(
    () => Object.values(shelf.quantities).reduce((sum, n) => sum + n, 0),
    [shelf.quantities],
  );
  const stage = stageOf(checkout, cartLines);

  const say = useCallback(
    (text: string, turn: Turn | null = null, products?: readonly Product[]) => {
      setMessages((previous) => [
        ...previous,
        { id: nextId(), role: "copilot", text, turn, products },
      ]);
    },
    [],
  );
  const trouble = useCallback((text: string) => {
    setMessages((previous) => [...previous, { id: nextId(), role: "problem", text }]);
  }, []);

  // Does this buyer have any order history? Asked once, because it decides whether the
  // chips offer help with an order -- and offering to solve a problem nobody has is worse
  // than not offering.
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const page = await api.orders({ limit: 1 });
        if (live) setHasOrders(page.orders.length > 0);
      } catch {
        // No history is the same as an unreadable history for this one decision.
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const greeted = useRef(false);
  useEffect(() => {
    if (greeted.current) return;
    greeted.current = true;
    say(
      "Hello. Tell me what you need and I will put it in your cart — or open the store and " +
        "point at it. I can propose; approving and paying stay with you.",
    );
  }, [say]);

  /** The one writer. Nothing in this file calls `api.setLine`. */
  const putInCart = useCallback(
    async (sku: string, units: number): Promise<void> => {
      setWriting(true);
      try {
        const held = shelf.quantities[sku] ?? 0;
        await shelf.setQuantity(sku, held + units);
      } finally {
        setWriting(false);
      }
    },
    [shelf],
  );

  /**
   * The product the buyer named, resolved against the merchant's own catalogue.
   *
   * Two readings of a phrase that starts with a number are tried, because "7 Up" and
   * "2 milk" look identical to a parser and mean opposite things. Whichever the merchant
   * actually sells wins; when both do, the one with the better search score does.
   */
  const resolve = useCallback(async (phrase: string, whole: string) => {
    const attempts = phrase === whole ? [phrase] : [whole, phrase];
    for (const attempt of attempts) {
      if (attempt.trim().length === 0) continue;
      const found = await api.search(attempt, { limit: 5 });
      const usable = found.hits.filter((hit) => hit.is_available && hit.stock_units > 0);
      if (usable.length > 0) return usable;
    }
    return [];
  }, []);

  /**
   * Ask the specialist for the sentence, and give up on it quickly.
   *
   * The cart write has already happened by the time this runs, so nothing the model does or
   * fails to do can change what the buyer owns -- this is only about words. It is worth
   * waiting a moment for, because the specialist is where the offer comes from ("would you
   * like bread with that?"), and a shop that never offers anything is not doing its job.
   * It is not worth waiting long for: a buyer watching three dots after their cart has
   * visibly changed is being made to wait for nothing.
   *
   * Returns whether it spoke, so the caller can fall back to its own plain confirmation.
   */
  const askForWords = useCallback(
    async (text: string, products?: readonly Product[]): Promise<boolean> => {
      setPending(true);
      const controller = new AbortController();
      const giveUp = window.setTimeout(() => controller.abort(), WORDS_TIMEOUT_MS);
      try {
        const turn = await api.agentTurn(
          {
            message: text,
            basket_id: shelf.basketId ?? undefined,
            checkout_id: checkoutId ?? undefined,
          },
          controller.signal,
        );
        const reply = turn.reply.trim();
        if (reply.length === 0) return false;
        say(reply, turn, products);
        return true;
      } catch {
        return false;
      } finally {
        window.clearTimeout(giveUp);
        setPending(false);
      }
    },
    [checkoutId, say, shelf.basketId],
  );

  const addByPhrase = useCallback(
    async (phrase: string, quantity: number | null, whole: string) => {
      const hits = await resolve(phrase, whole);
      if (hits.length === 0) {
        say(`I could not find "${phrase}" on the shelf. Would you like me to look for something else?`);
        return;
      }
      const [best, ...rest] = hits;
      if (best === undefined) return;
      // A second hit scoring the same as the first is not a winner, it is a question.
      const tied = rest.filter((hit) => hit.score === best.score);
      if (tied.length > 0) {
        setMessages((previous) => [
          ...previous,
          {
            id: nextId(),
            role: "copilot",
            text: "I found more than one of those. Which did you mean?",
            turn: null,
            products: [best, ...tied].slice(0, 4),
          },
        ]);
        return;
      }
      if (quantity === null) {
        setAwaitingCount(best);
        say(
          `${best.display_name} — ${formatMinor(best.unit_price_minor, best.unit_price.currency)}. ` +
            `How many would you like?`,
          null,
          [best],
        );
        return;
      }
      const units = quantity;
      await putInCart(best.sku, units);

      // The cart is already right; what is left is what to SAY, and that is the specialist's
      // job -- it is the turn where a shop offers the one thing that goes with what you just
      // bought. So the assistant is asked for the sentence, and the plain confirmation below
      // is what the buyer gets if it cannot answer in time. One sentence either way: two
      // confirmations of one add is the thing this flow was rebuilt to stop.
      const plain =
        `${units > 1 ? `${units} × ` : ""}${best.display_name} — ` +
        `${formatMinor(best.unit_price_minor, best.unit_price.currency)}. It is in your cart.`;
      // The card goes up either way. A buyer who asked for milk should see the milk that
      // arrived -- the photograph is how they check the shop understood them, and a line of
      // text naming a product is not the same as showing it.
      const spoke = await askForWords(whole, [best]);
      if (!spoke) say(plain, null, [best]);
    },
    [askForWords, putInCart, resolve, say],
  );

  /** Open the checkout the buyer is about to be asked to approve. */
  const review = useCallback(async () => {
    const id = shelf.basketId;
    if (id === null || cartLines === 0) {
      say("Your cart is empty. Tell me what you need, or open the store.");
      return;
    }
    setWriting(true);
    try {
      const card = await api.openCheckout(id, newIdempotencyKey());
      setCheckoutId(card.checkout_id);
      setCard(card);
      setPayAllowed(false);
      await shelf.reload();
    } catch (error) {
      // A cart whose checkout is already at the provider cannot be reopened, and that is
      // the right answer -- money may be moving against those exact lines. But it is a dead
      // end for whoever is holding it, so say what happened and give them the one way out
      // there is. The old cart is not forgotten quietly: they may still be paying for it.
      if (error instanceof ApiError && error.problem.reason === "payment_in_flight") {
        say(
          "That cart is already at the payment page and cannot be changed while a payment " +
            "may be going through. If you have finished with it, say \u201cstart a new cart\u201d " +
            "and I will begin a fresh one.",
        );
        return;
      }
      trouble(humanMessage(error));
    } finally {
      setWriting(false);
    }
  }, [cartLines, say, shelf, trouble]);

  /** Leave a cart that cannot be reopened, and begin one this buyer can actually use. */
  const startFreshCart = useCallback(async () => {
    setWriting(true);
    try {
      const made = await api.createBasket(newIdempotencyKey());
      cart.setBasketId(made.basket_id);
      setCheckoutId(null);
      setCheckout(null);
      setCard(null);
      setPayAllowed(false);
      await shelf.reload();
      say("Right, a fresh cart. What would you like?");
    } catch (error) {
      trouble(humanMessage(error));
    } finally {
      setWriting(false);
    }
  }, [cart, say, shelf, trouble]);

  /** Ask the model for words. It cannot write anything; the cart is already decided. */
  const ask = useCallback(
    async (text: string) => {
      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;
      setPending(true);
      try {
        const turn = await api.agentTurn(
          {
            message: text,
            basket_id: shelf.basketId ?? undefined,
            checkout_id: checkoutId ?? undefined,
          },
          controller.signal,
        );
        say(turn.reply, turn);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        trouble(humanMessage(error));
      } finally {
        if (inFlight.current === controller) inFlight.current = null;
        setPending(false);
      }
    },
    [checkoutId, say, shelf.basketId, trouble],
  );

  /**
   * Ask the platform for money back on one order, and report what it answered.
   *
   * A refund is admitted or denied by the kernel against its own capture ledger, and both
   * answers arrive as HTTP 200 -- "a refund is already in flight", "nothing remains to
   * refund" and "this attempt is reconciling" are the platform working correctly, not
   * failures. So this states the decision rather than celebrating a request: telling a
   * buyer their money is coming back when the kernel declined would be the single most
   * damaging sentence this assistant could say.
   *
   * No amount is sent. Omitted means "everything still refundable", and the figure is the
   * kernel's to resolve against captures and refunds already in flight -- arithmetic this
   * browser cannot do correctly and has no business attempting.
   */
  const requestRefund = useCallback(
    async (orderId: string) => {
      setWriting(true);
      try {
        const result = await api.requestRefund(orderId, { reason: "buyer_requested" });
        if (result.decision.allowed && result.refund !== null) {
          say(
            `Your refund has been requested on order ${orderId}. The platform has accepted it ` +
              `and it is now with the payment provider.`,
          );
        } else {
          say(
            `The platform did not accept that refund: ${result.decision.explanation}. ` +
              `Nothing about your order has changed.`,
          );
        }
      } catch (error) {
        trouble(humanMessage(error));
      } finally {
        setWriting(false);
      }
    },
    [say, trouble],
  );

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (text.length === 0 || pending || writing) return;
      setMessages((previous) => [...previous, { id: nextId(), role: "buyer", text }]);

      // A count answers the question that is standing, and only that question. Read first,
      // because "2" is a quantity here and nothing at all anywhere else.
      if (awaitingCount !== null) {
        const count = readCount(text);
        if (count !== null) {
          const product = awaitingCount;
          setAwaitingCount(null);
          setPending(true);
          try {
            await putInCart(product.sku, count);
            const plain =
              `${count} × ${product.display_name} — ` +
              `${formatMinor(product.unit_price_minor * count, product.unit_price.currency)}. ` +
              `It is in your cart.`;
            const spoke = await askForWords(`add ${count} ${product.display_name}`, [product]);
            if (!spoke) say(plain, null, [product]);
          } catch (error) {
            trouble(humanMessage(error));
          } finally {
            setPending(false);
          }
          return;
        }
        // Anything else drops the question rather than holding the buyer to it. They asked
        // about something else, and a shop that keeps demanding a number is not listening.
        setAwaitingCount(null);
      }

      const intent = readIntent(text);
      const approving = checkout?.state === "APPROVAL_REQUIRED" && checkout.approval_card !== null;

      try {
        switch (intent.kind) {
          case "yes":
            // At the card the word belongs to the card, and nowhere else.
            if (approving) {
              setApproveNonce((n) => n + 1);
              return;
            }
            await ask(text);
            return;
          case "no":
            if (approving && checkout?.approval_card != null) {
              // Recorded, not just said. A buyer who was asked for money and declined is a
              // fact about this checkout, and the audit stream is where it belongs -- an
              // approval card sitting unanswered for ten minutes with no record of the
              // question having been put reads, later, as nobody having asked.
              try {
                await api.hold(checkout.approval_card);
              } catch {
                // The hold is evidence, not a gate. If it could not be written the buyer
                // still declined, and telling them their "no" failed would be worse than
                // useless: nothing was going to happen either way.
              }
              say(HELD_OFF);
              return;
            }
            if (cartLines > 0 && checkoutId === null) {
              await review();
              return;
            }
            await ask(text);
            return;
          case "add":
            setPending(true);
            try {
              await addByPhrase(intent.phrase, intent.quantity, text);
            } finally {
              setPending(false);
            }
            return;
          case "checkout":
            await review();
            return;
          case "show_cart":
            setCartOpen(true);
            say(
              cartLines === 0
                ? "Your cart is empty just now."
                : "Your cart is on the right, with what each line costs.",
            );
            return;
          case "orders":
            setOrdersOpen(true);
            return;
          case "fresh_cart":
            await startFreshCart();
            return;
          case "refund":
            await requestRefund(intent.orderId);
            return;
          case "ask":
            await ask(text);
            return;
        }
      } catch (error) {
        trouble(humanMessage(error));
      }
    },
    [
      addByPhrase,
      ask,
      askForWords,
      awaitingCount,
      putInCart,
      cartLines,
      checkout,
      checkoutId,
      pending,
      requestRefund,
      review,
      startFreshCart,
      say,
      trouble,
      writing,
    ],
  );

  /**
   * The embedded checkout tells the box what it is showing, and the box says it aloud.
   *
   * Driven from state rather than from the callback itself: the callback runs during the
   * journey's own render, and a message appended there was lost to the render it
   * interrupted.
   */
  useEffect(() => {
    if (checkout === null) return;
    if (checkout.state === "APPROVAL_REQUIRED" && checkout.approval_card !== null) {
      const key = `${checkout.checkout_id}:${checkout.current_version}`;
      if (announced.current !== key) {
        announced.current = key;
        say(orderSentence(checkout.approval_card));
      }
    }
    if (checkout.state === "PAID" && paidAnnounced.current !== checkout.checkout_id) {
      paidAnnounced.current = checkout.checkout_id;
      say(paidSentence(checkout));
      void shelf.reload();
      setHasOrders(true);
    }
  }, [checkout, say, shelf]);

  const onCheckoutState = useCallback((next: Checkout | null) => {
    setCheckout(next);
    if (next === null) {
      setCard(null);
      return;
    }
    if (next.approval_card != null) setCard(next.approval_card);
    if (next.state === "CANCELLED" || next.state === "REJECTED" || next.state === "EXPIRED") {
      setCheckoutId(null);
      setCheckout(null);
      setCard(null);
      setPayAllowed(false);
      return;
    }
    // "Approve to pay" was the permission. Once the kernel has admitted the version the
    // provider's sheet is the next act and needs no second press.
    if (PAY_NEXT.has(next.state) && payAsked.current !== next.checkout_id) {
      payAsked.current = next.checkout_id;
      setPayAllowed(true);
    }
  }, []);

  /**
   * What the buyer said out loud, taken into the same flow their typing goes through.
   *
   * Only settled buyer turns, and only the ones the gateway still considers fresh: a
   * transcript that aged past its window was heard and shown but must not act, because a
   * sentence spoken a minute ago and delivered late is not consent to anything now. The
   * sequence number is the high-water mark, so a re-render never replays a turn.
   */
  useEffect(() => {
    for (const entry of voice.transcript.entries) {
      if (entry.kind !== "buyer" || entry.source !== "voice") continue;
      if (entry.stale || entry.seq <= spokenUpTo.current) continue;
      spokenUpTo.current = entry.seq;
      void send(entry.text);
    }
  }, [send, voice.transcript.entries]);

  const listening = voice.mic === "live" && voice.connection === "open";
  const micBlocked = voice.mic === "denied" || voice.mic === "failed";
  const toggleMic = useCallback(() => {
    if (voice.connection === "idle" || voice.connection === "closed") {
      voice.start();
      voice.setTransmitting(true);
      return;
    }
    voice.stop();
  }, [voice]);

  // While a question is standing the chips answer it, because a row of unrelated
  // suggestions under an unanswered question is the shop talking over itself.
  const chips = awaitingCount !== null ? QUANTITY_CHIPS : chipsFor(stage, hasOrders);

  return (
    <div
      className={cx(
        "flex min-h-0 flex-col overflow-hidden bg-[var(--rzp-navy)] text-slate-100",
        fullscreen ? "fixed inset-0 z-50" : "h-[100dvh]",
      )}
    >
      <header className="flex shrink-0 items-center gap-3 border-b border-[var(--rzp-line)] px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-[15px] font-semibold tracking-tight">
            <span className="text-[#FFD166]">Razor</span>
            <span className="text-[#06D6A0]">Sharp</span>
          </p>
          <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-slate-500">
            Shopping copilot
          </p>
        </div>
        <div className="ml-auto">
          <ShopNav
            open={storeOpen ? "store" : ordersOpen ? "orders" : cartOpen ? "cart" : null}
            cartCount={cartCount}
            fullscreen={fullscreen}
            onToggleFullscreen={() => setFullscreen((on) => !on)}
            onOpen={(place: Place) => {
              // One place at a time. Two sheets over each other is two ways to be lost.
              setStoreOpen(place === "store" ? !storeOpen : false);
              setOrdersOpen(place === "orders" ? !ordersOpen : false);
              setCartOpen(place === "cart" ? !cartOpen : false);
            }}
          />
        </div>
      </header>

      {/* The conversation, the cart and whatever panel is open share this region. The
          composer does NOT: it sits below all of it, so a buyer browsing the shelf or
          reading their orders can still just say what they want. A copilot you have to
          close a screen to talk to is a chat window, not a copilot. */}
      <div className="relative flex min-h-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <ChatStream
            messages={messages}
            pending={pending}
            busySku={shelf.busySku}
            onAdd={(sku) => void putInCart(sku, 1)}
            checkout={
              checkoutId === null ? null : (
                <CheckoutJourney
                  checkoutId={checkoutId}
                  embedded
                  onState={onCheckoutState}
                  payAllowed={payAllowed}
                  approveNonce={approveNonce}
                />
              )
            }
          />
        </main>

        <CartRail
          basket={shelf.basket}
          checkout={checkout}
          card={card}
          busySku={shelf.busySku}
          writing={writing}
          onSetQuantity={(sku, quantity) => void shelf.setQuantity(sku, quantity)}
          onCheckout={() => void review()}
          className={cx(
            // Beside the conversation from tablet width up, where there is room for both.
            // Narrower than that it slides in over the conversation from the cart icon,
            // because a 340px column on a phone leaves the chat unusable.
            //
            // The `md:` half of the open state matters: without it, pressing the cart icon
            // on a wide screen laid the column over the conversation it was already sitting
            // beside, and the approval card underneath showed through it. One rail, in one
            // place, whichever way the buyer asked for it.
            "w-[340px] shrink-0",
            cartOpen
              ? "absolute inset-y-0 right-0 z-10 flex md:static md:z-auto"
              : "hidden md:flex",
          )}
        />

        <StoreSheet
          open={storeOpen}
          quantities={shelf.quantities}
          busySku={shelf.busySku}
          onAdd={(sku) => void putInCart(sku, 1)}
          onClose={() => setStoreOpen(false)}
        />
        <OrdersSheet
          open={ordersOpen}
          onClose={() => setOrdersOpen(false)}
          onAsk={(text) => {
            setOrdersOpen(false);
            void send(text);
          }}
        />
      </div>

      <StageRail stage={stage} />
      <Composer
        chips={chips}
        pending={pending || writing}
        onSend={(text) => void send(text)}
        listening={listening}
        micBlocked={micBlocked}
        speaking={voice.transcript.speaking}
        onToggleMic={toggleMic}
      />
    </div>
  );
}

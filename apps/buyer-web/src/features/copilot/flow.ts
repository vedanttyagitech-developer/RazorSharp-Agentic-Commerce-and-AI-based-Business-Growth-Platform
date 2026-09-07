/**
 * What the buyer just asked for, and what the copilot says back.
 *
 * This file exists because the flow used to live inside the model. The assistant was asked
 * to add something, and whether anything reached the cart depended on whether it chose to
 * call the tool that turn -- so "add amul gold" filled the cart on one turn and left it
 * empty on the next, with the same sentence on screen both times. A shop cannot work that
 * way. The buyer's own words decide what happens; the model supplies language and product
 * identity, and nothing else.
 *
 * Everything here is a pure function of text and state, so the decisions can be tested
 * without a browser, a socket or a model.
 */

import type { ApprovalCard, Checkout, Quote } from "@/lib/api/types";
import { formatMinor } from "@/lib/money";

/** Where the buyer is in the one path this shop has. Drawn as a rail across the top. */
export type Stage = "discover" | "cart" | "approve" | "pay" | "order";

export const STAGES: readonly { key: Stage; label: string }[] = [
  { key: "discover", label: "Discover" },
  { key: "cart", label: "Cart" },
  { key: "approve", label: "Approve" },
  { key: "pay", label: "Pay" },
  { key: "order", label: "Order" },
];

/**
 * The stage, read off the facts rather than remembered.
 *
 * A remembered stage is a fourth thing that can disagree with the cart, the checkout and
 * the server; this one cannot, because it is derived from them every render.
 */
export function stageOf(checkout: Checkout | null, cartLines: number): Stage {
  if (checkout !== null) {
    switch (checkout.state) {
      case "PAID":
        return "order";
      case "APPROVED":
      case "EXECUTION_PENDING":
      case "AWAITING_PAYMENT":
      case "PAYMENT_UNKNOWN":
      case "PAYMENT_FAILED":
        return "pay";
      case "APPROVAL_REQUIRED":
        return "approve";
      default:
        break;
    }
  }
  return cartLines > 0 ? "cart" : "discover";
}

export type Intent =
  | { kind: "add"; phrase: string; quantity: number | null }
  | { kind: "checkout" }
  | { kind: "yes" }
  | { kind: "no" }
  | { kind: "show_cart" }
  | { kind: "orders" }
  | { kind: "refund"; orderId: string }
  | { kind: "fresh_cart" }
  | { kind: "ask" };

/*
 * The three languages this shop is spoken to in. Kept as words rather than as a clever
 * regex because someone will have to read this list in a hurry and decide whether a
 * sentence a buyer complained about should have matched.
 */
const YES = [
  "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please do", "go ahead", "do it",
  "haan", "han", "haa", "ha", "ji", "ji haan", "theek hai", "thik hai", "kar do", "karo",
  "हाँ", "हां", "जी", "ठीक है", "कर दो",
];
const NO = [
  "no", "nope", "nah", "not now", "no thanks", "no thank you", "nothing", "nothing else",
  "that's all", "thats all", "that is all", "done", "bas", "bas itna", "nahi", "nahin",
  "nai", "rehne do", "rahne do", "ruko", "abhi nahi", "kuch nahi",
  "नहीं", "नही", "बस", "रहने दो", "रुको", "कुछ नहीं",
];

/** An instruction to put something in the cart, in any of the three languages. */
const ADD =
  /\b(add|buy|order|get me|take|put)\b|\b(chahiye|chaahiye|de do|dedo|dena|dalo|daal do|daldo|le lo|lelo|add kar|add kro|add karo)\b|(चाहिए|दे दो|डाल दो|ले लो|जोड़)/i;

/** An instruction to stop shopping and see the bill. */
const CHECKOUT =
  /\b(checkout|check out|proceed|place (the )?order|order now|buy now|pay now|payment)\b|\b(checkout kar|order kar do|bill bana|paisa|bhugtan)\b|(चेकआउट|ऑर्डर कर|भुगतान)/i;

const SHOW_CART = /\b(cart|cart|my order|what.?s in|kitna hua|total)\b|(कार्ट|कितना हुआ)/i;

/**
 * A request for money back, and the order it is about.
 *
 * The order id is required rather than inferred. "Refund my last order" reads as
 * unambiguous until a buyer has two recent ones, and asking the kernel to reverse a
 * payment against a guess is not a mistake worth risking to save a question.
 */
/** Leaving a cart that cannot be reopened, which is the only way out of one. */
const FRESH_CART = /\b(start (a )?new cart|fresh cart|new cart|clear (my )?cart|naya cart)\b|(नया कार्ट)/i;

const REFUND = /\b(refund|money back|return this|wapas|paisa wapas|refund kar)\b|(रिफंड|पैसा वापस)/i;
/**
 * Either way an order can be named: its reference or its id.
 *
 * The reference is what the shop shows and what a buyer will type back; the id is what a
 * link or a support tool might carry. Both are matched, and whichever was written is passed
 * on unchanged -- this decides nothing about which one the platform resolves.
 */
const ORDER_ID =
  /\b(RS-\d{6}-[0-9A-HJKMNP-TV-Z]{7}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i;
const ORDERS =
  /\b(my orders|order history|previous order|last order|track|where is my|order status)\b|\b(mera order|purane order|kahan hai)\b|(मेरा ऑर्डर|कहाँ है|ट्रैक)/i;

const NUMBER_WORDS: Readonly<Record<string, number>> = {
  one: 1, a: 1, an: 1, ek: 1, "एक": 1,
  two: 2, do: 2, "दो": 2, couple: 2,
  three: 3, teen: 3, "तीन": 3,
  four: 4, char: 4, chaar: 4, "चार": 4,
  five: 5, panch: 5, paanch: 5, "पांच": 5, "पाँच": 5,
  six: 6, chah: 6, chhah: 6, "छह": 6,
  ten: 10, das: 10, dus: 10, "दस": 10,
};

function normalise(text: string): string {
  return text.trim().toLowerCase().replace(/[!.?,]+$/g, "").trim();
}

export function saidYes(text: string): boolean {
  return YES.includes(normalise(text));
}

export function saidNo(text: string): boolean {
  return NO.includes(normalise(text));
}

/**
 * The quantity the buyer named and the words that name the product, separated.
 *
 * Only a leading count is taken, and only when something is left after it. "7 Up" and
 * "50-50 biscuit" are products whose names begin with a number, and reading those as
 * quantities is how a buyer asking for one bottle of a soft drink ends up with seven of
 * something else. The caller resolves both readings against the catalogue and keeps
 * whichever the merchant actually sells, so this returns the count without insisting on it.
 */
export function splitQuantity(phrase: string): { quantity: number | null; rest: string } {
  const words = phrase.trim().split(/\s+/);
  const [head, ...tail] = words;
  if (head === undefined || tail.length === 0) return { quantity: null, rest: phrase.trim() };
  const digits = /^\d{1,2}$/.test(head) ? Number.parseInt(head, 10) : null;
  const named = NUMBER_WORDS[head.toLowerCase()] ?? null;
  const quantity = digits ?? named;
  if (quantity === null || quantity < 1) return { quantity: null, rest: phrase.trim() };
  return { quantity, rest: tail.join(" ") };
}

/** Strip the instruction words, leaving what the buyer is actually naming. */
export function productPhrase(text: string): string {
  return text
    .replace(
      /\b(please|kindly|can you|could you|i want to|i want|i need|add|buy|order|get me|take|put|to (my|the) cart|in (my|the) cart|mujhe|mereko|ek|de do|dedo|dena|dalo|daal do|daldo|le lo|lelo|chahiye|chaahiye|add kar(o|do)?|add kro)\b/gi,
      " ",
    )
    .replace(/\s+/g, " ")
    .trim();
}

export function readIntent(text: string): Intent {
  const trimmed = text.trim();
  if (trimmed.length === 0) return { kind: "ask" };
  if (saidYes(trimmed)) return { kind: "yes" };
  if (saidNo(trimmed)) return { kind: "no" };
  if (FRESH_CART.test(trimmed)) return { kind: "fresh_cart" };
  if (REFUND.test(trimmed)) {
    const named = ORDER_ID.exec(trimmed);
    if (named?.[1] !== undefined) return { kind: "refund", orderId: named[1] };
    // A refund with no order named is a question about which order, not a refund.
    return { kind: "orders" };
  }
  if (ORDERS.test(trimmed)) return { kind: "orders" };
  if (CHECKOUT.test(trimmed)) return { kind: "checkout" };
  if (ADD.test(trimmed)) {
    const phrase = productPhrase(trimmed);
    const { quantity, rest } = splitQuantity(phrase);
    return { kind: "add", phrase: rest.length > 0 ? rest : phrase, quantity };
  }
  if (SHOW_CART.test(trimmed)) return { kind: "show_cart" };
  return { kind: "ask" };
}

// --------------------------------------------------------------- what the copilot says

/**
 * The order, said out loud at the moment the buyer is asked to pay for it.
 *
 * A buyer who arrived here by talking should not have to read a table to find out what
 * they are approving. Every figure is the card's own; nothing is added up here.
 */
export function orderSentence(card: ApprovalCard): string {
  const lines = card.quote?.lines ?? [];
  const items =
    lines.length > 0
      ? lines.map((line) => `${line.quantity} × ${line.name}`).join(", ")
      : "your cart";
  return (
    `Here is your order: ${items}. The total is ${formatMinor(card.amount_minor, card.currency)}, ` +
    `including delivery and tax. Say yes to approve and pay, or no to hold off.`
  );
}

/**
 * What the copilot says when the buyer declines at the approval card.
 *
 * A "no" here is not a cancellation and must not be treated as one: the buyer declined to
 * pay at this moment, which leaves the order exactly where it was. The reply says that,
 * and then asks the one question that actually moves things -- whether something should
 * come out or change -- because "let me know if you need anything" leaves a buyer holding
 * an unpaid order with no idea what to do with it.
 */
export const HELD_OFF =
  "No problem, I have put the order on hold. Nothing has been paid and your cart is exactly " +
  "as it was. Would you like to drop an item, change one, or pay for it later?";

export function paidSentence(
  checkout: Checkout,
  /**
   * The card the buyer approved, held by the host.
   *
   * Not read off the checkout: admission consumes the approval, so by the time a checkout
   * reads PAID it no longer carries one -- and the sentence that tells a buyer what they
   * just paid for was falling back to "your items" for no reason other than that.
   */
  approved: ApprovalCard | null,
): string {
  const card = checkout.approval_card ?? approved;
  const lines = card?.quote?.lines ?? [];
  const items =
    lines.length > 0
      ? lines.map((line) => `${line.quantity} × ${line.name}`).join(", ")
      : "your order";
  const amount = card === null ? "" : `${formatMinor(card.amount_minor, card.currency)} `;
  // The reference, which a person can repeat; the id is what the platform joins on and is
  // no use to anybody reading it aloud. Falls back to the id only when the server sent no
  // reference, because inventing one here would be a second definition of an order's name.
  const named = checkout.order_reference ?? checkout.order_id;
  const order = named === null ? "" : ` Your order number is ${named}.`;
  return `Payment of ${amount}for ${items} was successful and your order is confirmed.${order}`;
}

/**
 * What a shop says after it has been paid.
 *
 * The conversation used to stop dead at "your order is confirmed", which is where a real
 * shopkeeper would say something. This is the moment a buyer is most willing to buy again
 * -- they are standing there, they have just paid, and they are waiting -- and it costs
 * them nothing to be asked. It offers rather than pushes: the order is on its way whatever
 * they answer.
 */
export function deliverySentence(checkout: Checkout): string {
  const named = checkout.order_reference ?? checkout.order_id;
  const order = named === null ? "your order" : `order ${named}`;
  return (
    `It is on its way. Would you like anything else while ${order} is being delivered? ` +
    `You can also track it from Orders.`
  );
}

/**
 * The one thing worth offering next, when the shop can see a reason for it.
 *
 * Only the free-delivery case is decided here, because it is the only suggestion whose
 * reason is a figure the server already returned. Anything to do with which product goes
 * with which is the model's to make, from products it has actually read.
 */
export function deliveryNudge(quote: Quote | null): string | null {
  if (quote === null || quote.free_delivery_applied) return null;
  const gap = quote.gap_to_free_delivery_minor;
  if (gap === null || gap <= 0) return null;
  return `You are ${formatMinor(gap, quote.currency)} away from free delivery.`;
}

// ------------------------------------------------------------------------- suggestions

export interface Chip {
  label: string;
  send: string;
}

/**
 * The counts a shop offers when it asks how many.
 *
 * Four, because a buyer wanting seven of something will say so, and a row of ten numbers
 * is a form. They are ordinary sentences like every other chip, so answering by typing
 * "three" does the same thing as pressing one.
 */
export const QUANTITY_CHIPS: readonly Chip[] = [
  { label: "1", send: "1" },
  { label: "2", send: "2" },
  { label: "3", send: "3" },
  { label: "6", send: "6" },
];

/**
 * A bare count, when the shop has just asked for one.
 *
 * Only meaningful while a question is standing: "2" typed out of nowhere is not an order
 * for two of anything, and reading it as one would put something in the cart that nobody
 * asked for.
 */
export function readCount(text: string): number | null {
  const word = text.trim().toLowerCase().replace(/[.!]+$/, "");
  const digits = /^\d{1,2}$/.test(word) ? Number.parseInt(word, 10) : null;
  const named = NUMBER_WORDS[word] ?? null;
  const count = digits ?? named;
  return count !== null && count >= 1 && count <= 50 ? count : null;
}

/**
 * The chips under the composer: what a buyer at this point in the flow usually wants next.
 *
 * They send ordinary sentences through the ordinary path, so a chip can never do something
 * the buyer could not have typed. Order help appears only once there is an order to ask
 * about -- a support prompt on an empty cart is an offer to solve a problem nobody has.
 */
export function chipsFor(stage: Stage, hasOrders: boolean): readonly Chip[] {
  const orderHelp: Chip = { label: "Help with a recent order", send: "help with my recent order" };
  switch (stage) {
    case "discover":
      return hasOrders
        ? [{ label: "Open the store", send: "show me the store" }, orderHelp]
        : [{ label: "Open the store", send: "show me the store" }];
    case "cart":
      return [
        { label: "Add more items", send: "I want to add something else" },
        { label: "Proceed to checkout", send: "proceed to checkout" },
        ...(hasOrders ? [orderHelp] : []),
      ];
    case "approve":
      return [
        { label: "Approve and pay", send: "yes" },
        { label: "Hold off", send: "no" },
        { label: "What am I paying for?", send: "show me the cart" },
      ];
    case "pay":
      return [{ label: "Check payment", send: "what is the payment status" }];
    case "order":
      return [
        { label: "Track this order", send: "track my order" },
        { label: "Shop again", send: "show me the store" },
      ];
  }
}

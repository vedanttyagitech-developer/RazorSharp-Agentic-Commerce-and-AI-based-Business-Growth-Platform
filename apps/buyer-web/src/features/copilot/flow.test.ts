/**
 * The flow's decisions, held in place.
 *
 * These are the rules that were learned the expensive way. For two days the shop's
 * behaviour depended on whether a model chose to call a tool, and the symptom was always
 * the same: the assistant said it had added something and the cart stayed empty. Moving
 * the decisions into `flow.ts` fixed that, and these tests are what stop them drifting
 * back -- every case below is one that actually went wrong on screen.
 */

import { describe, expect, it } from "vitest";

import type { ApprovalCard, Checkout } from "@/lib/api/types";

import {
  HELD_OFF,
  chipsFor,
  deliveryNudge,
  deliverySentence,
  orderSentence,
  paidSentence,
  productPhrase,
  readIntent,
  saidNo,
  saidYes,
  splitQuantity,
  stageOf,
} from "./flow";

describe("reading what the buyer asked for", () => {
  it("takes an add in English, Hinglish and Hindi", () => {
    for (const said of [
      "add amul gold milk",
      "mujhe do packet doodh chahiye",
      "ek bread daal do",
      "दो दूध चाहिए",
    ]) {
      expect(readIntent(said).kind, said).toBe("add");
    }
  });

  it("hears a bare yes and a bare no, and nothing longer", () => {
    expect(saidYes("haan")).toBe(true);
    expect(saidYes("ok")).toBe(true);
    expect(saidNo("nahi")).toBe(true);
    expect(saidNo("bas")).toBe(true);
    // "no thanks, add bread instead" is an instruction, not a refusal, and treating it as
    // one would silently drop the add.
    expect(saidNo("no thanks, add bread instead")).toBe(false);
    expect(saidYes("yes please add two more")).toBe(false);
  });

  it("routes checkout, cart and order questions away from the shelf", () => {
    expect(readIntent("proceed to checkout").kind).toBe("checkout");
    expect(readIntent("what is in my cart").kind).toBe("show_cart");
    expect(readIntent("where is my order").kind).toBe("orders");
    expect(readIntent("do you have oat milk?").kind).toBe("ask");
  });
});

describe("a phrase that starts with a number", () => {
  it("reads a leading count as a count", () => {
    expect(splitQuantity("2 milk")).toEqual({ quantity: 2, rest: "milk" });
    expect(splitQuantity("do doodh")).toEqual({ quantity: 2, rest: "doodh" });
  });

  it("still offers the whole phrase, so a product named after a number survives", () => {
    // "7 Up" and "50-50 biscuit" are products. The split says 7 and "Up"; the caller tries
    // both readings against the catalogue and keeps whichever the merchant actually sells.
    // What matters here is that the original words are not thrown away.
    const split = splitQuantity("7 up");
    expect(split.quantity).toBe(7);
    expect(split.rest).toBe("up");
  });

  it("keeps a bare number as the whole phrase, having nothing left to name", () => {
    expect(splitQuantity("7")).toEqual({ quantity: null, rest: "7" });
  });
});

describe("what is left after the instruction words", () => {
  it("keeps the product and drops the asking", () => {
    expect(productPhrase("please add amul gold full cream milk to my cart")).toBe(
      "amul gold full cream milk",
    );
    expect(productPhrase("mujhe brown bread chahiye")).toBe("brown bread");
  });
});

describe("the stage rail", () => {
  const at = (state: string): Checkout => ({ state }) as unknown as Checkout;

  it("is read off the facts, never remembered", () => {
    expect(stageOf(null, 0)).toBe("discover");
    expect(stageOf(null, 2)).toBe("cart");
    expect(stageOf(at("APPROVAL_REQUIRED"), 0)).toBe("approve");
    expect(stageOf(at("EXECUTION_PENDING"), 0)).toBe("pay");
    expect(stageOf(at("AWAITING_PAYMENT"), 0)).toBe("pay");
    expect(stageOf(at("PAID"), 0)).toBe("order");
  });

  it("falls back to the cart when a checkout ended without paying", () => {
    // A cancelled or invalidated checkout is not a stage the buyer is in; the cart they
    // still hold is.
    expect(stageOf(at("CANCELLED"), 3)).toBe("cart");
    expect(stageOf(at("INVALIDATED"), 0)).toBe("discover");
  });
});

describe("what the copilot says at the approval", () => {
  const card = {
    amount_minor: 15500,
    currency: "INR",
    total: { minor: 15500, currency: "INR", display: "155.00" },
    quote: {
      lines: [
        { quantity: 1, name: "Amul Gold Full Cream Milk 1 L" },
        { quantity: 2, name: "Britannia Brown Bread 400 g" },
      ],
    },
  } as unknown as ApprovalCard;

  it("names every line and the amount the button will charge", () => {
    const said = orderSentence(card);
    expect(said).toContain("1 × Amul Gold Full Cream Milk 1 L");
    expect(said).toContain("2 × Britannia Brown Bread 400 g");
    // The figure must be the card's own, formatted the way the button formats it: a
    // sentence and a button that disagree about the amount is the worst bug on this screen.
    expect(said).toContain("₹155.00");
    expect(said).toMatch(/yes to approve and pay, or no to hold off/);
  });

  it("treats a no as a hold and asks what to change", () => {
    // Not a cancellation. The buyer declined to pay now, which leaves the order where it
    // was -- and then the shop has to ask the one question that moves things along.
    expect(HELD_OFF).toContain("on hold");
    expect(HELD_OFF).toContain("Nothing has been paid");
    expect(HELD_OFF).toMatch(/drop an item, change one, or pay for it later/);
  });
});

describe("what the copilot says once it is paid", () => {
  it("reads the card the host held, because a paid checkout no longer carries one", () => {
    // Admission consumes the approval. Reading the items off the checkout at PAID gave
    // "your order" and no amount, on the one screen where the buyer most wants both.
    const spent = { state: "PAID", order_id: "01a0", approval_card: null } as unknown as Checkout;
    const held = {
      amount_minor: 9900,
      currency: "INR",
      total: { minor: 9900, currency: "INR", display: "99.00" },
      quote: { lines: [{ quantity: 3, name: "Amul Taaza Toned Milk 500 ml" }] },
    } as unknown as ApprovalCard;
    const said = paidSentence(spent, held);
    expect(said).toContain("₹99.00");
    expect(said).toContain("3 × Amul Taaza Toned Milk 500 ml");
  });

  it("asks for more business while the order is on its way", () => {
    // The conversation used to stop at "confirmed", which is exactly where a shopkeeper
    // would say something.
    const paid = { state: "PAID", order_id: "01a07800-abcd" } as unknown as Checkout;
    const said = deliverySentence(paid);
    expect(said).toContain("on its way");
    expect(said).toContain("01a07800-abcd");
    expect(said).toMatch(/anything else/i);
  });

  it("states the amount, the items and the order number", () => {
    const checkout = {
      state: "PAID",
      order_id: "01a07800-abcd",
      reference: "RS-260905-TESTREF",
      approval_card: {
        amount_minor: 15500,
        currency: "INR",
        total: { minor: 15500, currency: "INR", display: "155.00" },
        quote: { lines: [{ quantity: 1, name: "Amul Gold Full Cream Milk 1 L" }] },
      },
    } as unknown as Checkout;
    const said = paidSentence(checkout, null);
    expect(said).toContain("₹155.00");
    expect(said).toContain("Amul Gold Full Cream Milk 1 L");
    expect(said).toContain("successful");
    expect(said).toContain("01a07800-abcd");
  });
});

describe("the free-delivery nudge", () => {
  it("speaks only when the merchant says there is a gap", () => {
    expect(
      deliveryNudge({
        currency: "INR",
        free_delivery_applied: false,
        gap_to_free_delivery_minor: 4000,
      } as never),
    ).toBe("You are ₹40.00 away from free delivery.");
  });

  it("says nothing once delivery is already free, and nothing when there is no threshold", () => {
    expect(
      deliveryNudge({
        currency: "INR",
        free_delivery_applied: true,
        gap_to_free_delivery_minor: 4000,
      } as never),
    ).toBeNull();
    expect(
      deliveryNudge({
        currency: "INR",
        free_delivery_applied: false,
        gap_to_free_delivery_minor: null,
      } as never),
    ).toBeNull();
    expect(deliveryNudge(null)).toBeNull();
  });
});

describe("the chips under the composer", () => {
  it("offers order help only to someone who has an order", () => {
    // An assistant that opens with "need help with an order?" to a buyer who has never
    // bought anything is offering to solve a problem nobody has.
    const without = chipsFor("discover", false).map((chip) => chip.label);
    const with_ = chipsFor("discover", true).map((chip) => chip.label);
    expect(without.some((label) => /order/i.test(label))).toBe(false);
    expect(with_.some((label) => /order/i.test(label))).toBe(true);
  });

  it("sends sentences a buyer could have typed, never a privileged action", () => {
    for (const stage of ["discover", "cart", "approve", "pay", "order"] as const) {
      for (const chip of chipsFor(stage, true)) {
        expect(chip.send.trim().length).toBeGreaterThan(0);
        expect(chip.send).not.toMatch(/^\//);
      }
    }
  });

  it("puts approving and holding off in front of the buyer at the approval", () => {
    const labels = chipsFor("approve", false).map((chip) => chip.label);
    expect(labels).toContain("Approve and pay");
    expect(labels).toContain("Hold off");
  });
});

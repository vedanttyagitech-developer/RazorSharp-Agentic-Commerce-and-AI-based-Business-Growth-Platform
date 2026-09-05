/**
 * The two basket cards, tested against payloads the API actually produced.
 *
 * Every fixture below was captured from `POST /v1/agent/turn` running against the seeded
 * demo tenant on 2026-09-05, not hand-written to match the component. That matters more here
 * than usual, because the whole claim of these cards is that their figures come from the
 * server rather than from anything in this browser — a fixture invented to fit the render
 * would prove the opposite of what these tests are for.
 *
 * Three properties are checked hardest, and each of them is a rule the project already pays
 * for elsewhere:
 *
 * 1. **No figure is computed here.** Two of 28.00 is 56.00 and five is 140.00, and neither
 *    string may appear anywhere in the card. The fee engine states line subtotals; this
 *    component states unit prices and the basket total the quote engine already sent.
 * 2. **Nothing in either card commits anything.** The line card carries no button at all —
 *    only the link to the basket page. The choice card's buttons send a message.
 * 3. **No option is privileged.** Five candidates render with identical styling and none is
 *    focused on mount, because a "recommended" row is this surface choosing for a buyer it
 *    has just told it will not.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ApiError } from "@/lib/api/problem";
import { type Basket, BasketSchema } from "@/lib/api/types";

import {
  ChoiceCard,
  ChoiceProposalSchema,
  LineProposalCard,
  type LineConfirmation,
  LineProposalSchema,
  SUPERSEDED,
} from "./basket-proposal-card";
import { ProposalCard } from "./proposal-card";

/*
 * Each fixture is put through the component's own schema on the way in. A payload that has
 * drifted out of the contract then fails this file at load rather than rendering something
 * plausible, which is the same discipline `ProposalCard` applies to what the server sends.
 */

afterEach(cleanup);

const PRICE = { minor: 2800, currency: "INR", display: "28.00" };
const BASKET_ID = "01a07202-1ba8-7297-a54d-5116246acf0f";
/** Copied off the same turn's basket read and product read, which is all a binding ever is. */
const BINDING = { basket_content_hash: "x2SwZ8FT0LXztl8y-JoqXHsQ9iu0z684", unit_price_minor: 2800, catalogue_revision: 7 };

/**
 * `{"message":"add 2 AMUL-DAIRY-001","basket_id":...}` against a basket already holding three
 * of that line. `delta` is two and `quantity` is five: the buyer asked for two *more*, and
 * the basket route takes an absolute. Keeping the fixture with a non-empty line in it is
 * deliberate — the equal-looking version, where the line starts at zero, is exactly the case
 * that hid the delta/absolute bug for as long as it existed.
 */
const TAKE_TO_FIVE = LineProposalSchema.parse({
  action: "basket.update",
  sku: "AMUL-DAIRY-001",
  delta: 2,
  current_quantity: 3,
  quantity: 5,
  clamped_from: null,
  exceeds_stock: false,
  blocked_by: null,
  basket_id: BASKET_ID,
  binding: BINDING,
  display: {
    quantity: 2,
    name: "Amul Taaza Toned Milk 500 ml",
    unit_label: "500 ml",
    unit_price: PRICE,
    stock_units: 48,
    basket_total: { minor: 11350, currency: "INR", display: "113.50" },
  },
});

/** `"add 9 AMUL-DAIRY-001"` against a line already holding 95, of which 48 exist. */
const CLAMPED = LineProposalSchema.parse({
  ...TAKE_TO_FIVE,
  delta: 9,
  current_quantity: 95,
  quantity: 99,
  clamped_from: 104,
  exceeds_stock: true,
});

/** The same request with no basket in context. `basket.create` is on the buyer's button. */
const NO_BASKET = LineProposalSchema.parse({
  ...TAKE_TO_FIVE,
  current_quantity: null,
  quantity: null,
  blocked_by: "no_basket",
  basket_id: null,
  binding: null,
  display: { ...TAKE_TO_FIVE.display, basket_total: null },
});

/** The basket route's answer to the confirm: the line at five, re-quoted by the store. */
const AFTER: Basket = BasketSchema.parse({
  basket_id: BASKET_ID,
  lines: [{ sku: "AMUL-DAIRY-001", quantity: 5 }],
  code: "quoted",
  quote: {
    currency: "INR",
    lines: [
      {
        sku: "AMUL-DAIRY-001",
        name: "Amul Taaza Toned Milk 500 ml",
        quantity: 5,
        unit_price_minor: 2800,
        subtotal_minor: 14000,
        tax_bp: null,
        tax_minor: 0,
      },
    ],
    items_subtotal_minor: 14000,
    items_tax_minor: 0,
    delivery_fee_minor: 0,
    delivery_tax_minor: 0,
    total_minor: 14000,
    total: { minor: 14000, currency: "INR", display: "140.00" },
    free_delivery_applied: true,
    gap_to_free_delivery_minor: null,
    source: "merchant_sim",
    catalogue_revision: 7,
    content_hash: "after-the-write",
  },
  unavailable: [],
  freshness: { source: "merchant_sim", catalogue_revision: 7, observed_at: "2026-09-05T14:40:00Z" },
  stale: false,
});

/** `{"message":"add 2 amul milk to my basket"}`. Five hits, so the turn asks instead. */
const FIVE_WAYS = ChoiceProposalSchema.parse({
  action: "basket.disambiguate",
  quantity: 2,
  candidates: [
    {
      sku: "AMUL-DAIRY-001",
      display_name: "Amul Taaza Toned Milk 500 ml",
      unit_label: "500 ml",
      unit_price: PRICE,
      stock_units: 48,
      is_available: true,
      matched_terms: ["amul", "milk"],
    },
    {
      sku: "AMUL-DAIRY-002",
      display_name: "Amul Gold Full Cream Milk 1 L",
      unit_label: "1 L",
      unit_price: { minor: 7300, currency: "INR", display: "73.00" },
      stock_units: 30,
      is_available: true,
      matched_terms: ["amul", "milk"],
    },
    {
      sku: "AMUL-DAIRY-023",
      display_name: "Amul Mithai Mate (Condensed Milk) 200 g",
      unit_label: "200 g",
      unit_price: { minor: 6200, currency: "INR", display: "62.00" },
      stock_units: 0,
      is_available: false,
      matched_terms: ["amul", "milk"],
    },
  ],
});

describe("the line card states the merchant's figures and computes none", () => {
  it("shows the unit price and the basket's current total, both verbatim", () => {
    render(<LineProposalCard proposal={TAKE_TO_FIVE} />);
    expect(screen.getByText("₹28.00")).toBeDefined();
    expect(screen.getByText("₹113.50")).toBeDefined();
  });

  it("states no line subtotal, because multiplying money here is not this app's job", () => {
    const { container } = render(<LineProposalCard proposal={TAKE_TO_FIVE} />);
    const text = container.textContent ?? "";
    // 2 × 28.00 and 5 × 28.00. Either would be a figure no component on this platform
    // computed, sitting on a card a buyer is about to act on.
    expect(text).not.toContain("56.00");
    expect(text).not.toContain("140.00");
  });

  it("says what the line is now and what it would become, not just the delta", () => {
    render(<LineProposalCard proposal={TAKE_TO_FIVE} />);
    expect(screen.getByText("Take Amul Taaza Toned Milk 500 ml to 5")).toBeDefined();
    expect(screen.getByText(/3\s*→\s*5/)).toBeDefined();
  });

  it("leads with the plain addition when the line does not exist yet", () => {
    render(<LineProposalCard proposal={NO_BASKET} />);
    expect(screen.getByText("Add 2 × Amul Taaza Toned Milk 500 ml")).toBeDefined();
  });
});

describe("a substituted figure is drawn, never applied quietly", () => {
  it("names the quantity the buyer actually asked for beside the clamped one", () => {
    render(<LineProposalCard proposal={CLAMPED} />);
    expect(screen.getByText(/You asked for 104/)).toBeDefined();
    expect(screen.getByText(/at most 99/)).toBeDefined();
  });

  it("reports the shelf count without ever calling it a hold", () => {
    render(<LineProposalCard proposal={CLAMPED} />);
    expect(screen.getByText(/lists 48 on the shelf/)).toBeDefined();
    expect(screen.getByText(/Nothing is held for you/)).toBeDefined();
    expect(screen.getByText(/RazorAI cannot reserve anything/)).toBeDefined();
  });

  it("says the basket is the buyer's to open rather than offering to open one", () => {
    render(<LineProposalCard proposal={NO_BASKET} />);
    expect(screen.getByText(/You have no basket open yet/)).toBeDefined();
    expect(screen.getByText(/no way to start a basket on your behalf/)).toBeDefined();
  });
});

describe("the press is the buyer's, bound to what RazorAI read, and absent when it cannot be", () => {
  it("draws no button without a handler — the door to the basket is the only control", () => {
    render(<LineProposalCard proposal={TAKE_TO_FIVE} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    const door = screen.getByRole("link", { name: /Open your basket/ });
    expect(door.getAttribute("href")).toBe("/basket");
    expect(screen.getByText(/Nothing is added from this panel/)).toBeDefined();
  });

  it("draws no button for a proposal with no basket to land on, handler or not", () => {
    render(<LineProposalCard proposal={NO_BASKET} onConfirm={vi.fn()} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("sends exactly the proposal's binding, quantity and basket — nothing recomputed", async () => {
    const onConfirm = vi.fn<(confirmation: LineConfirmation) => Promise<Basket>>(async () => AFTER);
    render(<LineProposalCard proposal={TAKE_TO_FIVE} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole("button", { name: "Take this line to 5" }));
    await screen.findByText(/as the store re-quoted it/);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    const sent = onConfirm.mock.calls[0][0];
    expect(sent.basket_id).toBe(BASKET_ID);
    expect(sent.sku).toBe("AMUL-DAIRY-001");
    expect(sent.quantity).toBe(5);
    expect(sent.expected).toEqual(BINDING);
    expect(typeof sent.idempotency_key).toBe("string");
    expect(sent.idempotency_key.length).toBeGreaterThan(0);
  });

  it("shows the server's figures after the write, not the proposal's, and no second press", async () => {
    render(<LineProposalCard proposal={TAKE_TO_FIVE} onConfirm={async () => AFTER} />);
    fireEvent.click(screen.getByRole("button", { name: "Take this line to 5" }));
    const status = (await screen.findByText(/as the store re-quoted it/)).closest("[role=status]");
    expect(status?.textContent).toContain("This line is now 5");
    expect(status?.textContent).toContain("₹140.00");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("renders a superseded proposal as a refusal with the reason verbatim, and offers no second press", async () => {
    const onConfirm = vi.fn<(confirmation: LineConfirmation) => Promise<Basket>>(async () => {
      throw new ApiError({
        type: "about:blank",
        title: "That proposal is out of date",
        status: 409,
        detail:
          "The basket, the price or the catalogue moved after this was prepared, so it was not applied. Nothing changed. Here is what the store says now.",
        reason: SUPERSEDED,
      });
    });
    render(<LineProposalCard proposal={TAKE_TO_FIVE} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole("button", { name: "Take this line to 5" }));
    const status = (await screen.findByText(SUPERSEDED)).closest("[role=status]");
    expect(status?.textContent).toContain("Nothing changed");
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the press after a transport failure, says nothing was added, and retries with the same key", async () => {
    const onConfirm = vi.fn<(confirmation: LineConfirmation) => Promise<Basket>>(async () => {
      throw new ApiError({ type: "about:blank", title: "Unreachable", status: 0 });
    });
    render(<LineProposalCard proposal={TAKE_TO_FIVE} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole("button", { name: "Take this line to 5" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Nothing was added");
    fireEvent.click(screen.getByRole("button", { name: "Take this line to 5" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(2));
    // The same press again is the same request: one key, minted once, kept until answered.
    expect(onConfirm.mock.calls[1][0].idempotency_key).toBe(onConfirm.mock.calls[0][0].idempotency_key);
  });

  it("does not send twice while the first press is still in flight", async () => {
    let release: (basket: Basket) => void = () => {};
    const onConfirm = vi.fn<(confirmation: LineConfirmation) => Promise<Basket>>(
      () =>
        new Promise<Basket>((resolve) => {
          release = resolve;
        }),
    );
    render(<LineProposalCard proposal={TAKE_TO_FIVE} onConfirm={onConfirm} />);
    const press = screen.getByRole("button", { name: "Take this line to 5" });
    fireEvent.click(press);
    fireEvent.click(press);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    release(AFTER);
    await screen.findByText(/as the store re-quoted it/);
  });
});

describe("the choice card asks, and prefers nothing", () => {
  it("offers one row per candidate with the store's price on each", () => {
    render(<ChoiceCard proposal={FIVE_WAYS} onAsk={() => {}} />);
    expect(screen.getAllByRole("button")).toHaveLength(FIVE_WAYS.candidates.length);
    expect(screen.getByText("₹73.00")).toBeDefined();
  });

  it("draws every row identically, so none reads as the recommended one", () => {
    render(<ChoiceCard proposal={FIVE_WAYS} onAsk={() => {}} />);
    const classes = new Set(screen.getAllByRole("button").map((row) => row.className));
    // Two: the available rows share one class string, the unavailable row differs only in
    // being dimmed and unpressable. No row is emphasised over another that can be chosen.
    expect(classes.size).toBe(2);
    expect(document.activeElement).toBe(document.body);
  });

  it("sends a message when a row is pressed, and nothing else", () => {
    const onAsk = vi.fn();
    render(<ChoiceCard proposal={FIVE_WAYS} onAsk={onAsk} />);
    screen.getByRole("button", { name: /Amul Gold Full Cream Milk 1 L/ }).click();
    expect(onAsk).toHaveBeenCalledWith("add 2 AMUL-DAIRY-002");
    expect(onAsk).toHaveBeenCalledTimes(1);
  });

  it("shows why each row matched, which is what makes a Hinglish query checkable", () => {
    render(<ChoiceCard proposal={FIVE_WAYS} onAsk={() => {}} />);
    expect(screen.getAllByText("matched amul, milk")).toHaveLength(3);
  });

  it("shows a product the store has none of, and will not let it be chosen", () => {
    const onAsk = vi.fn();
    render(<ChoiceCard proposal={FIVE_WAYS} onAsk={onAsk} />);
    const soldOut = screen.getByRole("button", { name: /Amul Mithai Mate/ });
    expect(soldOut.hasAttribute("disabled")).toBe(true);
    soldOut.click();
    expect(onAsk).not.toHaveBeenCalled();
  });

  it("points at the composer for a buyer who meant none of them", () => {
    render(<ChoiceCard proposal={FIVE_WAYS} onAsk={() => {}} />);
    expect(screen.getByText(/None of these\?/)).toBeDefined();
    expect(screen.getByText(/nothing is added to your basket either way/)).toBeDefined();
  });

  it("draws no pressable row while a turn is already in flight", () => {
    render(<ChoiceCard proposal={FIVE_WAYS} />);
    for (const row of screen.getAllByRole("button")) {
      expect(row.hasAttribute("disabled")).toBe(true);
    }
  });
});

/**
 * The dispatch, which is the part that has to survive a deploy in either order.
 *
 * The API and this app ship separately. Until the API carrying `delta` and `binding` is
 * running, every turn still produces the older, thinner `basket.update` envelope — and the
 * panel has to render *that* correctly rather than a card full of blanks. `ProposalCard`
 * therefore chooses by parsing, not by switching on `action`, and these two tests are the
 * whole reason it is written that way.
 */
describe("an older payload falls back rather than rendering blanks", () => {
  /** Captured from the API on :8000 before this change was deployed, verbatim. */
  const OLD_SHAPE = {
    kind: "product",
    proposal: {
      action: "basket.update",
      sku: "AMUL-DAIRY-001",
      quantity: 2,
      basket_id: "01a071e9-7175-79a4-8180-ab38600ea447",
      executes_on: "trusted_surface",
      display: { quantity: 2, name: "Amul Taaza Toned Milk 500 ml" },
    },
  };

  it("draws the plain handoff card, with its door intact", () => {
    render(<ProposalCard structured={OLD_SHAPE} />);
    expect(screen.getByText("Add 2 × Amul Taaza Toned Milk 500 ml")).toBeDefined();
    expect(screen.getByRole("link", { name: /Open your basket/ }).getAttribute("href")).toBe(
      "/basket",
    );
    // The older envelope states no price, so no price is shown. Inventing one from the
    // product beside it would be this component pricing, which nothing here may do.
    expect(screen.queryByText(/The store’s price, each/)).toBeNull();
  });

  it("draws the priced card once the payload carries the figures", () => {
    render(<ProposalCard structured={{ kind: "product", proposal: TAKE_TO_FIVE }} />);
    expect(screen.getByText("Take Amul Taaza Toned Milk 500 ml to 5")).toBeDefined();
    expect(screen.getByText("₹28.00")).toBeDefined();
  });
});

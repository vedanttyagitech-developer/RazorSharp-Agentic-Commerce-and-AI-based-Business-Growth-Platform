/**
 * The cart screen: the line the merchant took away, and the line it merely capped.
 *
 * A cart is the one screen in this storefront where the buyer, not the server, decides
 * what happens next, so the failure that matters here is not a wrong total — it is a
 * screen that *quietly* stops being about the cart the buyer has. Four ways that can
 * happen, and this file is organised around them:
 *
 *  1. **A line disappears.** The merchant declines to price something and the row for it
 *     vanishes between one render and the next. The buyer is left comparing a
 *     half-remembered list against a shorter one and no sentence anywhere says what left.
 *     So the delisted line has to stay, keep its identity, and stay removable.
 *  2. **A line the merchant never complained about is condemned with it.** This merchant
 *     refuses a cart *whole*: `merchant_sim.fees.quote_basket` returns no quote at all
 *     when any line is unavailable, so "absent from `quote.lines`" becomes "all of them"
 *     the moment one line is short. A screen that read unavailability out of that absence
 *     struck out every product, took away every stepper — the one control that could have
 *     fixed it — and printed a sentence saying one line was declined directly beneath
 *     three it had visibly declined. `cart.unavailable` is the merchant naming the line
 *     it refused; the quote's silence is not.
 *  3. **A price appears anyway.** There is no fixture layer in this app and no arithmetic
 *     in the money components; the only figures on this screen are integers the quote
 *     engine sent. When the merchant sends no quote, the strongest assertion available is
 *     that the rendered document contains no `₹` at all — not a remembered total, not a
 *     zero. A zero here reads as "nothing is owed", which is a different fact from "we do
 *     not know what is owed".
 *  4. **A failure passes for a state.** A read that failed and a cart that is empty look
 *     identical if the screen is careless, and so do "the merchant re-priced this" and
 *     "these were always the figures".
 *
 * Every body below was captured from the live API on 2026-09-05 by driving a real cart.
 * Two captures matter most, and they are the two halves of case 2: asking for twenty of a
 * product with ten in stock (which becomes the delisting fixture, with the two fields the
 * comment names changed), and asking for twenty-one of a product with twenty (which is
 * kept exactly as it arrived). They differ in the one way that decides what the buyer can
 * do about it: a capped line is one tap from being fine and keeps every control, while a
 * withdrawn line has nothing behind it and can only be removed.
 *
 * The harness mocks `@/lib/api/client`, not `./use-cart`, so the real hook runs: the
 * write chain, the idempotency keys and the remembered names are part of what is under
 * test rather than part of the scaffolding. `@/components/providers` *is* mocked rather
 * than wrapped, because its store is a module-level singleton backed by `localStorage`
 * that reads storage exactly once per module load — a second test here could not seed a
 * different cart into it, and the tests would run in an order that mattered. What it
 * exports into this screen is four values and three setters, and substituting those is
 * honest as long as it is said out loud: nothing here exercises the persistence, the
 * cross-tab `storage` event or the header's count. Those belong to the provider and to
 * its own tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";

import { ApiError, type Problem } from "@/lib/api/problem";
import type { Cart } from "@/lib/api/types";

import { CartView } from "./cart-view";

/* ---------------------------------------------------------------- the environment */

const CART_ID = "01a070e9-c3d9-7d09-b638-a3082ede458d";
/** The second capture's cart. A cart answers for one identifier, so it needs its own. */
const CAPPED_BASKET_ID = "01a0715c-ffa1-7297-a31f-eed873277d94";

/**
 * Where `use-cart` keeps the last name each SKU was quoted under.
 *
 * The key is repeated here rather than exported, because a test that seeds it is asserting
 * something about the stored shape: if the hook ever changes where it keeps names, the one
 * test below that depends on remembered names should fail loudly rather than quietly stop
 * covering anything.
 */
const NAMES_KEY = "acr.cart.names";

const mocks = vi.hoisted(() => {
  let minted = 0;
  return {
    api: {
      cart: vi.fn(),
      setLine: vi.fn(),
      createCart: vi.fn(),
      openCheckout: vi.fn(),
    },
    newIdempotencyKey: vi.fn(() => `key-${(minted += 1)}`),
    resetKeys: () => {
      minted = 0;
    },
    push: vi.fn(),
    context: {
      cartId: "01a070e9-c3d9-7d09-b638-a3082ede458d" as string | null,
      lineCount: 2,
      itemCount: 3,
      totalMinor: 57995 as number | null,
      currency: "INR",
      setCartId: vi.fn(),
      setLineCount: vi.fn(),
      refresh: vi.fn(async () => {}),
    },
  };
});

vi.mock("@/lib/api/client", () => ({
  api: mocks.api,
  newIdempotencyKey: mocks.newIdempotencyKey,
}));

vi.mock("@/components/providers", () => ({
  useCartContext: () => mocks.context,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push }),
}));

/* ------------------------------------------------------------------ the fixtures */

/**
 * `PUT /v1/carts/{id}/lines/INDI-STPL-001` with quantity 1, on a cart already holding
 * two litres of milk. Captured 2026-09-05 09:32, HTTP 200. Every figure below is the fee
 * engine's; nothing in this file adds two of them together.
 */
const PRICED: Cart = {
  cart_id: CART_ID,
  lines: [
    { sku: "AMUL-DAIRY-001", quantity: 2 },
    { sku: "INDI-STPL-001", quantity: 1 },
  ],
  code: "OK",
  quote: {
    currency: "INR",
    lines: [
      {
        sku: "AMUL-DAIRY-001",
        name: "Amul Taaza Toned Milk 500 ml",
        quantity: 2,
        unit_price_minor: 2800,
        subtotal_minor: 5600,
        tax_bp: 0,
        tax_minor: 0,
      },
      {
        sku: "INDI-STPL-001",
        name: "India Gate Classic Basmati Rice 5 kg",
        quantity: 1,
        unit_price_minor: 49900,
        subtotal_minor: 49900,
        tax_bp: 500,
        tax_minor: 2495,
      },
    ],
    items_subtotal_minor: 55500,
    items_tax_minor: 2495,
    delivery_fee_minor: 0,
    delivery_tax_minor: 0,
    total_minor: 57995,
    total: { minor: 57995, currency: "INR", display: "579.95" },
    free_delivery_applied: true,
    gap_to_free_delivery_minor: 0,
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 5,
    content_hash: "JHRrIm84fNfahOwj0k6lJ293WCb18K9sjnWFVDN80zo",
  },
  unavailable: [],
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 5,
    observed_at: "2026-09-05T09:32:43.419978Z",
  },
  stale: false,
};

/**
 * The merchant's answer when the rice line cannot be supplied at all. Captured 2026-09-05
 * 09:32 by asking for twenty of a product with ten in stock, then changed in exactly two
 * places to say "delisted" instead of "short": `available_units` 10 → 0 and `listed` true
 * → false, with `requested` and the line's quantity moved to the 2 the buyer asks for.
 *
 * Everything that carries meaning is the captured body. `quote` really is null — the
 * merchant refuses the whole cart rather than pricing the remainder — `code` really is
 * `STALE_CHECKOUT`, and `unavailable` really does name the one line, which is how the
 * screen can say how many lines were declined without counting anything itself.
 */
const RICE_DELISTED: Cart = {
  cart_id: CART_ID,
  lines: [
    { sku: "AMUL-DAIRY-001", quantity: 2 },
    { sku: "INDI-STPL-001", quantity: 2 },
  ],
  code: "STALE_CHECKOUT",
  quote: null,
  unavailable: [{ sku: "INDI-STPL-001", requested: 2, available_units: 0, listed: false }],
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 5,
    observed_at: "2026-09-05T09:32:43.462018Z",
  },
  stale: false,
};

/**
 * The same priced cart, re-quoted after the catalogue moved.
 *
 * `stale` is `cart.catalogue_revision != store.revision` in `cart_service.basket_body`,
 * so producing one live means moving the shared merchant catalogue underneath a cart,
 * which would change what every other reader of this API sees. The captured body is used
 * with that single boolean flipped.
 */
const REPRICED: Cart = { ...PRICED, stale: true };

/**
 * The other half of case 2, and the one this file exists for.
 *
 * `PUT /v1/carts/{id}/lines/AMUL-DAIRY-005 {"quantity": 21}` against a cart already
 * holding three lines, captured verbatim on 2026-09-05 and unedited. Twenty butters
 * exist; twenty-one were asked for. The merchant is still selling the butter — `listed`
 * is true and `available_units` is 20 — so this is a cart one tap from being priced,
 * and every figure the buyer needs to make that tap is in `unavailable`.
 */
const BUTTER_CAPPED: Cart = {
  cart_id: CAPPED_BASKET_ID,
  lines: [
    { sku: "AASH-STPL-002", quantity: 1 },
    { sku: "ACT-SNCK-019", quantity: 3 },
    { sku: "AMUL-DAIRY-005", quantity: 21 },
  ],
  code: "STALE_CHECKOUT",
  quote: null,
  unavailable: [{ sku: "AMUL-DAIRY-005", requested: 21, available_units: 20, listed: true }],
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 1509,
    observed_at: "2026-09-05T11:38:49.751425Z",
  },
  stale: false,
};

/**
 * The names those three SKUs were last quoted under, as `use-cart` would have stored
 * them from the responses that priced this cart before the twenty-first butter.
 *
 * A cart line is `{sku, quantity}`; the human name of a product reaches this screen only
 * on a quote row, and there is no quote in the body above. Seeding storage is how a buyer
 * arrives at a refused cart in real life — they had it priced a moment ago, or they
 * reloaded the page — and it is the situation the remembered names exist for.
 */
const REMEMBERED_NAMES: Record<string, string> = {
  "AASH-STPL-002": "Aashirvaad Shudh Chakki Atta 5 kg",
  "ACT-SNCK-019": "Act II Butter Popcorn 150 g",
  "AMUL-DAIRY-005": "Amul Butter (Salted) 100 g",
};

/** This app's own proxy when the API is not listening. Captured 2026-09-05. */
const UNREACHABLE: Problem = {
  type: "about:blank",
  title: "The store is not reachable",
  status: 503,
  detail: "No response from http://127.0.0.1:8000.",
};

/** `PUT .../lines/AMUL-DAIRY-001` with a body the endpoint's schema rejects. Captured 2026-09-05. */
const UNPROCESSABLE: Problem = {
  type: "about:blank",
  title: "Request validation failed",
  status: 422,
  detail: "The request body, query or path did not match the endpoint's schema.",
  instance: `/v1/carts/${CART_ID}/lines/AMUL-DAIRY-001`,
  errors: [
    {
      type: "int_parsing",
      loc: ["body", "quantity"],
      msg: "Input should be a valid integer, unable to parse string as an integer",
      input: "many",
    },
  ],
};

/* ---------------------------------------------------------------------- helpers */

async function settle(): Promise<void> {
  await act(async () => {});
  await act(async () => {});
  await act(async () => {});
}

async function press(name: string | RegExp): Promise<void> {
  const button = screen.getByRole("button", { name });
  await act(async () => {
    button.click();
  });
  await settle();
}

/**
 * The rows of the cart itself.
 *
 * The bill panel lists the declined lines too, by name, so `getAllByRole("listitem")`
 * alone would hand back the merchant's explanation of a line as though it were the line.
 * Every cart row carries the product's photograph — the muted ones included, greyed —
 * and nothing in the panel does, so that is the discriminator.
 */
function basketRows(): HTMLElement[] {
  return screen.getAllByRole("listitem").filter((row) => row.querySelector("img") !== null);
}

/**
 * The one row whose rendered text names `label`.
 *
 * A row is identified by whichever of its two identities is on screen: the name, when the
 * merchant has ever quoted one, and the SKU on a row it has withdrawn — which is printed
 * there precisely so a line with no price is still a line the buyer can point at.
 */
function rowNaming(label: string): HTMLElement {
  const rows = basketRows().filter((row) => row.textContent?.includes(label));
  if (rows.length !== 1) throw new Error(`expected one cart row naming ${label}, found ${rows.length}`);
  return rows[0];
}

async function mountBasket(): Promise<ReturnType<typeof render>> {
  const view = render(<CartView />);
  await settle();
  return view;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.resetKeys();
  window.localStorage.clear();
  mocks.context.cartId = CART_ID;
  mocks.context.refresh.mockResolvedValue(undefined);
});

afterEach(cleanup);

/* ----------------------------------------- the line the merchant took away mid-session */

describe("a line the merchant delisted while the cart was open", () => {
  /**
   * The scene: a priced cart on screen, the buyer asks for one more bag of rice, and the
   * merchant answers that it cannot price the cart at all any more. The request is the
   * absolute quantity 2, because that is the whole shape of this API's line writes.
   */
  async function delistMidSession(): Promise<ReturnType<typeof render>> {
    mocks.api.cart.mockResolvedValue(PRICED);
    mocks.api.setLine.mockResolvedValue(RICE_DELISTED);
    const view = await mountBasket();
    // The row is there to be pressed only because the first read priced it: ₹499.00 a bag
    // and ₹499.00 for the one bag, both integers the quote sent.
    expect(rowNaming("India Gate Classic Basmati Rice 5 kg").textContent).toContain("₹499.00");
    await press("Increase the quantity of India Gate Classic Basmati Rice 5 kg");
    return view;
  }

  it("keeps the delisted line on screen instead of dropping it between renders", async () => {
    await delistMidSession();

    // It is on screen under its SKU as well as its name now: the quote that carried the
    // name is gone, so the name on the row is the one the last quote gave it, held across
    // the response — which is why the control below can still be asked for by name rather
    // than degrading to a bare identifier the buyer never saw. What it must not do is
    // disappear.
    const rice = rowNaming("INDI-STPL-001");
    expect(within(rice).getByText("No longer available")).toBeDefined();
    expect(within(rice).getByText("India Gate Classic Basmati Rice 5 kg")).toBeDefined();
    // And it is still the buyer's to act on: this is the one control that resolves it.
    // The merchant has withdrawn the product, so there is no quantity to climb down to
    // and no stepper offering one — removal is the whole of what it can offer.
    expect(
      screen.getByRole("button", { name: "Remove India Gate Classic Basmati Rice 5 kg from the cart" }),
    ).toBeDefined();
    expect(screen.queryByRole("button", { name: /^Change to/ })).toBeNull();

    // The other line did not vanish either, and the count still counts both.
    expect(rowNaming("Amul Taaza Toned Milk 500 ml")).toBeDefined();
    expect(screen.getByText("2 items")).toBeDefined();
  });

  it("says the merchant declined the rice, and does not say it declined the milk", async () => {
    await delistMidSession();

    /*
     * The defect this pins. The merchant refuses a cart whole rather than pricing the
     * remainder, so `quote` is null and *every* line is missing from it — and a screen
     * that derived "unavailable" from that absence put "No longer available" under the
     * milk, which is available, listed, and in stock. A buyer reads that as their milk
     * having gone because somebody else's rice ran out.
     *
     * `cart.unavailable` is the merchant naming the line it would not sell, and it names
     * one. So exactly one row may carry that chip, and it is the one the merchant named.
     */
    expect(screen.getAllByText("No longer available")).toHaveLength(1);
    expect(within(rowNaming("INDI-STPL-001")).getByText("No longer available")).toBeDefined();

    /*
     * And the milk is not described at all — it is simply still there, whole: its name,
     * its stepper, no price and no chip claiming anything about it. Nothing on the row
     * needs to explain itself, because the fact it would be explaining is a fact about the
     * cart rather than about the product, and the bill panel states it there.
     */
    const milk = rowNaming("Amul Taaza Toned Milk 500 ml");
    expect(within(milk).queryByText("No longer available")).toBeNull();
    expect(
      within(milk).getByRole("button", { name: "Increase the quantity of Amul Taaza Toned Milk 500 ml" }),
    ).toBeDefined();
    expect(screen.getByText(/could not price/).textContent).toContain(
      "The other line is priced as before and will be again.",
    );
  });

  it("invents no price for it, and no price for anything else either", async () => {
    const { container } = await delistMidSession();

    // The merchant sent no quote, so there is no figure on this screen that anything
    // computed. A remembered ₹579.95 here would be a price the merchant has withdrawn.
    expect(container.textContent).not.toContain("₹");
    expect(within(rowNaming("INDI-STPL-001")).queryByText(/each/)).toBeNull();
  });

  it("says there is no total, in the merchant's own count, rather than showing a stale one", async () => {
    await delistMidSession();

    expect(screen.getByText("This cart has no total yet")).toBeDefined();
    // One line was declined and the merchant is the one that says so: the count is the
    // length of `unavailable`, not a number this screen derived from the quote's absence,
    // and it is printed beside the number of lines the buyer can see so the two can be
    // checked against each other at a glance.
    expect(screen.getByText(/could not price/).textContent).toMatch(/1 of these 2 lines/);
    // Restated in the merchant's own words, not paraphrased into a stock figure it never
    // sent: a withdrawn product has no `available_units` worth printing.
    expect(screen.getByText(/the merchant no longer lists it/)).toBeDefined();
    // And the remedy matches the refusal. There is no quantity to come down to.
    expect(screen.getByText(/Remove it and the total comes back/)).toBeDefined();
    // The kernel's recovery code, printed as it arrived.
    expect(screen.getByText("code STALE_CHECKOUT")).toBeDefined();
    expect(screen.queryByLabelText("Bill details")).toBeNull();
  });

  it("will not let a cart with no total be taken to checkout", async () => {
    await delistMidSession();

    // Opening a checkout freezes a quote as version 1. There is no quote to freeze, and a
    // button that tried would fail server-side after the buyer had already committed to it.
    const proceed = screen.getByRole("button", { name: "Proceed to checkout" });
    expect((proceed as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.api.openCheckout).not.toHaveBeenCalled();
  });

  it("asks the merchant for the absolute quantity, not for a change of one", async () => {
    await delistMidSession();

    // Three surfaces write to one cart. A delta would let two of them race and leave the
    // buyer holding whichever arrived second.
    expect(mocks.api.setLine).toHaveBeenCalledTimes(1);
    expect(mocks.api.setLine).toHaveBeenCalledWith(CART_ID, "INDI-STPL-001", 2, expect.any(String));
  });
});

/* ------------------------------------------- the line the merchant merely capped */

describe("one line over the merchant's stock", () => {
  /*
   * The same refusal shape as above — no quote, `STALE_CHECKOUT`, one line named — and the
   * opposite thing to do about it. Twenty-one butters were asked for and twenty exist, so
   * the merchant is still selling it and the cart is one tap from being priced. This is
   * the case that showed the old screen was reading the quote's silence rather than the
   * merchant's word: one line over stock struck out all three products, removed every
   * stepper, and printed "1 line" beneath three visibly condemned rows.
   */
  beforeEach(() => {
    mocks.context.cartId = CAPPED_BASKET_ID;
    window.localStorage.setItem(NAMES_KEY, JSON.stringify(REMEMBERED_NAMES));
    mocks.api.cart.mockResolvedValue(BUTTER_CAPPED);
  });

  it("marks that line and no other", async () => {
    await mountBasket();
    // The one the merchant named, in the merchant's two integers.
    expect(screen.getByText(/Only 20 left/)).toBeDefined();
    // And exactly one such mark on the page, not one per line.
    expect(screen.getAllByRole("button", { name: /^Change to/ })).toHaveLength(1);
  });

  it("leaves the other two lines with their names and their steppers", async () => {
    await mountBasket();
    for (const name of ["Aashirvaad Shudh Chakki Atta 5 kg", "Act II Butter Popcorn 150 g"]) {
      // The names come from the last response that priced these SKUs, not from this one:
      // there is no quote here to carry them, and three raw identifiers would be a worse
      // account of the cart than the one the buyer had a moment ago.
      expect(screen.getByText(name)).toBeDefined();
      expect(screen.getByRole("button", { name: `Increase the quantity of ${name}` })).toBeDefined();
    }
  });

  it("keeps the refused line's own stepper too, so the buyer can climb back down", async () => {
    await mountBasket();
    expect(
      screen.getByRole("button", { name: "Decrease the quantity of Amul Butter (Salted) 100 g" }),
    ).toBeDefined();
  });

  it("calls no line unavailable, because the merchant withdrew nothing", async () => {
    await mountBasket();
    expect(screen.queryByText("No longer available")).toBeNull();
    expect(screen.queryByText("Out of stock")).toBeNull();
  });

  it("counts the refused lines the same way in words as it marks them on screen", async () => {
    await mountBasket();
    // "1 of these 3 lines" — the sentence that used to say 1 while striking through 3. The
    // count is read off `textContent` because the two integers are their own elements.
    const notice = screen.getByText(/could not price/);
    expect(notice.textContent).toMatch(/1 of these 3 lines/);
  });

  it("tells the buyer the count that would fix it", async () => {
    await mountBasket();
    expect(screen.getByText(/you asked for 21, the merchant has 20/)).toBeDefined();
    // The merchant still stocks it, so the remedy is a smaller quantity rather than a
    // removal — and the button above offers that exact quantity rather than asking the
    // buyer to work out from "we have 20" that they should press minus once.
    expect(screen.getByText(/Bring that line down to what is available/)).toBeDefined();
  });

  it("still refuses to show a total, because the merchant sent none", async () => {
    const { container } = await mountBasket();
    expect(screen.queryByLabelText("Bill details")).toBeNull();
    expect(container.textContent).not.toContain("₹");
    expect(
      (screen.getByRole("button", { name: "Proceed to checkout" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

/* ------------------------------------------------------------- the re-priced notice */

describe("a cart the merchant re-priced", () => {
  it("says the catalogue moved rather than swapping the figures silently", async () => {
    mocks.api.cart.mockResolvedValue(REPRICED);
    await mountBasket();

    expect(screen.getByText("This cart was re-priced")).toBeDefined();
    expect(screen.getByText(/recomputed at the current revision/)).toBeDefined();
    // The new figures are shown, and they are the ones the server sent.
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
  });

  it("says nothing of the sort when the catalogue has not moved", async () => {
    mocks.api.cart.mockResolvedValue(PRICED);
    await mountBasket();

    // A notice that is always on is a notice nobody reads.
    expect(screen.queryByText("This cart was re-priced")).toBeNull();
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
  });
});

/* -------------------------------------------------------------- a read that failed */

describe("a cart that could not be read", () => {
  it("renders the server's own sentence, a retry, and no figures at all", async () => {
    mocks.api.cart.mockRejectedValue(new ApiError(UNREACHABLE));
    const { container } = await mountBasket();

    const failure = screen.getByRole("alert");
    expect(within(failure).getByText("This cart could not be loaded")).toBeDefined();
    expect(within(failure).getByText("No response from http://127.0.0.1:8000.")).toBeDefined();
    // Not an empty cart. "Your cart is empty" is a claim about the buyer's cart, and
    // a failed read has checked nothing.
    expect(screen.queryByText("Your cart is empty")).toBeNull();
    expect(container.textContent).not.toContain("₹");
    expect(screen.getByRole("button", { name: "Try again" })).toBeDefined();
  });

  it("re-reads when the retry is pressed and shows what comes back", async () => {
    mocks.api.cart.mockRejectedValueOnce(new ApiError(UNREACHABLE));
    mocks.api.cart.mockResolvedValue(PRICED);
    await mountBasket();

    await press("Try again");

    expect(mocks.api.cart).toHaveBeenCalledTimes(2);
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
    expect(screen.queryByText("This cart could not be loaded")).toBeNull();
  });
});

/* ------------------------------------------------------------- a write that failed */

describe("a line write that failed", () => {
  it("keeps the cart on screen and reports the failure inline", async () => {
    mocks.api.cart.mockResolvedValue(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");

    expect(screen.getByText("The last change did not go through")).toBeDefined();
    expect(screen.getByText(/did not match the endpoint's schema/)).toBeDefined();
    // The cart the buyer was looking at is still the cart on screen: the last good
    // read, with the server's own figures, not a blank page and not a zeroed total.
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
    expect(rowNaming("Amul Taaza Toned Milk 500 ml")).toBeDefined();
    expect(rowNaming("India Gate Classic Basmati Rice 5 kg")).toBeDefined();
    // And the failure is recoverable from where it is reported.
    expect(screen.getByRole("button", { name: "Reload the cart" })).toBeDefined();
  });

  it("replays the same idempotency key when the same change is asked for again", async () => {
    mocks.api.cart.mockResolvedValue(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");
    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");

    expect(mocks.api.setLine).toHaveBeenCalledTimes(2);
    const [first, second] = mocks.api.setLine.mock.calls;
    // Both presses asked for the same absolute quantity, so they are one intention. A
    // first attempt whose response was lost must be replayed, never applied twice. The key
    // is named rather than merely compared: two calls sending no key would pass an
    // equality check and would be exactly the defect this test exists for.
    expect(first[2]).toBe(3);
    expect(second[2]).toBe(3);
    expect(first[3]).toBe("key-1");
    expect(second[3]).toBe("key-1");
  });

  it("clears the failure when the reload succeeds", async () => {
    mocks.api.cart.mockResolvedValue(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");
    await press("Reload the cart");

    expect(screen.queryByText("The last change did not go through")).toBeNull();
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
  });

  it("brings a delisting that arrived in the meantime back with the reload", async () => {
    // The failed write is one event and the merchant's catalogue moving is another. The
    // reload is where the second one becomes visible, and the line must survive it.
    mocks.api.cart.mockResolvedValueOnce(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    mocks.api.cart.mockResolvedValue({
      ...RICE_DELISTED,
      // The write failed, so the rice line is still the 1 the buyer had.
      lines: [
        { sku: "AMUL-DAIRY-001", quantity: 2 },
        { sku: "INDI-STPL-001", quantity: 1 },
      ],
      unavailable: [{ sku: "INDI-STPL-001", requested: 1, available_units: 0, listed: false }],
    } satisfies Cart);
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");
    await press("Reload the cart");

    expect(rowNaming("INDI-STPL-001")).toBeDefined();
    expect(screen.getByText(/could not price/).textContent).toMatch(/1 of these 2 lines/);
    expect((screen.getByRole("button", { name: "Proceed to checkout" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

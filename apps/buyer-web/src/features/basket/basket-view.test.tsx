/**
 * The basket screen, and the line the merchant took away while the buyer was holding it.
 *
 * A basket is the one screen in this storefront where the buyer, not the server, decides
 * what happens next, so the failure that matters here is not a wrong total — it is a
 * screen that *quietly* stops being about the basket the buyer has. Three ways that can
 * happen, and this file is organised around them:
 *
 *  1. **A line disappears.** The merchant declines to price something and the row for it
 *     vanishes between one render and the next. The buyer is left comparing a
 *     half-remembered list against a shorter one and no sentence anywhere says what left.
 *     So the delisted line has to stay, keep its identity, and stay removable.
 *  2. **A price appears for it anyway.** There is no fixture layer in this app and no
 *     arithmetic in the money components; the only figures on this screen are integers the
 *     quote engine sent. When the merchant sends no quote, the strongest assertion
 *     available is that the rendered document contains no `₹` at all — not a remembered
 *     total, not a zero. A zero here reads as "nothing is owed", which is a different fact
 *     from "we do not know what is owed".
 *  3. **A failure passes for a state.** A read that failed and a basket that is empty look
 *     identical if the screen is careless, and so do "the merchant re-priced this" and
 *     "these were always the figures".
 *
 * The bodies below were captured from the live API on 2026-09-05 by walking a real basket:
 * open it, put two lines in, then ask for more of one line than the merchant has. That
 * last request is the important capture, because of what it revealed: **this merchant
 * refuses a basket whole.** `merchant_sim.fees.quote_basket` returns no quote at all when
 * any line is unavailable — "Pricing the remainder would hand the buyer a total for a
 * basket they never asked for" — so `quote: null` with `code: "STALE_CHECKOUT"` and the
 * offending line named in `unavailable` is the real shape of a mid-session delisting, and
 * a partly-priced quote is not a body this merchant can send. Every fixture here is that
 * capture; where a field had to change to turn "you asked for twenty and there are ten"
 * into "the merchant delisted it", the comment names the fields and nothing else moved.
 *
 * `@/components/providers` is mocked rather than wrapped. Its store is a module-level
 * singleton backed by `localStorage` that reads storage exactly once per module load, so a
 * second test in this file could not seed a different basket into it — the tests would run
 * in an order that mattered. What it exports into this screen is four values and three
 * setters, and substituting those is honest as long as it is said out loud: nothing here
 * exercises the persistence, the cross-tab `storage` event or the header's count. Those
 * belong to the provider and to its own tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";

import { ApiError, type Problem } from "@/lib/api/problem";
import type { Basket } from "@/lib/api/types";

import { BasketView } from "./basket-view";

/* ---------------------------------------------------------------- the environment */

const BASKET_ID = "01a070e9-c3d9-7d09-b638-a3082ede458d";

const mocks = vi.hoisted(() => {
  let minted = 0;
  return {
    api: {
      basket: vi.fn(),
      setLine: vi.fn(),
      createBasket: vi.fn(),
      openCheckout: vi.fn(),
    },
    newIdempotencyKey: vi.fn(() => `key-${(minted += 1)}`),
    resetKeys: () => {
      minted = 0;
    },
    push: vi.fn(),
    context: {
      basketId: "01a070e9-c3d9-7d09-b638-a3082ede458d" as string | null,
      lineCount: 2,
      itemCount: 3,
      totalMinor: 57995 as number | null,
      currency: "INR",
      setBasketId: vi.fn(),
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
  useBasketContext: () => mocks.context,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push }),
}));

/* ------------------------------------------------------------------ the fixtures */

/**
 * `PUT /v1/baskets/{id}/lines/INDI-STPL-001` with quantity 1, on a basket already holding
 * two litres of milk. Captured 2026-09-05 09:32, HTTP 200. Every figure below is the fee
 * engine's; nothing in this file adds two of them together.
 */
const PRICED: Basket = {
  basket_id: BASKET_ID,
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
 * The merchant's answer when the rice line cannot be supplied. Captured 2026-09-05 09:32
 * by asking for twenty of a product with ten in stock, then changed in exactly two places
 * to say "delisted" instead of "short": `available_units` 10 → 0 and `listed` true →
 * false, with `requested` and the line's quantity moved to the 2 the buyer asks for below.
 *
 * Everything that carries meaning is the captured body. `quote` really is null — the
 * merchant refuses the whole basket rather than pricing the remainder — `code` really is
 * `STALE_CHECKOUT`, and `unavailable` really does name the one line, which is how the
 * screen can say how many lines were declined without counting anything itself.
 */
const RICE_DELISTED: Basket = {
  basket_id: BASKET_ID,
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
 * The same priced basket, re-quoted after the catalogue moved.
 *
 * `stale` is `basket.catalogue_revision != store.revision` in `basket_service.basket_body`,
 * so producing one live means moving the shared merchant catalogue underneath a basket,
 * which would change what every other reader of this API sees. The captured body is used
 * with that single boolean flipped.
 */
const REPRICED: Basket = { ...PRICED, stale: true };

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
  instance: `/v1/baskets/${BASKET_ID}/lines/AMUL-DAIRY-001`,
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
 * The one row whose rendered text names `label`.
 *
 * A priced row is identified by the name the quote gave it; a row the merchant would not
 * price has no name to be given, so it is identified by its SKU — which is the point of
 * printing the SKU on it at all.
 */
function rowNaming(label: string): HTMLElement {
  const rows = screen.getAllByRole("listitem").filter((row) => row.textContent?.includes(label));
  if (rows.length !== 1) throw new Error(`expected one basket row naming ${label}, found ${rows.length}`);
  return rows[0];
}

async function mountBasket(): Promise<ReturnType<typeof render>> {
  const view = render(<BasketView />);
  await settle();
  return view;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.resetKeys();
  mocks.context.basketId = BASKET_ID;
  mocks.context.refresh.mockResolvedValue(undefined);
});

afterEach(cleanup);

/* ----------------------------------------- the line the merchant took away mid-session */

describe("a line the merchant delisted while the basket was open", () => {
  /**
   * The scene: a priced basket on screen, the buyer asks for one more bag of rice, and the
   * merchant answers that it cannot price the basket at all any more. The request is the
   * absolute quantity 2, because that is the whole shape of this API's line writes.
   */
  async function delistMidSession(): Promise<ReturnType<typeof render>> {
    mocks.api.basket.mockResolvedValue(PRICED);
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

    // It was on screen a render ago under its name; it is on screen now under its SKU,
    // because the quote that carried the name is gone. What it must not do is disappear.
    const rice = rowNaming("INDI-STPL-001");
    expect(within(rice).getByText("No longer available")).toBeDefined();
    // And it is still the buyer's to act on: this is the one control that resolves it.
    expect(
      screen.getByRole("button", { name: "Remove INDI-STPL-001 from the basket" }),
    ).toBeDefined();

    // The other line did not vanish either, and the count still counts both.
    expect(rowNaming("AMUL-DAIRY-001")).toBeDefined();
    expect(screen.getByText("2 items")).toBeDefined();
  });

  it("says the merchant declined the rice, and does not say it declined the milk", async () => {
    await delistMidSession();

    /*
     * The defect this pins. The merchant refuses a basket whole rather than pricing the
     * remainder, so `quote` is null and *every* line is missing from it — and a screen
     * that derived "unavailable" from that absence put "No longer available" under the
     * milk, which is available, listed, and in stock. A buyer reads that as their milk
     * having gone because somebody else's rice ran out.
     *
     * `basket.unavailable` is the merchant naming the line it would not sell, and it names
     * one. So exactly one row may carry that chip, and it is the one the merchant named.
     */
    expect(screen.getAllByText("No longer available")).toHaveLength(1);
    expect(within(rowNaming("INDI-STPL-001")).getByText("No longer available")).toBeDefined();
    expect(within(rowNaming("AMUL-DAIRY-001")).queryByText("No longer available")).toBeNull();

    // The milk is described by what is actually true of it: the merchant sent no price for
    // it, and that is a fact about the basket rather than about the product.
    const milk = rowNaming("AMUL-DAIRY-001");
    expect(within(milk).getByText("Not priced")).toBeDefined();
    expect(milk.textContent).toContain("That is not a statement about this product.");
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

    expect(screen.getByText("This basket cannot be priced")).toBeDefined();
    // One line was declined and the merchant is the one that says so: the count is the
    // length of `unavailable`, not a number this screen derived from the quote's absence.
    expect(screen.getByText(/The merchant declined 1 of these lines/)).toBeDefined();
    expect(screen.getByText(/Remove them and the total comes back/)).toBeDefined();
    // The kernel's recovery code, printed as it arrived.
    expect(screen.getByText("code STALE_CHECKOUT")).toBeDefined();
    expect(screen.queryByLabelText("Bill details")).toBeNull();
  });

  it("will not let a basket with no total be taken to checkout", async () => {
    await delistMidSession();

    // Opening a checkout freezes a quote as version 1. There is no quote to freeze, and a
    // button that tried would fail server-side after the buyer had already committed to it.
    const proceed = screen.getByRole("button", { name: "Proceed to checkout" });
    expect((proceed as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.api.openCheckout).not.toHaveBeenCalled();
  });

  it("asks the merchant for the absolute quantity, not for a change of one", async () => {
    await delistMidSession();

    // Three surfaces write to one basket. A delta would let two of them race and leave the
    // buyer holding whichever arrived second.
    expect(mocks.api.setLine).toHaveBeenCalledTimes(1);
    expect(mocks.api.setLine).toHaveBeenCalledWith(BASKET_ID, "INDI-STPL-001", 2, expect.any(String));
  });
});

/* ------------------------------------------------------------- the re-priced notice */

describe("a basket the merchant re-priced", () => {
  it("says the catalogue moved rather than swapping the figures silently", async () => {
    mocks.api.basket.mockResolvedValue(REPRICED);
    await mountBasket();

    expect(screen.getByText("This basket was re-priced")).toBeDefined();
    expect(screen.getByText(/recomputed at the current revision/)).toBeDefined();
    // The new figures are shown, and they are the ones the server sent.
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
  });

  it("says nothing of the sort when the catalogue has not moved", async () => {
    mocks.api.basket.mockResolvedValue(PRICED);
    await mountBasket();

    // A notice that is always on is a notice nobody reads.
    expect(screen.queryByText("This basket was re-priced")).toBeNull();
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
  });
});

/* -------------------------------------------------------------- a read that failed */

describe("a basket that could not be read", () => {
  it("renders the server's own sentence, a retry, and no figures at all", async () => {
    mocks.api.basket.mockRejectedValue(new ApiError(UNREACHABLE));
    const { container } = await mountBasket();

    const failure = screen.getByRole("alert");
    expect(within(failure).getByText("This basket could not be loaded")).toBeDefined();
    expect(within(failure).getByText("No response from http://127.0.0.1:8000.")).toBeDefined();
    // Not an empty basket. "Your basket is empty" is a claim about the buyer's basket, and
    // a failed read has checked nothing.
    expect(screen.queryByText("Your basket is empty")).toBeNull();
    expect(container.textContent).not.toContain("₹");
    expect(screen.getByRole("button", { name: "Try again" })).toBeDefined();
  });

  it("re-reads when the retry is pressed and shows what comes back", async () => {
    mocks.api.basket.mockRejectedValueOnce(new ApiError(UNREACHABLE));
    mocks.api.basket.mockResolvedValue(PRICED);
    await mountBasket();

    await press("Try again");

    expect(mocks.api.basket).toHaveBeenCalledTimes(2);
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
    expect(screen.queryByText("This basket could not be loaded")).toBeNull();
  });
});

/* ------------------------------------------------------------- a write that failed */

describe("a line write that failed", () => {
  it("keeps the basket on screen and reports the failure inline", async () => {
    mocks.api.basket.mockResolvedValue(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");

    expect(screen.getByText("The last change did not go through")).toBeDefined();
    expect(screen.getByText(/did not match the endpoint's schema/)).toBeDefined();
    // The basket the buyer was looking at is still the basket on screen: the last good
    // read, with the server's own figures, not a blank page and not a zeroed total.
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
    expect(rowNaming("Amul Taaza Toned Milk 500 ml")).toBeDefined();
    expect(rowNaming("India Gate Classic Basmati Rice 5 kg")).toBeDefined();
    // And the failure is recoverable from where it is reported.
    expect(screen.getByRole("button", { name: "Reload the basket" })).toBeDefined();
  });

  it("replays the same idempotency key when the same change is asked for again", async () => {
    mocks.api.basket.mockResolvedValue(PRICED);
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
    mocks.api.basket.mockResolvedValue(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");
    await press("Reload the basket");

    expect(screen.queryByText("The last change did not go through")).toBeNull();
    expect(within(screen.getByLabelText("Bill details")).getByText("₹579.95")).toBeDefined();
  });

  it("brings a delisting that arrived in the meantime back with the reload", async () => {
    // The failed write is one event and the merchant's catalogue moving is another. The
    // reload is where the second one becomes visible, and the line must survive it.
    mocks.api.basket.mockResolvedValueOnce(PRICED);
    mocks.api.setLine.mockRejectedValue(new ApiError(UNPROCESSABLE));
    mocks.api.basket.mockResolvedValue({
      ...RICE_DELISTED,
      // The write failed, so the rice line is still the 1 the buyer had.
      lines: [
        { sku: "AMUL-DAIRY-001", quantity: 2 },
        { sku: "INDI-STPL-001", quantity: 1 },
      ],
      unavailable: [{ sku: "INDI-STPL-001", requested: 1, available_units: 0, listed: false }],
    } satisfies Basket);
    await mountBasket();

    await press("Increase the quantity of Amul Taaza Toned Milk 500 ml");
    await press("Reload the basket");

    expect(rowNaming("INDI-STPL-001")).toBeDefined();
    expect(screen.getByText(/The merchant declined 1 of these lines/)).toBeDefined();
    expect((screen.getByRole("button", { name: "Proceed to checkout" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

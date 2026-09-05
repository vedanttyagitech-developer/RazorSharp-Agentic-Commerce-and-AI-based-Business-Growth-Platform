/**
 * The shelf a reply draws, and the one rule it must never break: it does not invent a price.
 *
 * A product row inside a conversation is the most tempting place in this app to compute
 * something. The gateway sends `unit_price` as the API's own money object, and it sends
 * `null` when the row it read had none -- a delisted product, or a payload whose shape
 * drifted. The tests below pin the honest rendering of that: "amount not stated", never a
 * zero, never a dash a buyer could read as free.
 *
 * The availability tests matter for the same reason. `available` is the gateway's word, and
 * a card that offered an Add press on a sold-out product would send the buyer a write the
 * server is going to refuse -- while looking, on screen, exactly like a product they can
 * have.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { ReplyItem } from "@/features/voice/wire";

import { ProductCards, itemsFromStructured } from "./product-cards";

afterEach(cleanup);

const MILK: ReplyItem = {
  sku: "AMUL-DAIRY-001",
  name: "Amul Taaza Toned Milk 500 ml",
  unit_price: { minor: 2800, currency: "INR", display: "28.00" },
  stock_units: 30,
  available: true,
};

const ONIONS: ReplyItem = {
  sku: "ONION-1KG",
  name: "Red Onions 1 kg",
  unit_price: { minor: 4200, currency: "INR", display: "42.00" },
  stock_units: 12,
  available: true,
};

const SOLD_OUT: ReplyItem = {
  sku: "PANEER-200G",
  name: "Fresh Paneer 200 g",
  unit_price: { minor: 9900, currency: "INR", display: "99.00" },
  stock_units: 0,
  available: false,
};

describe("the row itself", () => {
  it("draws one card per product, named", () => {
    render(<ProductCards items={[MILK, ONIONS]} />);
    expect(screen.getByText("Amul Taaza Toned Milk 500 ml")).toBeDefined();
    expect(screen.getByText("Red Onions 1 kg")).toBeDefined();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("renders nothing at all for an empty list, rather than an empty frame", () => {
    const { container } = render(<ProductCards items={[]} />);
    expect(container.textContent).toBe("");
    expect(container.querySelector("ul")).toBeNull();
  });

  it("shows the photo for the sku, with the product's name as its alt text", () => {
    render(<ProductCards items={[MILK]} />);
    const photo = screen.getByAltText("Amul Taaza Toned Milk 500 ml");
    expect(photo.getAttribute("src")).toBe("/products/AMUL-DAIRY-001.webp");
  });

  it("staggers the entrance so five cards arrive as a row, not as a flash", () => {
    render(<ProductCards items={[MILK, ONIONS, SOLD_OUT]} />);
    const cards = screen.getAllByRole("listitem");
    expect(cards[0]?.style.animationDelay).toBe("0ms");
    expect(cards[1]?.style.animationDelay).toBe("60ms");
    expect(cards[2]?.style.animationDelay).toBe("120ms");
    expect(cards.every((card) => card.className.includes("card-enter"))).toBe(true);
  });
});

describe("the price is the server's, never this component's", () => {
  it("renders the money object the gateway sent", () => {
    render(<ProductCards items={[MILK]} />);
    expect(screen.getByText("₹28.00")).toBeDefined();
  });

  it("says the amount was not stated when the row carried no price", () => {
    render(<ProductCards items={[{ ...MILK, unit_price: null }]} />);
    expect(screen.getByText("amount not stated")).toBeDefined();
    expect(screen.queryByText("₹0.00")).toBeNull();
  });

  it("says the same for a price whose shape drifted, rather than trusting it", () => {
    render(<ProductCards items={[{ ...MILK, unit_price: { minor: "28" } }]} />);
    expect(screen.getByText("amount not stated")).toBeDefined();
  });
});

describe("the press is the shelf's own write", () => {
  it("calls onAdd with the sku that was pressed", () => {
    const onAdd = vi.fn();
    render(<ProductCards items={[MILK, ONIONS]} onAdd={onAdd} />);
    fireEvent.click(screen.getByRole("button", { name: "Add Red Onions 1 kg" }));
    expect(onAdd).toHaveBeenCalledWith("ONION-1KG");
    expect(onAdd).toHaveBeenCalledTimes(1);
  });

  it("offers no press at all without a handler, so the row cannot swallow a click", () => {
    render(<ProductCards items={[MILK]} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("refuses a second press while that sku's write is in flight", () => {
    const onAdd = vi.fn();
    render(<ProductCards items={[MILK]} onAdd={onAdd} busySku="AMUL-DAIRY-001" />);
    const press = screen.getByRole("button", { name: "Add Amul Taaza Toned Milk 500 ml" });
    expect(press.hasAttribute("disabled")).toBe(true);
  });

  it("leaves the other cards pressable while one sku is busy", () => {
    const onAdd = vi.fn();
    render(<ProductCards items={[MILK, ONIONS]} onAdd={onAdd} busySku="AMUL-DAIRY-001" />);
    fireEvent.click(screen.getByRole("button", { name: "Add Red Onions 1 kg" }));
    expect(onAdd).toHaveBeenCalledWith("ONION-1KG");
  });
});

describe("a sold-out product is not offered", () => {
  it("says so in words, not only by dimming the photo", () => {
    render(<ProductCards items={[SOLD_OUT]} onAdd={vi.fn()} />);
    expect(screen.getByText("Out of stock")).toBeDefined();
  });

  it("draws no Add press, even when a handler was given", () => {
    render(<ProductCards items={[SOLD_OUT]} onAdd={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Add Fresh Paneer/ })).toBeNull();
  });
});

describe("itemsFromStructured reads the payloads a turn actually carries", () => {
  it("takes the rows of a search page from `hits`", () => {
    const items = itemsFromStructured({
      query: "doodh",
      hits: [
        { sku: "AMUL-DAIRY-001", display_name: "Amul Taaza Toned Milk 500 ml", unit_price: { minor: 2800, currency: "INR", display: "28.00" }, stock_units: 30, is_available: true },
        { sku: "ONION-1KG", display_name: "Red Onions 1 kg", unit_price: { minor: 4200, currency: "INR", display: "42.00" }, stock_units: 12, is_available: true },
      ],
    });
    expect(items).toHaveLength(2);
    expect(items[0]?.sku).toBe("AMUL-DAIRY-001");
    expect(items[0]?.name).toBe("Amul Taaza Toned Milk 500 ml");
  });

  it("takes one product from a payload whose fields are spread flat", () => {
    const items = itemsFromStructured({
      sku: "ONION-1KG",
      display_name: "Red Onions 1 kg",
      unit_price: { minor: 4200, currency: "INR", display: "42.00" },
      stock_units: 12,
      is_available: true,
    });
    expect(items).toHaveLength(1);
    expect(items[0]?.name).toBe("Red Onions 1 kg");
  });

  it("finds no shelf in a payload that carries no product", () => {
    expect(itemsFromStructured(null)).toHaveLength(0);
    expect(itemsFromStructured({ kind: "basket", lines: [] })).toHaveLength(0);
    expect(itemsFromStructured({ checkout_id: "c1", current_version: 2 })).toHaveLength(0);
  });

  it("repeats the gateway's availability rule rather than inventing a second one", () => {
    const explicitlyUnavailable = itemsFromStructured({ sku: "X", is_available: false });
    expect(explicitlyUnavailable[0]?.available).toBe(false);

    const soldOutDespiteTheFlag = itemsFromStructured({ sku: "X", is_available: true, stock_units: 0 });
    expect(soldOutDespiteTheFlag[0]?.available).toBe(false);

    const absentIsAvailable = itemsFromStructured({ sku: "X" });
    expect(absentIsAvailable[0]?.available).toBe(true);
  });

  it("falls back to the sku for a row with no name, rather than rendering an empty card", () => {
    const items = itemsFromStructured({ sku: "MYSTERY-1" });
    expect(items[0]?.name).toBe("MYSTERY-1");
  });
});

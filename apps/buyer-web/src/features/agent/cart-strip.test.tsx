/**
 * The strip is a mirror of a basket it does not own, and the tests hold it to that.
 *
 * Every write here leaves as a call back to the panel -- `onSetQuantity`, `onCheckout` --
 * and never as local state, because two writers for one basket is exactly the drift this
 * copilot box must not have. So the checks are about faithfulness rather than cleverness:
 * a minus asks for one fewer and a plus for one more, the SKU whose write is in flight is
 * the only chip frozen, and a total the platform cannot state is drawn as "amount not
 * stated" rather than as a zero nobody agreed to. The last two guard the animation
 * contract: `cart-pulse` sits on the total, and its key follows the amount so a changed
 * total remounts and the 700ms one-shot replays.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Money } from "@/lib/api/types";

import { CartStrip } from "./cart-strip";

afterEach(cleanup);

/** A real SKU with a photograph on disk, so `primaryImage` returns a file, not a placeholder. */
const MILK = "AMUL-DAIRY-001";
const BREAD = "ENGL-BAKE-002";

const NAMES: Record<string, string> = {
  [MILK]: "Amul Taaza Toned Milk",
  [BREAD]: "English Oven Sandwich Bread",
};

const money = (minor: number): Money => ({ minor, currency: "INR", display: `₹${minor / 100}` });

/** The props a mounted strip always needs; each test overrides only what it is about. */
function baseProps() {
  return {
    lines: [
      { sku: MILK, quantity: 2 },
      { sku: BREAD, quantity: 1 },
    ],
    names: NAMES,
    total: money(9000),
    busySku: null,
    onSetQuantity: vi.fn(),
  };
}

describe("it draws the basket it was handed", () => {
  it("renders one chip per line, each with its merchant name and quantity", () => {
    render(<CartStrip {...baseProps()} />);
    expect(screen.getByText("Amul Taaza Toned Milk")).toBeDefined();
    expect(screen.getByText("English Oven Sandwich Bread")).toBeDefined();
    expect(screen.getByText("2")).toBeDefined();
    expect(screen.getByText("1")).toBeDefined();
  });

  it("falls back to the raw SKU when the merchant sent no name for it", () => {
    render(<CartStrip {...baseProps()} names={{}} />);
    expect(screen.getByText(MILK)).toBeTruthy();
  });
});

describe("it writes by asking the owner, never itself", () => {
  it("asks for one fewer on a minus press", () => {
    const props = baseProps();
    render(<CartStrip {...props} />);
    fireEvent.click(screen.getByLabelText("Decrease Amul Taaza Toned Milk"));
    expect(props.onSetQuantity).toHaveBeenCalledWith(MILK, 1);
  });

  it("asks for one more on a plus press", () => {
    const props = baseProps();
    render(<CartStrip {...props} />);
    fireEvent.click(screen.getByLabelText("Increase Amul Taaza Toned Milk"));
    expect(props.onSetQuantity).toHaveBeenCalledWith(MILK, 3);
  });

  it("freezes only the chip whose write is in flight", () => {
    render(<CartStrip {...baseProps()} busySku={MILK} />);
    // The busy line's two presses are disabled; the other line's are still live.
    expect(screen.getByLabelText("Decrease Amul Taaza Toned Milk").hasAttribute("disabled")).toBe(
      true,
    );
    expect(screen.getByLabelText("Increase Amul Taaza Toned Milk").hasAttribute("disabled")).toBe(
      true,
    );
    expect(
      screen.getByLabelText("Decrease English Oven Sandwich Bread").hasAttribute("disabled"),
    ).toBe(false);
    expect(
      screen.getByLabelText("Increase English Oven Sandwich Bread").hasAttribute("disabled"),
    ).toBe(false);
  });
});

describe("the empty basket says so, quietly and verbatim", () => {
  it("renders exactly the one line, and no chips", () => {
    render(
      <CartStrip
        lines={[]}
        names={NAMES}
        total={null}
        busySku={null}
        onSetQuantity={vi.fn()}
      />,
    );
    expect(
      screen.getByText("Your cart is empty. Ask for something, or say yes to an offer."),
    ).toBeDefined();
    expect(screen.queryByLabelText("Increase Amul Taaza Toned Milk")).toBeNull();
  });
});

describe("the checkout press exists only when it can do something", () => {
  it("fires the handler when one was given", () => {
    const onCheckout = vi.fn();
    render(<CartStrip {...baseProps()} onCheckout={onCheckout} />);
    fireEvent.click(screen.getByText("Checkout"));
    expect(onCheckout).toHaveBeenCalledTimes(1);
  });

  it("renders no press at all when there is no handler", () => {
    render(<CartStrip {...baseProps()} onCheckout={undefined} />);
    expect(screen.queryByText("Checkout")).toBeNull();
  });

  it("is disabled while a checkout is being opened", () => {
    render(<CartStrip {...baseProps()} onCheckout={vi.fn()} checkoutBusy />);
    expect(screen.getByText("Checkout").hasAttribute("disabled")).toBe(true);
  });
});

describe("the total is the money object, or an honest absence", () => {
  it("renders the amount it was handed", () => {
    const { container } = render(<CartStrip {...baseProps()} total={money(9000)} />);
    // The strip renders whatever `<Amount>` formats from the money object -- 9000 minor is
    // `₹90.00` -- so a real figure is on screen rather than a bare zero or an em dash.
    expect(container.textContent).toContain("₹90.00");
    expect(container.textContent).not.toContain("amount not stated");
  });

  it("says 'amount not stated' rather than a zero when the total is null", () => {
    const { container } = render(<CartStrip {...baseProps()} total={null} />);
    expect(container.textContent).toContain("amount not stated");
    // The absent total is an em dash, never a numeric 0.
    expect(container.textContent).not.toContain("₹0");
  });
});

describe("the pulse contract: cart-pulse on the total, keyed to the amount", () => {
  it("puts the cart-pulse class on the span wrapping the total", () => {
    const { container } = render(<CartStrip {...baseProps()} total={money(9000)} />);
    expect(container.querySelector(".cart-pulse")).toBeTruthy();
  });

  it("remounts the total span when the amount changes, so the one-shot replays", () => {
    const props = baseProps();
    const { container, rerender } = render(<CartStrip {...props} total={money(9000)} />);
    const first = container.querySelector(".cart-pulse");
    rerender(<CartStrip {...props} total={money(12000)} />);
    const second = container.querySelector(".cart-pulse");
    // A key change means React mounts a new node rather than updating the old one, which
    // is what restarts the 700ms animation. The node identity is the observable proof.
    expect(second).toBeTruthy();
    expect(second).not.toBe(first);
  });
});

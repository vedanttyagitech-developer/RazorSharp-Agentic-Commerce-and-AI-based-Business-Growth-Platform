/**
 * The rail's whole job is to say one true thing at a glance: which rung of the spine the
 * order is on. So the tests check the register as much as the content — the five rungs are
 * always drawn in order, the reserve simulator is an aside that appears only when the
 * session says `reserve` and never joins the five, the live step wears its stage's colour
 * while the steps behind it are marked done, and the fill line's width tracks the active
 * index rather than being decorative. And because the surface is dark-only, a light token
 * leaking in would be a real regression, so it is asserted against directly.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { StageRail } from "./stage-rail";

afterEach(cleanup);

describe("it draws the five rungs of the spine", () => {
  it("renders all five labels, in order", () => {
    render(<StageRail stage="discover" />);
    for (const label of ["Discover", "Cart", "Approve", "Pay", "Order"]) {
      expect(screen.getByText(label)).toBeDefined();
    }
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(5);
    expect(items[0].textContent).toContain("Discover");
    expect(items[4].textContent).toContain("Order");
  });

  it("marks the live step with aria-current and its steps-behind as done", () => {
    render(<StageRail stage="pay" />);
    const items = screen.getAllByRole("listitem");
    // Pay is index 3: Discover/Cart/Approve are behind it and settled, Order is ahead.
    expect(items[0].getAttribute("data-state")).toBe("done");
    expect(items[2].getAttribute("data-state")).toBe("done");
    expect(items[3].getAttribute("data-state")).toBe("active");
    expect(items[3].getAttribute("aria-current")).toBe("step");
    expect(items[4].getAttribute("data-state")).toBe("future");
  });
});

describe("the active step carries its stage's colour", () => {
  it("hands each stage its own colour to CSS as --stage-colour", () => {
    // A few stages, so the mapping is checked rather than a single lucky value.
    const cases: [Parameters<typeof StageRail>[0]["stage"], string][] = [
      ["discover", "#7C8FF5"],
      ["cart", "#4FD9F2"],
      ["approve", "#B08CFF"],
      ["order", "#10b981"],
    ];
    for (const [stage, colour] of cases) {
      const { container } = render(<StageRail stage={stage} />);
      const rail = container.querySelector(".stage-rail") as HTMLElement;
      expect(rail).toBeTruthy();
      expect(rail.style.getPropertyValue("--stage-colour")).toBe(colour);
      cleanup();
    }
  });

  it("gives the live dot the breathing and active classes", () => {
    render(<StageRail stage="approve" />);
    const items = screen.getAllByRole("listitem");
    const dot = items[2].querySelector("span") as HTMLElement;
    expect(dot.classList.contains("stage-dot-active")).toBeTruthy();
    expect(dot.classList.contains("breathing-dot")).toBeTruthy();
  });
});

describe("the reserve simulator is an aside, not a rung", () => {
  it("does not show while the session is on a real rung", () => {
    render(<StageRail stage="cart" />);
    expect(screen.queryByText(/Reserve Pay/)).toBeNull();
    // Still only the five rungs — the chip added nothing to the list.
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });

  it("appears only in the reserve stage, and never as a sixth rung", () => {
    render(<StageRail stage="reserve" />);
    expect(screen.getByText("Reserve Pay · simulator")).toBeDefined();
    // The five rungs stay five; the chip sits apart from the ordered list.
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });
});

describe("the fill line reflects the active index", () => {
  it("sits at zero on the first rung and fills toward the last", () => {
    const { container: first } = render(<StageRail stage="discover" />);
    const discoverFill = first.querySelector(".stage-fill") as HTMLElement;
    expect(discoverFill.style.width).toBe("0%");
    cleanup();

    const { container: mid } = render(<StageRail stage="approve" />);
    const approveFill = mid.querySelector(".stage-fill") as HTMLElement;
    // Approve is index 2 of 5, so 2/4 = 50%.
    expect(approveFill.style.width).toBe("50%");
    cleanup();

    const { container: last } = render(<StageRail stage="order" />);
    const orderFill = last.querySelector(".stage-fill") as HTMLElement;
    expect(orderFill.style.width).toBe("100%");
  });
});

describe("it stays dark-only", () => {
  it("carries none of the light surface tokens", () => {
    const { container } = render(<StageRail stage="pay" />);
    const html = container.innerHTML;
    // The dark surface uses bg-white/[0.06] and border-white/10; a light token would be a
    // slip into the storefront palette, which the copilot scene is deliberately not.
    for (const lightToken of ["bg-surface", "bg-page", "text-ink", "border-line", "bg-white "]) {
      expect(html.includes(lightToken)).toBeFalsy();
    }
    expect(html.includes("bg-white/[0.06]")).toBeTruthy();
    expect(html.includes("border-white/10")).toBeTruthy();
  });
});

/**
 * The slip is a consent chokepoint, so the tests hold it to the register of one.
 *
 * A permission request is not an error and must never be announced as one — a buyer taught
 * to read the pause as a crash learns to dismiss it. So the card is checked for what it is
 * NOT (`role="alert"`) as carefully as for what it does. The stake has to survive a
 * monochrome screen, so every tier is asserted as a WORD, not a colour class. And because
 * the whole point is that nothing writes without a press, the two ways a buyer can answer —
 * the pointer and the keyboard — are both exercised, including the case that must stay
 * silent: keys while the decision is already in flight.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { PermissionSlip, type PermissionTier } from "./permission-slip";

afterEach(cleanup);

describe("it states what is being asked", () => {
  it("renders the title and the detail line together", () => {
    render(
      <PermissionSlip
        tier="MEDIUM"
        title="Place the order for ₹4,200"
        detail="3 items · delivered to Bengaluru 560001"
        allowLabel="Place the order"
        onAllow={() => {}}
        onDeny={() => {}}
      />,
    );

    expect(screen.getByText("Place the order for ₹4,200")).toBeDefined();
    expect(screen.getByText("3 items · delivered to Bengaluru 560001")).toBeDefined();
  });

  it("names the tier as a word for every tier, so the stake survives a monochrome screen", () => {
    // A buyer with red/green colour blindness reads emerald and rose the same; the word is
    // the primary signal and the colour only agrees with it, so the word is what the test
    // demands of all three.
    for (const tier of ["LOW", "MEDIUM", "HIGH"] as PermissionTier[]) {
      render(
        <PermissionSlip
          tier={tier}
          title={`ask at ${tier}`}
          allowLabel="Allow"
          onAllow={() => {}}
          onDeny={() => {}}
        />,
      );
      expect(screen.getByText(tier)).toBeDefined();
      cleanup();
    }
  });

  it("is not announced as an error, and can be found as a permission request", () => {
    render(
      <PermissionSlip
        tier="HIGH"
        title="Pay ₹4,200 now"
        allowLabel="Pay ₹4,200"
        onAllow={() => {}}
        onDeny={() => {}}
      />,
    );

    // The pause is the system working. A permission request drawn as an alert teaches the
    // buyer to dismiss the one screen this project exists to make them read.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("group", { name: "Permission request" })).toBeDefined();
  });
});

describe("it answers the pointer", () => {
  it("fires onAllow from the filled press and onDeny from the ghost press", () => {
    const onAllow = vi.fn();
    const onDeny = vi.fn();
    render(
      <PermissionSlip
        tier="LOW"
        title="Read the catalogue"
        allowLabel="Allow"
        onAllow={onAllow}
        onDeny={onDeny}
      />,
    );

    fireEvent.click(screen.getByText("Allow"));
    expect(onAllow).toHaveBeenCalledTimes(1);
    expect(onDeny).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Deny"));
    expect(onDeny).toHaveBeenCalledTimes(1);
    expect(onAllow).toHaveBeenCalledTimes(1);
  });

  it("holds both presses disabled while the decision is in flight", () => {
    render(
      <PermissionSlip
        tier="HIGH"
        title="Pay ₹4,200 now"
        allowLabel="Pay ₹4,200"
        onAllow={() => {}}
        onDeny={() => {}}
        busy
      />,
    );

    expect(screen.getByText("Pay ₹4,200").hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Deny").hasAttribute("disabled")).toBe(true);
  });
});

describe("it answers the keyboard on its own card", () => {
  it("allows on Enter and denies on Escape", () => {
    const onAllow = vi.fn();
    const onDeny = vi.fn();
    render(
      <PermissionSlip
        tier="MEDIUM"
        title="Change the cart"
        allowLabel="Allow"
        onAllow={onAllow}
        onDeny={onDeny}
      />,
    );

    const card = screen.getByRole("group", { name: "Permission request" });

    fireEvent.keyDown(card, { key: "Enter" });
    expect(onAllow).toHaveBeenCalledTimes(1);
    expect(onDeny).not.toHaveBeenCalled();

    fireEvent.keyDown(card, { key: "Escape" });
    expect(onDeny).toHaveBeenCalledTimes(1);
    expect(onAllow).toHaveBeenCalledTimes(1);
  });

  it("ignores Enter and Escape while busy, exactly as it disables the presses", () => {
    // A key must not do what a click cannot: an in-flight decision is answered once, and a
    // second keypress on the still-mounted card must reach nothing.
    const onAllow = vi.fn();
    const onDeny = vi.fn();
    render(
      <PermissionSlip
        tier="HIGH"
        title="Pay ₹4,200 now"
        allowLabel="Pay ₹4,200"
        onAllow={onAllow}
        onDeny={onDeny}
        busy
      />,
    );

    const card = screen.getByRole("group", { name: "Permission request" });
    fireEvent.keyDown(card, { key: "Enter" });
    fireEvent.keyDown(card, { key: "Escape" });

    expect(onAllow).not.toHaveBeenCalled();
    expect(onDeny).not.toHaveBeenCalled();
  });
});

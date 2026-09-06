/**
 * Where the floating copilot box appears, now that the home route is the copilot itself.
 *
 * This file used to be about dismissal: the box opened itself on the home shelf, a close
 * was remembered for the tab, and the tests held that arrangement in place. None of that
 * exists any more, and the tests were rewritten rather than deleted because the thing they
 * were really protecting is still worth protecting -- that the buyer never ends up with
 * two assistants on one screen, each with its own transcript and its own idea of which
 * cart is current.
 *
 * The home route draws the copilot full-height, with its own header, its own cart column
 * and the shelf inside it. This launcher is what the storefront's other pages carry, where
 * the assistant is a visitor rather than the room.
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pathname = vi.hoisted(() => ({ value: "/" }));

vi.mock("next/navigation", () => ({
  usePathname: () => pathname.value,
}));

vi.mock("@/features/agent/razorai-panel", () => ({
  RazorAIMark: () => <span data-testid="mark" />,
  RazorAIPanel: ({ open }: { open: boolean; onClose: () => void }) =>
    open ? <div data-testid="panel" /> : null,
}));

import { RazorAILauncher } from "@/features/agent/launcher";

describe("RazorAILauncher", () => {
  beforeEach(() => {
    pathname.value = "/";
  });

  it("draws nothing at all on the home route, which is the copilot", () => {
    const { container } = render(<RazorAILauncher />);
    expect(container.innerHTML).toBe("");
  });

  it("offers its button on the storefront's other pages", () => {
    pathname.value = "/c/dairy";
    render(<RazorAILauncher />);
    expect(screen.getByRole("button", { name: /razorai/i })).toBeDefined();
    expect(screen.queryByTestId("panel")).toBeNull();
  });

  it("stays closed on a checkout until the buyer asks for it", () => {
    // The approval card is the whole point of that page, and a box that opened itself over
    // it would cover the one thing the buyer navigated there to read.
    pathname.value = "/checkout/01a07758-88b0-73e8-acac-bbfc8eb39cc1";
    render(<RazorAILauncher />);
    expect(screen.queryByTestId("panel")).toBeNull();
  });
});

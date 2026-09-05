/**
 * What these tests protect is honesty, not layout.
 *
 * The Reserve Pay demo is a labelled simulator (specification 12.3). Its load-bearing
 * properties are that the simulation banner is always there and cannot be dismissed, that
 * "granting" a mandate calls no network client at all, that every amount is rendered from
 * integer minor units, and that the granted screen states nothing was recorded. If any of
 * those slips, the screen stops being a labelled simulator and becomes a fixture on the
 * money path — the one thing this storefront must not ship.
 *
 * buyer-web has no jest-dom, so assertions use `toBeTruthy()` / `container.textContent`
 * rather than `toBeInTheDocument()`, and every render is torn down in `afterEach`.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReservePayDemo, SimulationBanner } from "./reserve-pay-demo";

describe("SimulationBanner", () => {
  afterEach(cleanup);

  it("cites specification 12.3 and states Safe Mode with no money movement", () => {
    const { container } = render(<SimulationBanner />);
    const text = container.textContent ?? "";
    expect(text).toMatch(/Specification 12\.3/);
    expect(text).toMatch(/Safe Mode/);
    expect(text).toMatch(/no money can move/i);
    expect(text).toMatch(/no mandate exists/i);
    expect(text).toMatch(/no authority was granted/i);
  });

  it("surfaces the real SUBMISSION.md rationale rather than an invented one", () => {
    const { container } = render(<SimulationBanner />);
    expect(container.textContent).toMatch(
      /machine that pays without a human present, and calling it safe/,
    );
  });

  it("offers no way to dismiss the banner", () => {
    const { container } = render(<SimulationBanner />);
    // A dismiss/close control would turn a labelled simulator into a hideable one.
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(container.textContent ?? "").not.toMatch(/dismiss|close|hide/i);
  });
});

describe("ReservePayDemo", () => {
  afterEach(cleanup);

  it("keeps the simulation banner present through the grant transition", () => {
    const { container } = render(<ReservePayDemo />);
    expect(container.textContent).toMatch(/Specification 12\.3/);

    fireEvent.click(screen.getByRole("button", { name: /Approve mandate \(simulated\)/i }));

    // Still there after granting: the banner is permanent, not gated on a phase.
    expect(container.textContent).toMatch(/Specification 12\.3/);
    expect(container.textContent).toMatch(/Safe Mode/);
  });

  it("makes no network call of any kind when a mandate is granted", () => {
    // If any code path tried to reach a client, fetch is where it would land. It must not
    // be touched: the grant is a React state transition and nothing else.
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((() => {
      throw new Error("the simulator must not call the network");
    }) as unknown as typeof fetch);

    render(<ReservePayDemo />);
    fireEvent.click(screen.getByRole("button", { name: /Approve mandate \(simulated\)/i }));

    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("states that nothing was recorded once the mandate is granted", () => {
    const { container } = render(<ReservePayDemo />);
    fireEvent.click(screen.getByRole("button", { name: /Approve mandate \(simulated\)/i }));

    const text = container.textContent ?? "";
    expect(text).toMatch(/Nothing was written/i);
    expect(text).toMatch(/no admission route was called/i);
    expect(text).toMatch(/approved on screen only/i);
  });

  it("renders caps as grouped rupees from integer minor units", () => {
    const { container } = render(<ReservePayDemo />);
    fireEvent.click(screen.getByRole("button", { name: /Approve mandate \(simulated\)/i }));

    const text = container.textContent ?? "";
    // Defaults: cumulative 500000 paise -> ₹5,000.00, per-action 150000 paise -> ₹1,500.00.
    expect(text).toMatch(/₹5,000\.00/);
    expect(text).toMatch(/₹1,500\.00/);
  });

  it("mirrors real kernel authority kinds, not invented ones", () => {
    const { container } = render(<ReservePayDemo />);
    const text = container.textContent ?? "";
    // AuthorityKind has exactly two members.
    expect(text).toMatch(/Reserve \(reusable pool\)/i);
    expect(text).toMatch(/Single-use/i);
    // And the kernel's own refusal vocabulary is taught, not paraphrased away.
    expect(text).toMatch(/CAPACITY_EXCEEDED/);
    expect(text).toMatch(/MERCHANT_OUT_OF_SCOPE/);
  });
});

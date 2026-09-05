/**
 * The Reserve Pay debit ledger's load-bearing properties.
 *
 * The visual is incidental. What these tests protect is that the screen is honest by
 * construction: the simulation label is always present, no network client is ever reached,
 * no rendered amount is a float, every refusal names the kernel's real reason, and the
 * running capacity never goes negative — the same invariant the kernel's own CHECK
 * constraint enforces on `consumed_amount`.
 *
 * buyer-web has no jest-dom, so assertions use `toBeTruthy()` / `container.textContent`
 * rather than `toBeInTheDocument()`, and each test cleans up after itself.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DebitLedger, SimulationBanner, walkLedger } from "./debit-ledger";

afterEach(cleanup);

describe("SimulationBanner", () => {
  it("is always present and cites specification 12.3 by number", () => {
    const { container } = render(<SimulationBanner />);
    expect(container.textContent).toMatch(/specification 12\.3/);
    expect(container.textContent).toMatch(/No debit occurred/);
    expect(container.textContent).toMatch(/no reserve authority exists/);
  });

  it("names AUTONOMOUS_COMPLETION as Safe-Mode blocked", () => {
    const { container } = render(<SimulationBanner />);
    expect(container.textContent).toMatch(/AUTONOMOUS_COMPLETION/);
    expect(container.textContent).toMatch(/Safe Mode/i);
  });
});

describe("DebitLedger", () => {
  it("always renders the simulation banner", () => {
    const { container } = render(<DebitLedger />);
    expect(container.textContent).toMatch(/Simulation — specification 12\.3/);
  });

  it("never calls a network client", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<DebitLedger />);
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("renders no float anywhere — every amount is whole rupees from integer paise", () => {
    const { container } = render(<DebitLedger />);
    const text = container.textContent ?? "";
    // A stray float would surface as a decimal that is NOT a well-formed money string. The
    // arithmetic mistake this guards against is an amount rendered as e.g. 480.0 or 0.5 —
    // never through `formatMinor`, which always yields two decimal places or none. Strip
    // the legitimate non-money decimals (the spec citation and ISO timestamps carry dots
    // and colons of their own), then strip well-formed money, then assert nothing remains.
    const scrubbed = text
      .replace(/specification 12\.3/g, "")
      .replace(/12\.3/g, "")
      .replace(/\d{4}-\d{2}-\d{2}T[\d:.]+Z/g, "")
      .replace(/₹[\d,]+(?:\.\d{2})?/g, "");
    expect(scrubbed).not.toMatch(/\d+\.\d+/);
    // The specification citation is still present in the untouched text.
    expect(text).toMatch(/12\.3/);
  });

  it("renders all three refusals with their kernel reason named", () => {
    const { container } = render(<DebitLedger />);
    const text = container.textContent ?? "";
    // Per-transaction (per-action amount) cap: travels as AUTHORITY_INSUFFICIENT.
    expect(text).toMatch(/AUTHORITY_INSUFFICIENT/);
    // Cumulative capacity: the literal AuthorityReason.
    expect(text).toMatch(/CAPACITY_EXCEEDED/);
    // Lapse mid-flight: the revocation-epoch reason.
    expect(text).toMatch(/AUTHORITY_EPOCH_STALE/);
    // Each refusal carries a word, not colour alone.
    expect(text).toMatch(/Over per-debit limit/);
    expect(text).toMatch(/Exceeds remaining capacity/);
    expect(text).toMatch(/Authority lapsed/);
    // Three rows are refused.
    expect(container.querySelectorAll("li").length).toBeGreaterThanOrEqual(3);
  });

  it("names Operation RESERVE_DEBIT, the real kernel operation", () => {
    const { container } = render(<DebitLedger />);
    expect(container.textContent).toMatch(/RESERVE_DEBIT/);
  });
});

describe("walkLedger", () => {
  it("subtracts only admitted debits and leaves capacity unchanged on a refusal", () => {
    const rows = [
      { id: "a", bought: "x", amountMinor: 40_000, at: "t", outcome: "ADMITTED" as const },
      { id: "b", bought: "y", amountMinor: 60_000, at: "t", outcome: "ADMITTED" as const },
      {
        id: "c",
        bought: "z",
        amountMinor: 999_999,
        at: "t",
        outcome: "REFUSED" as const,
        refusal: {
          reason: "CAPACITY_EXCEEDED",
          recovery: "AUTHORITY_INSUFFICIENT",
          explain: "",
          glyph: "capacity" as const,
          word: "w",
        },
      },
    ];
    const walked = walkLedger(rows, 500_000);
    expect(walked[0].remainingMinor).toBe(460_000);
    expect(walked[1].remainingMinor).toBe(400_000);
    // The refusal allocated nothing; capacity is unchanged.
    expect(walked[2].remainingMinor).toBe(400_000);
    expect(walked[2].admitted).toBe(false);
  });

  it("keeps running capacity as an integer and never negative", () => {
    const walked = walkLedger(
      [
        { id: "a", bought: "x", amountMinor: 300_000, at: "t", outcome: "ADMITTED" as const },
        // Larger than remaining even though marked ADMITTED: mirror refuses to over-allocate,
        // exactly as the kernel only admits when amount <= remaining.
        { id: "b", bought: "y", amountMinor: 400_000, at: "t", outcome: "ADMITTED" as const },
      ],
      500_000,
    );
    for (const entry of walked) {
      expect(Number.isInteger(entry.remainingMinor)).toBe(true);
      expect(entry.remainingMinor).toBeGreaterThanOrEqual(0);
    }
    // The second debit could not fit, so it was not allocated.
    expect(walked[1].admitted).toBe(false);
    expect(walked[1].remainingMinor).toBe(200_000);
  });
});

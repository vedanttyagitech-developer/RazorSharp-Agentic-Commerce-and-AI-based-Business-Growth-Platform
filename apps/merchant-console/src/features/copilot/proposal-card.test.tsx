/**
 * The apply press, which is the one control in this feature that changes a merchant's shop.
 *
 * The property under test is narrow and it is the whole point: **applied means the platform
 * said so.** A card that painted itself applied because a button had been pressed would
 * tell a merchant their shelf is restocked while the request was sitting in a failed fetch,
 * and they would stop looking at it. So three claims are asserted separately, because any
 * one of them alone is satisfiable by a card that still misleads:
 *
 *  1. Before the press, the exact endpoint and the exact body are on screen. A person
 *     pressing a button is entitled to know what it sends.
 *  2. After a successful press, the applied state carries the platform's own answer -- its
 *     injection id, its revision pair, its audit event -- rather than a cheerful sentence.
 *  3. After a failed press, nothing reads as applied and the failure is rendered as the
 *     problem document it was.
 *
 * The fourth test is the guard: a proposal naming an endpoint this console does not
 * implement is offered no button at all, and says why. That is what keeps "the console
 * applies proposals only through the audited scenario route" from being a comment.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/problem";
import type { Injection } from "@/lib/api/types";

import { APPLY_ENDPOINT, type Proposal } from "./payloads";
import { ProposalCard } from "./proposal-card";

const mockApi = vi.hoisted(() => ({ applyProposedChange: vi.fn() }));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

vi.mock("next/link", () => ({
  default: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

function proposal(overrides: Record<string, unknown> = {}): Proposal {
  return {
    kind: "proposal",
    proposal_id: "prp_01a06fb0",
    lever: "top_seller_out_of_stock",
    title: "Restock AMUL-DAIRY-004",
    rationale: "A listed product has no units and cannot be bought.",
    metric: "failures attributable to stock",
    gate: "authoritative inventory and human-applied operational proposal",
    evidence: {
      source: "merchant-sim:demo-grocery/v1",
      window: "all-time",
      sample_size: 247,
      synthetic: true,
      catalogue_revision: 3,
      read_by: ["merchant.inventory_anomalies.read"],
    },
    change: {
      endpoint: APPLY_ENDPOINT,
      body: { kind: "STOCK_SET", sku: "AMUL-DAIRY-004", value: 24 },
      reversible: true,
      reverses_to: { kind: "STOCK_SET", sku: "AMUL-DAIRY-004", value: 0 },
    },
    applied: false,
    where: "merchant_console",
    ...overrides,
  } as Proposal;
}

const injection: Injection = {
  injection_id: "inj_01a06fb0000070008000000000000001",
  kind: "STOCK_SET",
  label: "Stock set to 24 for AMUL-DAIRY-004",
  sku: "AMUL-DAIRY-004",
  note: "",
  currency: "INR",
  deltas: [{ field: "stock_units", before: 0, after: 24 }],
  revision_before: 3,
  revision_after: 4,
  injected_at: "2026-09-05T09:40:00.000000Z",
  audit_event_id: "01a06fb4-0000-7000-8000-000000000001",
  scenario_run_id: "01a06fb5-0000-7000-8000-000000000001",
  audit_payload: {},
};

afterEach(() => {
  cleanup();
  mockApi.applyProposedChange.mockReset();
});

describe("ProposalCard", () => {
  it("shows the request it will send before anything is sent", () => {
    render(<ProposalCard proposal={proposal()} />);

    const request = screen.getByText(/POST \/v1\/scenario\/injections/);
    expect(request.textContent).toContain('"kind": "STOCK_SET"');
    expect(request.textContent).toContain('"sku": "AMUL-DAIRY-004"');
    expect(request.textContent).toContain('"value": 24');
    expect(mockApi.applyProposedChange).not.toHaveBeenCalled();
    expect(screen.queryByText("APPLIED BY YOU")).toBeNull();
  });

  it("carries the lever, the metric, the gate and the evidence specification 6.6 requires", () => {
    render(<ProposalCard proposal={proposal()} />);

    expect(screen.getByText("Top-seller out-of-stock alert")).toBeTruthy();
    expect(screen.getByText("failures attributable to stock")).toBeTruthy();
    expect(
      screen.getByText("authoritative inventory and human-applied operational proposal"),
    ).toBeTruthy();
    expect(screen.getByText("synthetic data")).toBeTruthy();
    expect(screen.getByText("source merchant-sim:demo-grocery/v1")).toBeTruthy();
    expect(screen.getByText("merchant.inventory_anomalies.read")).toBeTruthy();
  });

  it("sends the proposal's own body, verbatim, and nothing else", async () => {
    mockApi.applyProposedChange.mockResolvedValue(injection);
    render(<ProposalCard proposal={proposal()} />);

    fireEvent.click(screen.getByText("Apply this change"));

    await waitFor(() => expect(mockApi.applyProposedChange).toHaveBeenCalled());
    const [body] = mockApi.applyProposedChange.mock.calls[0];
    expect(body).toEqual({ kind: "STOCK_SET", sku: "AMUL-DAIRY-004", value: 24 });
  });

  it("draws the applied state from the platform's answer", async () => {
    mockApi.applyProposedChange.mockResolvedValue(injection);
    render(<ProposalCard proposal={proposal()} />);

    fireEvent.click(screen.getByText("Apply this change"));

    await screen.findByText("APPLIED BY YOU");
    expect(screen.getByText(injection.injection_id)).toBeTruthy();
    expect(screen.getByText("3 → 4")).toBeTruthy();
    expect(screen.getByText("stock_units: 0 → 24")).toBeTruthy();
    expect(screen.getByText(injection.audit_event_id)).toBeTruthy();
  });

  it("leaves the card unapplied when the press fails, and renders the problem", async () => {
    mockApi.applyProposedChange.mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "The platform is unavailable",
        status: 503,
        detail: "No response from the scenario controller.",
      }),
    );
    render(<ProposalCard proposal={proposal()} />);

    fireEvent.click(screen.getByText("Apply this change"));

    await screen.findByText("The platform is unavailable");
    expect(screen.getByText("503")).toBeTruthy();
    expect(screen.getByText("NOT APPLIED")).toBeTruthy();
    expect(screen.queryByText("APPLIED BY YOU")).toBeNull();
    expect(screen.getByText(/still a proposal/)).toBeTruthy();
    // A write that failed is not described as a read that failed.
    expect(screen.queryByText("READ FAILED")).toBeNull();
    // The control is still there, because the change is still unapplied.
    expect(screen.getByText("Apply this change")).toBeTruthy();
  });

  it("offers no button for a proposal naming an endpoint this console does not apply", () => {
    render(
      <ProposalCard
        proposal={proposal({
          change: {
            endpoint: "POST /v1/catalogue/products/AMUL-DAIRY-004",
            body: { kind: "STOCK_SET", sku: "AMUL-DAIRY-004", value: 24 },
            reversible: true,
          },
        })}
      />,
    );

    expect(screen.queryByText("Apply this change")).toBeNull();
    expect(screen.getByText("NOT APPLICABLE HERE")).toBeTruthy();
  });

  it("ignores an applied flag the agent had no authority to set", () => {
    render(<ProposalCard proposal={proposal({ applied: true })} />);

    expect(screen.getByText("IGNORED CLAIM")).toBeTruthy();
    expect(screen.queryByText("APPLIED BY YOU")).toBeNull();
    expect(screen.getByText("Apply this change")).toBeTruthy();
  });
});

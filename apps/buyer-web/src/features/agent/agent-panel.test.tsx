import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BasketRefContext, ClientContext } from "@/components/providers";
import type { CommerceClient } from "@/lib/api/client";
import { AgentPanel } from "./agent-panel";

afterEach(cleanup);

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

describe("AgentPanel", () => {
  let mockClient: Partial<CommerceClient>;

  beforeEach(() => {
    mockClient = {
      search: vi.fn().mockResolvedValue({
        query: "doodh",
        normalized_query: "doodh",
        locale: "en-IN",
        hits: [],
        freshness: { source: "merchant-sim", catalogue_revision: 1, observed_at: new Date().toISOString() },
      }),
      createBasket: vi.fn().mockResolvedValue({ basket_id: "bsk_agent_01" }),
      getBasket: vi.fn().mockResolvedValue({
        basket_id: "bsk_agent_01",
        lines: [{ sku: "GRO-DAIRY-001", quantity: 1 }],
        code: "OK",
        quote: null,
        unavailable: [],
        freshness: { source: "merchant-sim", catalogue_revision: 1, observed_at: new Date().toISOString() },
        stale: false,
      }),
      setBasketLine: vi.fn().mockResolvedValue({
        basket_id: "bsk_agent_01",
        lines: [{ sku: "GRO-DAIRY-001", quantity: 1 }],
        code: "OK",
        quote: null,
        unavailable: [],
        freshness: { source: "merchant-sim", catalogue_revision: 1, observed_at: new Date().toISOString() },
        stale: false,
      }),
    };
  });

  it("renders floating trigger and expands into full agent panel", async () => {
    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: null,
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <AgentPanel />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    const trigger = screen.getByRole("button", { name: /Open AI Shopping Assistant/i });
    expect(trigger).toBeTruthy();
    expect(trigger.textContent).toContain("Ask Zepto AI");

    fireEvent.click(trigger);

    expect(screen.getByRole("dialog", { name: /AI Shopping Assistant/i })).toBeTruthy();
    expect(screen.getByText(/Zepto Shopping Agent/i)).toBeTruthy();
    expect(screen.getByText(/Governed Autonomous Assistant · Track 1/i)).toBeTruthy();
  });

  it("triggers Price Shift Refusal Hero Card with before-and-after deltas", async () => {
    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: "bsk_hero_01",
            setBasketId: () => undefined,
            lineCount: 1,
            setLineCount: () => undefined,
            totalMinor: 5600,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <AgentPanel />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    // Open panel
    fireEvent.click(screen.getByRole("button", { name: /Open AI Shopping Assistant/i }));

    // Click quick prompt for hero moment
    const heroBtn = screen.getByRole("button", { name: /Simulate Price Shift Refusal/i });
    fireEvent.click(heroBtn);

    await waitFor(() => {
      expect(screen.getByRole("alert", { name: /Kernel Price Protection Refusal/i })).toBeTruthy();
    });

    expect(screen.getByText(/Hero Moment · Kernel Guard/i)).toBeTruthy();
    expect(screen.getByText(/v1 INVALIDATED ➔ v2 PROPOSED/i)).toBeTruthy();
    expect(screen.getByText(/The merchant modified pricing while checkout was in progress/i)).toBeTruthy();
    expect(screen.getByText(/Authorize Version 2/i)).toBeTruthy();
  });

  it("renders text conversation with specialist routing, tool chips, and quick prompts", async () => {
    render(
      <ClientContext.Provider value={mockClient as CommerceClient}>
        <BasketRefContext.Provider
          value={{
            basketId: null,
            setBasketId: () => undefined,
            lineCount: 0,
            setLineCount: () => undefined,
            totalMinor: null,
            setTotalMinor: () => undefined,
            currency: "INR",
            setCurrency: () => undefined,
          }}
        >
          <AgentPanel />
        </BasketRefContext.Provider>
      </ClientContext.Provider>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Open AI Shopping Assistant/i }));

    expect(screen.getByPlaceholderText(/Ask anything in English, Hindi, or Hinglish/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Send message/i })).toBeTruthy();
    expect(screen.getByText(/Shopping Specialist/i)).toBeTruthy();
    expect(screen.getByText(/Propose only/i)).toBeTruthy();
  });
});

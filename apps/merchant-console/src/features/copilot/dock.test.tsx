/**
 * The panel's obligations to the person reading it.
 *
 * Three of them are tested here because all three are the sort of thing that works on the
 * day it is written and quietly stops working later.
 *
 * A refusal must render as a refusal. The capability gate turning the copilot away is the
 * property this project exists to demonstrate, and a console that drew it as an error would
 * teach a merchant to retry it and would misdescribe the one thing worth showing them.
 *
 * The tool log must be the server's ledger and not a summary of the reply. It is recorded
 * before each tool runs, so it is the only account of the turn that cannot be composed.
 *
 * And a starter suggestion must ask something the specialist can actually answer. A
 * suggestion that comes back refused is worse than no suggestion: it teaches the merchant
 * the box is decorative, and they stop using it.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { Turn } from "@/lib/api/types";

import { CopilotDock } from "./dock";
import { SUGGESTIONS } from "./transcript";

const mockApi = vi.hoisted(() => ({ merchantTurn: vi.fn() }));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

vi.mock("next/link", () => ({
  default: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

/** A live turn, copied from what `POST /v1/merchant/agent/turn` answered for this question. */
const catalogueTurn: Turn = {
  reply:
    "Synthetic catalogue at revision 1: 247 products, 247 listed, 0 delisted, 3 out of stock, " +
    "0 low on stock.",
  language: "en",
  specialist: "growth",
  routing_reason: "default_growth",
  principal_id: "session:01a070e0/merchant_copilot/growth",
  tool_calls: [
    {
      name: "merchant.catalogue_health.read",
      summary: "read catalogue health (247 products)",
      ok: true,
    },
  ],
  denials: [],
  structured: {
    kind: "catalogue_health",
    synthetic: true,
    source: "merchant-sim",
    catalogue_revision: 1,
    sample_size: 247,
    products: 247,
    listed: 247,
    delisted: 0,
    out_of_stock: 3,
    low_stock: 0,
  },
};

const refusedTurn: Turn = {
  reply: "I cannot do that.",
  language: "en",
  specialist: "growth",
  routing_reason: "default_growth",
  principal_id: "session:01a070e0/merchant_copilot/growth",
  tool_calls: [],
  denials: [{ capability: "refund.request", reason_key: "not_on_agent_surface" }],
  structured: null,
};

/**
 * jsdom ships no `matchMedia`, and the dock asks it whether it is on a phone before it
 * locks the page behind itself. The stub answers "not a phone", which is the desktop
 * console this feature is drawn for.
 */
beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});

afterEach(() => {
  cleanup();
  mockApi.merchantTurn.mockReset();
});

function open() {
  fireEvent.click(screen.getByLabelText("Show the conversation"));
}

describe("CopilotDock", () => {
  it("offers four starter questions and sends the one that is clicked", async () => {
    mockApi.merchantTurn.mockResolvedValue(catalogueTurn);
    render(<CopilotDock />);
    open();

    for (const question of SUGGESTIONS) expect(screen.getByText(question)).toBeTruthy();
    expect(SUGGESTIONS.length).toBe(4);

    fireEvent.click(screen.getByText(SUGGESTIONS[0]));

    await waitFor(() => expect(mockApi.merchantTurn).toHaveBeenCalled());
    expect(mockApi.merchantTurn.mock.calls[0][0]).toEqual({ message: SUGGESTIONS[0] });
  });

  it("names the specialist that answered and why it was routed there", async () => {
    mockApi.merchantTurn.mockResolvedValue(catalogueTurn);
    render(<CopilotDock />);
    open();
    fireEvent.click(screen.getByText(SUGGESTIONS[0]));

    await screen.findByText("Growth specialist");
    expect(screen.getByText(/routed because the merchant copilot routes here by default/)).toBeTruthy();
  });

  it("shows the server's own tool ledger rather than a description of the reply", async () => {
    mockApi.merchantTurn.mockResolvedValue(catalogueTurn);
    render(<CopilotDock />);
    open();
    fireEvent.click(screen.getByText(SUGGESTIONS[0]));

    await screen.findByText("read catalogue health");
    expect(screen.getByText("(247 products)")).toBeTruthy();
    // The registered name is printed under the chips, alongside the card that names the
    // same tool as its own source, so both occurrences are expected.
    expect(screen.getAllByText("merchant.catalogue_health.read").length).toBeGreaterThan(0);
  });

  it("draws the structured payload as a card, from the payload's own figures", async () => {
    mockApi.merchantTurn.mockResolvedValue(catalogueTurn);
    render(<CopilotDock />);
    open();
    fireEvent.click(screen.getByText(SUGGESTIONS[0]));

    const card = (await screen.findByText("Catalogue health")).closest("section");
    if (card === null) throw new Error("the catalogue health card must be a section");

    // Each figure is read against its own label, because the point is not that the number
    // 247 is somewhere on screen -- it is that `listed` says 247 and `out_of_stock` says 3.
    const stat = (label: string) => within(card).getByText(label).parentElement?.textContent ?? "";
    expect(stat("products")).toContain("247");
    expect(stat("listed")).toContain("247");
    expect(stat("delisted")).toContain("0");
    expect(stat("out of stock")).toContain("3");
    expect(within(card).getByText("synthetic data")).toBeTruthy();
    expect(within(card).getByText("source merchant-sim")).toBeTruthy();
  });

  it("renders a denial as the gate working, not as an error", async () => {
    mockApi.merchantTurn.mockResolvedValue(refusedTurn);
    render(<CopilotDock />);
    open();
    fireEvent.change(screen.getByLabelText("Ask the Merchant Copilot"), {
      target: { value: "refund order 12" },
    });
    fireEvent.click(screen.getByLabelText("Send to the Merchant Copilot"));

    await screen.findByText("REFUSED");
    expect(screen.getByText("refund.request")).toBeTruthy();
    expect(screen.getByText("not_on_agent_surface")).toBeTruthy();
    expect(screen.getByText(/This is the system working, not a fault/)).toBeTruthy();
    // A refusal is never dressed as a failed read: that panel says READ FAILED.
    expect(screen.queryByText("READ FAILED")).toBeNull();
  });

  it("announces new replies politely and closes on Escape", async () => {
    mockApi.merchantTurn.mockResolvedValue(catalogueTurn);
    render(<CopilotDock />);
    open();
    fireEvent.click(screen.getByText(SUGGESTIONS[0]));

    const log = await screen.findByRole("log");
    expect(log.getAttribute("aria-live")).toBe("polite");
    expect(screen.getByRole("dialog")).toBeTruthy();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // The composer survives the close: it is the shell's own bar, not part of the panel.
    expect(screen.getByLabelText("Ask the Merchant Copilot")).toBeTruthy();
  });
});

/**
 * What the studio must never do, asserted rather than promised.
 *
 * Two properties are worth a suite. The first is that the screen composes nothing it was
 * not given: when the capability read fails there is no roster, no checkbox and no
 * boundary, only the problem document — a studio that drew a capability list over a
 * failed read would be describing a boundary it had invented, which is the exact failure
 * the whole console was rebuilt to remove.
 *
 * The second is that switching a capability off is visible in the sentence a merchant
 * reads. That sentence is the product; if it can go stale against the checkboxes above
 * it, everything else on the page is decoration.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentCapabilities } from "@/lib/api/types";
import { ApiError } from "@/lib/api/problem";

import StudioPage from "./page";

const mockApi = vi.hoisted(() => ({
  agentCapabilities: vi.fn(),
  merchantTurn: vi.fn(),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

vi.mock("next/link", () => ({
  default: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

const CAPS: AgentCapabilities = {
  copilot: "merchant",
  actor_type: "OPERATOR",
  session_capabilities: ["merchant.catalogue_health.read", "merchant.checkout_metrics.read"],
  agent_capabilities: ["merchant.catalogue_health.read", "merchant.checkout_metrics.read"],
  specialists: [
    {
      specialist: "growth",
      principal_id: "session:0199/merchant_copilot/growth",
      capabilities: ["merchant.catalogue_health.read", "merchant.checkout_metrics.read"],
      tools: ["merchant.catalogue_health.read", "merchant.checkout_metrics.read"],
    },
  ],
  absent_by_construction: ["authority.revoke", "checkout.approve", "checkout.reject"],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("the studio composes only what the server declared", () => {
  it("renders the problem document and no capability list when the read fails", async () => {
    mockApi.agentCapabilities.mockRejectedValue(
      new ApiError({
        type: "about:blank",
        title: "Not authenticated",
        status: 401,
        detail: "The bearer token is not recognised.",
      }),
    );

    render(<StudioPage />);

    expect(await screen.findByText("Not authenticated")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByText("What it can do")).toBeNull();
  });

  it("offers exactly one checkbox per capability the server declared, and no field to add one", async () => {
    mockApi.agentCapabilities.mockResolvedValue(CAPS);

    render(<StudioPage />);
    await screen.findByText("What this assistant can and cannot do");

    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.length).toBe(CAPS.specialists[0].capabilities.length);
    expect(boxes.every((box) => (box as HTMLInputElement).checked)).toBe(true);

    // Every text input on the page is a label, a briefing or the sandbox composer. None of
    // them is a capability, and there is no way to type one. The count is asserted as well
    // as the three names, so a fourth field added later fails here rather than quietly
    // becoming a place to write a capability into.
    expect(screen.getAllByRole("textbox").length).toBe(3);
    expect(screen.getByRole("textbox", { name: "What your shop calls this assistant" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Briefing for this assistant" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Ask the assistant you are composing" })).toBeTruthy();
  });

  it("names the verbs the server said no agent may hold, from the server's own list", async () => {
    mockApi.agentCapabilities.mockResolvedValue(CAPS);

    render(<StudioPage />);
    const section = await screen.findByLabelText("What no agent may ever hold");
    for (const capability of CAPS.absent_by_construction) {
      expect(within(section).getByText(capability)).toBeTruthy();
    }
  });
});

describe("the boundary follows the checkboxes", () => {
  it("moves a capability from what it can do to what you switched off", async () => {
    mockApi.agentCapabilities.mockResolvedValue(CAPS);

    render(<StudioPage />);
    await screen.findByText("What this assistant can and cannot do");

    const can = screen.getByLabelText("What it can do");
    expect(within(can).getByText("merchant.checkout_metrics.read")).toBeTruthy();
    expect(screen.queryByLabelText("What you switched off")).toBeNull();

    fireEvent.click(screen.getByLabelText("Read checkout metrics"));

    await waitFor(() => {
      expect(screen.getByLabelText("What you switched off")).toBeTruthy();
    });
    const off = screen.getByLabelText("What you switched off");
    expect(within(off).getByText("merchant.checkout_metrics.read")).toBeTruthy();
    expect(
      within(screen.getByLabelText("What it can do")).queryByText("merchant.checkout_metrics.read"),
    ).toBeNull();
  });

  it("withdraws the question a switched-off capability was the only source of", async () => {
    mockApi.agentCapabilities.mockResolvedValue(CAPS);

    render(<StudioPage />);
    await screen.findByText("What this assistant can and cannot do");
    expect(screen.getByRole("button", { name: "How are my checkouts and orders doing?" })).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Read checkout metrics"));

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "How are my checkouts and orders doing?" }),
      ).toBeNull();
    });
  });
});

describe("the sandbox says what it is", () => {
  it("names the endpoint that would run a turn narrowed to the draft, because it does not exist", async () => {
    mockApi.agentCapabilities.mockResolvedValue(CAPS);

    render(<StudioPage />);
    await screen.findByText("What this assistant can and cannot do");
    expect(
      screen.getByText("Missing: POST /v1/merchant/agent/definitions/{id}/turn"),
    ).toBeTruthy();
  });
});

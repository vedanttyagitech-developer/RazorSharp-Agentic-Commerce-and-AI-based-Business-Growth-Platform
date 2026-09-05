/**
 * The overview's one job on the outbox panel: never let a stalled refund read as settled.
 *
 * A tenant can have money owed to a buyer parked a week out and still render, on every
 * summary screen a merchant looks at, as a small tidy queue with nothing wrong. That is
 * what happened here: two commands scheduled seven days away, three refunds none of which
 * succeeded, an empty review queue, and an overview reporting "PENDING 2" — every figure
 * on it true, and the screen as a whole false.
 *
 * So the claims are tested separately, because a screen can satisfy any one of them and
 * still mislead:
 *
 *  1. The stalled count is on the overview, not only in a timestamp column on another
 *     tab. A fact a merchant has to go looking for is a fact the console did not tell
 *     them.
 *  2. The oldest command is named. A bare "2 stalled" makes the merchant go and match a
 *     number against a list, which is the work the panel exists to save.
 *  3. Parked and overdue stay apart. The remedies are opposite — one is a scheduling
 *     decision somebody made, the other is a worker that is not running — and a merged
 *     total sends the reader after the wrong one.
 *  4. Zero is stated. A warning that only appears when it is bad cannot be distinguished,
 *     when absent, from a warning that was never computed.
 *  5. A failed read renders the problem document and *no* reassurance. This is the one
 *     that matters most: a panel that falls back to "nothing parked" when it could not
 *     read anything is telling the exact lie this whole change exists to stop.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/problem";
import type { OutboxPage, OutboxWaiting } from "@/lib/api/types";

import OverviewPage from "./page";

const mockApi = vi.hoisted(() => ({
  session: vi.fn(),
  safeMode: vi.fn(),
  outbox: vi.fn(),
  orders: vi.fn(),
  refunds: vi.fn(),
  retainedRevenue: vi.fn(),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

/** The two commands the audit actually found, as the API reports them. */
const PARKED_RECONCILE = {
  command_id: "01a06fc3-6d11-7e4c-b310-e0973c18ff10",
  command_type: "RECONCILE_REFUND",
  status: "PENDING",
  attempts: 0,
  available_at: "2026-09-12T04:11:31.509122Z",
  leased_until: null,
  correlation_id: "01a06fc3-6d11-7e4c-b310-e0973c18ff11",
  created_at: "2026-09-05T04:11:13.537036Z",
};

function waiting(over: Partial<OutboxWaiting> = {}): OutboxWaiting {
  return {
    parked: 2,
    overdue: 0,
    parked_beyond_seconds: 3600,
    overdue_beyond_seconds: 60,
    oldest: PARKED_RECONCILE,
    ...over,
  };
}

function outboxPage(over: Partial<OutboxWaiting> = {}): OutboxPage {
  return {
    commands: [PARKED_RECONCILE],
    counts: { PENDING: 2, LEASED: 0, DONE: 53, FAILED: 0, DEAD: 2 },
    waiting: waiting(over),
    limit: 1,
  };
}

/** The outbox panel, found by its own heading rather than by position on the page. */
function outboxPanel(): HTMLElement {
  const heading = screen.getAllByText("Durable outbox")[0];
  const panel = heading.closest("section") ?? heading.parentElement?.parentElement;
  if (!panel) throw new Error("the durable outbox panel is not on the overview");
  return panel as HTMLElement;
}

beforeEach(() => {
  // Every other read on this page resolves, so nothing below can pass merely because the
  // screen was empty.
  mockApi.session.mockResolvedValue({
    merchant_id: "01a06dc7-09fb-7087-bb73-b9c5c646e743",
  });
  mockApi.safeMode.mockResolvedValue({
    mode: "NORMAL",
    scope: "tenant",
    safe_mode: false,
    permitted: { DELEGATED_DEBIT: true, REFUND_EXECUTE: true, RECONCILIATION: true },
  });
  mockApi.orders.mockResolvedValue({ counts: { CONFIRMED: 5 }, scope: "tenant" });
  mockApi.refunds.mockResolvedValue({
    counts: { REFUND_PENDING: 1, REFUND_UNKNOWN: 1, REFUND_FAILED: 1 },
    scope: "tenant",
  });
  mockApi.retainedRevenue.mockRejectedValue(
    new ApiError({ type: "about:blank", title: "Nothing to account for", status: 404 }),
  );
  mockApi.outbox.mockResolvedValue(outboxPage());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the overview says when money work is parked", () => {
  it("puts the stalled count on the overview, beside the status counts it corrects", async () => {
    render(<OverviewPage />);
    const panel = within(await screen.findByText("MONEY WORK IS STALLED").then(outboxPanel));

    expect(panel.getByText("MONEY WORK IS STALLED")).toBeTruthy();
    // The PENDING count is still shown. The point is not that it was wrong — it is that
    // on its own it could not say this, and both now sit on the same panel.
    expect(panel.getByText("PENDING")).toBeTruthy();
  });

  it("names the oldest command instead of leaving a bare number", async () => {
    render(<OverviewPage />);
    await screen.findByText("MONEY WORK IS STALLED");
    const panel = within(outboxPanel());

    expect(panel.getByText("RECONCILE_REFUND")).toBeTruthy();
    expect(panel.getByText(/01a06fc3/)).toBeTruthy();
  });

  it("keeps parked and overdue apart, because the remedies are opposite", async () => {
    mockApi.outbox.mockResolvedValue(outboxPage({ parked: 1, overdue: 1 }));
    render(<OverviewPage />);
    await screen.findByText("MONEY WORK IS STALLED");
    const panel = outboxPanel();

    expect(within(panel).getByText(/parked/)).toBeTruthy();
    expect(within(panel).getByText(/overdue/)).toBeTruthy();
    // Two lines with their own counts, not one merged total presented as the whole story.
    expect(within(panel).queryByText(/2 parked and overdue/)).toBeNull();
  });

  it("states the zero rather than falling silent when nothing is stalled", async () => {
    mockApi.outbox.mockResolvedValue(outboxPage({ parked: 0, overdue: 0, oldest: null }));
    render(<OverviewPage />);

    expect(await screen.findByText(/Nothing parked and nothing overdue/)).toBeTruthy();
    expect(screen.queryByText("MONEY WORK IS STALLED")).toBeNull();
  });

  it("renders the problem document and no reassurance when the read fails", async () => {
    mockApi.outbox.mockRejectedValue(
      new ApiError({ type: "about:blank", title: "The API raised an unhandled error", status: 500 }),
    );
    render(<OverviewPage />);
    await screen.findByText("The API raised an unhandled error");

    // The failure is stated, and nothing is invented in place of the figures.
    expect(screen.getAllByText("READ FAILED").length).toBeGreaterThan(0);
    // Neither the calm sentence nor the alarming one: a console with no fixtures behind
    // this panel must not answer a question it could not read the answer to.
    expect(screen.queryByText(/Nothing parked and nothing overdue/)).toBeNull();
    expect(screen.queryByText("MONEY WORK IS STALLED")).toBeNull();
  });
});

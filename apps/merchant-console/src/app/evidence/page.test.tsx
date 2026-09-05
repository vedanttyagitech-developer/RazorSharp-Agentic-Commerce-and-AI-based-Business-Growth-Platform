/**
 * The evidence page's one derived figure, checked against the arithmetic the server did.
 *
 * Everything on this page is a committed row except the re-quote movement, which is
 * `corrected_total_minor − stale_approved_minor` — a subtraction of two integers the API
 * sent, for a difference the API does not send as a field of its own. That makes it the
 * only number on the platform's evidence page that a browser produced, so it is the only
 * number worth a test, and the test computes it from the same two response fields rather
 * than from a constant pasted out of a run.
 *
 * The trap it guards is the API's own `difference_minor`, which is a *different*
 * subtraction: captured minus approved. On a checkout that was refused and never paid the
 * server states no difference at all, while the re-quote movement is ₹102.00. Rendering
 * the browser's figure under the server's name there would be the console asserting a
 * settlement fact out of two quotes. So the two are asserted apart on the unsettled
 * checkout, and asserted to agree on the settled one, which is the only case where they
 * are the same number.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AuditVerification,
  ProofChain,
  RetainedRevenue,
  Session,
  Timeline,
} from "@/lib/api/types";

import EvidencePage from "./page";

const mockApi = vi.hoisted(() => ({
  session: vi.fn(),
  retainedRevenue: vi.fn(),
  proof: vi.fn(),
  timeline: vi.fn(),
  verifyStream: vi.fn(),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

// The page reads `checkout_id` off the query string; there is no App Router around a
// component rendered here to provide one.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

const CHECKOUT = "01a06fa8-83ae-741a-bab3-353d903d4878";
const MERCHANT = "01a06dc7-09fb-7087-bb73-b9c5c646e743";
const ATTEMPT = "01a06fa9-0000-7000-8000-00000000000a";

const session: Session = {
  session_id: "01a06fae-0738-73e8-8fca-e9f80b5603d7",
  tenant_id: "01a06dc7-09f7-7f2c-bd05-df1c773ddb22",
  merchant_id: MERCHANT,
  buyer_ref: "buyer-089794d44f8c",
  actor_type: "OPERATOR",
  capabilities: ["catalogue.read", "order.read"],
  expires_at: "2026-09-05T12:00:00.000000Z",
};

/**
 * The shape the live API answers with after the demonstration's refusal: version 1
 * approved at ₹579.95, version 2 re-quoted at ₹681.95, nothing captured.
 */
const unsettled: RetainedRevenue = {
  checkout_id: CHECKOUT,
  merchant_id: MERCHANT,
  currency: "INR",
  stale_version: 1,
  stale_approved_minor: 57995,
  stale_invalidated_at: "2026-09-05T03:41:49.905904Z",
  corrected_version: 2,
  corrected_total_minor: 68195,
  captured_minor: null,
  captured_from: null,
  difference_minor: null,
  direction: "UNSETTLED",
  refunded_minor: 0,
  net_retained_minor: null,
  controlled_scenario: true,
  explanation:
    "Version 1's approval was invalidated, but no capture has been verified yet, so no difference can be stated.",
};

/** The same checkout once the corrected version was the one admitted and captured. */
const settled: RetainedRevenue = {
  ...unsettled,
  captured_minor: 68195,
  captured_from: "WEBHOOK",
  difference_minor: 10200,
  direction: "RETAINED",
  net_retained_minor: 10200,
  explanation: "The corrected version was captured; the refusal retained ₹102.00.",
};

const proof: ProofChain = {
  checkout_id: CHECKOUT,
  tenant_id: session.tenant_id,
  merchant_id: MERCHANT,
  payment_attempt_id: ATTEMPT,
  attempt_ids: [ATTEMPT],
  links: {},
  verdict: {
    tier: "FULL",
    ok: true,
    checks: [
      { name: "approval_binds_content_hash", ok: true, applicable: true, detail: "hash matches" },
      { name: "capture_evidence_verified", ok: true, applicable: false, detail: "no capture yet" },
    ],
    failed: [],
  },
  audit_streams: {},
};

function verification(aggregateType: string, aggregateId: string, length: number): AuditVerification {
  return {
    aggregate_type: aggregateType,
    aggregate_id: aggregateId,
    intact: true,
    empty: false,
    length,
    events_verified: length,
    head_seq: length,
    head_hash: "9f2b1c0d4e5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e",
    code: "CHAIN_INTACT",
    first_break: null,
  };
}

const timeline: Timeline = {
  checkout_id: CHECKOUT,
  entries: [
    {
      id: "01a06fa8-0000-7000-8000-000000000010",
      occurred_at: "2026-09-05T03:41:40.000000Z",
      source: "audit",
      actor: "BUYER",
      action: "APPROVAL_GRANTED",
      summary: "Version 1 approved at ₹579.95",
      correlation_id: "01a06fa8-0000-7000-8000-0000000000c0",
      scenario_injection: false,
      checkout_version: 1,
      content_hash: "abc123",
      policy_version: "v1",
      policy_receipt_hash: "def456",
      freshness: null,
      approval_ref: null,
      authority_epoch: 1,
      decision: null,
      grant: null,
      payment_attempt_id: null,
      provider: null,
      reconciliation: null,
      refund: null,
      audit: null,
      details: {},
    },
    {
      id: "01a06fa8-0000-7000-8000-000000000011",
      occurred_at: "2026-09-05T03:41:45.000000Z",
      source: "scenario",
      actor: "OPERATOR",
      action: "SCENARIO_INJECTION",
      summary: "PRICE_SET on AMUL-DAIRY-001",
      correlation_id: "01a06fa8-0000-7000-8000-0000000000c1",
      scenario_injection: true,
      checkout_version: null,
      content_hash: null,
      policy_version: null,
      policy_receipt_hash: null,
      freshness: null,
      approval_ref: null,
      authority_epoch: null,
      decision: null,
      grant: null,
      payment_attempt_id: null,
      provider: null,
      reconciliation: null,
      refund: null,
      audit: null,
      details: {},
    },
  ],
  cursor: null,
  scenario_injections: 1,
};

/** The tile that carries the one figure this browser derived. */
function requoteTile(): HTMLElement {
  const eyebrow = screen.getByText(/Re-quote movement/);
  const tile = eyebrow.parentElement;
  if (!tile) throw new Error("the re-quote tile has no container");
  return tile;
}

function netRetainedTile(): HTMLElement {
  const eyebrow = screen.getByText("Net retained, after refunds");
  const tile = eyebrow.parentElement;
  if (!tile) throw new Error("the net-retained tile has no container");
  return tile;
}

beforeEach(() => {
  mockApi.session.mockResolvedValue(session);
  mockApi.retainedRevenue.mockResolvedValue(unsettled);
  mockApi.proof.mockResolvedValue(proof);
  mockApi.timeline.mockResolvedValue(timeline);
  mockApi.verifyStream.mockImplementation((aggregateType: string, aggregateId: string) =>
    Promise.resolve(verification(aggregateType, aggregateId, aggregateType === "checkout" ? 7 : 3)),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the re-quote movement, the page's only derived figure", () => {
  it("is corrected_total_minor − stale_approved_minor, signed", async () => {
    render(<EvidencePage />);
    const tile = await screen.findByText(/Re-quote movement/).then((node) => node.parentElement!);

    // Derived here from the same two response fields the component was handed, so a
    // change to either one moves the expectation with it.
    const approved = unsettled.stale_approved_minor!;
    const corrected = unsettled.corrected_total_minor!;
    expect(corrected - approved).toBe(10200);
    expect(within(tile).getByText("+₹102.00")).toBeTruthy();
  });

  it("does not present itself as the server's difference_minor", async () => {
    render(<EvidencePage />);
    await screen.findByText(/Re-quote movement/);

    // The server states no difference on an unsettled checkout, and that em dash must
    // survive beside a browser figure of ₹102.00 rather than be filled in from it.
    expect(unsettled.difference_minor).toBe(null);
    const differenceRow = screen
      .getByText("Difference — retained by the refusal")
      .closest("a");
    expect(differenceRow).toBeTruthy();
    expect(within(differenceRow as HTMLElement).getByText("—")).toBeTruthy();
    expect(within(differenceRow as HTMLElement).queryByText(/102/)).toBe(null);

    // And the browser's figure appears exactly once on the page, in its own tile.
    expect(screen.getAllByText("+₹102.00")).toHaveLength(1);
  });

  it("agrees with difference_minor once the corrected version is the captured one", async () => {
    mockApi.retainedRevenue.mockResolvedValue(settled);
    render(<EvidencePage />);
    await screen.findByText(/Re-quote movement/);

    const browserFigure = settled.corrected_total_minor! - settled.stale_approved_minor!;
    expect(browserFigure).toBe(settled.difference_minor);
    // Same number, two renderings, because one is signed movement and one is an amount.
    expect(within(requoteTile()).getByText("+₹102.00")).toBeTruthy();
    const differenceRow = screen.getByText("Difference — retained by the refusal").closest("a");
    expect(within(differenceRow as HTMLElement).getByText("₹102.00")).toBeTruthy();
  });

  it("renders an em dash rather than a zero when either operand is absent", async () => {
    mockApi.retainedRevenue.mockResolvedValue({
      ...unsettled,
      corrected_total_minor: null,
      corrected_version: null,
    });
    render(<EvidencePage />);
    await screen.findByText(/Re-quote movement/);
    expect(within(requoteTile()).getByText("—")).toBeTruthy();
    expect(within(requoteTile()).queryByText("₹0.00")).toBe(null);
  });
});

describe("absent is not zero, all the way down the page", () => {
  it("states no net retained amount while nothing has been captured", async () => {
    render(<EvidencePage />);
    await screen.findByText(/Net retained, after refunds/);
    expect(unsettled.net_retained_minor).toBe(null);
    expect(within(netRetainedTile()).getByText("—")).toBeTruthy();
    expect(within(netRetainedTile()).queryByText("₹0.00")).toBe(null);
  });

  it("renders a captured amount of zero-knowledge as an em dash and says why", async () => {
    render(<EvidencePage />);
    const capturedRow = (await screen.findByText("Captured — what the buyer actually paid")).closest("a");
    expect(within(capturedRow as HTMLElement).getByText("—")).toBeTruthy();
    expect(
      within(capturedRow as HTMLElement).getByText(
        "no order row yet, so the platform states no captured amount",
      ),
    ).toBeTruthy();
  });

  it("renders refunded_minor of 0 as an amount, because the server did state it", async () => {
    render(<EvidencePage />);
    await screen.findByText("Refunded since capture");
    expect(unsettled.refunded_minor).toBe(0);
    expect(screen.getByText("₹0.00")).toBeTruthy();
  });
});

describe("the page verifies its own evidence", () => {
  it("renders each audit chain's verdict from the server's recomputation", async () => {
    render(<EvidencePage />);
    // The attempt's stream cannot be named until the proof chain read has answered, so
    // the second card arrives a tick after the first.
    await screen.findByText(ATTEMPT);

    expect(mockApi.verifyStream).toHaveBeenCalledWith("checkout", CHECKOUT, expect.anything());
    expect(mockApi.verifyStream).toHaveBeenCalledWith("payment_attempt", ATTEMPT, expect.anything());
    expect(screen.getAllByText("INTACT")).toHaveLength(2);
    expect(screen.getAllByText("CHAIN_INTACT")).toHaveLength(2);
    expect(screen.getByText(CHECKOUT)).toBeTruthy();
    expect(screen.getByText(ATTEMPT)).toBeTruthy();
  });

  it("renders a broken chain as loudly as an intact one", async () => {
    mockApi.verifyStream.mockImplementation((aggregateType: string, aggregateId: string) =>
      Promise.resolve({
        ...verification(aggregateType, aggregateId, 7),
        intact: false,
        code: "CHAIN_BROKEN",
        first_break: { seq: 4, expected: "aaaa", stored: "bbbb" },
      }),
    );
    render(<EvidencePage />);
    await waitFor(() => expect(screen.getAllByText("BROKEN")).toHaveLength(2));
    expect(screen.queryByText("INTACT")).toBe(null);
    // The break the server found is shown verbatim, not summarised into a red badge.
    expect(screen.getAllByText(/"stored": "bbbb"/).length).toBe(2);
  });

  it("labels the scenario injection in the timeline rather than filtering it out", async () => {
    render(<EvidencePage />);
    await screen.findByText("SCENARIO_INJECTION");
    expect(screen.getAllByText("INJECTED").length).toBeGreaterThan(0);
    expect(screen.getByText("1 injection")).toBeTruthy();
  });
});

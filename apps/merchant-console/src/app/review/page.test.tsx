/**
 * The review queue's priority row, which used to invent a tier.
 *
 * The page mapped a hardcoded `["P1","P2","P3","P4"]` and rendered
 * `priority_counts[priority] ?? 0`, so a platform whose `Priority` enum has three members
 * was drawn with a fourth chip reading "P4 · 0". Two things were wrong with that and only
 * one of them is obvious.
 *
 * The obvious one: it is a figure nobody read from the API, on the console whose entire
 * premise is that every figure on every page was read from the API in that page load.
 *
 * The one that matters more: of the two ways to get this wrong, it is the worse. A missing
 * chip is a gap a reviewer notices. A chip reading zero is an assurance -- it says the tier
 * exists and is quiet, which is a claim about the queue that nobody made and that a
 * reviewer working down a list will act on by not looking further.
 *
 * The end-to-end suite catches this against the running API. These tests catch it against
 * a response shape, which is what makes them survive a platform that adds a P0 tomorrow:
 * the correct behaviour is not "render three" but "render what arrived".
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Queue } from "@/lib/api/types";

import ReviewPage from "./page";

const mockApi = vi.hoisted(() => ({ queue: vi.fn(), reviewCase: vi.fn() }));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

const SCOPE =
  "P0 ships the human-review queue and its evidence. No case is assigned, decided, " +
  "annotated or resolved here.";

/** An empty queue, counted the way `routers/review.py` counts it: one key per Priority. */
function emptyQueue(counts: Record<string, number> = { P1: 0, P2: 0, P3: 0 }): Queue {
  return { cases: [], priority_counts: counts, limit: 50, scope: SCOPE } as Queue;
}

/** Every priority chip on the screen, as text. */
function priorityChips(): string[] {
  return screen
    .queryAllByText(/^P\d+ · \d+$/)
    .map((node) => (node.textContent ?? "").replace(/\s+/g, " ").trim());
}

beforeEach(() => {
  mockApi.queue.mockResolvedValue(emptyQueue());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the priority row renders the API's map and nothing else", () => {
  it("renders one chip per priority the API counted", async () => {
    render(<ReviewPage />);
    await screen.findByText(/^P1 · /);
    expect(priorityChips()).toEqual(["P1 · 0", "P2 · 0", "P3 · 0"]);
  });

  it("does not render a priority the API never sent", async () => {
    render(<ReviewPage />);
    await screen.findByText(/^P1 · /);
    // The regression itself. P4 is not a tier this platform has.
    expect(screen.queryByText(/^P4 · /)).toBeNull();
  });

  it("renders a priority the API counted at zero, because zero is an answer", async () => {
    mockApi.queue.mockResolvedValue(emptyQueue({ P1: 0, P2: 4, P3: 0 }));
    render(<ReviewPage />);
    await screen.findByText("P2 · 4");
    // "No P1 cases" and "P1 not shown" are different claims about a queue somebody is
    // working down, so a counted zero must still appear.
    expect(priorityChips()).toEqual(["P1 · 0", "P2 · 4", "P3 · 0"]);
  });

  it("renders a priority the platform adds later without this file being edited", async () => {
    // The point of reading the map rather than a literal: the console follows the API.
    mockApi.queue.mockResolvedValue(emptyQueue({ P0: 2, P1: 0, P2: 0, P3: 1 }));
    render(<ReviewPage />);
    await screen.findByText("P0 · 2");
    expect(priorityChips()).toEqual(["P0 · 2", "P1 · 0", "P2 · 0", "P3 · 1"]);
  });

  it("renders no chips at all when the API sent no counts", async () => {
    // An empty map is not three zeros. If the API stopped counting, the honest screen says
    // nothing rather than inventing the tiers it used to know about.
    mockApi.queue.mockResolvedValue(emptyQueue({}));
    render(<ReviewPage />);
    await screen.findByText(/Nothing has been escalated/);
    expect(priorityChips()).toEqual([]);
  });
});

describe("an empty queue is a fact, not a blank screen", () => {
  it("says why the queue is empty rather than showing nothing", async () => {
    render(<ReviewPage />);
    const empty = await screen.findByText(/Nothing has been escalated for a person to decide/);
    expect(empty.textContent).toContain(
      "an empty queue is the platform reporting that it settled everything it saw",
    );
  });

  it("renders the scope the API stated, so the surface's limits come from the API", async () => {
    render(<ReviewPage />);
    expect(await screen.findByText(new RegExp(`scope ${SCOPE.slice(0, 40)}`))).toBeTruthy();
  });

  it("shows the failure rather than an empty queue when the read fails", async () => {
    mockApi.queue.mockRejectedValue(new Error("upstream is gone"));
    render(<ReviewPage />);
    await screen.findByText("READ FAILED");
    // The distinction the whole console turns on: nothing that reads as "there is no work".
    expect(screen.queryByText(/Nothing has been escalated/)).toBeNull();
    expect(priorityChips()).toEqual([]);
  });
});

describe("the queue resolves nothing", () => {
  it("offers no control beyond selecting a case", async () => {
    const queue = emptyQueue({ P1: 1, P2: 0, P3: 0 });
    queue.cases = [
      {
        case_key: "case-1",
        state: "AWAITING_HUMAN",
        priority: "P1",
        reason_code: "REFUND_UNRESOLVED",
        reason_family: "refund",
        checkout_id: "01a06fb2-0000-7000-8000-000000000001",
        payment_attempt_id: null,
        refund_id: null,
        monetary_exposure: null,
        opened_at: "2026-09-05T03:00:00.000000Z",
        opened_by: "SYSTEM",
        target_response_by: "2026-09-05T04:00:00.000000Z",
        target_response_seconds: 3600,
        correlation_id: "01a06fb3-0000-7000-8000-000000000001",
        attempts_used: 6,
        attempts_bound: 6,
        detections: 1,
        audit_event_id: "01a06fb4-0000-7000-8000-000000000001",
        audit_aggregate_type: "payment_attempt",
        audit_aggregate_id: "01a06fb5-0000-7000-8000-000000000001",
        audit_seq: 12,
        audit_self_hash: "hash",
        proof_chain: { payment_attempt_id: null },
      },
    ] as Queue["cases"];
    mockApi.queue.mockResolvedValue(queue);

    render(<ReviewPage />);
    await screen.findByText("REFUND_UNRESOLVED");

    // Exactly one control, and it is the case card. An assign or resolve button that did
    // nothing would tell a reviewer the case was settled here when it was not.
    const main = screen.getByRole("main");
    expect(within(main).getAllByRole("button")).toHaveLength(1);
    expect(within(main).queryAllByRole("textbox")).toHaveLength(0);
    expect(within(main).queryAllByRole("combobox")).toHaveLength(0);
  });
});

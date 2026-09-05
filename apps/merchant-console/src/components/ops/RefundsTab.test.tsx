/**
 * The refund vocabulary, which is the one place in this console where a conflation costs
 * a buyer money.
 *
 * REFUND_PENDING means the provider was asked and has not answered — the money is in
 * flight and the correct action is to wait. REFUND_UNKNOWN means the answer was lost —
 * the money may or may not have moved and reconciliation, not an operator, owns the row.
 * An operator who reads either of those as REFUND_FAILED retries the refund, and the
 * buyer is refunded twice.
 *
 * So "visibly different" is tested as three separate claims, because any one of them
 * alone can be satisfied by a screen that still misleads:
 *
 *  1. Three different colours. A shared tone would put pending and failed in the same
 *     visual bucket at a glance.
 *  2. Three different sentences, on the row itself. Colour is not available to every
 *     reader, does not survive a greyscale projector, and carries no instruction — only
 *     the sentence says who owns the row.
 *  3. The state's own name as text on the row, so a screenshot of it is unambiguous
 *     without the legend.
 *
 * `row_status` is asserted alongside `state` because they are different columns and an
 * operator reconciling against the database needs the second one.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RefundListItem, RefundsPage } from "@/lib/api/types";
import { toneForState } from "@/components/ui";

import { RefundsTab } from "./RefundsTab";

const mockApi = vi.hoisted(() => ({ refunds: vi.fn() }));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...actual, api: mockApi };
});

/** The three states this test exists for, and the fact each one asserts about money. */
const CRITICAL = ["REFUND_PENDING", "REFUND_UNKNOWN", "REFUND_FAILED"] as const;

function refund(state: string, rowStatus: string, index: number): RefundListItem {
  return {
    refund_id: `01a06fb0-0000-7000-8000-00000000000${index}`,
    order_id: `01a06fb1-0000-7000-8000-00000000000${index}`,
    checkout_id: `01a06fb2-0000-7000-8000-00000000000${index}`,
    payment_attempt_id: `01a06fb3-0000-7000-8000-00000000000${index}`,
    amount_minor: 57995,
    currency: "INR",
    amount: { minor: 57995, currency: "INR", display: "579.95" },
    captured_minor: 68195,
    state,
    row_status: rowStatus,
    reason: "buyer_requested",
    automatic: false,
    provider_refund_id: state === "REFUND_PENDING" ? "rfnd_TEST0000000001" : null,
    created_at: "2026-09-05T03:30:00.000000Z",
    updated_at: "2026-09-05T03:35:00.000000Z",
    age_seconds: 300,
  };
}

const page: RefundsPage = {
  refunds: [
    refund("REFUND_PENDING", "PENDING", 1),
    refund("REFUND_UNKNOWN", "UNKNOWN", 2),
    refund("REFUND_FAILED", "FAILED", 3),
  ],
  next_cursor: null,
  limit: 25,
  scope: "tenant",
  counts: {
    REFUND_PENDING: 1,
    REFUND_UNKNOWN: 1,
    REFUND_FAILED: 1,
    RECONCILING: 0,
    ESCALATED: 0,
    PARTIALLY_REFUNDED: 0,
    REFUNDED: 0,
  },
};

/** The table row carrying a given refund state. */
function rowFor(state: string): HTMLElement {
  const chips = screen.getAllByText(state);
  const row = chips.map((chip) => chip.closest("tr")).find((node): node is HTMLTableRowElement => node !== null);
  if (!row) throw new Error(`no table row renders ${state}`);
  return row;
}

beforeEach(() => {
  mockApi.refunds.mockResolvedValue(page);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("toneForState keeps the three refund outcomes apart", () => {
  it("gives each of the three a different tone", () => {
    const tones = CRITICAL.map((state) => toneForState(state));
    expect(tones).toEqual(["info", "warn", "danger"]);
    expect(new Set(tones).size).toBe(3);
  });

  // A state this console has never heard of must fall through to a neutral tone rather
  // than borrow the colour of whichever case sits above it in the switch.
  it("does not colour an unrecognised state like a known one", () => {
    expect(toneForState("REFUND_SOMETHING_NEW")).toBe("muted");
  });
});

describe("the refund list renders the three states as three different things", () => {
  it("gives each row a different colour class", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");

    const classes = CRITICAL.map((state) => {
      const chip = within(rowFor(state)).getAllByText(state)[0];
      return chip.className;
    });
    expect(new Set(classes).size).toBe(3);
  });

  it("says on every row what the state means and who owns it", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");

    const sentences = [
      "the provider was asked and has not answered — in flight, do not retry",
      "the answer was lost; reconciliation owns this row, not an operator",
      "the provider said no — this refund did not happen",
    ];
    CRITICAL.forEach((state, index) => {
      expect(within(rowFor(state)).getByText(sentences[index])).toBeTruthy();
    });
    // Three distinct sentences, not one sentence with the state name substituted in.
    expect(new Set(sentences).size).toBe(3);
  });

  it("prints the state's own name, so colour is never the only carrier", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");
    for (const state of CRITICAL) {
      expect(within(rowFor(state)).getAllByText(state).length).toBeGreaterThan(0);
    }
  });

  it("shows row_status beside state, because they are different columns", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");

    expect(within(rowFor("REFUND_PENDING")).getByText("row_status PENDING")).toBeTruthy();
    expect(within(rowFor("REFUND_UNKNOWN")).getByText("row_status UNKNOWN")).toBeTruthy();
    expect(within(rowFor("REFUND_FAILED")).getByText("row_status FAILED")).toBeTruthy();
  });

  it("names a state outside its vocabulary rather than describing it wrongly", async () => {
    mockApi.refunds.mockResolvedValue({
      ...page,
      refunds: [refund("REFUND_SOMETHING_NEW", "SOMETHING_NEW", 4)],
    });
    render(<RefundsTab initialState={null} />);
    const row = await screen
      .findByText("REFUND_SOMETHING_NEW")
      .then((node) => node.closest("tr") as HTMLElement);
    expect(within(row).getByText("state not in this console's vocabulary")).toBeTruthy();
  });
});

describe("the triage tiles above the list", () => {
  it("carries a separate tile, meaning and count for each of the three", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");

    const meanings: Record<string, string> = {
      REFUND_PENDING: "the provider was asked and has not answered — in flight, do not retry",
      REFUND_UNKNOWN: "the answer was lost; reconciliation owns this row, not an operator",
      REFUND_FAILED: "the provider said no — this refund did not happen",
    };
    for (const state of CRITICAL) {
      // The tile is the div holding the state's chip, its sentence and its count -- not
      // the filter button of the same name, and not a row of the table.
      const tile = screen
        .getAllByText(state)
        .map((node) => node.parentElement)
        .find(
          (node): node is HTMLElement =>
            node !== null &&
            node.tagName === "DIV" &&
            node.closest("tr") === null &&
            node.textContent!.includes(meanings[state]),
        );
      expect(tile).toBeTruthy();
      expect(within(tile as HTMLElement).getByText("1")).toBeTruthy();
    }
  });

  it("renders a state the API counted at zero as zero, never as absent", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");
    const escalated = screen.getByRole("button", { name: /ESCALATED/ });
    expect(escalated.textContent?.replace(/\s+/g, "")).toBe("ESCALATED0");
  });
});

describe("filtering by state asks the API rather than the page", () => {
  it("re-reads with the chosen state instead of narrowing the rows in the browser", async () => {
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");
    expect(mockApi.refunds).toHaveBeenCalledWith(
      expect.objectContaining({ state: undefined, limit: 25 }),
    );

    fireEvent.click(screen.getByRole("button", { name: /REFUND_UNKNOWN/ }));
    await waitFor(() =>
      expect(mockApi.refunds).toHaveBeenCalledWith(
        expect.objectContaining({ state: "REFUND_UNKNOWN" }),
      ),
    );
  });

  it("opens on the state it was linked with, so an overview tile lands filtered", async () => {
    render(<RefundsTab initialState="REFUND_FAILED" />);
    await waitFor(() =>
      expect(mockApi.refunds).toHaveBeenCalledWith(
        expect.objectContaining({ state: "REFUND_FAILED" }),
      ),
    );
  });
});

/**
 * Who asked for the refund, which is a second fact about money on the same screen.
 *
 * Untested at either level until now, because every refund this tenant holds is a buyer
 * request. That is about to change: the automatic stale-capture path admits a refund under
 * the SYSTEM actor when a capture lands against an invalidated checkout, so `automatic:
 * true` rows will start appearing in a list where that branch has never been rendered.
 *
 * An operator who reads "the platform already refunded this" as "the buyer asked for this"
 * chases a customer who is not waiting; one who reads it the other way leaves a buyer
 * waiting for money that has already gone.
 *
 * Only the two branches are covered, and the omission is deliberate. `Flag` also renders
 * an absent value as `unknown`, and asserting that here would be asserting unreachable
 * behaviour: the API declares `automatic` as a required non-nullable boolean and
 * `RefundListItemSchema` matches it, so a null never reaches this column -- the read fails
 * validation first and the page renders a problem document instead. `Flag`'s third branch
 * is right to exist for the columns where the API does send a nullable boolean; it is not
 * this one.
 */
describe("the refund list says who asked for the refund", () => {
  async function rowWith(automatic: boolean) {
    mockApi.refunds.mockResolvedValue({
      ...page,
      refunds: [{ ...refund("REFUND_PENDING", "PENDING", 1), automatic }],
    });
    render(<RefundsTab initialState={null} />);
    await screen.findByText("rfnd_TEST0000000001");
    return within(rowFor("REFUND_PENDING"));
  }

  it("names a platform-initiated refund as automatic", async () => {
    const row = await rowWith(true);
    expect(row.getByText("automatic")).toBeTruthy();
    expect(row.queryByText("buyer-requested")).toBeNull();
  });

  it("names a buyer-initiated refund as buyer-requested", async () => {
    const row = await rowWith(false);
    expect(row.getByText("buyer-requested")).toBeTruthy();
    expect(row.queryByText("automatic")).toBeNull();
  });

  // Two different words, so the attribution survives a greyscale screenshot. The tones
  // differ too, but a tone is not a claim anybody can read aloud.
  it("distinguishes the two by their words, not only by colour", async () => {
    const automatic = (await rowWith(true)).getByText("automatic").textContent;
    cleanup();
    const requested = (await rowWith(false)).getByText("buyer-requested").textContent;
    expect(automatic).not.toBe(requested);
  });
});

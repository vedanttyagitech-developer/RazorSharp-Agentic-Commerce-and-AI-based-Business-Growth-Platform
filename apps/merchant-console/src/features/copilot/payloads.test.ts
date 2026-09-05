/**
 * The boundary between the copilot and this console, tested where it is load-bearing.
 *
 * Two properties are worth a test each and neither is obvious from reading the schemas.
 *
 * The first is that a payload is never silently dropped. A kind this console does not draw
 * and a kind it does draw arriving in the wrong shape are different failures, they are both
 * invisible to a reader of the reply above them, and the only thing standing between a
 * schema drift and a panel that quietly renders nothing is that `readPayload` reports both.
 *
 * The second is the apply guard. It is the one place in this feature that decides whether a
 * merchant is offered a button that changes their shop, and each of its three refusals is a
 * different way for a proposal to be unfit to press: a route this console does not
 * implement, a change the scenario controller would reject, and -- the one nobody could see
 * for themselves -- a proposal resting on no tool at all, which specification 6.6 forbids.
 */
import { describe, expect, it } from "vitest";

import { APPLY_ENDPOINT, applyBlockedReason, readPayload, type Proposal } from "./payloads";

const health = {
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
};

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

describe("readPayload", () => {
  it("draws the three metric kinds the growth specialist returns", () => {
    const reading = readPayload(health);
    expect(reading.card?.kind).toBe("catalogue_health");
    expect(reading.malformed).toBeNull();
    expect(reading.undrawn).toBeNull();
  });

  it("keeps a count of zero apart from a count the platform could not derive", () => {
    const measured = readPayload({ ...health, out_of_stock: 0 });
    const unmeasured = readPayload({ ...health, out_of_stock: null });
    expect(measured.card?.kind).toBe("catalogue_health");
    expect(unmeasured.card?.kind).toBe("catalogue_health");
    if (measured.card?.kind !== "catalogue_health" || unmeasured.card?.kind !== "catalogue_health") {
      throw new Error("both payloads must parse as catalogue health");
    }
    expect(measured.card.value.out_of_stock).toBe(0);
    expect(unmeasured.card.value.out_of_stock).toBeNull();
  });

  it("names a kind it cannot draw instead of dropping it", () => {
    expect(readPayload({ kind: "fulfilment_slots", items: [] }).undrawn).toBe("fulfilment_slots");
  });

  it("reports a kind it draws that arrived in the wrong shape", () => {
    const reading = readPayload({ ...health, synthetic: "yes" });
    expect(reading.card).toBeNull();
    expect(reading.malformed?.kind).toBe("catalogue_health");
    expect(reading.malformed?.detail).toContain("synthetic");
  });

  it("finds a proposal whether it is the payload or sits beside a card", () => {
    expect(readPayload(proposal()).proposal?.proposal_id).toBe("prp_01a06fb0");
    expect(readPayload({ ...health, proposal: proposal() }).proposal?.proposal_id).toBe(
      "prp_01a06fb0",
    );
    expect(readPayload({ ...health, proposal: proposal() }).card?.kind).toBe("catalogue_health");
  });

  it("treats a proposal missing its evidence as unreadable rather than half-drawing it", () => {
    const withoutEvidence: Record<string, unknown> = { ...proposal() };
    delete withoutEvidence.evidence;
    const reading = readPayload(withoutEvidence);
    expect(reading.proposal).toBeNull();
    expect(reading.malformed?.kind).toBe("proposal");
  });

  it("reads nothing from a turn that carried no payload", () => {
    expect(readPayload(null)).toEqual({
      card: null,
      proposal: null,
      undrawn: null,
      malformed: null,
    });
  });
});

describe("applyBlockedReason", () => {
  it("permits a proposal that names the scenario endpoint, a known kind and its evidence", () => {
    expect(applyBlockedReason(proposal())).toBeNull();
  });

  it("refuses a proposal naming any other endpoint", () => {
    const named = proposal({
      change: { ...proposal().change, endpoint: "POST /v1/catalogue/products/AMUL-DAIRY-004" },
    });
    expect(applyBlockedReason(named)).toContain("/v1/catalogue/products");
  });

  it("refuses a change the scenario controller does not accept", () => {
    const named = proposal({
      change: { ...proposal().change, body: { kind: "REWRITE_LEDGER", sku: "AMUL-DAIRY-004" } },
    });
    expect(applyBlockedReason(named)).toContain("REWRITE_LEDGER");
  });

  it("refuses a proposal that names no tool it rests on", () => {
    const named = proposal({ evidence: { ...proposal().evidence, read_by: [] } });
    expect(applyBlockedReason(named)).toContain("6.6");
  });
});

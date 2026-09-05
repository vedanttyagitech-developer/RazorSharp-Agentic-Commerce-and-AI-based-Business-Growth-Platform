/**
 * The tests that matter here are the ones about widening.
 *
 * Everything else on the boundary screen is presentation; the property the studio is
 * built to have is that no path through this code produces a capability the server did
 * not declare. So the suite pushes on that from both directions -- a composition asking
 * for more than its role holds, and a stored draft naming a capability the roster has
 * dropped -- and asserts the answer is a narrowing and a report, never a grant.
 */
import { describe, expect, it } from "vitest";

import type { AgentCapabilities } from "@/lib/api/types";

import { deriveBoundary, reconcileCalls } from "./boundary";
import { narrow, reconcile, startingComposition, type Composition } from "./composition";

/** The live shape of `GET /v1/agent/capabilities` for an operator session. */
const CAPS: AgentCapabilities = {
  copilot: "merchant",
  actor_type: "OPERATOR",
  session_capabilities: [
    "catalogue.read",
    "merchant.catalogue_health.read",
    "merchant.checkout_metrics.read",
    "merchant.growth_proposal.create",
    "merchant.inventory_anomalies.read",
    "order.read",
    "policy.search",
    "resolution.evaluate",
    "support.case.read",
    "support.escalate",
  ],
  agent_capabilities: [
    "catalogue.read",
    "merchant.catalogue_health.read",
    "merchant.checkout_metrics.read",
    "merchant.growth_proposal.create",
    "merchant.inventory_anomalies.read",
    "order.read",
    "policy.search",
    "resolution.evaluate",
    "support.case.read",
    "support.escalate",
  ],
  specialists: [
    {
      specialist: "growth",
      principal_id: "session:0199/merchant_copilot/growth",
      capabilities: [
        "merchant.catalogue_health.read",
        "merchant.checkout_metrics.read",
        "merchant.growth_proposal.create",
        "merchant.inventory_anomalies.read",
      ],
      tools: [
        "merchant.catalogue_health.read",
        "merchant.checkout_metrics.read",
        "merchant.inventory_anomalies.read",
      ],
    },
    {
      specialist: "case",
      principal_id: "session:0199/merchant_copilot/case",
      capabilities: ["support.case.read"],
      tools: ["support.case.read"],
    },
  ],
  absent_by_construction: ["authority.revoke", "checkout.approve", "checkout.reject"],
};

const GROWTH = CAPS.specialists[0];

describe("a composition can only narrow", () => {
  it("drops a capability the role does not declare, however it was asked for", () => {
    const kept = narrow(GROWTH, [
      "merchant.catalogue_health.read",
      "checkout.approve",
      "refund.request",
      "support.case.read",
    ]);
    expect(kept).toEqual(["merchant.catalogue_health.read"]);
  });

  it("orders by the server's list rather than by the order things were asked for", () => {
    const kept = narrow(GROWTH, [
      "merchant.inventory_anomalies.read",
      "merchant.catalogue_health.read",
    ]);
    expect(kept).toEqual([
      "merchant.catalogue_health.read",
      "merchant.inventory_anomalies.read",
    ]);
  });

  it("starts a new composition from the role's whole declared set", () => {
    expect(startingComposition(GROWTH).capabilities).toEqual(GROWTH.capabilities);
  });
});

describe("a stored draft is reconciled against the roster, and says what it lost", () => {
  it("removes a capability the roster no longer declares and names it", () => {
    const stored: Composition = {
      role: "growth",
      capabilities: ["merchant.catalogue_health.read", "merchant.pricing.write"],
      name: "Stock watcher",
      briefing: "",
    };
    const restored = reconcile(CAPS, stored);
    expect(restored?.composition.capabilities).toEqual(["merchant.catalogue_health.read"]);
    expect(restored?.dropped).toEqual(["merchant.pricing.write"]);
  });

  it("starts over when the stored role is gone, and reports which role that was", () => {
    const stored: Composition = {
      role: "shopping",
      capabilities: ["catalogue.read"],
      name: "",
      briefing: "",
    };
    const restored = reconcile(CAPS, stored);
    expect(restored?.missingRole).toBe("shopping");
    expect(restored?.composition.role).toBe("growth");
  });
});

describe("the boundary is derived from the document and the composition", () => {
  it("splits the role's set into what was kept and what was switched off", () => {
    const boundary = deriveBoundary(CAPS, {
      role: "growth",
      capabilities: ["merchant.catalogue_health.read"],
      name: "",
      briefing: "",
    });
    expect(boundary?.granted.map((row) => row.capability)).toEqual([
      "merchant.catalogue_health.read",
    ]);
    expect(boundary?.disabled.map((row) => row.capability)).toEqual([
      "merchant.checkout_metrics.read",
      "merchant.growth_proposal.create",
      "merchant.inventory_anomalies.read",
    ]);
  });

  it("carries the server's absent_by_construction through untouched", () => {
    const boundary = deriveBoundary(CAPS, startingComposition(GROWTH));
    expect(boundary?.neverOnAnyAgent).toEqual(CAPS.absent_by_construction);
    // The screen must never present that list as the whole boundary.
    expect(boundary?.exhaustive).toBe(false);
  });

  it("names what the other specialist holds, and does not attribute it to this one", () => {
    const boundary = deriveBoundary(CAPS, startingComposition(GROWTH));
    expect(boundary?.elsewhereOnTheSurface).toEqual([
      {
        role: "case",
        capabilities: [
          expect.objectContaining({ capability: "support.case.read" }) as unknown,
        ],
      },
    ]);
  });

  it("marks a capability with no tool of the same name, because a proposal is not a call", () => {
    const boundary = deriveBoundary(CAPS, startingComposition(GROWTH));
    const proposal = boundary?.granted.find(
      (row) => row.capability === "merchant.growth_proposal.create",
    );
    expect(proposal?.toolNamed).toBe(false);
    expect(proposal?.kind).toBe("propose");
    const health = boundary?.granted.find(
      (row) => row.capability === "merchant.catalogue_health.read",
    );
    expect(health?.toolNamed).toBe(true);
  });

  it("draws a capability it has no description for as undescribed rather than as a read", () => {
    const widened: AgentCapabilities = {
      ...CAPS,
      specialists: [
        { ...GROWTH, capabilities: [...GROWTH.capabilities, "merchant.mystery.read"] },
        CAPS.specialists[1],
      ],
    };
    const role = widened.specialists[0];
    const boundary = deriveBoundary(widened, startingComposition(role));
    const mystery = boundary?.granted.find((row) => row.capability === "merchant.mystery.read");
    expect(mystery?.known).toBe(false);
    expect(mystery?.kind).toBe("unclassified");
    expect(boundary?.counts.unclassified).toBe(1);
  });

  it("reports the narrowing the harness performed, and empty when it performed none", () => {
    expect(deriveBoundary(CAPS, startingComposition(GROWTH))?.withheldFromEveryAgent).toEqual([]);

    const buyerish: AgentCapabilities = {
      ...CAPS,
      session_capabilities: [...CAPS.session_capabilities, "checkout.approve"],
    };
    expect(deriveBoundary(buyerish, startingComposition(GROWTH))?.withheldFromEveryAgent).toEqual([
      "checkout.approve",
    ]);
  });

  it("answers null for a role the roster does not have", () => {
    expect(
      deriveBoundary(CAPS, { role: "shopping", capabilities: [], name: "", briefing: "" }),
    ).toBeNull();
  });
});

describe("a turn's calls are measured against the draft, never called denials", () => {
  it("separates the calls inside the grant from the ones outside it", () => {
    const composition: Composition = {
      role: "growth",
      capabilities: ["merchant.catalogue_health.read"],
      name: "",
      briefing: "",
    };
    const { inside, outside } = reconcileCalls(composition, [
      { name: "merchant.catalogue_health.read" },
      { name: "merchant.checkout_metrics.read" },
    ]);
    expect(inside).toEqual(["merchant.catalogue_health.read"]);
    expect(outside).toEqual(["merchant.checkout_metrics.read"]);
  });

  it("counts a name that is not a capability against neither side", () => {
    const composition = startingComposition(GROWTH);
    const { inside, outside } = reconcileCalls(composition, [{ name: "present_metrics" }]);
    expect(inside).toEqual([]);
    expect(outside).toEqual([]);
  });
});

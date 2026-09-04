import {
  DEMO_AUDIT_VERIFICATION,
  DEMO_INSPECTOR,
  DEMO_ORDERS,
  DEMO_OUTBOX,
  DEMO_PROOF_CHAIN,
  DEMO_REFUNDS,
  DEMO_RETAINED_REVENUE_HEADLINE,
  DEMO_REVIEW_QUEUE,
  DEMO_SAFE_MODE,
} from "./fixtures";
import { CATALOGUE_PRODUCTS } from "./products-data";
import type {
  AuditStreamVerificationOut,
  CatalogueProduct,
  InspectorAttemptOut,
  OrderOut,
  OutboxOut,
  ProofChainOut,
  RefundItem,
  RetainedRevenueOut,
  ReviveOut,
  ReviewQueueCase,
  SafeModeOut,
  ScenarioInjectionOut,
} from "./types";

/**
 * Console Typed API Client.
 *
 * Calls the same-origin server proxy (/api/backend/...) which securely forwards to
 * http://localhost:8000 with X-Scenario-Key injected.
 *
 * If the live API is unreachable, seamlessly returns deterministic demo fixtures
 * with `is_live: false`, upholding honest labeling (Rule 2).
 */
export class MerchantConsoleClient {
  private base = "/api/backend";

  async getRetainedRevenue(
    merchantId = "demo-grocery",
    checkoutId = "chk_hero_stale_refusal_01"
  ): Promise<RetainedRevenueOut> {
    try {
      const res = await fetch(
        `${this.base}/v1/merchants/${encodeURIComponent(merchantId)}/evidence/retained-revenue?checkout_id=${encodeURIComponent(checkoutId)}`,
        { method: "GET", cache: "no-store" }
      );
      if (res.ok) {
        const data = (await res.json()) as RetainedRevenueOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return { ...DEMO_RETAINED_REVENUE_HEADLINE, checkout_id: checkoutId, is_live: false };
  }

  async getProofChain(
    checkoutId = "chk_hero_stale_refusal_01",
    attemptId?: string
  ): Promise<ProofChainOut> {
    try {
      const url = attemptId
        ? `${this.base}/v1/checkouts/${encodeURIComponent(checkoutId)}/proof?payment_attempt_id=${encodeURIComponent(attemptId)}`
        : `${this.base}/v1/checkouts/${encodeURIComponent(checkoutId)}/proof`;
      const res = await fetch(url, { method: "GET", cache: "no-store" });
      if (res.ok) {
        const data = (await res.json()) as ProofChainOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return { ...DEMO_PROOF_CHAIN, checkout_id: checkoutId, is_live: false };
  }

  async verifyAuditStream(
    aggregateType = "checkout",
    aggregateId = "chk_hero_stale_refusal_01"
  ): Promise<AuditStreamVerificationOut> {
    try {
      const res = await fetch(
        `${this.base}/v1/audit/streams/${encodeURIComponent(aggregateType)}/${encodeURIComponent(aggregateId)}/verify`,
        { method: "GET", cache: "no-store" }
      );
      if (res.ok) {
        const data = (await res.json()) as AuditStreamVerificationOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return {
      ...DEMO_AUDIT_VERIFICATION,
      aggregate_type: aggregateType,
      aggregate_id: aggregateId,
      is_live: false,
    };
  }

  async getOutbox(status?: string, limit = 50): Promise<OutboxOut> {
    try {
      const query = status ? `?status=${encodeURIComponent(status)}&limit=${limit}` : `?limit=${limit}`;
      const res = await fetch(`${this.base}/v1/ops/outbox${query}`, {
        method: "GET",
        cache: "no-store",
      });
      if (res.ok) {
        const data = (await res.json()) as OutboxOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return { ...DEMO_OUTBOX, is_live: false };
  }

  async reviveOutboxCommand(commandId: string): Promise<ReviveOut> {
    try {
      const res = await fetch(`${this.base}/v1/ops/outbox/${encodeURIComponent(commandId)}/revive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
      });
      if (res.ok) {
        return (await res.json()) as ReviveOut;
      }
    } catch {
      // Graceful fallback
    }
    return {
      command_id: commandId,
      code: "REVIVED_IN_SIMULATION",
      status: "PENDING",
      retry_at: new Date().toISOString(),
    };
  }

  async getSafeMode(): Promise<SafeModeOut> {
    try {
      const res = await fetch(`${this.base}/v1/ops/safe-mode`, {
        method: "GET",
        cache: "no-store",
      });
      if (res.ok) {
        const data = (await res.json()) as SafeModeOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return { ...DEMO_SAFE_MODE, is_live: false };
  }

  async setSafeMode(enabled: boolean, reason?: string): Promise<SafeModeOut> {
    try {
      const res = await fetch(`${this.base}/v1/ops/safe-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, reason }),
        cache: "no-store",
      });
      if (res.ok) {
        const data = (await res.json()) as SafeModeOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return {
      ...DEMO_SAFE_MODE,
      safe_mode: enabled,
      mode: enabled ? "SAFE_MODE" : "NORMAL",
      reason_code: reason || (enabled ? "OPERATOR_KILL_SWITCH_ENGAGED" : "OPERATOR_STAND_DOWN"),
      is_live: false,
    };
  }

  async getOrders(): Promise<OrderOut[]> {
    // In P0, individual orders are read via /v1/orders/{id}. If live batch endpoint arrives, we call it.
    return DEMO_ORDERS;
  }

  async getRefunds(): Promise<RefundItem[]> {
    return DEMO_REFUNDS;
  }

  async getReviewQueue(): Promise<ReviewQueueCase[]> {
    return DEMO_REVIEW_QUEUE;
  }

  async getInspectorAttempt(attemptId = "att_hero_02"): Promise<InspectorAttemptOut> {
    try {
      const res = await fetch(
        `${this.base}/v1/inspector/payment-attempts/${encodeURIComponent(attemptId)}`,
        { method: "GET", cache: "no-store" }
      );
      if (res.ok) {
        const data = (await res.json()) as InspectorAttemptOut;
        return { ...data, is_live: true };
      }
    } catch {
      // Graceful fallback
    }
    return { ...DEMO_INSPECTOR, payment_attempt_id: attemptId, is_live: false };
  }

  async injectScenario(
    kind: string,
    sku?: string,
    value?: boolean | number,
    note = "Console operator trigger"
  ): Promise<ScenarioInjectionOut | null> {
    try {
      const res = await fetch(`${this.base}/v1/scenario/injections`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ kind, sku, value, note }),
        cache: "no-store",
      });
      if (res.ok) {
        return (await res.json()) as ScenarioInjectionOut;
      }
    } catch {
      // Graceful fallback
    }
    return {
      injection_id: `inj_sim_${Date.now()}`,
      kind,
      sku,
      label: "SCENARIO_INJECTION (Simulated)",
      new_catalogue_revision: 14,
    };
  }

  getCatalogueProducts(): CatalogueProduct[] {
    return CATALOGUE_PRODUCTS;
  }
}

export const consoleClient = new MerchantConsoleClient();

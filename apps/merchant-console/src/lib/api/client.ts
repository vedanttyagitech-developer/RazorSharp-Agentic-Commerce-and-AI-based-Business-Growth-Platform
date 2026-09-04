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
  OrdersOut,
  OutboxOut,
  ProofChainOut,
  RefundItem,
  RefundsOut,
  RetainedRevenueOut,
  ReviveOut,
  ReviewQueueOut,
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
const isMockMode = typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_MODE === "mock";

export class MerchantConsoleClient {
  private base = "/api/backend";

  async getRetainedRevenue(
    merchantId = "demo-grocery",
    checkoutId = "chk_hero_stale_refusal_01"
  ): Promise<RetainedRevenueOut> {
    if (isMockMode) {
      return { ...DEMO_RETAINED_REVENUE_HEADLINE, checkout_id: checkoutId, is_live: false };
    }
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
    if (isMockMode) {
      return { ...DEMO_PROOF_CHAIN, checkout_id: checkoutId, is_live: false };
    }
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
    if (isMockMode) {
      return {
        ...DEMO_AUDIT_VERIFICATION,
        aggregate_type: aggregateType,
        aggregate_id: aggregateId,
        is_live: false,
      };
    }
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
    if (isMockMode) {
      return { ...DEMO_OUTBOX, is_live: false };
    }
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
    if (isMockMode) {
      return { ...DEMO_SAFE_MODE, is_live: false };
    }
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

  async getOrders(status?: string, limit = 50, cursor?: string): Promise<OrdersOut> {
    if (isMockMode) {
      return { orders: DEMO_ORDERS, is_live: false };
    }
    try {
      const params = new URLSearchParams();
      if (status && status !== "ALL") params.set("status", status);
      if (limit) params.set("limit", String(limit));
      if (cursor) params.set("cursor", cursor);
      const query = params.toString() ? `?${params.toString()}` : "";
      const res = await fetch(`${this.base}/v1/orders${query}`, {
        method: "GET",
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        const ordersList: OrderOut[] = Array.isArray(data)
          ? data
          : Array.isArray(data.orders)
          ? data.orders
          : Array.isArray(data.items)
          ? data.items
          : [];
        return {
          orders: ordersList,
          cursor: data.cursor ?? null,
          is_live: true,
        };
      }
    } catch {
      // Graceful fallback
    }
    return {
      orders: DEMO_ORDERS,
      is_live: false,
    };
  }

  async getRefunds(state?: string, limit = 50, cursor?: string): Promise<RefundsOut> {
    if (isMockMode) {
      return { refunds: DEMO_REFUNDS, is_live: false };
    }
    try {
      const params = new URLSearchParams();
      if (state && state !== "ALL") params.set("state", state);
      if (limit) params.set("limit", String(limit));
      if (cursor) params.set("cursor", cursor);
      const query = params.toString() ? `?${params.toString()}` : "";
      const res = await fetch(`${this.base}/v1/refunds${query}`, {
        method: "GET",
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        const refundsList: RefundItem[] = Array.isArray(data)
          ? data
          : Array.isArray(data.refunds)
          ? data.refunds
          : Array.isArray(data.items)
          ? data.items
          : [];
        return {
          refunds: refundsList,
          cursor: data.cursor ?? null,
          is_live: true,
        };
      }
    } catch {
      // Graceful fallback
    }
    return {
      refunds: DEMO_REFUNDS,
      is_live: false,
    };
  }

  async getReviewQueue(): Promise<ReviewQueueOut> {
    // Review queue stays simulated: human-review cases are created by Reconciliation and Resolution services (not yet built)
    return {
      cases: DEMO_REVIEW_QUEUE,
      is_live: false,
    };
  }

  async getInspectorAttempt(attemptId = "att_hero_02"): Promise<InspectorAttemptOut> {
    if (isMockMode) {
      return { ...DEMO_INSPECTOR, payment_attempt_id: attemptId, is_live: false };
    }
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
        signal: AbortSignal.timeout(1200),
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

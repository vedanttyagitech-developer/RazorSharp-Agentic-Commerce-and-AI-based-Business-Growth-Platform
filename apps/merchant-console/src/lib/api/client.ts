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

  async getOrder(orderId: string): Promise<OrderOut | null> {
    if (isMockMode) {
      const found = DEMO_ORDERS.find((o) => o.order_id === orderId);
      return found || null;
    }
    try {
      const res = await fetch(`${this.base}/v1/orders/${encodeURIComponent(orderId)}`, {
        method: "GET",
        cache: "no-store",
        signal: AbortSignal.timeout(1200),
      });
      if (res.ok) {
        const row = (await res.json()) as Record<string, unknown>;
        const paymentObj = typeof row.payment === "object" && row.payment !== null ? (row.payment as Record<string, unknown>) : null;
        const captureEvidence = paymentObj && typeof paymentObj.capture_evidence === "object" && paymentObj.capture_evidence !== null
          ? (paymentObj.capture_evidence as Record<string, unknown>)
          : typeof row.capture_evidence === "object" && row.capture_evidence !== null
          ? (row.capture_evidence as Record<string, unknown>)
          : null;
        const amountObj = typeof row.amount === "object" && row.amount !== null ? (row.amount as Record<string, unknown>) : null;
        const rawRefunds = Array.isArray(row.refunds) ? row.refunds : [];
        const refundsList = rawRefunds.map((rf: Record<string, unknown>) => ({
          refund_id: String(rf.refund_id ?? ""),
          status: String(rf.state ?? rf.status ?? "REFUNDED"),
          amount_minor: Number(rf.amount_minor ?? 0),
          reason: String(rf.reason ?? rf.reason_code ?? "Customer requested"),
        }));
        const rawState = String(row.state ?? row.status ?? "CONFIRMED");
        const totalMinor = Number(row.amount_minor ?? row.total_minor ?? amountObj?.minor ?? 0);
        const ver = Number(row.version ?? row.checkout_version ?? 1);

        return {
          order_id: String(row.order_id ?? orderId),
          checkout_id: String(row.checkout_id ?? ""),
          status: rawState,
          state: rawState,
          total_minor: totalMinor,
          amount_minor: totalMinor,
          currency: String(row.currency ?? "INR"),
          checkout_version: ver,
          version: ver,
          capture_evidence_source: String(captureEvidence?.kind ?? row.capture_evidence_source ?? "WEBHOOK"),
          capture_evidence: captureEvidence
            ? {
                kind: String(captureEvidence.kind ?? "WEBHOOK"),
                reference: captureEvidence.reference ? String(captureEvidence.reference) : undefined,
                verified_at: captureEvidence.verified_at ? String(captureEvidence.verified_at) : undefined,
              }
            : null,
          payment_attempt_id: String(paymentObj?.payment_attempt_id ?? row.payment_attempt_id ?? ""),
          policy_receipt_hash: String(row.policy_receipt_hash ?? ""),
          content_hash: String(row.content_hash ?? ""),
          razorpay_order_id: (paymentObj?.provider_order_id ?? row.razorpay_order_id ?? null) as string | null,
          razorpay_payment_id: (paymentObj?.provider_payment_id ?? row.razorpay_payment_id ?? null) as string | null,
          refunded_minor: Number(row.refunded_minor ?? 0),
          refund_count: Number(row.refund_count ?? refundsList.length),
          age_seconds: Number(row.age_seconds ?? 0),
          created_at: String(row.created_at ?? new Date().toISOString()),
          refunds: refundsList,
        };
      }
    } catch {
      // Fallback to mock item if present
    }
    const found = DEMO_ORDERS.find((o) => o.order_id === orderId);
    return found || null;
  }

  async getOrders(status?: string, limit = 50, cursor?: string): Promise<OrdersOut> {
    if (isMockMode) {
      const filtered = !status || status === "ALL" ? DEMO_ORDERS : DEMO_ORDERS.filter((o) => o.status === status);
      return {
        orders: filtered,
        counts: {
          CONFIRMED: DEMO_ORDERS.filter((o) => o.status === "CAPTURED" || o.status === "CONFIRMED").length,
          FULFILMENT_BLOCKED: 0,
          CANCELLED: 0,
          PARTIALLY_REFUNDED: DEMO_ORDERS.filter((o) => o.status === "PARTIALLY_REFUNDED").length,
          REFUNDED: 0,
        },
        scope: "tenant",
        is_live: false,
      };
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
        signal: AbortSignal.timeout(1200),
      });
      if (res.ok) {
        const data = await res.json();
        const rawList: Record<string, unknown>[] = Array.isArray(data)
          ? data
          : Array.isArray(data.orders)
          ? data.orders
          : Array.isArray(data.items)
          ? data.items
          : [];
        const ordersList: OrderOut[] = rawList.map((row) => {
          const amountObj = typeof row.amount === "object" && row.amount !== null ? (row.amount as Record<string, unknown>) : null;
          const captureEvidence = typeof row.capture_evidence === "object" && row.capture_evidence !== null ? (row.capture_evidence as Record<string, unknown>) : null;
          const rawState = String(row.state ?? row.status ?? "CONFIRMED");
          const totalMinor = Number(row.amount_minor ?? row.total_minor ?? amountObj?.minor ?? 0);
          const ver = Number(row.version ?? row.checkout_version ?? 1);
          return {
            order_id: String(row.order_id ?? ""),
            checkout_id: String(row.checkout_id ?? ""),
            status: rawState,
            state: rawState,
            total_minor: totalMinor,
            amount_minor: totalMinor,
            currency: String(row.currency ?? "INR"),
            checkout_version: ver,
            version: ver,
            capture_evidence_source: String(captureEvidence?.kind ?? row.capture_evidence_source ?? "WEBHOOK"),
            capture_evidence: captureEvidence
              ? {
                  kind: String(captureEvidence.kind ?? "WEBHOOK"),
                  reference: captureEvidence.reference ? String(captureEvidence.reference) : undefined,
                  verified_at: captureEvidence.verified_at ? String(captureEvidence.verified_at) : undefined,
                }
              : null,
            payment_attempt_id: String(row.payment_attempt_id ?? ""),
            policy_receipt_hash: String(row.policy_receipt_hash ?? ""),
            razorpay_order_id: (row.razorpay_order_id ?? null) as string | null,
            razorpay_payment_id: (row.razorpay_payment_id ?? null) as string | null,
            refunded_minor: Number(row.refunded_minor ?? 0),
            refund_count: Number(row.refund_count ?? 0),
            age_seconds: Number(row.age_seconds ?? 0),
            created_at: String(row.created_at ?? new Date().toISOString()),
            refunds: Array.isArray(row.refunds) ? (row.refunds as OrderOut["refunds"]) : [],
          };
        });
        return {
          orders: ordersList,
          cursor: (data.next_cursor ?? data.cursor ?? null) as string | null,
          next_cursor: (data.next_cursor ?? data.cursor ?? null) as string | null,
          scope: (data.scope ?? "tenant") as "own" | "tenant",
          counts: (typeof data.counts === "object" && data.counts !== null ? data.counts : {}) as Record<string, number>,
          is_live: true,
        };
      }
    } catch {
      // Graceful fallback
    }
    const filtered = !status || status === "ALL" ? DEMO_ORDERS : DEMO_ORDERS.filter((o) => o.status === status);
    return {
      orders: filtered,
      counts: {
        CONFIRMED: DEMO_ORDERS.filter((o) => o.status === "CAPTURED" || o.status === "CONFIRMED").length,
        FULFILMENT_BLOCKED: 0,
        CANCELLED: 0,
        PARTIALLY_REFUNDED: DEMO_ORDERS.filter((o) => o.status === "PARTIALLY_REFUNDED").length,
        REFUNDED: 0,
      },
      scope: "tenant",
      is_live: false,
    };
  }

  async getRefunds(state?: string, limit = 50, cursor?: string): Promise<RefundsOut> {
    if (isMockMode) {
      const filtered = !state || state === "ALL" ? DEMO_REFUNDS : DEMO_REFUNDS.filter((r) => r.state === state);
      return {
        refunds: filtered,
        counts: {
          REFUND_PENDING: DEMO_REFUNDS.filter((r) => r.state === "REFUND_PENDING").length,
          REFUND_UNKNOWN: DEMO_REFUNDS.filter((r) => r.state === "REFUND_UNKNOWN").length,
          REFUND_FAILED: DEMO_REFUNDS.filter((r) => r.state === "REFUND_FAILED").length,
          RECONCILING: 0,
          ESCALATED: 0,
          PARTIALLY_REFUNDED: 0,
          REFUNDED: DEMO_REFUNDS.filter((r) => r.state === "PROCESSED").length,
        },
        scope: "tenant",
        is_live: false,
      };
    }
    try {
      const params = new URLSearchParams();
      const queryState = state === "PROCESSED" ? "REFUNDED" : state;
      if (queryState && queryState !== "ALL") params.set("state", queryState);
      if (limit) params.set("limit", String(limit));
      if (cursor) params.set("cursor", cursor);
      const query = params.toString() ? `?${params.toString()}` : "";
      const res = await fetch(`${this.base}/v1/refunds${query}`, {
        method: "GET",
        cache: "no-store",
        signal: AbortSignal.timeout(1200),
      });
      if (res.ok) {
        const data = await res.json();
        const rawList: Record<string, unknown>[] = Array.isArray(data)
          ? data
          : Array.isArray(data.refunds)
          ? data.refunds
          : Array.isArray(data.items)
          ? data.items
          : [];
        const refundsList: RefundItem[] = rawList.map((row) => {
          const rawState = String(row.state ?? "REFUND_PENDING");
          const wireState = rawState === "REFUNDED" ? "PROCESSED" : rawState;
          const amountObj = typeof row.amount === "object" && row.amount !== null ? (row.amount as Record<string, unknown>) : null;
          return {
            refund_id: String(row.refund_id ?? ""),
            order_id: row.order_id ? String(row.order_id) : null,
            checkout_id: String(row.checkout_id ?? ""),
            payment_attempt_id: String(row.payment_attempt_id ?? ""),
            amount_minor: Number(row.amount_minor ?? amountObj?.minor ?? 0),
            currency: String(row.currency ?? "INR"),
            state: wireState as RefundItem["state"],
            row_status: String(row.row_status ?? (wireState === "PROCESSED" ? "PROCESSED" : wireState.replace("REFUND_", ""))),
            reason: String(row.reason ?? "buyer_requested"),
            automatic: Boolean(row.automatic),
            reconciliation_attempts: Number(row.reconciliation_attempts ?? (wireState === "REFUND_UNKNOWN" ? 2 : 0)),
            provider_refund_id: row.provider_refund_id ? String(row.provider_refund_id) : null,
            captured_minor: row.captured_minor !== undefined && row.captured_minor !== null ? Number(row.captured_minor) : null,
            created_at: String(row.created_at ?? new Date().toISOString()),
            updated_at: String(row.updated_at ?? row.created_at ?? new Date().toISOString()),
            age_seconds: Number(row.age_seconds ?? 0),
          };
        });
        return {
          refunds: refundsList,
          cursor: (data.next_cursor ?? data.cursor ?? null) as string | null,
          next_cursor: (data.next_cursor ?? data.cursor ?? null) as string | null,
          scope: (data.scope ?? "tenant") as "own" | "tenant",
          counts: (typeof data.counts === "object" && data.counts !== null ? data.counts : {}) as Record<string, number>,
          is_live: true,
        };
      }
    } catch {
      // Graceful fallback
    }
    const filtered = !state || state === "ALL" ? DEMO_REFUNDS : DEMO_REFUNDS.filter((r) => r.state === state);
    return {
      refunds: filtered,
      counts: {
        REFUND_PENDING: DEMO_REFUNDS.filter((r) => r.state === "REFUND_PENDING").length,
        REFUND_UNKNOWN: DEMO_REFUNDS.filter((r) => r.state === "REFUND_UNKNOWN").length,
        REFUND_FAILED: DEMO_REFUNDS.filter((r) => r.state === "REFUND_FAILED").length,
        RECONCILING: 0,
        ESCALATED: 0,
        PARTIALLY_REFUNDED: 0,
        REFUNDED: DEMO_REFUNDS.filter((r) => r.state === "PROCESSED").length,
      },
      scope: "tenant",
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

export interface RetainedRevenueOut {
  checkout_id: string;
  merchant_id: string;
  currency: string;
  stale_version: number | null;
  stale_approved_minor: number | null;
  stale_invalidated_at: string | null;
  corrected_version: number | null;
  corrected_total_minor: number | null;
  captured_minor: number | null;
  captured_from: string | null;
  difference_minor: number | null;
  direction: string;
  refunded_minor: number;
  net_retained_minor: number | null;
  controlled_scenario: boolean;
  explanation: string;
}

export interface InjectionOut {
  injection_id: string;
  kind: string;
  label: string;
  sku?: string;
  new_catalogue_revision?: number;
}

export interface InspectorOut {
  payment_attempt_id: string;
  checkout_id: string;
  checkout_version: number;
  state: string;
  amount_minor: number;
  currency: string;
  provider_order_id: string | null;
  provider_payment_id: string | null;
  grants: unknown[];
  commands: unknown[];
  provider_requests: unknown[];
  webhook_deliveries: unknown[];
  reconciliation_runs: unknown[];
}

export const API_BASE =
  typeof window !== "undefined" && process.env.NEXT_PUBLIC_API_BASE
    ? process.env.NEXT_PUBLIC_API_BASE
    : "http://localhost:8000";

export const SCENARIO_KEY =
  process.env.NEXT_PUBLIC_SCENARIO_KEY || "local-demo-scenario-key";

/**
 * Fetch authoritative Step 11 Retained Revenue from live Commerce API.
 * Returns null if the backend is offline/unreachable, enabling honest fallback labeling.
 */
export async function fetchLiveRetainedRevenue(
  merchantId: string = "demo-grocery"
): Promise<RetainedRevenueOut | null> {
  try {
    const res = await fetch(`${API_BASE}/v1/merchants/${encodeURIComponent(merchantId)}/evidence/retained-revenue`, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "X-Scenario-Key": SCENARIO_KEY,
      },
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as RetainedRevenueOut;
  } catch {
    return null;
  }
}

/**
 * Trigger live merchant price surge injection to cause kernel refusal on in-flight checkouts.
 */
export async function injectPriceSurge(
  sku: string,
  newPricePaise: number
): Promise<InjectionOut | null> {
  try {
    const res = await fetch(`${API_BASE}/v1/scenario/injections`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Scenario-Key": SCENARIO_KEY,
      },
      body: JSON.stringify({
        kind: "PRICE_SET",
        sku,
        value: newPricePaise,
      }),
    });
    if (!res.ok) return null;
    return (await res.json()) as InjectionOut;
  } catch {
    return null;
  }
}

/**
 * Fetch full 10-link protocol evidence for one payment attempt from live inspector.
 */
export async function fetchLiveInspector(
  attemptId: string
): Promise<InspectorOut | null> {
  try {
    const res = await fetch(`${API_BASE}/v1/inspector/payment-attempts/${encodeURIComponent(attemptId)}`, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "X-Scenario-Key": SCENARIO_KEY,
      },
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as InspectorOut;
  } catch {
    return null;
  }
}

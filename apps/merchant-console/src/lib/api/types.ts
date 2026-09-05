/**
 * Typed models matching FastAPI Commerce API schemas (Pydantic models).
 *
 * All money figures are integer minor units (paise), never floats.
 * Source tags distinguish LIVE · VERIFIED data from SIMULATED · MOCK fixtures.
 */

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
  is_live?: boolean;
}

export interface CheckOut {
  name: string;
  ok: boolean;
  applicable: boolean;
  detail: string;
}

export interface VerdictOut {
  tier: string;
  ok: boolean;
  checks: CheckOut[];
  failed: string[];
}

export interface ProofChainOut {
  checkout_id: string;
  tenant_id: string;
  merchant_id: string;
  payment_attempt_id: string | null;
  attempt_ids: string[];
  links: Record<string, unknown>;
  verdict: VerdictOut;
  audit_streams: Record<string, unknown>;
  is_live?: boolean;
}

export interface AuditStreamVerificationOut {
  stream_id: string;
  aggregate_type: string;
  aggregate_id: string;
  intact: boolean;
  chain_length: number;
  head_sequence: number;
  first_broken_sequence?: number | null;
  verified_at: string;
  is_live?: boolean;
}

export type OutboxStatus = "PENDING" | "LEASED" | "DONE" | "FAILED" | "DEAD";

export interface OutboxCommandOut {
  command_id: string;
  command_type: string;
  status: OutboxStatus;
  attempts: number;
  available_at: string;
  leased_until: string | null;
  correlation_id: string;
  created_at: string;
  last_error?: string | null;
}

export interface OutboxOut {
  commands: OutboxCommandOut[];
  counts: Record<OutboxStatus, number>;
  limit: number;
  is_live?: boolean;
}

export interface ReviveOut {
  command_id: string;
  code: string;
  status: string;
  retry_at: string | null;
}

export interface BannerOut {
  scope: string;
  tenant_id: string;
  reason_code: string;
  actor: string;
  since: string;
  blocked: string[];
  still_available: string[];
}

export interface SafeModeOut {
  mode: string;
  scope: string;
  tenant_id: string;
  safe_mode: boolean;
  reason_code: string;
  actor: string;
  since: string | null;
  banner: BannerOut | null;
  blocked: string[];
  still_available: string[];
  permitted: Record<string, boolean>;
  revoked_grant_ids: string[];
  is_live?: boolean;
}

export interface InspectorFinding {
  name: string;
  ok: boolean;
  detail: string;
}

export interface InspectorAttemptOut {
  payment_attempt_id: string;
  checkout_id: string;
  checkout_version: number;
  status: string;
  amount_minor: number;
  currency: string;
  provider_payment_id?: string | null;
  provider_order_id?: string | null;
  timeline: Array<{
    seq: number;
    event_type: string;
    occurred_at: string;
    details?: string;
  }>;
  grants: Array<{
    grant_id: string;
    grant_type: string;
    status: string;
    expires_at: string;
    consumed_at: string | null;
  }>;
  outbox_commands: OutboxCommandOut[];
  provider_requests: Array<{
    provider_request_id: string;
    operation: string;
    method: string;
    url: string;
    http_status: number;
    outcome_code: string;
    request_at: string;
  }>;
  webhook_deliveries: Array<{
    inbox_id: string;
    event_type: string;
    signature_verified: boolean;
    duplicate: boolean;
    duplicate_count: number;
    received_at: string;
  }>;
  reconciliation_runs: Array<{
    reconciliation_run_id: string;
    attempt_number: number;
    reason: string;
    decision: string;
    resulting_transition: string;
    created_at: string;
  }>;
  order: {
    order_id: string;
    state: string;
    amount_minor: number;
    currency: string;
    capture_evidence: {
      source: string;
      verified_at?: string;
    };
    created_at: string;
  } | null;
  refunds: Array<{
    refund_id: string;
    state: string;
    amount_minor: number;
    reason_code: string;
    created_at: string;
  }>;
  findings: InspectorFinding[];
  is_live?: boolean;
}

export interface CaptureEvidenceInfo {
  kind: string;
  reference?: string | null;
  verified_at?: string | null;
}

export interface OrderOut {
  order_id: string;
  checkout_id: string;
  status: string;
  state?: string;
  total_minor: number;
  amount_minor?: number;
  currency: string;
  checkout_version: number;
  version?: number;
  capture_evidence_source: string;
  capture_evidence?: CaptureEvidenceInfo | null;
  payment_attempt_id?: string;
  policy_receipt_hash?: string;
  content_hash?: string;
  razorpay_order_id?: string | null;
  razorpay_payment_id?: string | null;
  refunded_minor?: number;
  refund_count?: number;
  age_seconds?: number;
  amount?: { minor: number; currency: string; display: string };
  created_at: string;
  refunds: Array<{
    refund_id: string;
    status: string;
    amount_minor: number;
    reason: string;
  }>;
}

export interface RefundItem {
  refund_id: string;
  order_id: string | null;
  checkout_id: string;
  payment_attempt_id?: string;
  amount_minor: number;
  currency: string;
  amount?: { minor: number; currency: string; display: string };
  captured_minor?: number | null;
  state:
    | "REFUND_PENDING"
    | "REFUND_UNKNOWN"
    | "REFUND_FAILED"
    | "RECONCILING"
    | "ESCALATED"
    | "PARTIALLY_REFUNDED"
    | "REFUNDED"
    | "PROCESSED";
  row_status?: string;
  provider_refund_id: string | null;
  reason: string;
  automatic?: boolean;
  reconciliation_attempts: number;
  created_at: string;
  updated_at?: string;
  age_seconds?: number;
}

export interface ReviewQueueCase {
  case_id: string;
  checkout_id: string;
  order_id: string | null;
  blocking_reason_code: string;
  escalated_at: string;
  provider_state_at_escalation: string;
  proof_chain_ref: string;
  redacted_timeline_snippet: string;
  amount_minor: number;
  currency: string;
}

export interface ScenarioInjectionOut {
  injection_id: string;
  kind: string;
  label: string;
  sku?: string;
  new_catalogue_revision?: number;
  deltas?: Array<{
    field: string;
    before: unknown;
    after: unknown;
  }>;
}

export interface CatalogueProduct {
  sku: string;
  name: string;
  category: string;
  unit: string;
  list_price_minor: number;
  current_price_minor?: number;
  stock_units: number;
  is_listed: boolean;
  tax_basis_points: number;
  brand?: string;
  category_tiles?: string[];
  synonyms?: string[];
}

export interface SpecRevenueMetric {
  key: string;
  label: string;
  valueDisplay: string;
  description: string;
  source: "live" | "simulated";
  groundingProof?: string;
}

export interface OrdersOut {
  orders: OrderOut[];
  cursor?: string | null;
  next_cursor?: string | null;
  limit?: number;
  scope?: "own" | "tenant";
  counts?: Record<string, number>;
  is_live: boolean;
}

export interface RefundsOut {
  refunds: RefundItem[];
  cursor?: string | null;
  next_cursor?: string | null;
  limit?: number;
  scope?: "own" | "tenant";
  counts?: Record<string, number>;
  is_live: boolean;
}

export interface ReviewQueueOut {
  cases: ReviewQueueCase[];
  is_live: boolean;
}

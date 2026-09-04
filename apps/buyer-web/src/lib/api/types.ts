// PROVISIONAL: reconcile with OpenAPI after commerce-api lands.
//
// Every response shape the storefront depends on lives in this one module so the
// reconciliation with the generated OpenAPI client (spec 22.1) is a single diff. Field
// names follow the kernel and merchant-sim packages where those already exist
// (CheckoutState, PaymentState, RecoveryCode, Quote.to_checkout_content, Delta,
// KernelDecision); everything else is a best guess against ADR 0003's endpoint catalogue.
//
// Money is always integer minor units plus an ISO 4217 code (spec 24.1). The UI never
// adds two of these together; it only formats what the server returns.
import { z } from "zod";

// ---------------------------------------------------------------- closed enums (kernel)

export const CheckoutStateSchema = z.enum([
  "DRAFT",
  "QUOTED",
  "RESERVED",
  "APPROVAL_REQUIRED",
  "APPROVED",
  "EXECUTION_PENDING",
  "AWAITING_PAYMENT",
  "PAID",
  "PAYMENT_FAILED",
  "PAYMENT_UNKNOWN",
  "INVALIDATED",
  "INVALIDATED_AWAITING_PAYMENT_RESULT",
  "CANCELLED",
  "EXPIRED",
]);
export type CheckoutState = z.infer<typeof CheckoutStateSchema>;

export const PaymentStateSchema = z.enum([
  "CREATED",
  "SUBMITTED",
  "AUTHORIZED",
  "CAPTURED",
  "FAILED",
  "EXPIRED",
  "UNKNOWN",
  "RECONCILING",
  "ESCALATED",
  "STALE_CAPTURE",
  "REFUND_PENDING",
  "PARTIALLY_REFUNDED",
  "REFUNDED",
  "REFUND_UNKNOWN",
  "REFUND_FAILED",
  "AUTO_REFUND_PENDING",
]);
export type PaymentState = z.infer<typeof PaymentStateSchema>;

export const RecoveryCodeSchema = z.enum([
  "OK",
  "DUPLICATE_OPERATION",
  "CONCURRENT_OPERATION",
  "STALE_CHECKOUT",
  "REAPPROVAL_REQUIRED",
  "RESERVATION_EXPIRED",
  "AUTHORITY_REVOKED",
  "AUTHORITY_INSUFFICIENT",
  "PAYMENT_FAILED",
  "PAYMENT_PENDING",
  "PAYMENT_UNKNOWN",
  "STALE_CAPTURE",
  "REFUND_ALLOWED",
  "REFUND_REVIEW_REQUIRED",
  "RESOLUTION_PLAN_ISSUED",
  "RESOLUTION_PLAN_EXPIRED",
  "RECONCILIATION_IN_PROGRESS",
  "POLICY_EXCEPTION",
  "HUMAN_REVIEW_REQUIRED",
  "SAFE_MODE_ACTIVE",
]);
export type RecoveryCode = z.infer<typeof RecoveryCodeSchema>;

export const OrderStateSchema = z.enum([
  "CONFIRMED",
  "FULFILMENT_BLOCKED",
  "CANCELLED",
  "PARTIALLY_REFUNDED",
  "REFUNDED",
]);
export type OrderState = z.infer<typeof OrderStateSchema>;

// -------------------------------------------------------------------- shared shapes

const minor = z.number().int();
const currency = z.string().length(3);

/** Provenance stamp (spec 6.2, 20.1). `catalogue_revision` is the freshness token. */
export const FreshnessSchema = z.object({
  source: z.string(),
  catalogue_revision: z.number().int().nonnegative(),
  observed_at: z.string(),
});
export type Freshness = z.infer<typeof FreshnessSchema>;

/** A product as it exists right now: catalogue record plus live merchant state. */
export const ProductSchema = z.object({
  sku: z.string(),
  display_name: z.string(),
  name_en: z.string(),
  name_hi: z.string(),
  category: z.string(),
  unit_label: z.string(),
  unit_price_minor: minor,
  currency,
  tax_bp: z.number().int(),
  stock_units: z.number().int().nonnegative(),
  /** Listing and stock are reported separately: sold-out and delisted lead to different conversations. */
  is_listed: z.boolean(),
  is_available: z.boolean(),
  freshness: FreshnessSchema,
});
export type Product = z.infer<typeof ProductSchema>;

export const SearchHitSchema = ProductSchema.extend({
  score: z.number().int(),
  matched_terms: z.array(z.string()),
});
export type SearchHit = z.infer<typeof SearchHitSchema>;

export const SearchResponseSchema = z.object({
  query: z.string(),
  normalized_query: z.string(),
  locale: z.string(),
  hits: z.array(SearchHitSchema),
  freshness: FreshnessSchema,
});
export type SearchResponse = z.infer<typeof SearchResponseSchema>;

// ---------------------------------------------------------------- basket and quote

export const QuoteLineSchema = z.object({
  sku: z.string(),
  name: z.string(),
  quantity: z.number().int().positive(),
  unit_price_minor: minor,
  subtotal_minor: minor,
  tax_bp: z.number().int(),
  tax_minor: minor,
});
export type QuoteLine = z.infer<typeof QuoteLineSchema>;

/** Mirrors merchant_sim.fees.Quote.to_checkout_content plus the derived gap and hash. */
export const QuoteSchema = z.object({
  currency,
  lines: z.array(QuoteLineSchema),
  items_subtotal_minor: minor,
  items_tax_minor: minor,
  delivery_fee_minor: minor,
  delivery_tax_minor: minor,
  total_minor: minor,
  free_delivery_applied: z.boolean(),
  gap_to_free_delivery_minor: minor,
  source: z.string(),
  catalogue_revision: z.number().int(),
  content_hash: z.string(),
});
export type Quote = z.infer<typeof QuoteSchema>;

export const UnavailabilitySchema = z.object({
  sku: z.string(),
  requested: z.number().int(),
  available_units: z.number().int(),
  listed: z.boolean(),
});
export type Unavailability = z.infer<typeof UnavailabilitySchema>;

export const BasketSchema = z.object({
  basket_id: z.string(),
  lines: z.array(z.object({ sku: z.string(), quantity: z.number().int().positive() })),
  code: RecoveryCodeSchema,
  quote: QuoteSchema.nullable(),
  unavailable: z.array(UnavailabilitySchema),
  freshness: FreshnessSchema,
  /** True when merchant state has moved since the quote was priced (GET re-quote). */
  stale: z.boolean(),
});
export type Basket = z.infer<typeof BasketSchema>;

// -------------------------------------------------------------- checkout and approval

export const DeltaSchema = z.object({
  field_path: z.string(),
  approved: z.unknown(),
  current: z.unknown(),
  reason: z.string(),
});
export type Delta = z.infer<typeof DeltaSchema>;

export const ReservationSchema = z.object({
  reservation_id: z.string(),
  state: z.enum(["ACTIVE", "RELEASED", "EXPIRED", "CONSUMED"]),
  expires_at: z.string(),
});
export type Reservation = z.infer<typeof ReservationSchema>;

/**
 * The trusted approval card (spec 4.3, 10.2). The approve action echoes exactly
 * `content_hash`, `amount_minor` and `currency` from this object and nothing else.
 */
export const ApprovalCardSchema = z.object({
  checkout_id: z.string(),
  version: z.number().int().positive(),
  content_hash: z.string(),
  policy_receipt_id: z.string(),
  policy_receipt_hash: z.string(),
  amount_minor: minor,
  currency,
  expires_at: z.string(),
  reservation: ReservationSchema.nullable(),
  // The card is built from the immutable version content, which is authoritative on its
  // own; the merchant quote is a convenience the server may not have reloaded.
  quote: QuoteSchema.nullable(),
  previous_version: z.number().int().nullable(),
  deltas: z.array(DeltaSchema),
});
export type ApprovalCard = z.infer<typeof ApprovalCardSchema>;

export const ApprovalEchoSchema = z
  .object({ content_hash: z.string(), amount_minor: minor, currency })
  .strict();
export type ApprovalEcho = z.infer<typeof ApprovalEchoSchema>;

export const ApprovalRecordSchema = z.object({
  approval_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  policy_receipt_hash: z.string(),
  amount_minor: minor,
  currency,
  approved_at: z.string(),
  expires_at: z.string(),
  authority_epoch: z.number().int(),
});
export type ApprovalRecord = z.infer<typeof ApprovalRecordSchema>;

export const VersionSummarySchema = z.object({
  version: z.number().int(),
  state: CheckoutStateSchema,
  content_hash: z.string(),
  // A version exists in QUOTED before its Policy-at-Sale Receipt is issued, so this is
  // absent until the checkout enters APPROVAL_REQUIRED.
  policy_receipt_hash: z.string().nullable(),
  amount_minor: minor,
  currency,
  created_at: z.string(),
  approval: ApprovalRecordSchema.nullable(),
});
export type VersionSummary = z.infer<typeof VersionSummarySchema>;

export const CaptureEvidenceSchema = z.object({
  kind: z.enum(["WEBHOOK", "PROVIDER_FETCH"]),
  reference: z.string(),
  verified_at: z.string(),
});
export type CaptureEvidence = z.infer<typeof CaptureEvidenceSchema>;

export const AttemptSummarySchema = z.object({
  attempt_id: z.string(),
  version: z.number().int(),
  state: PaymentStateSchema,
  razorpay_order_id: z.string().nullable(),
  razorpay_payment_id: z.string().nullable(),
  grant_id: z.string().nullable(),
  capture_evidence: CaptureEvidenceSchema.nullable(),
  reconciliation_attempts: z.number().int().nonnegative(),
});
export type AttemptSummary = z.infer<typeof AttemptSummarySchema>;

export const CheckoutSchema = z.object({
  checkout_id: z.string(),
  basket_id: z.string(),
  state: CheckoutStateSchema,
  current_version: z.number().int(),
  versions: z.array(VersionSummarySchema),
  approval_card: ApprovalCardSchema.nullable(),
  attempt: AttemptSummarySchema.nullable(),
  order_id: z.string().nullable(),
  /** Non-empty only while a reapproval is outstanding. */
  deltas: z.array(DeltaSchema),
  cancellable: z.boolean(),
  updated_at: z.string(),
});
export type Checkout = z.infer<typeof CheckoutSchema>;

/** transaction_kernel.contracts.KernelDecision, verbatim (spec 26.3). */
export const KernelDecisionSchema = z.object({
  decision_id: z.string(),
  allowed: z.boolean(),
  code: RecoveryCodeSchema,
  explanation: z.string(),
  checkout: z
    .object({ checkout_id: z.string(), version: z.number().int(), content_hash: z.string() })
    .nullable(),
  deltas: z.array(DeltaSchema),
  grant_id: z.string().nullable(),
  payment_attempt_id: z.string().nullable(),
  next_version: z.number().int().nullable(),
  correlation_id: z.string().nullable(),
});
export type KernelDecision = z.infer<typeof KernelDecisionSchema>;

export const ApproveResponseSchema = z.object({
  approval: ApprovalRecordSchema,
  checkout: CheckoutSchema,
});
export type ApproveResponse = z.infer<typeof ApproveResponseSchema>;

/** ADR D9: a duplicate submit is 200 with outcome DUPLICATE_OPERATION and the winner's attempt. */
export const SubmitResponseSchema = z.object({
  decision: KernelDecisionSchema,
  outcome: RecoveryCodeSchema,
  attempt_id: z.string().nullable(),
  checkout: CheckoutSchema,
});
export type SubmitResponse = z.infer<typeof SubmitResponseSchema>;

export const CancelResponseSchema = z.object({
  decision: KernelDecisionSchema,
  checkout: CheckoutSchema,
});
export type CancelResponse = z.infer<typeof CancelResponseSchema>;

// ---------------------------------------------------------------------- payment

export const PaymentHandoffSchema = z.object({
  checkout_id: z.string(),
  version: z.number().int(),
  attempt_id: z.string().nullable(),
  state: PaymentStateSchema.nullable(),
  provider: z.literal("razorpay"),
  /** Public key id; fetched at runtime, never hard-coded in the bundle. */
  razorpay_key_id: z.string(),
  razorpay_order_id: z.string().nullable(),
  amount_minor: minor,
  currency,
  merchant_name: z.string(),
  description: z.string(),
  prefill: z
    .object({ name: z.string().optional(), email: z.string().optional(), contact: z.string().optional() })
    .optional(),
});
export type PaymentHandoff = z.infer<typeof PaymentHandoffSchema>;

export const VerifyRequestSchema = z.object({
  checkout_id: z.string(),
  razorpay_order_id: z.string(),
  razorpay_payment_id: z.string(),
  razorpay_signature: z.string(),
});
export type VerifyRequest = z.infer<typeof VerifyRequestSchema>;

/** ADR D8: the browser callback is recorded, never treated as capture evidence. */
export const VerifyResponseSchema = z.object({
  accepted: z.boolean(),
  attempt_id: z.string().nullable(),
  state: PaymentStateSchema,
  evidence_kind: z.literal("BROWSER_CALLBACK"),
  message: z.string(),
});
export type VerifyResponse = z.infer<typeof VerifyResponseSchema>;

// ------------------------------------------------------------------------ orders

export const RefundSchema = z.object({
  refund_id: z.string(),
  amount_minor: minor,
  currency,
  state: PaymentStateSchema,
  reason: z.string(),
  automatic: z.boolean(),
  created_at: z.string(),
});
export type Refund = z.infer<typeof RefundSchema>;

export const OrderSchema = z.object({
  order_id: z.string(),
  checkout_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  policy_receipt_hash: z.string(),
  state: OrderStateSchema,
  amount_minor: minor,
  currency,
  /**
   * The paid version's quote, when the server carried it through. The order row stores
   * the authoritative amount in amount_minor and references the checkout version for the
   * rest, so treat this as a convenience and never as the source of the total.
   */
  quote: QuoteSchema.nullable(),
  payment: AttemptSummarySchema,
  refunds: z.array(RefundSchema),
  created_at: z.string(),
});
export type Order = z.infer<typeof OrderSchema>;

export const RefundRequestSchema = z.object({
  amount_minor: minor.optional(),
  reason: z.string().min(1),
});
export type RefundRequest = z.infer<typeof RefundRequestSchema>;

export const RefundResponseSchema = z.object({
  decision: KernelDecisionSchema,
  refund: RefundSchema.nullable(),
  order: OrderSchema,
});
export type RefundResponse = z.infer<typeof RefundResponseSchema>;

// -------------------------------------------------------- timeline, events and proof

/** One action-timeline row (spec 26.1). Secrets and full signatures are already redacted. */
export const TimelineRowSchema = z.object({
  event_id: z.string(),
  occurred_at: z.string(),
  actor: z.string(),
  action: z.string(),
  source: z.string().nullable(),
  version: z.number().int().nullable(),
  hash_short: z.string().nullable(),
  policy_evaluated: z.string().nullable(),
  policy_receipt_hash: z.string().nullable(),
  authority_ref: z.string().nullable(),
  decision: z
    .object({ allowed: z.boolean(), code: RecoveryCodeSchema, reason: z.string() })
    .nullable(),
  grant: z.object({ grant_id: z.string(), status: z.string() }).nullable(),
  provider_result: z.string().nullable(),
  reconciliation_result: z.string().nullable(),
  correlation_id: z.string(),
  scenario_injection: z.boolean(),
});
export type TimelineRow = z.infer<typeof TimelineRowSchema>;

export const TimelineResponseSchema = z.object({
  checkout_id: z.string(),
  rows: z.array(TimelineRowSchema),
});
export type TimelineResponse = z.infer<typeof TimelineResponseSchema>;

/** Payload of one SSE `timeline` event on /v1/checkouts/{id}/events. `id:` is event_id. */
export const CheckoutEventSchema = z.object({
  event_id: z.string(),
  checkout_id: z.string(),
  checkout_state: CheckoutStateSchema,
  payment_state: PaymentStateSchema.nullable(),
  order_id: z.string().nullable(),
  row: TimelineRowSchema,
});
export type CheckoutEvent = z.infer<typeof CheckoutEventSchema>;

export const ProofLinkSchema = z.object({
  step: z.number().int(),
  name: z.string(),
  present: z.boolean(),
  verified: z.boolean(),
  reference: z.string().nullable(),
  hash: z.string().nullable(),
  detail: z.string(),
});
export type ProofLink = z.infer<typeof ProofLinkSchema>;

export const ProofChainSchema = z.object({
  checkout_id: z.string(),
  version: z.number().int().nullable(),
  links: z.array(ProofLinkSchema),
  verdict: z.object({
    ok: z.boolean(),
    summary: z.string(),
    checks: z.array(z.object({ name: z.string(), ok: z.boolean(), detail: z.string() })),
  }),
  attempt_id: z.string().nullable(),
});
export type ProofChain = z.infer<typeof ProofChainSchema>;

// ------------------------------------------------------------------ session, config

/** Server-side only: the token is set as an HttpOnly cookie and never returned to JS. */
export const DemoSessionSchema = z.object({
  token: z.string(),
  session_id: z.string(),
  actor_type: z.string(),
  expires_at: z.string(),
});
export type DemoSession = z.infer<typeof DemoSessionSchema>;

export const SessionInfoSchema = z.object({
  active: z.boolean(),
  session_id: z.string().nullable(),
  expires_at: z.string().nullable(),
  mode: z.enum(["live", "mock"]),
});
export type SessionInfo = z.infer<typeof SessionInfoSchema>;

export const DegradationSchema = z.object({
  component: z.string(),
  notice: z.string(),
});
export type Degradation = z.infer<typeof DegradationSchema>;

/** GET /v1/config: redacted runtime facts. */
export const RuntimeConfigSchema = z.object({
  profile: z.string(),
  razorpay_mode: z.string(),
  safe_mode: z.boolean(),
  degraded: z.array(DegradationSchema),
});
export type RuntimeConfig = z.infer<typeof RuntimeConfigSchema>;

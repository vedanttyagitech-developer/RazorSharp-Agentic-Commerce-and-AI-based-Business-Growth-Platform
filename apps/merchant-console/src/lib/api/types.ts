/**
 * The API's response shapes, as zod schemas, mirroring `commerce_api.schemas` and the
 * per-router wire models under `commerce_api.routers`.
 *
 * Parsed rather than cast. A cast is a promise the compiler cannot keep: the API is a
 * separate process that can be redeployed while this tab is open, and a silently missing
 * `captured_minor` would render as `NaN` on a page whose entire claim is that its figures
 * came from committed rows. Parsing turns that into a visible error at the boundary.
 *
 * Nullable rather than optional wherever the API declares `X | None`. The two are not the
 * same in zod, and treating a present `null` as an absent key is how a field that means
 * "the platform does not know this yet" becomes a field that means "not sent".
 *
 * The inspector's blocks are deliberately loose records. The API declares them as
 * `list[dict[str, Any]]` so that a schema gaining a column does not silently drop it, and
 * a strict mirror here would reintroduce exactly the loss that design avoided. The
 * console renders those rows key by key, whatever keys they carry.
 */
import { z } from "zod";

const Row = z.record(z.string(), z.unknown());

// ------------------------------------------------------------------ session, config

export const SessionSchema = z.object({
  session_id: z.string(),
  tenant_id: z.string(),
  merchant_id: z.string(),
  buyer_ref: z.string(),
  actor_type: z.string(),
  capabilities: z.array(z.string()),
  expires_at: z.string(),
});

export const RuntimeConfigSchema = z.object({
  profile: z.string(),
  razorpay_mode: z.string(),
  razorpay: z.object({
    test_mode: z.boolean(),
    key_id_prefix: z.string(),
    webhook_secret_configured: z.boolean(),
  }),
  database: z.object({
    reachable: z.boolean(),
    app_role: z.boolean(),
    kernel_role: z.boolean(),
  }),
  safe_mode: z.boolean(),
  scenario_routes_enabled: z.boolean(),
  demo_routes_enabled: z.boolean(),
  degraded: z.array(z.object({ component: z.string(), notice: z.string() })),
});

// ------------------------------------------------------------------------- money

export const MoneySchema = z.object({
  minor: z.number().int(),
  currency: z.string(),
  display: z.string(),
});

export const FreshnessSchema = z.object({
  source: z.string(),
  catalogue_revision: z.number().int(),
  observed_at: z.string(),
});

// ----------------------------------------------------------------------- catalogue

export const ProductSchema = z.object({
  sku: z.string(),
  display_name: z.string(),
  name_en: z.string(),
  name_hi: z.string(),
  category: z.string(),
  unit_label: z.string(),
  unit_price_minor: z.number().int(),
  unit_price: MoneySchema,
  currency: z.string(),
  tax_bp: z.number().int(),
  stock_units: z.number().int(),
  is_listed: z.boolean(),
  is_available: z.boolean(),
  freshness: FreshnessSchema,
});

export const SearchHitSchema = ProductSchema.extend({
  score: z.number().int(),
  matched_terms: z.array(z.string()),
});

export const SearchResponseSchema = z.object({
  query: z.string(),
  normalized_query: z.string(),
  locale: z.string(),
  hits: z.array(SearchHitSchema),
  skus: z.array(z.string()),
  freshness: FreshnessSchema,
});

export const CataloguePageSchema = z.object({
  products: z.array(ProductSchema),
  next_cursor: z.string().nullable(),
  limit: z.number().int(),
  matched: z.number().int(),
  counts_by_category: z.record(z.string(), z.number().int()),
  revision: z.number().int(),
});

// --------------------------------------------------------------------------- quote

export const QuoteLineSchema = z.object({
  sku: z.string(),
  name: z.string(),
  quantity: z.number().int(),
  unit_price_minor: z.number().int(),
  subtotal_minor: z.number().int(),
  // Null on a line rebuilt from an approved checkout document: the hashed bytes record
  // the tax charged, never the rate that produced it. Null is "not stated", not zero.
  tax_bp: z.number().int().nullable(),
  tax_minor: z.number().int(),
});

export const QuoteSchema = z.object({
  currency: z.string(),
  lines: z.array(QuoteLineSchema),
  items_subtotal_minor: z.number().int(),
  items_tax_minor: z.number().int(),
  delivery_fee_minor: z.number().int(),
  delivery_tax_minor: z.number().int(),
  discount_minor: z.number().int(),
  total_minor: z.number().int(),
  total: MoneySchema,
  free_delivery_applied: z.boolean(),
  gap_to_free_delivery_minor: z.number().int().nullable(),
  offer_label: z.string().nullable(),
  offer_valid_till: z.string().nullable(),
  source: z.string(),
  catalogue_revision: z.number().int(),
  content_hash: z.string(),
});

// ------------------------------------------------------------------ orders, refunds

export const CaptureEvidenceSchema = z.object({
  kind: z.string(),
  reference: z.string(),
  verified_at: z.string(),
});

export const AttemptSchema = z.object({
  attempt_id: z.string(),
  version: z.number().int(),
  state: z.string(),
  razorpay_order_id: z.string().nullable(),
  razorpay_payment_id: z.string().nullable(),
  grant_id: z.string().nullable(),
  capture_evidence: CaptureEvidenceSchema.nullable(),
  reconciliation_attempts: z.number().int(),
});

export const RefundSchema = z.object({
  refund_id: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  state: z.string(),
  reason: z.string(),
  automatic: z.boolean(),
  created_at: z.string(),
});

export const OrderSchema = z.object({
  order_id: z.string(),
  /**
   * The order as a person says it: `RS-260907-K7M4QX2`.
   *
   * The same string the buyer is shown, derived by the server from `order_id`. It is here
   * so that a buyer who telephones quoting their order number is naming something an
   * operator can actually search for -- a reference nobody on the merchant side can see is
   * a reference that does not exist.
   */
  reference: z.string(),
  checkout_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  policy_receipt_hash: z.string(),
  state: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  amount: MoneySchema,
  quote: QuoteSchema.nullable(),
  payment: AttemptSchema,
  refunds: z.array(RefundSchema),
  created_at: z.string(),
});

export const OrderSummarySchema = z.object({
  order_id: z.string(),
  /** The order as a person says it. See `OrderSchema.reference`. */
  reference: z.string(),
  checkout_id: z.string(),
  version: z.number().int(),
  payment_attempt_id: z.string(),
  policy_receipt_hash: z.string(),
  state: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  amount: MoneySchema,
  capture_evidence: CaptureEvidenceSchema.nullable(),
  razorpay_order_id: z.string().nullable(),
  razorpay_payment_id: z.string().nullable(),
  refunded_minor: z.number().int(),
  refund_count: z.number().int(),
  created_at: z.string(),
  age_seconds: z.number().int(),
});

export const OrdersPageSchema = z.object({
  orders: z.array(OrderSummarySchema),
  next_cursor: z.string().nullable(),
  limit: z.number().int(),
  scope: z.string(),
  counts: z.record(z.string(), z.number().int()),
});

export const RefundListItemSchema = z.object({
  refund_id: z.string(),
  order_id: z.string().nullable(),
  checkout_id: z.string(),
  payment_attempt_id: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  amount: MoneySchema,
  captured_minor: z.number().int().nullable(),
  state: z.string(),
  row_status: z.string(),
  reason: z.string(),
  automatic: z.boolean(),
  provider_refund_id: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
  age_seconds: z.number().int(),
});

export const RefundsPageSchema = z.object({
  refunds: z.array(RefundListItemSchema),
  next_cursor: z.string().nullable(),
  limit: z.number().int(),
  scope: z.string(),
  counts: z.record(z.string(), z.number().int()),
});

// -------------------------------------------------------------------- ops: outbox

export const OutboxCommandSchema = z.object({
  command_id: z.string(),
  command_type: z.string(),
  status: z.string(),
  attempts: z.number().int(),
  available_at: z.string(),
  leased_until: z.string().nullable(),
  correlation_id: z.string(),
  created_at: z.string(),
});

/**
 * Commands still owed a delivery that are not about to get one.
 *
 * Required, not optional. An API that stopped sending this block would make every
 * console read fail loudly rather than render a reassuring screen with the warning
 * silently missing, and of those two outcomes only the first is honest.
 */
export const OutboxWaitingSchema = z.object({
  parked: z.number().int(),
  overdue: z.number().int(),
  parked_beyond_seconds: z.number().int(),
  overdue_beyond_seconds: z.number().int(),
  oldest: OutboxCommandSchema.nullable(),
});

export const OutboxPageSchema = z.object({
  commands: z.array(OutboxCommandSchema),
  counts: z.record(z.string(), z.number().int()),
  waiting: OutboxWaitingSchema,
  limit: z.number().int(),
});

export const ReviveResultSchema = z.object({
  command_id: z.string(),
  code: z.string(),
  status: z.string(),
  retry_at: z.string().nullable(),
});

// ----------------------------------------------------------------- ops: safe mode

export const SafeModeBannerSchema = z.object({
  scope: z.string(),
  tenant_id: z.string().nullable(),
  reason_code: z.string(),
  actor: z.string(),
  since: z.string(),
  blocked: z.array(z.string()),
  still_available: z.array(z.string()),
});

export const SafeModeSchema = z.object({
  mode: z.string(),
  scope: z.string(),
  tenant_id: z.string(),
  safe_mode: z.boolean(),
  reason_code: z.string().nullable(),
  actor: z.string().nullable(),
  since: z.string().nullable(),
  banner: SafeModeBannerSchema.nullable(),
  blocked: z.array(z.string()),
  still_available: z.array(z.string()),
  permitted: z.record(z.string(), z.boolean()),
  revoked_grant_ids: z.array(z.string()),
});

// ------------------------------------------------------------- scenario injections

export const InjectionSchema = z.object({
  injection_id: z.string(),
  kind: z.string(),
  label: z.string(),
  sku: z.string().nullable(),
  note: z.string(),
  currency: z.string().nullable(),
  deltas: z.array(
    z.object({
      field: z.string(),
      before: z.union([z.boolean(), z.number()]),
      after: z.union([z.boolean(), z.number()]),
    }),
  ),
  revision_before: z.number().int(),
  revision_after: z.number().int(),
  injected_at: z.string(),
  audit_event_id: z.string(),
  scenario_run_id: z.string(),
  audit_payload: Row,
});

// ------------------------------------------------------------------------ evidence

export const RetainedRevenueSchema = z.object({
  checkout_id: z.string(),
  merchant_id: z.string(),
  currency: z.string(),
  stale_version: z.number().int().nullable(),
  stale_approved_minor: z.number().int().nullable(),
  stale_invalidated_at: z.string().nullable(),
  corrected_version: z.number().int().nullable(),
  corrected_total_minor: z.number().int().nullable(),
  captured_minor: z.number().int().nullable(),
  captured_from: z.string().nullable(),
  difference_minor: z.number().int().nullable(),
  direction: z.string(),
  refunded_minor: z.number().int(),
  net_retained_minor: z.number().int().nullable(),
  controlled_scenario: z.boolean(),
  explanation: z.string(),
});

export const AuditVerificationSchema = z.object({
  aggregate_type: z.string(),
  aggregate_id: z.string(),
  intact: z.boolean(),
  empty: z.boolean(),
  length: z.number().int(),
  events_verified: z.number().int(),
  head_seq: z.number().int().nullable(),
  head_hash: z.string().nullable(),
  code: z.string(),
  first_break: Row.nullable(),
});

export const ProofChainSchema = z.object({
  checkout_id: z.string(),
  tenant_id: z.string(),
  merchant_id: z.string(),
  payment_attempt_id: z.string().nullable(),
  attempt_ids: z.array(z.string()),
  links: Row,
  verdict: z.object({
    tier: z.string(),
    ok: z.boolean(),
    checks: z.array(
      z.object({
        name: z.string(),
        ok: z.boolean(),
        applicable: z.boolean(),
        detail: z.string(),
      }),
    ),
    failed: z.array(z.string()),
  }),
  audit_streams: z.record(z.string(), AuditVerificationSchema),
});

export const TimelineEntrySchema = z.object({
  id: z.string(),
  occurred_at: z.string(),
  source: z.string(),
  actor: z.string(),
  action: z.string(),
  summary: z.string(),
  correlation_id: z.string(),
  scenario_injection: z.boolean(),
  checkout_version: z.number().int().nullable(),
  content_hash: z.string().nullable(),
  policy_version: z.string().nullable(),
  policy_receipt_hash: z.string().nullable(),
  freshness: Row.nullable(),
  approval_ref: z.string().nullable(),
  authority_epoch: z.number().int().nullable(),
  decision: Row.nullable(),
  grant: Row.nullable(),
  payment_attempt_id: z.string().nullable(),
  provider: Row.nullable(),
  reconciliation: Row.nullable(),
  refund: Row.nullable(),
  audit: Row.nullable(),
  details: Row,
});

export const TimelineSchema = z.object({
  checkout_id: z.string(),
  entries: z.array(TimelineEntrySchema),
  cursor: z.string().nullable(),
  scenario_injections: z.number().int(),
});

// ----------------------------------------------------------------------- inspector

export const InspectorSchema = z.object({
  payment_attempt_id: z.string(),
  checkout_id: z.string(),
  checkout_version: z.number().int(),
  state: z.string(),
  amount_minor: z.number().int(),
  currency: z.string(),
  receipt: z.string(),
  provider_order_id: z.string().nullable(),
  provider_payment_id: z.string().nullable(),
  state_history: z.array(Row),
  grants: z.array(Row),
  commands: z.array(Row),
  provider_requests: z.array(Row),
  webhook_deliveries: z.array(Row),
  reconciliation_runs: z.array(Row),
  order: Row.nullable(),
  refunds: z.array(Row),
  findings: z.array(
    z.object({
      name: z.string(),
      ok: z.boolean(),
      detail: z.string(),
    }),
  ),
});

// --------------------------------------------------------------------------- types

export type Session = z.infer<typeof SessionSchema>;
export type RuntimeConfig = z.infer<typeof RuntimeConfigSchema>;
export type Money = z.infer<typeof MoneySchema>;
export type Freshness = z.infer<typeof FreshnessSchema>;
export type Product = z.infer<typeof ProductSchema>;
export type SearchHit = z.infer<typeof SearchHitSchema>;
export type SearchResponse = z.infer<typeof SearchResponseSchema>;
export type CataloguePage = z.infer<typeof CataloguePageSchema>;
export type Quote = z.infer<typeof QuoteSchema>;
export type QuoteLine = z.infer<typeof QuoteLineSchema>;
export type CaptureEvidence = z.infer<typeof CaptureEvidenceSchema>;
export type Attempt = z.infer<typeof AttemptSchema>;
export type Refund = z.infer<typeof RefundSchema>;
export type Order = z.infer<typeof OrderSchema>;
export type OrderSummary = z.infer<typeof OrderSummarySchema>;
export type OrdersPage = z.infer<typeof OrdersPageSchema>;
export type RefundListItem = z.infer<typeof RefundListItemSchema>;
export type RefundsPage = z.infer<typeof RefundsPageSchema>;
export type OutboxCommand = z.infer<typeof OutboxCommandSchema>;
export type OutboxPage = z.infer<typeof OutboxPageSchema>;
export type OutboxWaiting = z.infer<typeof OutboxWaitingSchema>;
export type ReviveResult = z.infer<typeof ReviveResultSchema>;
export type SafeMode = z.infer<typeof SafeModeSchema>;
export type SafeModeBanner = z.infer<typeof SafeModeBannerSchema>;
export type Injection = z.infer<typeof InjectionSchema>;
export type RetainedRevenue = z.infer<typeof RetainedRevenueSchema>;
export type AuditVerification = z.infer<typeof AuditVerificationSchema>;
export type ProofChain = z.infer<typeof ProofChainSchema>;
export type TimelineEntry = z.infer<typeof TimelineEntrySchema>;
export type Timeline = z.infer<typeof TimelineSchema>;
export type Inspector = z.infer<typeof InspectorSchema>;

/**
 * The injection kinds the scenario controller accepts, as `scenario_service` declares
 * them. The console offers only the two a merchant actually performs on a catalogue row;
 * the rest are demonstration levers that belong to the scenario runner, not to a price
 * list.
 */
export const INJECTION_KINDS = [
  "STOCK_SET",
  "STOCK_DECREMENT",
  "SELL_OUT",
  "PRICE_SET",
  "AVAILABILITY_SET",
  "DELIVERY_FEE_SET",
  "FREE_DELIVERY_THRESHOLD_SET",
  "CATALOGUE_RESET",
] as const;

export type InjectionKind = (typeof INJECTION_KINDS)[number];

/* ------------------------------------------------------------------ human review */

/**
 * The proof-chain reference a case carries, so a reviewer can leave this screen and go
 * check the money for themselves rather than taking the queue's word for it.
 */
export const ProofChainRefSchema = z.object({
  checkout_id: z.string(),
  payment_attempt_id: z.string().nullable(),
}).loose();

/**
 * One case in the human-review queue.
 *
 * The audit fields are not decoration. A case that names its own audit event, sequence
 * and self-hash is a case whose existence a reviewer can verify against the hash chain,
 * which is the difference between a queue and a list somebody typed.
 */
export const CaseSchema = z.object({
  case_key: z.string(),
  state: z.string(),
  priority: z.string(),
  reason_code: z.string(),
  reason_family: z.string(),
  checkout_id: z.string(),
  payment_attempt_id: z.string().nullable(),
  refund_id: z.string().nullable(),
  monetary_exposure: MoneySchema.nullable(),
  opened_at: z.string(),
  opened_by: z.string(),
  target_response_by: z.string(),
  target_response_seconds: z.number().int(),
  correlation_id: z.string(),
  attempts_used: z.number().int().nullable(),
  attempts_bound: z.number().int(),
  detections: z.number().int(),
  audit_event_id: z.string(),
  audit_aggregate_type: z.string(),
  audit_aggregate_id: z.string(),
  audit_seq: z.number().int(),
  audit_self_hash: z.string(),
  proof_chain: ProofChainRefSchema,
}).loose();

export const QueueSchema = z.object({
  cases: z.array(CaseSchema),
  priority_counts: z.record(z.string(), z.number().int()),
  limit: z.number().int(),
  scope: z.string(),
}).loose();

/** One redacted timeline row, as a reviewer reads it. */
export const CaseEventSchema = z.object({
  id: z.string(),
  occurred_at: z.string(),
  source: z.string(),
  actor: z.string(),
  action: z.string(),
  summary: z.string(),
  correlation_id: z.string(),
  scenario_injection: z.boolean(),
  checkout_version: z.number().int().nullable(),
  payment_attempt_id: z.string().nullable(),
  details: z.record(z.string(), z.unknown()),
}).loose();

/**
 * What the provider last said about a payment, or that it has not said anything.
 *
 * `present: false` is a real answer and must not render as a row of dashes that reads
 * like a failed load: "the provider has told us nothing" is precisely the state a
 * reviewer is being asked to act on.
 */
export const VerifiedStateSchema = z.object({
  present: z.boolean(),
  source: z.string().nullable(),
  status: z.string().nullable(),
  provider_status: z.string().nullable(),
  provider_payment_id: z.string().nullable(),
  provider_order_id: z.string().nullable(),
  amount_minor: z.number().int().nullable(),
}).loose();

export const CaseDetailSchema = z.object({
  case: CaseSchema,
  verified_provider_state: VerifiedStateSchema,
  refused_evidence: z.record(z.string(), z.unknown()).nullable(),
}).loose();

/**
 * One support case as the person answering it sees it.
 *
 * Not the review queue. That one is the kernel's own escalations -- a payment whose outcome
 * is unknown -- and it is keyed by a case key the kernel invented. This is a buyer saying
 * something is wrong with an order they bought, in their own words, and it is keyed by a
 * row a person can answer.
 *
 * **There is no amount on it and there is no field for one.** What is still refundable is
 * a question for the kernel at the moment somebody asks, through the same route the
 * buyer's own screen reads. A figure carried on a case is a figure that was true once.
 */
export const SupportCaseSchema = z.object({
  case_id: z.string(),
  order_id: z.string(),
  /** The order said out loud, `RS-260907-K7M4QX2`. Derived by the server from the id. */
  order_reference: z.string(),
  merchant_id: z.string(),
  reason: z.string(),
  /** What the buyer said. Shown as written, never parsed. */
  note: z.string(),
  status: z.string(),
  /** `AGENT` when the copilot raised it for them, which is a fact the reader deserves. */
  opened_by: z.string(),
  handled_by: z.string().nullable(),
  resolution_note: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const SupportQueueSchema = z.object({
  cases: z.array(SupportCaseSchema),
  returned: z.number().int(),
  limit: z.number().int(),
  /** True when the page is full, so a reader knows the queue may be longer than this. */
  may_have_more: z.boolean(),
});

export type SupportCase = z.infer<typeof SupportCaseSchema>;
export type SupportQueue = z.infer<typeof SupportQueueSchema>;

/**
 * One change a merchant proposed to their own shop.
 *
 * `content_hash` is on the wire because an approver has to send it back. It is not a
 * debugging aid: it is the thing being agreed to. An action edited between this screen
 * rendering and the button being pressed will carry a different digest, and the server
 * refuses the approval rather than attaching it to a document nobody read.
 *
 * `proposed_by` and `approved_by` stay separate. A model drafting a change is a fact
 * whoever approves deserves, and a model is never written into the second field.
 */
export const MerchantActionSchema = z.object({
  action_id: z.string(),
  merchant_id: z.string(),
  kind: z.string(),
  target: z.string(),
  proposal: z.record(z.string(), z.unknown()),
  /** The catalogue revision this was written against. Moves on, and then it is stale. */
  expected_revision: z.number().int(),
  state: z.string(),
  content_hash: z.string(),
  proposed_by: z.string(),
  approved_by: z.string().nullable(),
  outcome_note: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const MerchantActionListSchema = z.object({
  actions: z.array(MerchantActionSchema),
  returned: z.number().int(),
  limit: z.number().int(),
  may_have_more: z.boolean(),
});

/**
 * What execution answered. **200 either way**, like every other decision on this platform.
 *
 * `ok: false` covers ordinary outcomes as well as errors: a stale action, one edited after
 * approval, and a shop that refused a change which would have changed nothing. None of
 * those is a fault for the caller to fix, and all three are worth reading.
 */
export const MerchantActionResultSchema = z.object({
  action: MerchantActionSchema,
  ok: z.boolean(),
  reason: z.string(),
  allowed: z.array(z.string()),
});

export type MerchantAction = z.infer<typeof MerchantActionSchema>;
export type MerchantActionList = z.infer<typeof MerchantActionListSchema>;
export type MerchantActionResult = z.infer<typeof MerchantActionResultSchema>;

export type ProofChainRef = z.infer<typeof ProofChainRefSchema>;
export type Case = z.infer<typeof CaseSchema>;
export type Queue = z.infer<typeof QueueSchema>;
export type CaseEvent = z.infer<typeof CaseEventSchema>;
export type VerifiedState = z.infer<typeof VerifiedStateSchema>;
export type CaseDetail = z.infer<typeof CaseDetailSchema>;

/** Catalogue categories, in the order the storefront navigates them. */
export const CATEGORIES = [
  "dairy",
  "staples",
  "produce",
  "snacks",
  "beverages",
  "bakery",
  "household",
  "personal_care",
  "condiments",
  "electronics",
] as const;

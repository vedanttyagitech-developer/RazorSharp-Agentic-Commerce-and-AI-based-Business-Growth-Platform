/**
 * The API's response shapes, as zod schemas, mirroring `commerce_api.schemas`.
 *
 * Parsed rather than cast. A cast is a promise the compiler cannot keep: the server is a
 * separate process that can be redeployed while this tab is open, and a silently missing
 * `total_minor` would render as `NaN` inside an approval card the buyer is being asked to
 * consent to. Parsing turns that into an error at the boundary, where it is legible.
 *
 * Nullable rather than optional wherever the API declares `X | None`. The two are not the
 * same in zod, and treating a present `null` as an absent key is how a field that means
 * "not applicable" becomes a field that means "not sent".
 */
import { z } from "zod";

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

/**
 * One priced line of a quote.
 *
 * `tax_bp` is nullable because a line rebuilt from an approved checkout document has no
 * rate to report: the hashed document records the tax charged, not the rate that produced
 * it. Null there means the approved bytes do not state it, which is a different claim
 * from zero, and nothing on this surface fills the difference in.
 */
export const QuoteLineSchema = z.object({
  sku: z.string(),
  name: z.string(),
  quantity: z.number().int(),
  unit_price_minor: z.number().int(),
  subtotal_minor: z.number().int(),
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

export const CartLineSchema = z.object({
  sku: z.string(),
  quantity: z.number().int(),
});

/**
 * Why one requested line could not be priced. The merchant states the shortfall itself
 * -- how many were asked for, how many exist, whether the product is listed at all --
 * so the storefront never has to infer a reason from an absence in the quote.
 */
export const UnavailabilitySchema = z.object({
  sku: z.string(),
  requested: z.number().int(),
  available_units: z.number().int(),
  listed: z.boolean(),
});

export const CartSchema = z.object({
  cart_id: z.string(),
  lines: z.array(CartLineSchema),
  code: z.string(),
  quote: QuoteSchema.nullable(),
  unavailable: z.array(UnavailabilitySchema),
  freshness: FreshnessSchema,
  stale: z.boolean(),
});

export const ReservationSchema = z.object({
  reservation_id: z.string(),
  state: z.string(),
  expires_at: z.string().nullable(),
});

/**
 * One field that moved between the version the buyer approved and the version now
 * current. This is the payload of the refusal screen, and every value in it is the
 * server's; the storefront subtracts nothing.
 */
export const DeltaSchema = z.object({
  field_path: z.string(),
  approved: z.unknown(),
  current: z.unknown(),
  reason: z.string().nullable(),
});

export const ApprovalRecordSchema = z.object({
  approval_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  policy_receipt_hash: z.string().nullable(),
  amount_minor: z.number().int(),
  currency: z.string(),
  approved_at: z.string(),
  expires_at: z.string().nullable(),
  authority_epoch: z.number().int().nullable(),
});

export const ApprovalCardSchema = z.object({
  checkout_id: z.string(),
  version: z.number().int(),
  content_hash: z.string(),
  policy_receipt_id: z.string().nullable(),
  policy_receipt_hash: z.string().nullable(),
  amount_minor: z.number().int(),
  currency: z.string(),
  total: MoneySchema,
  expires_at: z.string().nullable(),
  reservation: ReservationSchema.nullable(),
  quote: QuoteSchema.nullable(),
  previous_version: z.number().int().nullable(),
  deltas: z.array(DeltaSchema),
});

export const VersionSummarySchema = z.object({
  version: z.number().int(),
  state: z.string(),
  content_hash: z.string(),
  policy_receipt_hash: z.string().nullable(),
  amount_minor: z.number().int(),
  currency: z.string(),
  created_at: z.string(),
  approval: ApprovalRecordSchema.nullable(),
});

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

export const CheckoutSchema = z.object({
  checkout_id: z.string(),
  cart_id: z.string(),
  state: z.string(),
  current_version: z.number().int(),
  versions: z.array(VersionSummarySchema),
  approval_card: ApprovalCardSchema.nullable(),
  attempt: AttemptSchema.nullable(),
  order_id: z.string().nullable(),
  /**
   * The order's spoken name, once there is an order.
   *
   * Optional on the wire, not because the server is unsure but because this field only
   * ever decorates a sentence. Required, it took down the whole checkout read: an API
   * serving code from before the field existed answered without it, the strict parse
   * failed, and the buyer got "this checkout could not be read" on the approval screen --
   * the one screen where a display-only field must never be able to stop anything.
   */
  order_reference: z.string().nullable().optional().default(null),
  deltas: z.array(DeltaSchema),
  cancellable: z.boolean(),
  updated_at: z.string(),
});

/**
 * Identity of one immutable checkout version: the unit an approval binds to.
 *
 * `content_hash` is the whole point of the shape -- a decision names the exact bytes it
 * ruled on, so a refusal can be shown against the hash the buyer approved rather than
 * against a checkout id that has since moved on. The server never sends a state here;
 * the checkout's own state is read from the checkout.
 */
export const CheckoutRefSchema = z.object({
  checkout_id: z.string(),
  version: z.number().int(),
  // Null on exactly one path: the duplicate-submission answer names the winning attempt's
  // version without re-reading the bytes that version was hashed from.
  content_hash: z.string().nullable(),
}).loose();

/**
 * The kernel's verdict, verbatim. Delivered with HTTP 200 whether or not it allowed the
 * action, so a caller must branch on `allowed` and never on the status code.
 */
export const DecisionSchema = z.object({
  decision_id: z.string(),
  allowed: z.boolean(),
  code: z.string(),
  explanation: z.string(),
  checkout: CheckoutRefSchema.nullable(),
  deltas: z.array(DeltaSchema),
  grant_id: z.string().nullable(),
  payment_attempt_id: z.string().nullable(),
  next_version: z.number().int().nullable(),
  correlation_id: z.string().nullable(),
});

export const PaymentHandoffSchema = z.object({
  checkout_id: z.string(),
  version: z.number().int(),
  // Null until the kernel has admitted a submission and opened an attempt. A handoff read
  // before that is the honest statement that no attempt exists yet, not a malformed reply.
  attempt_id: z.string().nullable(),
  state: z.string().nullable(),
  provider: z.string(),
  razorpay_key_id: z.string().nullable(),
  razorpay_order_id: z.string().nullable(),
  amount_minor: z.number().int(),
  currency: z.string(),
  merchant_name: z.string(),
  description: z.string(),
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

/**
 * Where a sale's seconds went, in four spans the server measured.
 *
 * Not a decomposition. Each span is independently nullable and they are **not** made to
 * sum to `duration_seconds`: a span whose ends cannot both be read is null rather than
 * folded into a neighbour, because attributing unexplained time to the queue or to the
 * buyer would invent the one thing these numbers exist to settle.
 *
 * `paying_seconds` is the only span Razorpay can see, and even their clock starts inside
 * it. Everything above that line is invisible to the provider.
 */
export const OrderTimingSchema = z.object({
  /** Version 1 frozen to the decision recorded: the person at the approval card. */
  deciding_seconds: z.number().int().nullable(),
  /** Decision recorded to Execution Grant issued: the kernel revalidating and authorising. */
  admitting_seconds: z.number().int().nullable(),
  /** Grant issued to grant consumed: the command waiting in the outbox for a worker. */
  queued_seconds: z.number().int().nullable(),
  /** Grant consumed to order written: the provider, the sheet, and the webhook back. */
  paying_seconds: z.number().int().nullable(),
});

export const OrderSchema = z.object({
  order_id: z.string(),
  /**
   * The order said out loud: `RS-260907-K7M4QX2`. Derived by the server from `order_id`.
   *
   * Falls back to the id when an API has not got the field yet, so a name for the order is
   * never the reason an order fails to load.
   */
  reference: z.string().optional(),
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
  /**
   * Whole seconds from the kernel's first freeze to the confirmed order, or null where
   * the opening version could not be read.
   *
   * The clock starts at version 1 -- the quote frozen, the hash minted, the stock held --
   * not at the cart, which is browsing, and not at the approval, which is already most of
   * the way through. Both ends are stamped and subtracted by the database.
   *
   * Optional on the wire: a client reading an older server renders nothing rather than
   * inventing a figure, and null means "not measured", never "instant".
   */
  duration_seconds: z.number().int().nullable().optional(),
  /**
   * The same span in four parts. Optional and defaulted to all-null, so a client reading
   * an older server renders no breakdown rather than a row of zeroes.
   */
  timing: OrderTimingSchema.optional().default({
    deciding_seconds: null,
    admitting_seconds: null,
    queued_seconds: null,
    paying_seconds: null,
  }),
});

/**
 * What `POST /v1/orders/{order_id}/refunds` answers, admitted or denied.
 *
 * HTTP **200 either way** (ADR 0003 D15), so `decision.allowed` is the only field that
 * says which. `refund` is null on a denial and `decision.explanation` says why -- and the
 * three commonest denials are ordinary platform behaviour rather than faults: a refund is
 * already in flight on this attempt, the amount asked for exceeds what is still
 * refundable, or the attempt's outcome is unknown and only reconciliation may speak.
 *
 * `order` is the order **re-read inside the same transaction that admitted the refund**,
 * which is why nothing that renders this result needs to fetch the order again: an
 * admitted refund has already moved the attempt to `REFUND_PENDING`, and this field is
 * the server's own account of that, not the client's guess at it.
 */
export const RefundResultSchema = z.object({
  decision: DecisionSchema,
  refund: RefundSchema.nullable(),
  order: OrderSchema,
});

/**
 * What `GET /v1/orders/{order_id}/refundable` answers: one figure, from the ledger.
 *
 * One, and deliberately only one. The captured total and the amounts held by refunds
 * already in flight are both known on the server and neither is sent, because handing a
 * browser both operands of a subtraction is handing it the subtraction -- and a browser
 * that can do the arithmetic will eventually do it differently from the kernel. The number
 * shown to a buyer before they confirm has to be the number the kernel will act on.
 *
 * `anything_remains` is the server saying "nothing is left" rather than this client
 * inferring it from a zero, which matters because zero has three causes here -- never
 * captured, fully refunded, or a refund in flight holding the balance -- and a surface
 * that derives the fact will sooner or later derive it wrongly.
 */
export const RefundableSchema = z.object({
  order_id: z.string(),
  refundable_minor: z.number().int(),
  currency: z.string(),
  refundable: MoneySchema,
  anything_remains: z.boolean(),
});

export const OrderSummarySchema = z.object({
  order_id: z.string(),
  /**
   * The order said out loud: `RS-260907-K7M4QX2`. Derived by the server from `order_id`.
   *
   * Falls back to the id when an API has not got the field yet, so a name for the order is
   * never the reason an order fails to load.
   */
  reference: z.string().optional(),
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
  /**
   * Whole seconds from the kernel's first freeze to the confirmed order, or null where
   * the opening version could not be read.
   *
   * The clock starts at version 1 -- the quote frozen, the hash minted, the stock held --
   * not at the cart, which is browsing, and not at the approval, which is already most of
   * the way through. Both ends are stamped and subtracted by the database.
   *
   * Optional on the wire: a client reading an older server renders nothing rather than
   * inventing a figure, and null means "not measured", never "instant".
   */
  duration_seconds: z.number().int().nullable().optional(),
  /**
   * Whether the terms THIS order was sold under still let the buyer send goods back.
   *
   * Resolved by the server from the Policy-at-Sale Receipt, never from the shop's current
   * policy, so it stays true for an old order after the merchant changes their mind.
   * Optional on the wire and defaulted to false: a client that cannot tell must not grow
   * a control offering something nobody promised.
   */
  return_offered: z.boolean().optional().default(false),
  /** When that window closes, or null where none is stated or none is offered. */
  return_closes_at: z.string().nullable().optional(),
});

export const OrdersPageSchema = z.object({
  orders: z.array(OrderSummarySchema),
  next_cursor: z.string().nullable(),
  limit: z.number().int(),
  scope: z.string(),
  counts: z.record(z.string(), z.number().int()),
});

/** One tool the agent actually called this turn. The chips in the panel come from here. */
export const ToolCallSchema = z.object({
  name: z.string(),
  summary: z.string(),
  ok: z.boolean(),
}).loose();

/** A capability the agent asked for and was refused. Rendered as the system working. */
export const DenialSchema = z.object({
  capability: z.string(),
  reason_key: z.string(),
}).loose();

export const TurnSchema = z.object({
  reply: z.string(),
  language: z.string(),
  specialist: z.string(),
  routing_reason: z.string(),
  principal_id: z.string(),
  tool_calls: z.array(ToolCallSchema),
  denials: z.array(DenialSchema),
  structured: z.unknown().nullable(),
});

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
  razorpay: z.unknown(),
  database: z.unknown(),
  safe_mode: z.unknown(),
  scenario_routes_enabled: z.boolean(),
  demo_routes_enabled: z.boolean(),
  degraded: z.unknown(),
}).loose();

/**
 * What `POST .../approve`, `.../reject` and `.../cancel` answer with. Probed against the
 * running API rather than read off the OpenAPI document, which declares these routes as
 * raw responses and so carries no schema for them.
 *
 * The three bodies are siblings, not one shape. Approve sends `{checkout, approval,
 * state}`; reject sends `state` and the invalidation it performed but no `approval`;
 * cancel sends the kernel's verdict -- `allowed`, `code`, `explanation` -- and no `state`
 * at all, because a refused cancellation did not move the checkout anywhere. Everything
 * not common to all three is therefore optional, and `checkout` is the only field a
 * caller may count on besides the identity of the route it called.
 */
export const ApprovalResultSchema = z.object({
  checkout: CheckoutRefSchema.nullable(),
  state: z.string().optional(),
  approval: ApprovalRecordSchema.nullable().optional(),
  allowed: z.boolean().optional(),
  code: z.string().optional(),
  explanation: z.string().nullable().optional(),
  from_state: z.string().nullable().optional(),
}).loose();

/**
 * The kernel's answer to a submission. A superset of `DecisionOut`: the route adds the
 * outbox command it enqueued and the attempt it opened.
 *
 * `allowed: false` arrives with HTTP **200**, carrying `code: "REAPPROVAL_REQUIRED"`,
 * `explanation: "merchant_state_changed_since_approval"`, the deltas, `next_version` and
 * the superseding version's own `approval_card`. That is the refusal the whole submission
 * turns on, and it is a normal return value.
 *
 * Which siblings the route adds depends on what it decided, so they are optional here
 * rather than merely nullable. An admitted submission enqueued a command and moved the
 * version, so it sends `command_id` and `state`; a refused one created neither and sends
 * neither, and declaring them required would make the refusal -- the one answer this
 * storefront exists to render -- fail at the boundary as a malformed reply. A submission
 * that lost a race to a live attempt reports the winner and sends a null `decision_id`,
 * because no new decision was taken.
 */
export const SubmitResultSchema = z.object({
  decision_id: z.string().nullable(),
  allowed: z.boolean(),
  code: z.string(),
  outcome: z.string().nullable(),
  explanation: z.string().nullable(),
  checkout: CheckoutRefSchema.nullable(),
  deltas: z.array(DeltaSchema),
  grant_id: z.string().nullable(),
  attempt_id: z.string().nullable(),
  payment_attempt_id: z.string().nullable(),
  next_version: z.number().int().nullable(),
  correlation_id: z.string().nullable(),
  state: z.string().nullable().optional(),
  command_id: z.string().nullable().optional(),
  approval_card: ApprovalCardSchema.optional(),
}).loose();

/**
 * The client's return from `/v1/payments/verify`. `evidence_kind` is never
 * `BROWSER_CALLBACK` applied as capture: the browser's return is recorded, and capture is
 * applied only from a webhook or a provider fetch (ADR 0003 D8).
 */
export const VerifyResultSchema = z.object({
  accepted: z.boolean(),
  attempt_id: z.string().nullable(),
  state: z.string().nullable(),
  evidence_kind: z.string().nullable(),
  message: z.string().nullable(),
}).loose();

export type ApprovalResult = z.infer<typeof ApprovalResultSchema>;
export type SubmitResult = z.infer<typeof SubmitResultSchema>;
export type VerifyResult = z.infer<typeof VerifyResultSchema>;

export type Money = z.infer<typeof MoneySchema>;
export type Product = z.infer<typeof ProductSchema>;
export type SearchHit = z.infer<typeof SearchHitSchema>;
export type SearchResponse = z.infer<typeof SearchResponseSchema>;
export type CataloguePage = z.infer<typeof CataloguePageSchema>;
export type Quote = z.infer<typeof QuoteSchema>;
export type QuoteLine = z.infer<typeof QuoteLineSchema>;
export type Cart = z.infer<typeof CartSchema>;

/**
 * The three figures a RazorAI line proposal was prepared against. They ride back with the
 * write that confirms it, so the server can refuse a proposal the world has moved past
 * (409 `proposal_superseded`) instead of applying it at a price, onto a cart, or against
 * a catalogue the buyer never saw. `basket_content_hash` is null for a cart that held
 * nothing when the proposal was made -- a claim, not an absence.
 */
export const ExpectedCartSchema = z.object({
  basket_content_hash: z.string().nullable(),
  unit_price_minor: z.number().int(),
  catalogue_revision: z.number().int(),
});

/**
 * What ``approve-and-pay`` answers: admission's own body, with the approval beside it.
 *
 * The same shape ``submit`` returns, because it IS a submit -- the approval simply happened
 * inside the same transaction rather than in a request before it. So a refusal arrives here
 * exactly as it arrives there: HTTP 200, ``decision.allowed`` false, the deltas and the next
 * version. A price that moved between the card being drawn and the buyer pressing is a
 * normal outcome of this call, not an error.
 */
export const ApproveAndPayResultSchema = SubmitResultSchema.extend({
  approval: z.unknown().nullable(),
});

/**
 * What ``hold`` answers: the buyer was asked and said not now, and nothing else happened.
 *
 * There is deliberately no decision here and no new state. The version stays where it was,
 * the reservation is kept, and the only thing that changed is that the audit stream now
 * records the question having been put.
 */
export const HoldResultSchema = z.object({
  checkout: CheckoutRefSchema,
  state: z.string(),
  reason: z.string(),
  held_at: z.string(),
  audit_event_id: z.string(),
  reservation: ReservationSchema.nullable(),
});

export type ExpectedCart = z.infer<typeof ExpectedCartSchema>;
export type CartLine = z.infer<typeof CartLineSchema>;
export type Unavailability = z.infer<typeof UnavailabilitySchema>;
export type CheckoutRef = z.infer<typeof CheckoutRefSchema>;
export type Delta = z.infer<typeof DeltaSchema>;
export type ApprovalCard = z.infer<typeof ApprovalCardSchema>;
export type VersionSummary = z.infer<typeof VersionSummarySchema>;
export type Attempt = z.infer<typeof AttemptSchema>;
export type Checkout = z.infer<typeof CheckoutSchema>;
export type Decision = z.infer<typeof DecisionSchema>;
export type PaymentHandoff = z.infer<typeof PaymentHandoffSchema>;
export type Order = z.infer<typeof OrderSchema>;
export type OrderSummary = z.infer<typeof OrderSummarySchema>;
export type OrdersPage = z.infer<typeof OrdersPageSchema>;
export type Refund = z.infer<typeof RefundSchema>;
export type RefundResult = z.infer<typeof RefundResultSchema>;
export type Refundable = z.infer<typeof RefundableSchema>;
export type ApproveAndPayResult = z.infer<typeof ApproveAndPayResultSchema>;
export type HoldResult = z.infer<typeof HoldResultSchema>;
export type Turn = z.infer<typeof TurnSchema>;
export type ToolCall = z.infer<typeof ToolCallSchema>;
export type Denial = z.infer<typeof DenialSchema>;
export type Session = z.infer<typeof SessionSchema>;
export type RuntimeConfig = z.infer<typeof RuntimeConfigSchema>;

/**
 * Every state a checkout version can hold, in the order `CheckoutState` declares them
 * (`packages/transaction-kernel/src/transaction_kernel/states.py`, specification 10.4).
 *
 * This list is the storefront's copy of a vocabulary the kernel owns, and it is a copy
 * only because TypeScript cannot import a Python enum. It is not maintained by hand:
 * `packages/commerce-api/tests/test_capi_storefront_states.py` reads this file, compares
 * it against `CheckoutState` in both directions and names any state that differs.
 *
 * It previously cited specification 8.2 and held sixteen strings, and that citation is
 * what made it look authoritative. 8.2 lists sixteen *journey stages in prose* --
 * "Searching", "Availability checked", "Revalidating", "Reconciliation", "Stale capture
 * and automatic refund" -- which describe what the interface must convey across the whole
 * journey and span three different machines: checkout state, payment state and refund
 * state. Six of those stages had been written down here as checkout states and are not.
 * `SUBMITTED`, `RECONCILING` and `STALE_CAPTURE` are `PaymentState` members: a payment
 * attempt is submitted, reconciling or stale; a checkout never is. `PAYMENT_PENDING`,
 * `REJECTED` and `FAILED` are no state at all -- declining a version answers
 * `{"state": "CANCELLED"}`, and the kernel's names for the other two are
 * `AWAITING_PAYMENT` and `PAYMENT_FAILED`. Meanwhile four real states were missing, among
 * them the one every paying buyer passes through. The number sixteen was a coincidence
 * worth nothing; the vocabulary is fourteen.
 *
 * Listed as a constant rather than a union of string literals because the server owns the
 * vocabulary: an unknown state must render as itself, not crash the page.
 */
export const CHECKOUT_STATES = [
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
] as const;

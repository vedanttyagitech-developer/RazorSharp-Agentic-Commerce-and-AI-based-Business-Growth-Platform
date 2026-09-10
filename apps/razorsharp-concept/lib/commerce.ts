// The typed commerce client.
//
// Every shape here was read off the backend's own models and handlers, not inferred from a
// sample response. Where a route has no `response_model` the shape came from the service
// function that builds the dict; those are marked.
//
// Three things about this API decide the whole design of this file:
//
//  1. **Money is integer minor units.** `Money.minor` is authoritative and `display` is a
//     rendering. Nothing here does arithmetic on an amount: the backend totals a bill and
//     the UI prints what it is given. A frontend that adds up lines will eventually show a
//     number the buyer did not approve.
//
//  2. **A kernel denial is HTTP 200.** `POST .../submit` and `approve-and-pay` answer 200
//     with `allowed: false` and a `code` when the Kernel refuses. Treating non-2xx as the
//     only failure silently reads a refusal as a success, so `admitted()` below forces the
//     caller to look at `allowed`.
//
//  3. **A retry must carry the key of the request it is retrying.** `idempotent()` binds
//     one key to one logical mutation so a retry replays the original answer instead of
//     executing twice. A new key is a new action, and that is a decision, never a default.

export class CommerceError extends Error {
  constructor(
    readonly status: number,
    readonly title: string,
    readonly detail: string,
    readonly code?: string,
    readonly problem?: Problem,
  ) {
    super(detail || title);
    this.name = 'CommerceError';
  }

  /** The backend is unreachable or refused to answer. Nothing was charged. */
  get unreachable(): boolean {
    return this.status === 503 || this.status === 0;
  }
}

/** RFC 9457. A 422 carries its field failures under `errors`, never under `detail`. */
export type Problem = {
  type?: string;
  title: string;
  status: number;
  detail?: string | null;
  instance?: string | null;
  code?: string;
  errors?: { type: string; loc: (string | number)[]; msg: string; input?: unknown }[];
  [extension: string]: unknown;
};

export type Money = { minor: number; currency: string; display: string };

export type Freshness = { source: string; catalogue_revision: number; observed_at: string };

export type QuoteLine = {
  sku: string;
  name: string;
  quantity: number;
  unit_price_minor: number;
  subtotal_minor: number;
  tax_bp: number;
  tax_minor: number;
};

/**
 * One priced bill. Verified against live responses on 9 Sep 2026, and the two producers
 * differ in one way worth knowing: the cart's quote carries `total` as `Money`, while the
 * approval card's quote carries `total_minor` as an integer. Both are optional here and
 * `totalMinor()` below reads whichever is present, so a caller never has to know which
 * endpoint the bill came from.
 */
export type Quote = {
  currency: string;
  lines: QuoteLine[];
  items_subtotal_minor: number;
  items_tax_minor: number;
  delivery_fee_minor: number;
  delivery_tax_minor: number;
  discount_minor: number;
  /** Present on an approval card's quote. */
  total_minor?: number;
  /** Present on a cart's quote. */
  total?: Money;
  free_delivery_applied: boolean;
  gap_to_free_delivery_minor: number;
  /** A running Merchant Policy offer, when one applies. */
  offer_label?: string | null;
  offer_valid_till?: string | null;
  content_hash?: string;
  catalogue_revision?: number;
  source?: string;
};

/** The bill's total in minor units, whichever producer built it. Never computed. */
export function totalMinor(quote: Quote): number {
  if (typeof quote.total_minor === 'number') return quote.total_minor;
  if (quote.total) return quote.total.minor;
  throw new CommerceError(
    0,
    'Bill has no total',
    'A quote arrived with neither total_minor nor total. The frontend must not add up the ' +
      'lines to cover for it: the buyer would approve a number the backend never produced.',
  );
}

export type CartLine = { sku: string; quantity: number };

export type Cart = {
  cart_id: string;
  sales_event_id?: string | null;
  lines: CartLine[];
  quote: Quote | null;
  unavailable: { sku: string; reason?: string }[];
  code: string;
  stale: boolean;
  freshness: Freshness | null;
};

export type Reservation = {
  reservation_id: string;
  state: 'ACTIVE' | 'CONSUMED' | 'RELEASED' | 'EXPIRED';
  expires_at: string;
};

/**
 * What the buyer is asked to approve. `content_hash`, `amount_minor` and `currency` are the
 * binding: they are echoed back on approval and the Kernel compares them against the locked
 * version, so a bill that moved underneath the buyer cannot be approved by accident.
 *
 * `expires_at` is the RESERVATION's expiry, not the approval's.
 */
export type ApprovalCard = {
  checkout_id: string;
  version: number;
  content_hash: string;
  policy_receipt_id: string;
  policy_receipt_hash: string;
  amount_minor: number;
  currency: string;
  total: Money;
  expires_at: string | null;
  reservation: Reservation | null;
  quote: Quote;
  previous_version: number | null;
  deltas: Delta[];
};

/**
 * One material difference the kernel found between the approved document and the live one.
 *
 * `field_path` is `total`, `subtotal_minor`, or `lines[<SKU>].<key>` -- brackets around the
 * SKU, not dots. `reason` is the kernel's own name for what the difference *is*
 * (`item_unavailable` rather than "quantity changed") and is the thing worth rendering;
 * it comes from a closed set in `transaction_kernel.material`.
 */
export type Delta = {
  field_path: string;
  approved: unknown;
  current: unknown;
  reason?: string;
};

/**
 * The checkout read model, narrowed to what a payment in progress has to watch.
 *
 * `order_id` is the fact that matters and it is written only from verified capture
 * evidence -- a WEBHOOK or a provider fetch, never from the browser's return. So this is
 * what "confirmed" means here, and nothing on the client may declare it sooner.
 */
export type CheckoutView = {
  checkout_id: string;
  cart_id: string;
  state: string;
  current_version: number;
  order_id: string | null;
  order_reference: string | null;
  cancellable: boolean;
  updated_at: string;
  attempt: { attempt_id: string; state: string; razorpay_order_id: string | null; payment_window_expires_at?: string | null; server_now?: string | null; window_closed?: boolean; } | null;
  approval_card: ApprovalCard | null;
};

/**
 * One row of the buyer's own checkout list: enough to find a checkout again.
 *
 * `live` is answered by the server from the Kernel's own set of non-terminal states. The
 * client does not decide it: a copy of that set living here would drift the first time a
 * state was added, and the drift would show up as a checkout the buyer could not reach.
 */
export type CheckoutSummary = {
  checkout_id: string;
  cart_id: string;
  state: string;
  version: number;
  live: boolean;
  amount_minor: number | null;
  currency: string | null;
  amount: Money | null;
  content_hash: string | null;
  created_at: string;
  updated_at: string;
  age_seconds: number;
};

export type CheckoutsPage = {
  checkouts: CheckoutSummary[];
  next_cursor: string | null;
  limit: number;
  scope: string;
  counts: Record<string, number>;
};

/**
 * A page of the merchant's catalogue.
 *
 * The list is under `products`, not `items`. This type said `items` until 2026-09-09 and
 * nothing noticed, because nothing called the method -- the shop was rendering eight
 * products written down in the front end instead. A shape nobody exercises is a shape
 * nobody has checked.
 */
export type CataloguePage = {
  products: ProductCard[];
  next_cursor: string | null;
  limit: number;
  /** How many products the filter matched, before this page was cut from them. */
  matched: number;
  counts_by_category: Record<string, number>;
  /** The catalogue revision these rows were read at. */
  revision: number;
};

/** The payment handoff: what the browser needs to open Razorpay Checkout. */
export type PaymentHandoff = {
  payment_window_expires_at?: string | null; server_now?: string | null; window_closed?: boolean;
  checkout_id: string;
  version: number;
  attempt_id: string | null;
  state: string | null;
  /** The PUBLIC key id. `rzp_test_...` in test mode. The secret never leaves the server. */
  razorpay_key_id: string | null;
  /** Null until the worker has created the order under its Execution Grant. */
  razorpay_order_id: string | null;
  amount_minor: number;
  currency: string;
  merchant_name: string | null;
  description: string | null;
};

/**
 * What the backend made of the browser's report. `accepted` says the callback was recorded,
 * NOT that the money moved: `evidence_kind` is always BROWSER_CALLBACK and ADR 0003 D8 puts
 * capture behind WEBHOOK or PROVIDER_FETCH evidence alone.
 */
export type VerifyResult = {
  accepted: boolean;
  attempt_id: string | null;
  state: string;
  evidence_kind: 'BROWSER_CALLBACK';
  message: string;
};

/**
 * The Kernel's answer. **This arrives with HTTP 200 whether or not it allowed the action.**
 * `allowed: false` with `code: "REAPPROVAL_REQUIRED"` means the bill moved and `next_version`
 * names the successor the buyer must approve instead.
 */
export type Decision = {
  decision_id: string;
  allowed: boolean;
  code: string;
  explanation: string;
  checkout: { checkout_id: string; version: number; content_hash: string };
  deltas: Delta[];
  next_version: number | null;
  correlation_id: string;
  grant_id?: string | null;
  payment_attempt_id?: string | null;
  attempt_id?: string | null;
  command_id?: string | null;
  outcome?: string;
  state?: string;
  approval_card?: ApprovalCard | null;
  provider_mode?: string;
};

/**
 * Where a sale's seconds went, in the four spans the backend measures.
 *
 * **Not a decomposition. They will not always add up to `duration_seconds`.** A span whose
 * two ends cannot both be read is `null` rather than folded into a neighbour, because
 * attributing unexplained time to the queue or to the buyer would invent the one thing the
 * breakdown exists to establish. Every field is independently nullable and a `null` means
 * *unmeasured*, never zero.
 *
 * Both ends of every span are stamped by the database and subtracted by the database, so an
 * API process with a skewed clock cannot report a sale as faster than it was.
 */
export type OrderTiming = {
  /** Version 1 frozen → the buyer's decision recorded: the person at the approval card. */
  deciding_seconds: number | null;
  /** Decision recorded → Execution Grant issued: the kernel revalidating and minting authority. */
  admitting_seconds: number | null;
  /** Grant issued → grant consumed: the command waiting in the outbox for a worker. */
  queued_seconds: number | null;
  /** Grant consumed → order written from capture evidence. The only span Razorpay can see. */
  paying_seconds: number | null;
};

/** A confirmed sale, bound to the exact policy and bytes the buyer approved. */
export type Order = {
  order_id: string;
  /** The order id said out loud: `RS-260909-XW5G26M`. Derived, never stored. */
  reference: string;
  checkout_id: string;
  version: number;
  content_hash: string;
  policy_receipt_hash: string;
  state: string;
  amount_minor: number;
  currency: string;
  amount: Money;
  quote: Quote | null;
  payment: { attempt_id: string; state: string; razorpay_order_id: string | null; razorpay_payment_id: string | null; capture_evidence: {kind:string;reference:string;verified_at:string} | null } | null;
  refunds: {refund_id:string;amount_minor:number;currency:string;state:string;reason:string;created_at:string}[];
  created_at: string;
  /**
   * Whole seconds from the kernel's first freeze to the confirmed order, or `null` where
   * the opening version cannot be read.
   *
   * The transaction starts at version 1, not at the cart: a cart is browsing. Razorpay's own
   * clock starts at `POST /v1/orders`, which is sent only after the buyer approves, so the
   * provider can time a payment and never a transaction. The half they cannot see is most
   * of it, which is why this number is the platform's to report.
   */
  duration_seconds: number | null;
  /** Always present; every field inside it may be null. */
  timing: OrderTiming;
};

export type OrderSummary = {
  order_id: string;
  reference: string;
  checkout_id: string;
  version: number;
  state: string;
  amount_minor: number;
  currency: string;
  amount: Money;
  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;
  refunded_minor: number;
  refund_count: number;
  created_at: string;
  age_seconds: number;
  duration_seconds: number | null;
};

export type OrdersPage = {
  orders: OrderSummary[];
  next_cursor: string | null;
  limit: number;
  scope: string;
  counts: Record<string, number>;
};

/** A catalogue hit, verified against a live `catalogue/search` response. */
export type ProductCard = {
  sku: string;
  display_name: string;
  name_en: string;
  name_hi: string;
  category: string;
  unit_label: string;
  unit_price_minor: number;
  unit_price: Money;
  currency: string;
  tax_bp: number;
  delivery_promise_days: number;
  stock_units: number;
  is_listed: boolean;
  is_available: boolean;
  freshness: Freshness;
};

/**
 * A search hit is a catalogue product plus its ranking. `score` is a rank, never a
 * percentage or a confidence, and `matched_terms` is not deduplicated -- one query token can
 * appear twice when a phrase bonus also fires.
 */
export type SearchHit = ProductCard & { score: number; matched_terms: string[] };

export type SearchResults = {
  query: string;
  /** The server's normalized form of `q`. Still contains stopwords; may be "". */
  normalized_query: string;
  /** The RESOLVED locale, not the raw parameter. */
  locale: 'en-IN' | 'hi-IN' | 'hi-Latn-IN';
  /** Ordered: score desc, then in-stock first, then sku ascending. */
  hits: SearchHit[];
  /**
   * The closed grounding list (specification 20.1): identical to `hits.map(h => h.sku)`.
   * A SKU outside it is one no tool returned, and nothing downstream -- a cart write, a
   * copilot sentence -- may name one. Out-of-stock and delisted products ARE returned and
   * ranked below sellable ones, so presence here is not a claim that something is buyable;
   * `is_available` is.
   */
  skus: string[];
  freshness: Freshness;
};

// ---------------------------------------------------------------------------- transport

const BRIDGE = '/api/commerce/';

type Options = {
  method?: 'GET' | 'POST' | 'PUT';
  body?: unknown;
  /** The stable key for ONE logical mutation. A retry of that mutation reuses it. */
  idempotencyKey?: string;
  signal?: AbortSignal;
  query?: Record<string, string | number | undefined>;
};

// Coalesce initial reads so catalogue and cart cannot mint different demo buyers.
let buyerBootstrap: Promise<void> | null = null;
export async function ensureBuyerSession(): Promise<void> {
  if (!buyerBootstrap) buyerBootstrap = fetch(BRIDGE + 'carts/current', {credentials:'same-origin'})
    .then(response => { if (!response.ok) throw new CommerceError(response.status, 'Session unavailable', 'Could not restore your shopping session. Please retry.'); })
    .catch(error => { buyerBootstrap = null; throw error; });
  return buyerBootstrap;
}
async function call<T>(path: string, options: Options = {}): Promise<T> {
  if (typeof window !== 'undefined') await ensureBuyerSession();
  const { method = 'GET', body, idempotencyKey, signal, query } = options;
  const search = query
    ? '?' +
      new URLSearchParams(
        Object.entries(query)
          .filter(([, v]) => v !== undefined && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ).toString()
    : '';

  const headers: Record<string, string> = {};
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (!idempotencyKey)
      throw new CommerceError(
        0,
        'Missing idempotency key',
        `A ${method} to ${path} needs a stable Idempotency-Key. Generating one per attempt ` +
          'would execute a retried mutation twice.',
      );
    headers['Idempotency-Key'] = idempotencyKey;
  }

  let response: Response;
  try {
    response = await fetch(BRIDGE + path + search, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    if ((cause as Error)?.name === 'AbortError') throw cause;
    throw new CommerceError(
      0,
      'The store is unreachable',
      'No response from the commerce backend. A submitted action may still have completed; check its status before retrying.',
    );
  }

  const text = await response.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }

  if (!response.ok) {
    const problem = (parsed ?? {}) as Problem;
    throw new CommerceError(
      response.status,
      problem.title || `HTTP ${response.status}`,
      problem.detail || problem.title || 'The store refused this request.',
      typeof problem.code === 'string' ? problem.code : undefined,
      problem,
    );
  }
  return parsed as T;
}

/**
 * A stable key for one logical mutation, kept for its retries.
 *
 * `crypto.randomUUID()` at the call site is the mistake this exists to prevent: the retry
 * then carries a different key and the backend, correctly, treats it as a second action.
 */
export function idempotencyKey(): string {
  return crypto.randomUUID();
}

/**
 * Read a Kernel answer honestly. Returns the decision when it allowed the action, and
 * throws a typed refusal when it did not -- because the alternative is a caller that checks
 * `response.ok`, sees 200, and reports a refusal as a success.
 */
export class KernelRefusal extends Error {
  constructor(readonly decision: Decision) {
    super(decision.explanation || decision.code);
    this.name = 'KernelRefusal';
  }
  get code(): string {
    return this.decision.code;
  }
  get needsReapproval(): boolean {
    return this.decision.code === 'REAPPROVAL_REQUIRED';
  }
}

export function admitted(decision: Decision): Decision {
  if (!decision.allowed) throw new KernelRefusal(decision);
  return decision;
}

// ------------------------------------------------------------------------------ surface

export const commerce = {
  catalogue: {
    /** `limit` is capped at 50 by the backend; anything outside 1..50 is a 422. */
    search: (query: string, opts: { limit?: number; signal?: AbortSignal } = {}) =>
      call<SearchResults>('catalogue/search', {
        query: { q: query, limit: opts.limit ?? 12 },
        signal: opts.signal,
      }),
    product: (sku: string, signal?: AbortSignal) =>
      call<ProductCard>(`catalogue/products/${encodeURIComponent(sku)}`, { signal }),
    /**
     * A page of the merchant's catalogue. `cursor` is the last SKU of the previous page.
     *
     * The cursor is not optional in practice: the backend caps `limit` at 100 and this
     * shop has 247 products, so a caller that ignores `next_cursor` shows the buyer a
     * shelf that stops without saying so.
     */
    list: (
      opts: {
        limit?: number;
        category?: string;
        cursor?: string | null;
        signal?: AbortSignal;
      } = {},
    ) =>
      call<CataloguePage>('catalogue/products', {
        query: { limit: opts.limit ?? 40, category: opts.category, cursor: opts.cursor ?? undefined },
        signal: opts.signal,
      }),
  },

  cart: {
    create: (key: string, signal?: AbortSignal) =>
      call<Cart>('carts', { method: 'POST', body: {}, idempotencyKey: key, signal }),
    current: (signal?: AbortSignal) => call<{ cart: Cart | null }>('carts/current', { signal }),
    read: (cartId: string, signal?: AbortSignal) => call<Cart>(`carts/${cartId}`, { signal }),
    /** Absolute quantity, never a delta: a retried increment would add twice. 0 removes. */
    setLine: (cartId: string, sku: string, quantity: number, key: string, signal?: AbortSignal, expected?: {basket_content_hash:string|null;unit_price_minor:number;catalogue_revision:number}) =>
      call<Cart>(`carts/${cartId}/lines/${encodeURIComponent(sku)}`, {
        method: 'PUT',
        body: { quantity, ...(expected ? {expected} : {}) },
        idempotencyKey: key,
        signal,
      }),
    checkout: (cartId: string, key: string, signal?: AbortSignal) =>
      call<ApprovalCard>(`carts/${cartId}/checkout`, {
        method: 'POST',
        body: {},
        idempotencyKey: key,
        signal,
      }),
  },

  checkout: {
    /**
     * The buyer's own checkouts, newest first. `live: true` returns the unfinished ones.
     *
     * This is how a surface that has lost its place finds it again -- a reload, a second
     * device, a voice session that never had a browser. It is also how this app avoids
     * opening a second checkout for a purchase it already started: a lost response is not
     * a reason to hold a second stock reservation for one basket.
     */
    list: (
      opts: { live?: boolean; limit?: number; cursor?: string; signal?: AbortSignal } = {},
    ) =>
      call<CheckoutsPage>('checkouts', {
        query: { live: opts.live ? 'true' : undefined, limit: opts.limit ?? 5, cursor: opts.cursor },
        signal: opts.signal,
      }),
    read: (checkoutId: string, signal?: AbortSignal) =>
      call<CheckoutView>(`checkouts/${checkoutId}`, { signal }),
    /** Records consent bound to the exact bill. Returns 200 with a decision either way. */
    approveAndPay: (
      checkoutId: string,
      version: number,
      binding: { content_hash: string; amount_minor: number; currency: string },
      key: string,
      signal?: AbortSignal,
    ) =>
      call<Decision>(`checkouts/${checkoutId}/versions/${version}/approve-and-pay`, {
        method: 'POST',
        body: binding,
        idempotencyKey: key,
        signal,
      }),
    payment: (checkoutId: string, signal?: AbortSignal) =>
      call<PaymentHandoff>(`checkouts/${checkoutId}/payment`, { signal }),
    cancel: (checkoutId: string, key: string, signal?: AbortSignal) =>
      call<Decision>(`checkouts/${checkoutId}/cancel`, {
        method: 'POST',
        body: {},
        idempotencyKey: key,
        signal,
      }),
  },

  payments: {
    reconcile: (checkoutId: string, signal?: AbortSignal) => call<{queued:boolean}>('payments/reconcile', {method:'POST', body:{checkout_id:checkoutId},idempotencyKey:`provider-recovery:${checkoutId}`,signal}),
    /**
     * Report what the browser was handed back. Answers 200 whether or not the Kernel
     * accepted it -- a refusal means a payment id was already recorded and this message
     * disagreed -- so a caller reads `accepted`, never the status code, and in both cases
     * the honest thing to show is "verifying".
     *
     * The key must be the one held for THIS return. A double-clicked "back to store" that
     * mints a fresh key enqueues a second reconciliation for one payment.
     */
    verify: (
      report: {
        checkout_id: string;
        razorpay_order_id: string;
        razorpay_payment_id: string;
        razorpay_signature: string;
      },
      key: string,
      signal?: AbortSignal,
    ) =>
      call<VerifyResult>('payments/verify', {
        method: 'POST',
        body: report,
        idempotencyKey: key,
        signal,
      }),
  },

  orders: {
    /** A page of the buyer's own orders. The key is `orders`, not `items`. */
    list: (opts: { limit?: number; signal?: AbortSignal } = {}) =>
      call<OrdersPage>('orders', { query: { limit: opts.limit ?? 20 }, signal: opts.signal }),
    read: (orderId: string, signal?: AbortSignal) =>
      call<Order>(`orders/${orderId}`, { signal }),
  },
};

export { call as rawCommerceCall };

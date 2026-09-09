/**
 * The typed client. Every call goes to this app's own origin at `/api/backend/...`.
 *
 * The browser never holds the bearer token. It is minted server-side by the route
 * handler in `src/app/api/backend/[...path]/route.ts` and attached there, so a token
 * cannot leak through an extension, a screenshot, or an XSS that gets as far as reading
 * `localStorage`. The client below therefore sends no credential of its own; the cookie
 * that identifies the browser session is `httpOnly` and set by that handler.
 *
 * Two rules this module enforces on behalf of every caller:
 *
 *  1. **A denial is not an error.** `POST .../submit` answers HTTP 200 with a decision
 *     whose `allowed` is false when the kernel refuses a stale approval. `submitVersion`
 *     returns that decision. Nothing here throws on it.
 *  2. **Responses are parsed, not cast.** A shape that does not match the contract fails
 *     at the boundary rather than rendering `NaN` inside an approval card.
 */
import { ApiError, problemFrom, transportProblem, type Problem } from "./problem";
import {
  ApprovalCardSchema,
  ApprovalResultSchema,
  CartSchema,
  CurrentCartSchema,
  CataloguePageSchema,
  CheckoutSchema,
  OrderSchema,
  OrdersPageSchema,
  PaymentHandoffSchema,
  RefundableSchema,
  RefundResultSchema,
  RuntimeConfigSchema,
  SearchResponseSchema,
  SessionSchema,
  SubmitResultSchema,
  TurnSchema,
  VerifyResultSchema,
  type ApprovalCard,
  type ApprovalResult,
  type Cart,
  type ExpectedCart,
  type CataloguePage,
  type Checkout,
  type Order,
  type OrdersPage,
  type PaymentHandoff,
  type Refundable,
  type RefundResult,
  type RuntimeConfig,
  type SearchResponse,
  type Session,
  type SubmitResult,
  type Turn,
  type VerifyResult,
  ApproveAndPayResult,
  ApproveAndPayResultSchema,
  HoldResult,
  HoldResultSchema,
} from "./types";
import type { z } from "zod";

const BASE = "/api/backend";

interface CallOptions {
  method?: string;
  body?: unknown;
  /** Sent as `Idempotency-Key`. Required by every mutation the API accepts. */
  idempotencyKey?: string;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
}

function withQuery(path: string, query: CallOptions["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function call<T>(
  schema: z.ZodType<T>,
  path: string,
  options: CallOptions = {},
): Promise<T> {
  const { method = "GET", body, idempotencyKey, query, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  let response: Response;
  try {
    response = await fetch(`${BASE}${withQuery(path, query)}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(transportProblem(cause));
  }

  if (!response.ok) throw new ApiError(await problemFrom(response));

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (cause) {
    throw new ApiError({
      type: "about:blank",
      title: "The server sent something that is not JSON",
      status: response.status,
      detail: cause instanceof Error ? cause.message : undefined,
    } satisfies Problem);
  }

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError({
      type: "about:blank",
      title: "The server sent a shape this app does not understand",
      status: response.status,
      detail: parsed.error.issues
        .slice(0, 3)
        .map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`)
        .join("; "),
    } satisfies Problem);
  }
  return parsed.data;
}

/** A fresh idempotency key for one mutation. Reused verbatim on a retry of that same call. */
export function newIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export const api = {
  /** Who this browser is. The token itself stays on the server. */
  session: (signal?: AbortSignal): Promise<Session> =>
    call(SessionSchema, "/session", { signal }),

  config: (signal?: AbortSignal): Promise<RuntimeConfig> =>
    call(RuntimeConfigSchema, "/v1/config", { signal }),

  // ------------------------------------------------------------------ discovery

  search: (q: string, opts: { locale?: string; limit?: number; signal?: AbortSignal } = {}): Promise<SearchResponse> =>
    call(SearchResponseSchema, "/v1/catalogue/search", {
      query: { q, locale: opts.locale, limit: opts.limit ?? 20 },
      signal: opts.signal,
    }),

  products: (
    opts: { category?: string; listed?: boolean; available?: boolean; limit?: number; cursor?: string; signal?: AbortSignal } = {},
  ): Promise<CataloguePage> =>
    call(CataloguePageSchema, "/v1/catalogue/products", {
      query: {
        category: opts.category,
        listed: opts.listed,
        available: opts.available,
        limit: opts.limit ?? 50,
        cursor: opts.cursor,
      },
      signal: opts.signal,
    }),

  // -------------------------------------------------------------------- cart

  createCart: (key = newIdempotencyKey()): Promise<Cart> =>
    call(CartSchema, "/v1/carts", { method: "POST", body: {}, idempotencyKey: key }),

  cart: (cartId: string, signal?: AbortSignal): Promise<Cart> =>
    call(CartSchema, `/v1/carts/${encodeURIComponent(cartId)}`, { signal }),

  /**
   * The cart this buyer already has on the server, or `null` if they have none.
   *
   * The cart belongs to the session, not to the browser that started it, and this is the
   * only way to ask which one it is. Without it a cart's identity lives solely in one
   * browser's `localStorage`, so a private window, a second device or cleared site data
   * open on an empty shop over a cart the server is still holding -- and the buyer has no
   * way to reach it, because nothing on the screen knows it exists.
   */
  currentCart: (signal?: AbortSignal): Promise<Cart | null> =>
    call(CurrentCartSchema, "/v1/carts/current", { signal }).then((answer) => answer.cart),

  /**
   * Set one line to an absolute quantity. `0` removes it. Re-quotes on the server.
   *
   * `expected` rides along only when the write confirms a RazorAI proposal: the cart
   * hash, unit price and catalogue revision that proposal was prepared against. The server
   * compares them under the cart's lock and answers 409 `proposal_superseded` if any has
   * moved, leaving the cart untouched. The cart page's own +/- controls send none,
   * because they were pressed against the cart on screen, which the server re-quotes on
   * every write regardless.
   */
  setLine: (
    cartId: string,
    sku: string,
    quantity: number,
    key = newIdempotencyKey(),
    expected?: ExpectedCart,
  ): Promise<Cart> =>
    call(CartSchema, `/v1/carts/${encodeURIComponent(cartId)}/lines/${encodeURIComponent(sku)}`, {
      method: "PUT",
      body: expected === undefined ? { quantity } : { quantity, expected },
      idempotencyKey: key,
    }),

  // ------------------------------------------------------------------ checkout

  /**
   * Open a checkout on a cart. Answers 201 with **version 1's approval card**, not a
   * checkout: the point of the call is to put in front of the buyer the exact bytes,
   * amount and reservation they are being asked to consent to.
   */
  openCheckout: (cartId: string, key = newIdempotencyKey()): Promise<ApprovalCard> =>
    call(ApprovalCardSchema, `/v1/carts/${encodeURIComponent(cartId)}/checkout`, {
      method: "POST",
      body: {},
      idempotencyKey: key,
    }),

  checkout: (checkoutId: string, signal?: AbortSignal): Promise<Checkout> =>
    call(CheckoutSchema, `/v1/checkouts/${encodeURIComponent(checkoutId)}`, { signal }),

  /**
   * The buyer consents to this exact version.
   *
   * Takes the whole card rather than an id and a version, because the request must echo
   * back the `content_hash`, `amount_minor` and `currency` that were on screen. The
   * server compares them against the version it holds, so consent is bound to the bytes
   * the buyer actually saw and a card that has moved underneath them is refused rather
   * than silently approved. Passing the card makes that impossible to get wrong.
   */
  approve: (card: ApprovalCard, key = newIdempotencyKey()): Promise<ApprovalResult> =>
    call(
      ApprovalResultSchema,
      `/v1/checkouts/${encodeURIComponent(card.checkout_id)}/versions/${card.version}/approve`,
      {
        method: "POST",
        body: {
          content_hash: card.content_hash,
          amount_minor: card.amount_minor,
          currency: card.currency,
        },
        idempotencyKey: key,
      },
    ),

  /**
   * One confirmation: record the approval and admit it in the same transaction.
   *
   * This is what the buyer's "Approve to pay" press should call. Approving and submitting as
   * two requests left the version sitting APPROVED with nothing spending it -- a window the
   * kernel has a sweeper for -- and made "one confirmation" a property of the screen rather
   * than of the platform. Here it is one act: APPROVAL_REQUIRED to APPROVED to
   * EXECUTION_PENDING, under one lock, or none of it.
   *
   * **Answers HTTP 200 admitted or refused**, exactly as `submitVersion` does. A price that
   * moved since the card was drawn comes back `allowed: false` with `REAPPROVAL_REQUIRED`,
   * the deltas and the next version. That is the demonstration working, not a fault, and
   * nothing here throws on it.
   */
  approveAndPay: (card: ApprovalCard, key = newIdempotencyKey()): Promise<ApproveAndPayResult> =>
    call(
      ApproveAndPayResultSchema,
      `/v1/checkouts/${encodeURIComponent(card.checkout_id)}/versions/${card.version}/approve-and-pay`,
      {
        method: "POST",
        body: {
          content_hash: card.content_hash,
          amount_minor: card.amount_minor,
          currency: card.currency,
        },
        idempotencyKey: key,
      },
    ),

  /**
   * The buyer was asked and said not now. Audited; nothing else happens.
   *
   * Not a rejection. Rejecting retires the version and hands the stock back, so "let me
   * think about it" used to cost the buyer their cart -- there was no other "no" to give.
   * After this the version is still APPROVAL_REQUIRED against the same hash, the hold is
   * still theirs, and a later yes approves the thing they were already looking at.
   */
  hold: (card: ApprovalCard, reason = "buyer_not_now", key = newIdempotencyKey()): Promise<HoldResult> =>
    call(
      HoldResultSchema,
      `/v1/checkouts/${encodeURIComponent(card.checkout_id)}/versions/${card.version}/hold`,
      {
        method: "POST",
        body: { content_hash: card.content_hash, reason },
        idempotencyKey: key,
      },
    ),

  reject: (card: ApprovalCard, reason = "buyer_declined", key = newIdempotencyKey()): Promise<ApprovalResult> =>
    call(
      ApprovalResultSchema,
      `/v1/checkouts/${encodeURIComponent(card.checkout_id)}/versions/${card.version}/reject`,
      {
        method: "POST",
        body: { content_hash: card.content_hash, reason },
        idempotencyKey: key,
      },
    ),

  /**
   * Kernel admission.
   *
   * Answers HTTP **200** either way. `allowed: false` with
   * `code: "REAPPROVAL_REQUIRED"`, `explanation: "merchant_state_changed_since_approval"`,
   * a populated `deltas` and a `next_version` is the refusal this whole submission turns
   * on -- verified live: approving at ₹721.95, a merchant price change, then this call
   * returning `total: 72195 -> 79895` and version 2 awaiting a fresh approval. It is a
   * normal return value here and must never be thrown.
   */
  submitVersion: (checkoutId: string, version: number, key = newIdempotencyKey()): Promise<SubmitResult> =>
    call(SubmitResultSchema, `/v1/checkouts/${encodeURIComponent(checkoutId)}/versions/${version}/submit`, {
      method: "POST",
      body: {},
      idempotencyKey: key,
    }),

  cancel: (checkoutId: string, reason = "buyer_cancelled", key = newIdempotencyKey()): Promise<ApprovalResult> =>
    call(ApprovalResultSchema, `/v1/checkouts/${encodeURIComponent(checkoutId)}/cancel`, {
      method: "POST",
      body: { reason },
      idempotencyKey: key,
    }),

  // ------------------------------------------------------------------- payment

  paymentHandoff: (checkoutId: string, signal?: AbortSignal): Promise<PaymentHandoff> =>
    call(PaymentHandoffSchema, `/v1/checkouts/${encodeURIComponent(checkoutId)}/payment`, { signal }),

  /**
   * Record that the browser came back from the provider. Never capture evidence
   * (ADR 0003 D8): capture is applied only from a webhook or a provider fetch, so this
   * call moves nothing and the UI must keep polling for the real state.
   */
  verifyPayment: (
    body: { checkout_id: string; razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string },
    key = newIdempotencyKey(),
  ): Promise<VerifyResult> =>
    call(VerifyResultSchema, "/v1/payments/verify", { method: "POST", body, idempotencyKey: key }),

  // -------------------------------------------------------------------- orders

  order: (orderId: string, signal?: AbortSignal): Promise<Order> =>
    call(OrderSchema, `/v1/orders/${encodeURIComponent(orderId)}`, { signal }),

  /**
   * Ask for money back on a confirmed order. **Answers HTTP 200 admitted or denied.**
   *
   * `amount_minor` is omitted for "everything still refundable", and omitted is the only
   * way to ask for that: the figure is resolved by the kernel against its own capture
   * ledger, which can see a refund already in flight at the provider that no arithmetic
   * in this browser could. Sending a computed total instead would be the storefront
   * asserting a number it cannot know, and the kernel would refuse it as `exceeds_remaining`
   * the moment the two disagreed.
   *
   * `reason` is a stable key stored as the refund's `reason_code`, not prose for a human.
   *
   * Nothing here throws on a denial. "A refund is already in flight", "nothing remains to
   * refund" and "this attempt is reconciling" all arrive as `decision.allowed: false` with
   * a `refund` of null, and each is the platform working correctly.
   */
  /**
   * What the kernel says is still refundable on this order, as one figure.
   *
   * Read before the buyer confirms, so the screen states the platform's number instead of
   * inviting the buyer to name one. It is a *reading*, not a promise: between this call and
   * the refund request a webhook can arrive, another refund can be admitted, and the
   * kernel decides again from the ledger at that moment. So a surface may show this and
   * must not send it back as `amount_minor` -- the request that follows still omits the
   * amount and lets the kernel resolve it.
   */
  refundable: (orderId: string, signal?: AbortSignal): Promise<Refundable> =>
    call(RefundableSchema, `/v1/orders/${encodeURIComponent(orderId)}/refundable`, { signal }),

  requestRefund: (
    orderId: string,
    body: { reason: string; amount_minor?: number | null },
    key = newIdempotencyKey(),
  ): Promise<RefundResult> =>
    call(RefundResultSchema, `/v1/orders/${encodeURIComponent(orderId)}/refunds`, {
      method: "POST",
      body: { reason: body.reason, amount_minor: body.amount_minor ?? null },
      idempotencyKey: key,
    }),

  orders: (opts: { status?: string; limit?: number; cursor?: string; signal?: AbortSignal } = {}): Promise<OrdersPage> =>
    call(OrdersPageSchema, "/v1/orders", {
      query: { status: opts.status, limit: opts.limit ?? 25, cursor: opts.cursor },
      signal: opts.signal,
    }),

  // ------------------------------------------------------------------- RazorAI

  /** One turn of the buyer copilot. The agent proposes; it holds no capability to pay. */
  agentTurn: (
    body: { message: string; locale?: string; cart_id?: string; checkout_id?: string; order_id?: string },
    signal?: AbortSignal,
  ): Promise<Turn> => call(TurnSchema, "/v1/agent/turn", { method: "POST", body, signal }),
};

export type Api = typeof api;
export { ApiError };

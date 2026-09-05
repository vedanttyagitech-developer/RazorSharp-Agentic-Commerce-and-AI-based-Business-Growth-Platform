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
  BasketSchema,
  CataloguePageSchema,
  CheckoutSchema,
  OrderSchema,
  OrdersPageSchema,
  PaymentHandoffSchema,
  RuntimeConfigSchema,
  SearchResponseSchema,
  SessionSchema,
  SubmitResultSchema,
  TurnSchema,
  VerifyResultSchema,
  type ApprovalCard,
  type ApprovalResult,
  type Basket,
  type CataloguePage,
  type Checkout,
  type Order,
  type OrdersPage,
  type PaymentHandoff,
  type RuntimeConfig,
  type SearchResponse,
  type Session,
  type SubmitResult,
  type Turn,
  type VerifyResult,
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

  // -------------------------------------------------------------------- basket

  createBasket: (key = newIdempotencyKey()): Promise<Basket> =>
    call(BasketSchema, "/v1/baskets", { method: "POST", body: {}, idempotencyKey: key }),

  basket: (basketId: string, signal?: AbortSignal): Promise<Basket> =>
    call(BasketSchema, `/v1/baskets/${encodeURIComponent(basketId)}`, { signal }),

  /** Set one line to an absolute quantity. `0` removes it. Re-quotes on the server. */
  setLine: (basketId: string, sku: string, quantity: number, key = newIdempotencyKey()): Promise<Basket> =>
    call(BasketSchema, `/v1/baskets/${encodeURIComponent(basketId)}/lines/${encodeURIComponent(sku)}`, {
      method: "PUT",
      body: { quantity },
      idempotencyKey: key,
    }),

  // ------------------------------------------------------------------ checkout

  /**
   * Open a checkout on a basket. Answers 201 with **version 1's approval card**, not a
   * checkout: the point of the call is to put in front of the buyer the exact bytes,
   * amount and reservation they are being asked to consent to.
   */
  openCheckout: (basketId: string, key = newIdempotencyKey()): Promise<ApprovalCard> =>
    call(ApprovalCardSchema, `/v1/baskets/${encodeURIComponent(basketId)}/checkout`, {
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

  orders: (opts: { status?: string; limit?: number; cursor?: string; signal?: AbortSignal } = {}): Promise<OrdersPage> =>
    call(OrdersPageSchema, "/v1/orders", {
      query: { status: opts.status, limit: opts.limit ?? 25, cursor: opts.cursor },
      signal: opts.signal,
    }),

  // ------------------------------------------------------------------- RazorAI

  /** One turn of the buyer copilot. The agent proposes; it holds no capability to pay. */
  agentTurn: (
    body: { message: string; locale?: string; basket_id?: string; checkout_id?: string; order_id?: string },
    signal?: AbortSignal,
  ): Promise<Turn> => call(TurnSchema, "/v1/agent/turn", { method: "POST", body, signal }),
};

export type Api = typeof api;
export { ApiError };

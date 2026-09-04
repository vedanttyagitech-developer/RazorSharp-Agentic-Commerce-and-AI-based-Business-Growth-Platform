/**
 * Typed fetch client for the ADR 0003 endpoint catalogue.
 *
 * Session model: the bearer token lives in an HttpOnly cookie set by /api/session and is
 * attached server-side by the /api/backend/[...path] route handler. Browser code never
 * sees it. The route handler is routing only (spec 21.5); FastAPI verifies the session
 * and tenant on every request.
 *
 * Mutations carry a fresh Idempotency-Key (crypto.randomUUID) per business operation.
 * The same key is reused if the call is retried after a session refresh, so a retry is a
 * replay, never a second operation (ADR D9).
 *
 * Errors: non-2xx responses become ApiError (RFC 9457). Kernel denials are 200 with a
 * structured decision and come back as data (ADR D15).
 */
import { z } from "zod";

import { NetworkError, contractError, parseProblem } from "./problem";
import {
  ApproveResponseSchema,
  BasketSchema,
  CancelResponseSchema,
  CheckoutEventSchema,
  CheckoutSchema,
  OrderSchema,
  PaymentHandoffSchema,
  ProductSchema,
  ProofChainSchema,
  RefundResponseSchema,
  RuntimeConfigSchema,
  SearchResponseSchema,
  SubmitResponseSchema,
  TimelineResponseSchema,
  VerifyResponseSchema,
  type ApprovalEcho,
  type ApproveResponse,
  type Basket,
  type CancelResponse,
  type Checkout,
  type CheckoutEvent,
  type Order,
  type PaymentHandoff,
  type Product,
  type ProofChain,
  type RefundRequest,
  type RefundResponse,
  type RuntimeConfig,
  type SearchResponse,
  type SubmitResponse,
  type TimelineResponse,
  type VerifyRequest,
  type VerifyResponse,
} from "./types";

export type ApiMode = "live" | "mock";

export type EventStreamStatus = "connecting" | "open" | "reconnecting" | "closed" | "unsupported";

export interface EventSubscriptionOptions {
  /** Resume point. EventSource re-sends it as Last-Event-ID on its own reconnects. */
  lastEventId?: string | null;
  onEvent: (event: CheckoutEvent) => void;
  onStatus?: (status: EventStreamStatus) => void;
}

export interface SearchParams {
  q: string;
  locale?: string;
  limit?: number;
}

/** The one interface both the live and the mock client implement. */
export interface CommerceClient {
  readonly mode: ApiMode;

  search(params: SearchParams): Promise<SearchResponse>;
  getProduct(sku: string): Promise<Product>;

  createBasket(): Promise<Basket>;
  setBasketLine(basketId: string, sku: string, quantity: number): Promise<Basket>;
  getBasket(basketId: string): Promise<Basket>;
  checkoutBasket(basketId: string): Promise<Checkout>;

  getCheckout(checkoutId: string): Promise<Checkout>;
  approveVersion(checkoutId: string, version: number, echo: ApprovalEcho): Promise<ApproveResponse>;
  rejectVersion(checkoutId: string, version: number, reason?: string): Promise<Checkout>;
  submitVersion(checkoutId: string, version: number): Promise<SubmitResponse>;
  cancelCheckout(checkoutId: string, reason?: string): Promise<CancelResponse>;

  getPaymentHandoff(checkoutId: string): Promise<PaymentHandoff>;
  verifyPayment(body: VerifyRequest): Promise<VerifyResponse>;

  getOrder(orderId: string): Promise<Order>;
  requestRefund(orderId: string, body: RefundRequest): Promise<RefundResponse>;

  getTimeline(checkoutId: string): Promise<TimelineResponse>;
  subscribeEvents(checkoutId: string, options: EventSubscriptionOptions): () => void;
  getProof(checkoutId: string): Promise<ProofChain>;
  getConfig(): Promise<RuntimeConfig>;

  /** Direct links to inspector JSON. Null when the mode has no HTTP surface (mock). */
  inspectorUrl(attemptId: string): string | null;
  proofUrl(checkoutId: string): string | null;
  auditVerifyUrl(streamType: string, id: string): string | null;
}

/** Same-origin base the browser uses; the route handler attaches the session. */
export const BROWSER_API_BASE = "/api/backend";

export interface LiveClientOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
  /** Called once on 401; return true to retry the call with the same Idempotency-Key. */
  onUnauthorized?: () => Promise<boolean>;
  eventSourceImpl?: typeof EventSource;
  /** Extra headers for server-side use (e.g. Authorization from cookies()). */
  headers?: Record<string, string>;
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

interface CallInit {
  body?: unknown;
  query?: Record<string, string | number | undefined>;
}

function enc(segment: string): string {
  return encodeURIComponent(segment);
}

function buildQuery(query: CallInit["query"]): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export function createLiveClient(options: LiveClientOptions): CommerceClient {
  const baseUrl = options.baseUrl.replace(/\/$/, "");
  const fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));

  async function call<T>(
    schema: z.ZodType<T>,
    method: Method,
    path: string,
    init: CallInit = {},
    idempotencyKey: string | null = method === "GET" ? null : newIdempotencyKey(),
    retried = false,
  ): Promise<T> {
    const url = `${baseUrl}${path}${buildQuery(init.query)}`;
    const headers = new Headers({ Accept: "application/json", ...(options.headers ?? {}) });
    if (init.body !== undefined) headers.set("Content-Type", "application/json");
    if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);

    let response: Response;
    try {
      response = await fetchImpl(url, {
        method,
        headers,
        body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (cause) {
      throw new NetworkError(`Could not reach the commerce API (${method} ${path})`, { cause });
    }

    if (response.status === 401 && !retried && options.onUnauthorized) {
      const refreshed = await options.onUnauthorized();
      if (refreshed) return call(schema, method, path, init, idempotencyKey, true);
    }
    if (!response.ok) throw await parseProblem(response);

    let json: unknown;
    try {
      json = await response.json();
    } catch {
      throw contractError(path, "2xx body was not JSON");
    }
    const parsed = schema.safeParse(json);
    if (!parsed.success) throw contractError(path, z.prettifyError(parsed.error));
    return parsed.data;
  }

  return {
    mode: "live",

    search: (params) =>
      call(SearchResponseSchema, "GET", "/v1/catalogue/search", {
        query: { q: params.q, locale: params.locale, limit: params.limit },
      }),
    getProduct: (sku) => call(ProductSchema, "GET", `/v1/catalogue/products/${enc(sku)}`),

    createBasket: () => call(BasketSchema, "POST", "/v1/baskets", { body: {} }),
    setBasketLine: (basketId, sku, quantity) =>
      call(BasketSchema, "PUT", `/v1/baskets/${enc(basketId)}/lines/${enc(sku)}`, {
        body: { quantity },
      }),
    getBasket: (basketId) => call(BasketSchema, "GET", `/v1/baskets/${enc(basketId)}`),
    checkoutBasket: (basketId) =>
      call(CheckoutSchema, "POST", `/v1/baskets/${enc(basketId)}/checkout`, { body: {} }),

    getCheckout: (checkoutId) => call(CheckoutSchema, "GET", `/v1/checkouts/${enc(checkoutId)}`),
    approveVersion: (checkoutId, version, echo) =>
      call(
        ApproveResponseSchema,
        "POST",
        `/v1/checkouts/${enc(checkoutId)}/versions/${version}/approve`,
        // Exactly the three fields the card displayed. Nothing is merged in.
        { body: { content_hash: echo.content_hash, amount_minor: echo.amount_minor, currency: echo.currency } },
      ),
    rejectVersion: (checkoutId, version, reason) =>
      call(CheckoutSchema, "POST", `/v1/checkouts/${enc(checkoutId)}/versions/${version}/reject`, {
        body: { reason: reason ?? "buyer_rejected" },
      }),
    submitVersion: (checkoutId, version) =>
      call(SubmitResponseSchema, "POST", `/v1/checkouts/${enc(checkoutId)}/versions/${version}/submit`, {
        body: {},
      }),
    cancelCheckout: (checkoutId, reason) =>
      call(CancelResponseSchema, "POST", `/v1/checkouts/${enc(checkoutId)}/cancel`, {
        body: { reason: reason ?? "buyer_cancelled" },
      }),

    getPaymentHandoff: (checkoutId) =>
      call(PaymentHandoffSchema, "GET", `/v1/checkouts/${enc(checkoutId)}/payment`),
    verifyPayment: (body) => call(VerifyResponseSchema, "POST", "/v1/payments/verify", { body }),

    getOrder: (orderId) => call(OrderSchema, "GET", `/v1/orders/${enc(orderId)}`),
    requestRefund: (orderId, body) =>
      call(RefundResponseSchema, "POST", `/v1/orders/${enc(orderId)}/refunds`, { body }),

    getTimeline: (checkoutId) =>
      call(TimelineResponseSchema, "GET", `/v1/checkouts/${enc(checkoutId)}/timeline`),
    getProof: (checkoutId) => call(ProofChainSchema, "GET", `/v1/checkouts/${enc(checkoutId)}/proof`),
    getConfig: () => call(RuntimeConfigSchema, "GET", "/v1/config"),

    subscribeEvents(checkoutId, { lastEventId, onEvent, onStatus }) {
      const ES =
        options.eventSourceImpl ?? (typeof EventSource !== "undefined" ? EventSource : undefined);
      if (!ES) {
        onStatus?.("unsupported");
        return () => {};
      }
      // EventSource cannot set headers on the first request, so the resume point travels
      // as a query parameter; the route handler turns it into Last-Event-ID upstream.
      // Automatic reconnects send the real Last-Event-ID header.
      const url =
        `${baseUrl}/v1/checkouts/${enc(checkoutId)}/events` +
        (lastEventId ? `?last_event_id=${enc(lastEventId)}` : "");
      const source = new ES(url, { withCredentials: true });
      onStatus?.("connecting");
      source.onopen = () => onStatus?.("open");
      source.onerror = () =>
        onStatus?.(source.readyState === ES.CLOSED ? "closed" : "reconnecting");
      const handle = (message: MessageEvent<string>) => {
        let json: unknown;
        try {
          json = JSON.parse(message.data);
        } catch {
          return;
        }
        const parsed = CheckoutEventSchema.safeParse(json);
        if (parsed.success) onEvent(parsed.data);
      };
      source.addEventListener("timeline", handle as EventListener);
      source.onmessage = handle;
      return () => {
        source.close();
        onStatus?.("closed");
      };
    },

    inspectorUrl: (attemptId) => `${baseUrl}/v1/inspector/payment-attempts/${enc(attemptId)}`,
    proofUrl: (checkoutId) => `${baseUrl}/v1/checkouts/${enc(checkoutId)}/proof`,
    auditVerifyUrl: (streamType, id) => `${baseUrl}/v1/audit/streams/${enc(streamType)}/${enc(id)}/verify`,
  };
}

export { ApiError, NetworkError, isApiError } from "./problem";

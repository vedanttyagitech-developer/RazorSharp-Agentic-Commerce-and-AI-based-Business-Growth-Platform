/**
 * The typed operator client. Every call goes to this app's own origin at `/api/backend/...`.
 *
 * The browser holds neither credential. The bearer token and the scenario key are both
 * attached server-side by `src/app/api/backend/[...path]/route.ts`, so the key that
 * widens every read to the whole tenant cannot be lifted out of a network panel during a
 * demonstration.
 *
 * The rule this module enforces on behalf of every caller: **it throws, and it never
 * substitutes.** There is no fixture behind any method here and no cached last-good
 * value. A read that fails raises `ApiError` carrying the problem document, and the page
 * renders the failure. The previous console fabricated an engaged Safe Mode out of a
 * request that had failed; the only durable defence against that is a client with
 * nothing to fabricate from.
 */
import { ApiError, problemFrom, transportProblem, type Problem } from "./problem";
import {
  AuditVerificationSchema,
  CataloguePageSchema,
  InjectionSchema,
  InspectorSchema,
  OrderSchema,
  OrdersPageSchema,
  OutboxPageSchema,
  ProofChainSchema,
  RefundsPageSchema,
  RetainedRevenueSchema,
  ReviveResultSchema,
  CaseDetailSchema,
  QueueSchema,
  MerchantActionListSchema,
  MerchantActionResultSchema,
  MerchantActionSchema,
  SupportCaseSchema,
  SupportQueueSchema,
  RuntimeConfigSchema,
  SafeModeSchema,
  SearchResponseSchema,
  SessionSchema,
  TimelineSchema,
  type AuditVerification,
  type CataloguePage,
  type Injection,
  type InjectionKind,
  type Inspector,
  type Order,
  type OrdersPage,
  type OutboxPage,
  type ProofChain,
  type RefundsPage,
  type RetainedRevenue,
  type ReviveResult,
  type CaseDetail,
  type Queue,
  type MerchantAction,
  type MerchantActionList,
  type MerchantActionResult,
  type SupportCase,
  type SupportQueue,
  type RuntimeConfig,
  type SafeMode,
  type SearchResponse,
  type Session,
  type Timeline,
} from "./types";
import type { z } from "zod";

const BASE = "/api/backend";

/**
 * No `Idempotency-Key` here, and that is deliberate rather than an omission. None of the
 * three mutations this console performs takes one: entering Safe Mode is an audited
 * administrative action, reviving a dead letter is a no-op on anything but a `DEAD` row,
 * and an injection is a labelled scenario change. The buyer surface's mutations move money
 * and do require the header; adding it here would imply a guarantee the API is not making.
 */
interface CallOptions {
  method?: string;
  body?: unknown;
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

async function call<T>(schema: z.ZodType<T>, path: string, options: CallOptions = {}): Promise<T> {
  const { method = "GET", body, query, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";

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
      title: "The API sent something that is not JSON",
      status: response.status,
      detail: cause instanceof Error ? cause.message : undefined,
    } satisfies Problem);
  }

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError({
      type: "about:blank",
      title: "The API sent a shape this console does not understand",
      status: response.status,
      detail: parsed.error.issues
        .slice(0, 4)
        .map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`)
        .join("; "),
      endpoint: path,
    } satisfies Problem);
  }
  return parsed.data;
}

export const api = {
  /** Who this console is: tenant, merchant and the capabilities the operator session carries. */
  session: (signal?: AbortSignal): Promise<Session> => call(SessionSchema, "/session", { signal }),

  config: (signal?: AbortSignal): Promise<RuntimeConfig> =>
    call(RuntimeConfigSchema, "/v1/config", { signal }),

// ------------------------------------------------------------------ human review

  /**
   * The human-review queue: cases a person must look at, with the counts by priority.
   *
   * Read-only, and that is the product decision rather than an unfinished screen. P0
   * ships the queue and the evidence, not a resolution workflow, so there is no assign,
   * no decision and no resolve here. A control that did nothing would be worse than no
   * control, and a reviewer acts outside this surface today.
   */
  queue: (opts: { limit?: number; signal?: AbortSignal } = {}): Promise<Queue> =>
    call(QueueSchema, "/v1/review/queue", { query: { limit: opts.limit ?? 50 }, signal: opts.signal }),

  /** One case with the evidence a reviewer is entitled to before deciding anything. */
  reviewCase: (caseKey: string, signal?: AbortSignal): Promise<CaseDetail> =>
    call(CaseDetailSchema, `/v1/review/queue/${encodeURIComponent(caseKey)}`, { signal }),

  // ---------------------------------------------------------------- merchant actions

  /** This merchant's own worklist, newest first: what they have been changing. */
  merchantActions: (
    opts: { state?: string; limit?: number; signal?: AbortSignal } = {},
  ): Promise<MerchantActionList> =>
    call(MerchantActionListSchema, "/v1/merchant/actions", {
      query: { state: opts.state, limit: opts.limit ?? 50 },
      signal: opts.signal,
    }),

  /** Draft one change. Nothing happens to the shop and nobody has been asked anything. */
  proposeAction: (
    body: { kind: string; target: string; proposal: Record<string, unknown> },
    signal?: AbortSignal,
  ): Promise<MerchantAction> =>
    call(MerchantActionSchema, "/v1/merchant/actions", {
      method: "POST",
      body,
      signal,
    }),

  submitAction: (actionId: string, signal?: AbortSignal): Promise<MerchantAction> =>
    call(MerchantActionSchema, `/v1/merchant/actions/${encodeURIComponent(actionId)}/submit`, {
      method: "POST",
      body: {},
      signal,
    }),

  /**
   * Agree to one exact document, named by its digest.
   *
   * `contentHash` is a required argument for the same reason the server requires it: an
   * approval names what was read, not the row it was read from. **Answers 409 when the
   * digest is no longer current**, carrying both values, and that refusal is the design
   * rather than an error -- it means somebody edited the proposal after this screen drew it.
   */
  approveAction: (
    actionId: string,
    contentHash: string,
    signal?: AbortSignal,
  ): Promise<MerchantAction> =>
    call(MerchantActionSchema, `/v1/merchant/actions/${encodeURIComponent(actionId)}/approve`, {
      method: "POST",
      body: { content_hash: contentHash },
      signal,
    }),

  rejectAction: (
    actionId: string,
    note: string,
    signal?: AbortSignal,
  ): Promise<MerchantAction> =>
    call(MerchantActionSchema, `/v1/merchant/actions/${encodeURIComponent(actionId)}/reject`, {
      method: "POST",
      body: { note },
      signal,
    }),

  /**
   * Carry out an approved change.
   *
   * **Answers 200 either way.** Read `ok`: a stale action, one edited after approval, and a
   * shop that refused a no-op are all ordinary outcomes with a reason.
   */
  executeAction: (actionId: string, signal?: AbortSignal): Promise<MerchantActionResult> =>
    call(
      MerchantActionResultSchema,
      `/v1/merchant/actions/${encodeURIComponent(actionId)}/execute`,
      { method: "POST", body: {}, signal },
    ),

  // ------------------------------------------------------------------------ helpdesk

  /**
   * The support queue: what buyers have raised, oldest first.
   *
   * Oldest first is the opposite of most lists and is the whole point. The case that has
   * waited longest is the one somebody is owed an answer on, and a queue sorted
   * newest-first is one that quietly abandons its own tail.
   *
   * Distinct from `queue()` above, which is the kernel's own escalations. A buyer saying a
   * bottle arrived broken and a payment whose outcome is unknown are two different jobs for
   * two different people, and merging them would bury the one a person can actually answer.
   */
  supportQueue: (
    opts: { status?: string; limit?: number; signal?: AbortSignal } = {},
  ): Promise<SupportQueue> =>
    call(SupportQueueSchema, "/v1/support/cases", {
      query: { status: opts.status, limit: opts.limit ?? 50 },
      signal: opts.signal,
    }),

  supportCase: (caseId: string, signal?: AbortSignal): Promise<SupportCase> =>
    call(SupportCaseSchema, `/v1/support/cases/${encodeURIComponent(caseId)}`, { signal }),

  /**
   * Pick up, answer or close one case.
   *
   * **Answers 409 when the move is not one this case can make**, naming the ones it could,
   * and that refusal is the reason this is worth having rather than a status dropdown. Two
   * people on one queue is ordinary; without it the second press silently overwrites the
   * first person's answer.
   *
   * There is no amount parameter and the server would refuse one. Resolving a case records
   * that a person dealt with it. Money goes back through the refund route, the kernel's
   * admission against its own ledger, and a capability this console does not hold.
   */
  advanceCase: (
    caseId: string,
    body: { status: string; note?: string },
    signal?: AbortSignal,
  ): Promise<SupportCase> =>
    call(SupportCaseSchema, `/v1/support/cases/${encodeURIComponent(caseId)}/advance`, {
      method: "POST",
      body: body.note ? { status: body.status, note: body.note } : { status: body.status },
      signal,
    }),

  // -------------------------------------------------------------------- collections

  /**
   * A page of orders plus `counts` across the whole scope.
   *
   * The counts are not derived from the page: an operator reading a queue depth off a
   * truncated list is an operator about to draw the wrong conclusion, so the API sends
   * both and this method keeps them together.
   */
  orders: (
    opts: { status?: string; limit?: number; cursor?: string; signal?: AbortSignal } = {},
  ): Promise<OrdersPage> =>
    call(OrdersPageSchema, "/v1/orders", {
      query: { status: opts.status, limit: opts.limit ?? 25, cursor: opts.cursor },
      signal: opts.signal,
    }),

  order: (orderId: string, signal?: AbortSignal): Promise<Order> =>
    call(OrderSchema, `/v1/orders/${encodeURIComponent(orderId)}`, { signal }),

  /**
   * A page of refunds, each carrying both its wire `state` and the `row_status` column.
   *
   * REFUND_PENDING, REFUND_UNKNOWN and REFUND_FAILED are three different facts about
   * money, and the list keeps them apart all the way to the screen.
   */
  refunds: (
    opts: { state?: string; limit?: number; cursor?: string; signal?: AbortSignal } = {},
  ): Promise<RefundsPage> =>
    call(RefundsPageSchema, "/v1/refunds", {
      query: { state: opts.state, limit: opts.limit ?? 25, cursor: opts.cursor },
      signal: opts.signal,
    }),

  /**
   * The merchant's own index, queried server-side.
   *
   * Not a client-side filter over whatever page happens to be loaded: that would search
   * fifty rows of two hundred and forty-seven and present the result as a catalogue
   * search. This is the same ranked index the buyer surface uses, so a SKU the console
   * cannot find here is a SKU the storefront cannot find either.
   */
  search: (
    q: string,
    opts: { limit?: number; signal?: AbortSignal } = {},
  ): Promise<SearchResponse> =>
    call(SearchResponseSchema, "/v1/catalogue/search", {
      query: { q, limit: opts.limit ?? 50 },
      signal: opts.signal,
    }),

  products: (
    opts: {
      category?: string;
      listed?: boolean;
      available?: boolean;
      limit?: number;
      cursor?: string;
      signal?: AbortSignal;
    } = {},
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

  // ------------------------------------------------------------------------ evidence

  /**
   * Step 11: what the platform retained by refusing a stale approval.
   *
   * Operator-only, which is why this console can call it and the storefront cannot. Omit
   * `checkoutId` for the newest refused approval on this merchant.
   */
  retainedRevenue: (
    merchantId: string,
    opts: { checkoutId?: string; signal?: AbortSignal } = {},
  ): Promise<RetainedRevenue> =>
    call(
      RetainedRevenueSchema,
      `/v1/merchants/${encodeURIComponent(merchantId)}/evidence/retained-revenue`,
      { query: { checkout_id: opts.checkoutId }, signal: opts.signal },
    ),

  /** Walk one aggregate's hash chain. Every hash is recomputed server-side, never trusted. */
  verifyStream: (
    aggregateType: string,
    aggregateId: string,
    signal?: AbortSignal,
  ): Promise<AuditVerification> =>
    call(
      AuditVerificationSchema,
      `/v1/audit/streams/${encodeURIComponent(aggregateType)}/${encodeURIComponent(aggregateId)}/verify`,
      { signal },
    ),

  proof: (checkoutId: string, signal?: AbortSignal): Promise<ProofChain> =>
    call(ProofChainSchema, `/v1/checkouts/${encodeURIComponent(checkoutId)}/proof`, { signal }),

  timeline: (checkoutId: string, signal?: AbortSignal): Promise<Timeline> =>
    call(TimelineSchema, `/v1/checkouts/${encodeURIComponent(checkoutId)}/timeline`, { signal }),

  // ----------------------------------------------------------------------- inspector

  inspect: (attemptId: string, signal?: AbortSignal): Promise<Inspector> =>
    call(InspectorSchema, `/v1/inspector/payment-attempts/${encodeURIComponent(attemptId)}`, { signal }),

  // ---------------------------------------------------------------------- operations

  outbox: (
    opts: { status?: string; limit?: number; signal?: AbortSignal } = {},
  ): Promise<OutboxPage> =>
    call(OutboxPageSchema, "/v1/ops/outbox", {
      query: { status: opts.status, limit: opts.limit ?? 50 },
      signal: opts.signal,
    }),

  /**
   * Return one dead letter to the queue.
   *
   * Answers 200 with `code: "CONCURRENT_OPERATION"` when the row was not `DEAD` -- the
   * operator asked a question and got a truthful answer -- so the caller must read `code`
   * rather than treat a 200 as success.
   */
  reviveCommand: (commandId: string, signal?: AbortSignal): Promise<ReviveResult> =>
    call(ReviveResultSchema, `/v1/ops/outbox/${encodeURIComponent(commandId)}/revive`, {
      method: "POST",
      body: {},
      signal,
    }),

  safeMode: (signal?: AbortSignal): Promise<SafeMode> =>
    call(SafeModeSchema, "/v1/ops/safe-mode", { signal }),

  /**
   * Throw or stand down the kill switch for this tenant.
   *
   * The returned mode is the one the API re-read *after* the write, inside the same
   * transaction, so it is what admission will decide a moment later. A caller must render
   * this answer and never its own optimistic assumption of it: a console that painted an
   * engaged banner from a request that failed would tell an operator the platform is
   * protected when it is not.
   */
  setSafeMode: (enabled: boolean, reason?: string, signal?: AbortSignal): Promise<SafeMode> =>
    call(SafeModeSchema, "/v1/ops/safe-mode", {
      method: "POST",
      body: reason ? { enabled, reason } : { enabled },
      signal,
    }),

  // ------------------------------------------------------------------------ scenario

  /**
   * Change merchant state, labelled as an injection.
   *
   * The only way this console mutates a price or a stock level. The change is applied by
   * the merchant simulator under its own lock and audited as `SCENARIO_INJECTION` in the
   * same transaction, and the response carries the revision it produced -- which is how a
   * page knows its catalogue read has gone stale.
   */
  inject: (
    body: { kind: InjectionKind; sku?: string; value?: number | boolean; note?: string },
    signal?: AbortSignal,
  ): Promise<Injection> =>
    call(InjectionSchema, "/v1/scenario/injections", {
      method: "POST",
      body: {
        kind: body.kind,
        ...(body.sku === undefined ? {} : { sku: body.sku }),
        ...(body.value === undefined ? {} : { value: body.value }),
        note: body.note ?? "",
      },
      signal,
    }),

};

export type Api = typeof api;
export { ApiError };

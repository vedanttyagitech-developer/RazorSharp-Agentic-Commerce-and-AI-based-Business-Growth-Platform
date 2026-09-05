/**
 * The boundary between this storefront and everything it does not control.
 *
 * Every figure a buyer is asked to consent to arrives through the function under test.
 * There is no fixture layer behind it and no mock server in front of it: what the API
 * sends is what the approval card draws. So the failure this file exists to prevent is a
 * narrow and specific one — a `catch` that produces a number. A client that swallowed a
 * malformed body and let `amount_minor` arrive as `undefined` would render `NaN` on a
 * screen whose entire purpose is to bind consent to an exact amount, and the buyer would
 * approve it. That is why the assertions below are not only "it throws" but "it throws
 * *rather than* resolving with a figure the server never sent".
 *
 * Four properties, each checked from several directions:
 *
 *  1. **A denial is not an error.** The kernel answers HTTP 200 with `allowed: false`
 *     when it refuses a stale approval. That is the platform working, and the client
 *     returns it. A client that threw on it would turn the one screen this product is
 *     built around into a generic error page.
 *  2. **Responses are parsed, not cast.** A body missing a required field fails at the
 *     boundary, and the failure names the field so the next person can see which side
 *     moved. Nothing resolves carrying `NaN` or `undefined` where money belongs.
 *  3. **The server's own sentence survives.** Problem documents reach the UI with their
 *     `status`, their `detail` and their extension members (`checkout_id`, `sku`,
 *     `header`) intact, because "No product with that SKU exists in this merchant's
 *     catalogue" is worth more to a buyer than "Not found".
 *  4. **The browser sends no credential.** The token lives on the server, behind
 *     `/api/backend`. Any `Authorization` header leaving this module would mean it had
 *     been handed to the tab, which is the thing the proxy exists to prevent.
 *
 * The fixtures are bodies the live API sent on 2026-09-05, pasted verbatim. The two
 * exceptions are marked at their definitions and say which named fields were changed and
 * why the live reproduction could not reach them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, newIdempotencyKey } from "./client";
import { ApiError, humanMessage } from "./problem";
import type { ApprovalCard } from "./types";

/* ---------------------------------------------------------------- the wiring */

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const fetchMock = vi.fn<Fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** One recorded outbound request, unpacked into the three things worth asserting on. */
interface Sent {
  url: string;
  method: string;
  headers: Record<string, string>;
  /** The parsed request body, or `undefined` when the client sent none at all. */
  body: unknown;
  init: RequestInit;
}

function sent(): Sent[] {
  return fetchMock.mock.calls.map(([input, rawInit]) => {
    const init = rawInit ?? {};
    const headers = (init.headers ?? {}) as Record<string, string>;
    return {
      url: String(input),
      method: init.method ?? "GET",
      headers,
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
      init,
    };
  });
}

function onlyRequest(): Sent {
  const all = sent();
  expect(all).toHaveLength(1);
  return all[0];
}

/** Case-insensitive header lookup: the client writes plain objects, not a `Headers`. */
function header(request: Sent, name: string): string | undefined {
  const found = Object.entries(request.headers).find(
    ([key]) => key.toLowerCase() === name.toLowerCase(),
  );
  return found?.[1];
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** An RFC 9457 body, served the way the API serves it: its own status and media type. */
function problemResponse(problem: { status: number } & Record<string, unknown>): Response {
  return new Response(JSON.stringify(problem), {
    status: problem.status,
    headers: { "Content-Type": "application/problem+json" },
  });
}

function replyWith(...responses: Response[]): void {
  for (const response of responses) fetchMock.mockResolvedValueOnce(response);
}

/** Resolve either way, so a test can assert on a *rejection* and on what did not happen. */
async function settle<T>(
  promise: Promise<T>,
): Promise<{ ok: true; value: T } | { ok: false; error: unknown }> {
  try {
    return { ok: true, value: await promise };
  } catch (error) {
    return { ok: false, error };
  }
}

function asApiError(error: unknown): ApiError {
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

/**
 * Every leaf in a parsed body, as `dotted.path -> value`.
 *
 * Used to state the property that matters more than any single field assertion: after a
 * successful parse there is no `NaN` and no `undefined` anywhere in the object the UI is
 * about to render. A single missing key is easy to assert on; this catches the ones
 * nobody thought to name.
 */
function leaves(value: unknown, path = ""): Array<[string, unknown]> {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => leaves(item, `${path}[${index}]`));
  }
  if (value !== null && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).flatMap(([key, item]) =>
      leaves(item, path ? `${path}.${key}` : key),
    );
  }
  return [[path, value]];
}

/** The paths whose value is a number that is not a number, or is simply not there. */
function holes(value: unknown): string[] {
  return leaves(value)
    .filter(([, leaf]) => (typeof leaf === "number" ? !Number.isFinite(leaf) : leaf === undefined))
    .map(([path]) => path);
}

/* ------------------------------------------------------------- the fixtures */

const BASKET_ID = "01a070e0-df4b-770a-a096-805a08b646d4";
const CHECKOUT_ID = "01a070e0-df64-7a40-b08e-a69974e53fac";
const ORDER_ID = "01a06fb2-1c40-7d61-9a37-0f4d2a4a8f11";

/** `GET /v1/baskets/{id}` with two lines priced. Captured 2026-09-05, HTTP 200. */
const BASKET = {
  basket_id: BASKET_ID,
  lines: [
    { sku: "AMUL-DAIRY-001", quantity: 2 },
    { sku: "INDI-STPL-001", quantity: 1 },
  ],
  code: "OK",
  quote: {
    currency: "INR",
    lines: [
      {
        sku: "AMUL-DAIRY-001",
        name: "Amul Taaza Toned Milk 500 ml",
        quantity: 2,
        unit_price_minor: 2800,
        subtotal_minor: 5600,
        tax_bp: 0,
        tax_minor: 0,
      },
      {
        sku: "INDI-STPL-001",
        name: "India Gate Classic Basmati Rice 5 kg",
        quantity: 1,
        unit_price_minor: 49900,
        subtotal_minor: 49900,
        tax_bp: 500,
        tax_minor: 2495,
      },
    ],
    items_subtotal_minor: 55500,
    items_tax_minor: 2495,
    delivery_fee_minor: 0,
    delivery_tax_minor: 0,
    total_minor: 57995,
    total: { minor: 57995, currency: "INR", display: "579.95" },
    free_delivery_applied: true,
    gap_to_free_delivery_minor: 0,
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 1,
    content_hash: "X3Emw-8W_7EETCJA9ZKsocSqk7uydL6v5DlBI2PnXq0",
  },
  unavailable: [],
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 1,
    observed_at: "2026-09-05T09:23:00.571795Z",
  },
  stale: false,
};

/** `POST /v1/baskets/{id}/checkout` — version 1's card. Captured 2026-09-05, HTTP 201. */
const APPROVAL_CARD = {
  checkout_id: CHECKOUT_ID,
  version: 1,
  content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
  policy_receipt_id: "01a070e0-df7d-7cda-b05e-7b506480b3cc",
  policy_receipt_hash: "NaBe7SjhQCQBWQQSV1jRlk2kRZtWcGyuNFG6_Nzg1Zo",
  amount_minor: 57995,
  currency: "INR",
  total: { minor: 57995, currency: "INR", display: "579.95" },
  expires_at: "2026-09-05T09:38:00.575554Z",
  reservation: {
    reservation_id: "01a070e0-df74-720b-868f-15336bb23d7f",
    state: "ACTIVE",
    expires_at: "2026-09-05T09:38:00.575554Z",
  },
  quote: BASKET.quote,
  previous_version: null,
  deltas: [],
};

/** `GET /v1/checkouts/{id}` in APPROVAL_REQUIRED. Captured 2026-09-05, HTTP 200. */
const CHECKOUT = {
  checkout_id: CHECKOUT_ID,
  basket_id: BASKET_ID,
  state: "APPROVAL_REQUIRED",
  current_version: 1,
  versions: [
    {
      version: 1,
      state: "APPROVAL_REQUIRED",
      content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
      policy_receipt_hash: "NaBe7SjhQCQBWQQSV1jRlk2kRZtWcGyuNFG6_Nzg1Zo",
      amount_minor: 57995,
      currency: "INR",
      created_at: "2026-09-05T09:23:00.575554Z",
      approval: null,
    },
  ],
  approval_card: { ...APPROVAL_CARD, quote: null },
  attempt: null,
  order_id: null,
  deltas: [],
  cancellable: true,
  updated_at: "2026-09-05T09:23:00.575554Z",
};

/**
 * `POST /v1/checkouts/{id}/versions/1/submit` after the merchant moved a price.
 * Captured 2026-09-05, **HTTP 200** — the status is the whole point of the fixture.
 */
const SUBMIT_REFUSED = {
  decision_id: "01a06fae-6f57-7986-9148-f9276d668c1e",
  allowed: false,
  code: "REAPPROVAL_REQUIRED",
  outcome: "REAPPROVAL_REQUIRED",
  explanation: "merchant_state_changed_since_approval",
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 1,
    content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
  },
  deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  grant_id: null,
  attempt_id: null,
  payment_attempt_id: null,
  next_version: 2,
  correlation_id: "01a06fae-6f4a-7b00-a1a4-b3ad6013d4d2",
};

/** `POST /v1/checkouts/{id}/cancel` on an APPROVED checkout. Captured 2026-09-05. */
const CANCEL_ALLOWED = {
  allowed: true,
  code: "OK",
  explanation: "cancelled",
  checkout: {
    checkout_id: CHECKOUT_ID,
    version: 1,
    content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
  },
  from_state: "APPROVED",
  grants_revoked: [],
  attempt_expired: null,
  reservation_release: "OK",
};

/**
 * A refused cancellation.
 *
 * The captured `CANCEL_ALLOWED` body with `allowed`, `code`, `explanation` and
 * `from_state` changed and nothing else touched. Reaching this live means driving a
 * checkout all the way to PAID and then cancelling it, which costs a stock reservation
 * this environment shares with other suites. The shape is the captured one; only the
 * verdict differs, and the verdict is what the assertion is about.
 */
const CANCEL_REFUSED = {
  ...CANCEL_ALLOWED,
  allowed: false,
  code: "NOT_CANCELLABLE",
  explanation: "checkout_not_cancellable_in_state",
  from_state: "PAID",
};

/* -------------------------------------------- the problem documents, verbatim */

const PROBLEM_CHECKOUT_NOT_FOUND = {
  type: "about:blank",
  title: "Checkout not found",
  status: 404,
  detail: "No checkout with that identifier belongs to this session.",
  instance: "/v1/checkouts/01a06fae-0000-7000-8000-000000000000",
  checkout_id: "01a06fae-0000-7000-8000-000000000000",
};

const PROBLEM_VALIDATION = {
  type: "about:blank",
  title: "Request validation failed",
  status: 422,
  detail: "The request body, query or path did not match the endpoint's schema.",
  instance: "/v1/baskets/01a070df-b86d-7e3d-972d-5d5755eb3c0d/lines/AMUL-DAIRY-001",
  errors: [
    {
      type: "int_parsing",
      loc: ["body", "quantity"],
      msg: "Input should be a valid integer, unable to parse string as an integer",
      input: "many",
    },
  ],
};

const PROBLEM_IDEMPOTENCY = {
  type: "about:blank",
  title: "Idempotency-Key required",
  status: 400,
  detail:
    "Every mutation must carry an Idempotency-Key header so a retry replays the original result instead of executing a second time.",
  instance: "/v1/baskets",
  header: "Idempotency-Key",
};

const PROBLEM_UNAUTHENTICATED = {
  type: "about:blank",
  title: "Not authenticated",
  status: 401,
  detail: "This endpoint requires an Authorization: Bearer <token> header.",
  instance: "/v1/orders",
};

const PROBLEM_DEAD_CURSOR = {
  type: "about:blank",
  title: "Invalid cursor",
  status: 400,
  detail: "The cursor is not one this endpoint issued. Start again without one.",
  instance: "/v1/orders",
};

const PROBLEM_UNKNOWN_SKU = {
  type: "about:blank",
  title: "Product not found",
  status: 404,
  detail: "No product with that SKU exists in this merchant's catalogue.",
  instance: "/v1/baskets/.../lines/NOPE-SKU-999",
  sku: "NOPE-SKU-999",
};

/**
 * A capability refusal.
 *
 * Assembled from the API's own producer rather than captured: `RequestContext.require`
 * in `commerce_api/deps.py` raises exactly this title, this sentence and these two
 * extension members. No read-only endpoint in the seeded demo tenant refuses a BUYER
 * session, so there is no free call that returns one.
 */
const PROBLEM_FORBIDDEN = {
  type: "about:blank",
  title: "Capability not held",
  status: 403,
  detail: "This session may not perform 'refund.issue'.",
  instance: "/v1/refunds",
  capability: "refund.issue",
  actor_type: "BUYER",
};

/**
 * What the storefront's own proxy answers when the Commerce API is not running. Copied
 * from `src/app/api/backend/[...path]/route.ts`, which is the only thing that can
 * produce a 503 on this path.
 */
const PROBLEM_UNREACHABLE = {
  type: "about:blank",
  title: "The store is not reachable",
  status: 503,
  detail: "No response from http://localhost:8000.",
};

/* ==================================================================== denials */

describe("a denial is not an error", () => {
  it("resolves with the kernel's refusal rather than throwing on it", async () => {
    // HTTP 200. Everything downstream branches on `allowed`, and a throw here would send
    // the refusal screen — the one screen this product exists to draw — down the generic
    // error path instead.
    replyWith(jsonResponse(SUBMIT_REFUSED, 200));

    const result = await api.submitVersion("01a06fae-2b52-755b-a664-ea5d66e5bc22", 1, "key-1");

    expect(result.allowed).toBe(false);
    expect(result.code).toBe("REAPPROVAL_REQUIRED");
    expect(result.explanation).toBe("merchant_state_changed_since_approval");
    expect(result.next_version).toBe(2);
  });

  it("carries the deltas through untouched, because they are the whole evidence", async () => {
    replyWith(jsonResponse(SUBMIT_REFUSED, 200));

    const result = await api.submitVersion("01a06fae-2b52-755b-a664-ea5d66e5bc22", 1, "key-1");

    // The refusal card subtracts nothing from these; if the client dropped or reshaped a
    // row here, the buyer would be shown a change the server did not report.
    expect(result.deltas).toEqual([
      { field_path: "total", approved: 8550, current: 9224, reason: "total_changed" },
    ]);
  });

  it("resolves when a cancellation is refused too", async () => {
    replyWith(jsonResponse(CANCEL_REFUSED, 200));

    const result = await api.cancel(CHECKOUT_ID, "buyer_cancelled", "key-2");

    expect(result.allowed).toBe(false);
    expect(result.code).toBe("NOT_CANCELLABLE");
    expect(result.from_state).toBe("PAID");
  });

  it("resolves when the cancellation succeeded, with the same shape", async () => {
    replyWith(jsonResponse(CANCEL_ALLOWED, 200));

    const result = await api.cancel(CHECKOUT_ID, "buyer_cancelled", "key-3");

    expect(result.allowed).toBe(true);
    expect(result.checkout?.checkout_id).toBe(CHECKOUT_ID);
  });

  it("does not throw on a submission the kernel admitted either", async () => {
    // The mirror of the refusal: same route, same status, `allowed: true`.
    const admitted = {
      ...SUBMIT_REFUSED,
      allowed: true,
      code: "OK",
      outcome: "OK",
      explanation: "admitted",
      deltas: [],
      next_version: null,
      attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
      payment_attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
    };
    replyWith(jsonResponse(admitted, 200));

    await expect(
      api.submitVersion("01a06fae-2b52-755b-a664-ea5d66e5bc22", 1, "key-4"),
    ).resolves.toMatchObject({ allowed: true, attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab" });
  });
});

/* ========================================================= problem documents */

describe("the server's own sentence reaches the UI", () => {
  it("keeps a 404's detail and its checkout_id extension member", async () => {
    replyWith(problemResponse(PROBLEM_CHECKOUT_NOT_FOUND));

    const outcome = await settle(api.checkout("01a06fae-0000-7000-8000-000000000000"));

    expect(outcome.ok).toBe(false);
    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(404);
    expect(error.problem.detail).toBe("No checkout with that identifier belongs to this session.");
    // The extension member is what lets a caller say *which* checkout is gone.
    expect(error.problem.checkout_id).toBe("01a06fae-0000-7000-8000-000000000000");
    expect(error.problem.title).toBe("Checkout not found");
    // Not the generic "Not found." from the fallback table.
    expect(humanMessage(error)).toBe("No checkout with that identifier belongs to this session.");
  });

  it("keeps a 422's per-field errors array, which is the only thing that locates the fault", async () => {
    replyWith(problemResponse(PROBLEM_VALIDATION));

    const outcome = await settle(api.setLine(BASKET_ID, "AMUL-DAIRY-001", 3, "key-5"));

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(422);
    expect(error.problem.errors).toEqual(PROBLEM_VALIDATION.errors);
    expect(humanMessage(error)).toBe(
      "The request body, query or path did not match the endpoint's schema.",
    );
  });

  it("keeps a 400's `header` extension member, which names what was missing", async () => {
    replyWith(problemResponse(PROBLEM_IDEMPOTENCY));

    const outcome = await settle(api.createBasket("key-6"));

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(400);
    expect(error.problem.header).toBe("Idempotency-Key");
    expect(humanMessage(error)).toBe(PROBLEM_IDEMPOTENCY.detail);
  });

  it("keeps a 404's `sku`, so the page can name the product that vanished", async () => {
    replyWith(problemResponse(PROBLEM_UNKNOWN_SKU));

    const outcome = await settle(api.setLine(BASKET_ID, "NOPE-SKU-999", 1, "key-7"));

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(404);
    expect(error.problem.sku).toBe("NOPE-SKU-999");
    expect(humanMessage(error)).toBe(
      "No product with that SKU exists in this merchant's catalogue.",
    );
  });

  it("reports a dead cursor as the 400 it is, with the cursor still on the wire", async () => {
    replyWith(problemResponse(PROBLEM_DEAD_CURSOR));

    const outcome = await settle(api.orders({ cursor: "zzzz" }));

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(400);
    expect(error.problem.title).toBe("Invalid cursor");
    expect(humanMessage(error)).toBe(
      "The cursor is not one this endpoint issued. Start again without one.",
    );
    // The cursor really was sent: otherwise this test would pass against a client that
    // silently dropped it and the paging bug would live somewhere else entirely.
    expect(onlyRequest().url).toBe("/api/backend/v1/orders?limit=25&cursor=zzzz");
  });

  it("uses the ApiError message itself as the server's sentence", async () => {
    replyWith(problemResponse(PROBLEM_CHECKOUT_NOT_FOUND));

    const outcome = await settle(api.checkout("01a06fae-0000-7000-8000-000000000000"));
    const error = asApiError(outcome.ok ? undefined : outcome.error);

    // `throw`n into a React error boundary or logged raw, an ApiError still says the
    // useful thing rather than "Error".
    expect(error.message).toBe("No checkout with that identifier belongs to this session.");
    expect(error.name).toBe("ApiError");
  });

  it("falls back to a generic sentence only when the server sent nothing usable", async () => {
    // A 500 from a proxy or a load balancer: HTML, not a problem document. There is no
    // server sentence to preserve, so the generic one is correct here and only here.
    replyWith(
      new Response("<!doctype html><title>502 Bad Gateway</title>", {
        status: 500,
        headers: { "Content-Type": "text/html" },
      }),
    );

    const outcome = await settle(api.orders());

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(500);
    expect(humanMessage(error)).toBe("Something went wrong on the server.");
    // The HTML did not get pasted into a buyer-facing string.
    expect(humanMessage(error)).not.toContain("<");
  });

  it("says something rather than nothing for anything that is not an ApiError", async () => {
    expect(humanMessage(new Error("boom"))).toBe("Something went wrong.");
    expect(humanMessage(undefined)).toBe("Something went wrong.");
  });
});

describe("the predicates callers branch on", () => {
  it("flags a 401 as unauthenticated and nothing else", async () => {
    replyWith(problemResponse(PROBLEM_UNAUTHENTICATED));

    const outcome = await settle(api.orders());

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.isUnauthenticated).toBe(true);
    expect(error.isForbidden).toBe(false);
    expect(error.isTransport).toBe(false);
    expect(error.problem.detail).toBe(
      "This endpoint requires an Authorization: Bearer <token> header.",
    );
  });

  it("flags a 403 as forbidden and never as something to retry", async () => {
    replyWith(problemResponse(PROBLEM_FORBIDDEN));

    const outcome = await settle(api.orders());

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.isForbidden).toBe(true);
    // A caller that retried on this would loop: the capability is not coming back.
    expect(error.isUnauthenticated).toBe(false);
    expect(error.isTransport).toBe(false);
    expect(error.problem.capability).toBe("refund.issue");
  });

  it("flags 0, 503 and 504 as transport, and a 500 as not", async () => {
    // 0 is the client's own code for "the request never left"; 503 and 504 are the two a
    // proxy in front of the API produces without the API having seen anything.
    replyWith(problemResponse(PROBLEM_UNREACHABLE));
    const unreachable = await settle(api.orders());
    expect(asApiError(unreachable.ok ? undefined : unreachable.error).isTransport).toBe(true);

    replyWith(problemResponse({ ...PROBLEM_UNREACHABLE, status: 504, title: "Gateway Timeout" }));
    const timedOut = await settle(api.orders());
    expect(asApiError(timedOut.ok ? undefined : timedOut.error).isTransport).toBe(true);

    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const offline = await settle(api.orders());
    expect(asApiError(offline.ok ? undefined : offline.error).isTransport).toBe(true);

    // A 500 means the request arrived and the server failed on it. Retrying that is a
    // different decision from retrying a request that never landed.
    replyWith(problemResponse({ type: "about:blank", title: "Internal Server Error", status: 500 }));
    const failed = await settle(api.orders());
    expect(asApiError(failed.ok ? undefined : failed.error).isTransport).toBe(false);
  });
});

/* ================================================== parsed, not cast */

describe("the response is parsed, not cast", () => {
  it("accepts the captured basket and leaves no hole anywhere in it", async () => {
    replyWith(jsonResponse(BASKET));

    const basket = await api.basket(BASKET_ID);

    expect(basket.quote?.total_minor).toBe(57995);
    // Nothing in the object the UI is about to render is NaN or missing.
    expect(holes(basket)).toEqual([]);
    // And the detector is not vacuous: it finds a hole where there is one.
    expect(holes({ quote: { total_minor: Number.NaN, total: { minor: undefined } } })).toEqual([
      "quote.total_minor",
      "quote.total.minor",
    ]);
  });

  it("accepts the captured approval card and leaves no hole in it", async () => {
    replyWith(jsonResponse(APPROVAL_CARD, 201));

    const card = await api.openCheckout(BASKET_ID, "key-8");

    expect(card.amount_minor).toBe(57995);
    expect(card.total.display).toBe("579.95");
    expect(holes(card)).toEqual([]);
  });

  it("refuses a quote with no total_minor, and names the field that is missing", async () => {
    // The redeploy case: a server that renamed or dropped a field while this tab was
    // open. Casting would hand the basket page `total_minor: undefined` and it would
    // render `NaN` beside a "Checkout" button.
    const quote = { ...BASKET.quote } as Record<string, unknown>;
    delete quote.total_minor;
    replyWith(jsonResponse({ ...BASKET, quote }));

    const outcome = await settle(api.basket(BASKET_ID));

    expect(outcome.ok).toBe(false);
    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.problem.title).toBe("The server sent a shape this app does not understand");
    expect(error.problem.detail).toContain("quote.total_minor");
  });

  it("refuses an approval card with no amount_minor, and names it", async () => {
    // The worst one in the app: `amount_minor` is the figure consent binds to.
    const card = { ...APPROVAL_CARD } as Record<string, unknown>;
    delete card.amount_minor;
    replyWith(jsonResponse(card, 201));

    const outcome = await settle(api.openCheckout(BASKET_ID, "key-9"));

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.problem.detail).toContain("amount_minor");
  });

  it("resolves with no figure at all on any body that is plausibly wrong", async () => {
    // Each of these is a shape a redeployed server or a hand-rolled mock really produces.
    // The assertion is not "it throws" but "nothing came back", because a client that
    // resolved with any of them would put a wrong number in front of a buyer.
    const corruptions: Array<[string, unknown]> = [
      ["amount_minor absent", omit(APPROVAL_CARD, "amount_minor")],
      ["amount_minor null", { ...APPROVAL_CARD, amount_minor: null }],
      ["amount_minor as a string", { ...APPROVAL_CARD, amount_minor: "57995" }],
      ["amount_minor as rupees, not paise", { ...APPROVAL_CARD, amount_minor: 579.95 }],
      ["total absent", omit(APPROVAL_CARD, "total")],
      ["total.minor absent", { ...APPROVAL_CARD, total: omit(APPROVAL_CARD.total, "minor") }],
      ["content_hash absent", omit(APPROVAL_CARD, "content_hash")],
      ["deltas absent", omit(APPROVAL_CARD, "deltas")],
      ["the body is a bare null", null],
      ["the body is an array", []],
      ["the body is a number", 57995],
    ];

    for (const [what, body] of corruptions) {
      fetchMock.mockReset();
      replyWith(jsonResponse(body, 201));

      const outcome = await settle(api.openCheckout(BASKET_ID, "key-10"));

      expect(outcome.ok, `${what} should not have resolved`).toBe(false);
      expect(asApiError(outcome.ok ? undefined : outcome.error).problem.title).toBe(
        "The server sent a shape this app does not understand",
      );
    }
  });

  it("keeps the status the malformed body arrived with, so a 200 does not become a 0", async () => {
    replyWith(jsonResponse(omit(APPROVAL_CARD, "amount_minor"), 201));

    const outcome = await settle(api.openCheckout(BASKET_ID, "key-11"));

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(201);
    // Which also means it is not mistaken for something worth retrying.
    expect(error.isTransport).toBe(false);
  });

  it("reports a 200 that is not JSON as a legible error rather than crashing on the parse", async () => {
    // A logged-out SSO redirect, or a captive portal: HTTP 200, HTML body.
    replyWith(
      new Response("<!doctype html><html><body>Sign in to continue</body></html>", {
        status: 200,
        headers: { "Content-Type": "text/html" },
      }),
    );

    const outcome = await settle(api.orders());

    expect(outcome.ok).toBe(false);
    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.problem.title).toBe("The server sent something that is not JSON");
    expect(error.status).toBe(200);
    // The `SyntaxError` did not escape as itself, and the message is a real sentence.
    expect(error).not.toBeInstanceOf(SyntaxError);
    expect(typeof error.message).toBe("string");
    expect(error.message.length).toBeGreaterThan(0);
  });

  it("reports an empty 200 body the same way", async () => {
    replyWith(new Response("", { status: 200, headers: { "Content-Type": "application/json" } }));

    const outcome = await settle(api.orders());

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.problem.title).toBe("The server sent something that is not JSON");
  });
});

/** A copy of `source` without `key`. Keeps the corruption table readable. */
function omit<T extends object>(source: T, key: keyof T & string): Record<string, unknown> {
  const copy = { ...source } as Record<string, unknown>;
  delete copy[key];
  return copy;
}

/* ======================================================= transport and abort */

describe("when the request never lands", () => {
  it("turns an unreachable server into status 0 rather than an exception nobody expects", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const outcome = await settle(api.orders());

    const error = asApiError(outcome.ok ? undefined : outcome.error);
    expect(error.status).toBe(0);
    expect(error.problem.title).toBe("Could not reach the server");
    expect(error.problem.detail).toBe("Failed to fetch");
    expect(error.isTransport).toBe(true);
  });

  it("re-throws an AbortError as an AbortError, and never as a request failure", async () => {
    // An aborted read is a component unmounting or a newer keystroke superseding this
    // one. Wrapped in an ApiError it would render as "Could not reach the server" on a
    // page that is working perfectly, and would look identical to being offline.
    const controller = new AbortController();
    fetchMock.mockImplementationOnce(async () => {
      controller.abort();
      throw new DOMException("The operation was aborted.", "AbortError");
    });

    const outcome = await settle(api.orders({ signal: controller.signal }));

    expect(outcome.ok).toBe(false);
    const error = outcome.ok ? undefined : outcome.error;
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe("AbortError");
    expect(error).not.toBeInstanceOf(ApiError);
  });

  it("passes the caller's signal through to fetch, so an abort can actually happen", async () => {
    const controller = new AbortController();
    replyWith(jsonResponse({ ...CHECKOUT }));

    await api.checkout(CHECKOUT_ID, controller.signal);

    expect(onlyRequest().init.signal).toBe(controller.signal);
  });
});

/* ==================================================== the request it sends */

/** One public call, with the request it is required to produce. */
interface Probe {
  readonly name: string;
  readonly run: () => Promise<unknown>;
  readonly url: string;
  readonly method: string;
  /** True when the API's `idempotency_key` dependency guards the route. */
  readonly needsKey: boolean;
}

const CARD = APPROVAL_CARD as ApprovalCard;

const PROBES: Probe[] = [
  { name: "session", run: () => api.session(), url: "/api/backend/session", method: "GET", needsKey: false },
  { name: "config", run: () => api.config(), url: "/api/backend/v1/config", method: "GET", needsKey: false },
  {
    name: "search",
    run: () => api.search("doodh"),
    url: "/api/backend/v1/catalogue/search?q=doodh&limit=20",
    method: "GET",
    needsKey: false,
  },
  {
    name: "products",
    run: () => api.products(),
    url: "/api/backend/v1/catalogue/products?limit=50",
    method: "GET",
    needsKey: false,
  },
  {
    name: "basket",
    run: () => api.basket(BASKET_ID),
    url: `/api/backend/v1/baskets/${BASKET_ID}`,
    method: "GET",
    needsKey: false,
  },
  {
    name: "checkout",
    run: () => api.checkout(CHECKOUT_ID),
    url: `/api/backend/v1/checkouts/${CHECKOUT_ID}`,
    method: "GET",
    needsKey: false,
  },
  {
    name: "paymentHandoff",
    run: () => api.paymentHandoff(CHECKOUT_ID),
    url: `/api/backend/v1/checkouts/${CHECKOUT_ID}/payment`,
    method: "GET",
    needsKey: false,
  },
  {
    name: "order",
    run: () => api.order(ORDER_ID),
    url: `/api/backend/v1/orders/${ORDER_ID}`,
    method: "GET",
    needsKey: false,
  },
  { name: "orders", run: () => api.orders(), url: "/api/backend/v1/orders?limit=25", method: "GET", needsKey: false },
  { name: "createBasket", run: () => api.createBasket(), url: "/api/backend/v1/baskets", method: "POST", needsKey: true },
  {
    name: "setLine",
    run: () => api.setLine(BASKET_ID, "AMUL-DAIRY-001", 2),
    url: `/api/backend/v1/baskets/${BASKET_ID}/lines/AMUL-DAIRY-001`,
    method: "PUT",
    needsKey: true,
  },
  {
    name: "openCheckout",
    run: () => api.openCheckout(BASKET_ID),
    url: `/api/backend/v1/baskets/${BASKET_ID}/checkout`,
    method: "POST",
    needsKey: true,
  },
  {
    name: "approve",
    run: () => api.approve(CARD),
    url: `/api/backend/v1/checkouts/${CHECKOUT_ID}/versions/1/approve`,
    method: "POST",
    needsKey: true,
  },
  {
    name: "reject",
    run: () => api.reject(CARD),
    url: `/api/backend/v1/checkouts/${CHECKOUT_ID}/versions/1/reject`,
    method: "POST",
    needsKey: true,
  },
  {
    name: "submitVersion",
    run: () => api.submitVersion(CHECKOUT_ID, 1),
    url: `/api/backend/v1/checkouts/${CHECKOUT_ID}/versions/1/submit`,
    method: "POST",
    needsKey: true,
  },
  {
    name: "cancel",
    run: () => api.cancel(CHECKOUT_ID),
    url: `/api/backend/v1/checkouts/${CHECKOUT_ID}/cancel`,
    method: "POST",
    needsKey: true,
  },
  {
    name: "verifyPayment",
    run: () =>
      api.verifyPayment({
        checkout_id: CHECKOUT_ID,
        razorpay_order_id: "order_TYDHez32NF91qq",
        razorpay_payment_id: "pay_TYDHfa11QQ2xzz",
        razorpay_signature: "9b0f1e",
      }),
    url: "/api/backend/v1/payments/verify",
    method: "POST",
    needsKey: true,
  },
  {
    // The one mutation the API does not guard with `idempotency_key`: `POST /v1/agent/turn`
    // in `commerce_api/routers/agent.py` takes no such dependency, because a turn proposes
    // and holds no capability to pay. Sending a key would be harmless; the assertion here
    // records that the client and the API agree, so a future guard shows up as a failure.
    name: "agentTurn",
    run: () => api.agentTurn({ message: "doodh chahiye" }),
    url: "/api/backend/v1/agent/turn",
    method: "POST",
    needsKey: false,
  },
];

/** Run every public call once against a stub that answers, and collect the requests. */
async function sweep(): Promise<Sent[]> {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => jsonResponse({}, 200));
  // The bodies do not parse, and that is fine: every assertion below is about what left
  // the browser, and the request was made before the schema ever ran.
  await Promise.allSettled(PROBES.map((probe) => probe.run()));
  return sent();
}

describe("the request the client actually sends", () => {
  it("addresses this app's own origin under /api/backend, on every single call", async () => {
    const requests = await sweep();

    expect(requests).toHaveLength(PROBES.length);
    for (const [index, probe] of PROBES.entries()) {
      expect(requests[index].url, probe.name).toBe(probe.url);
      expect(requests[index].method, probe.name).toBe(probe.method);
    }
  });

  it("never sends an Authorization header, because the browser does not hold the token", async () => {
    const requests = await sweep();

    // This is the assertion the whole proxy exists for. The token is minted server-side
    // and attached there; a credential leaving this module would mean it had been handed
    // to the tab, where an extension or a screenshot of the network panel can read it.
    for (const [index, request] of requests.entries()) {
      const names = Object.keys(request.headers).map((name) => name.toLowerCase());
      expect(names, PROBES[index].name).not.toContain("authorization");
      expect(names, PROBES[index].name).not.toContain("cookie");
      expect(names, PROBES[index].name).not.toContain("x-scenario-key");
    }
  });

  it("sends the session cookie by asking for it, not by carrying it", async () => {
    const requests = await sweep();

    for (const [index, request] of requests.entries()) {
      // `same-origin` is what lets the httpOnly cookie ride along; `no-store` is what
      // stops a stale price or a stale checkout state being served out of the bfcache.
      expect(request.init.credentials, PROBES[index].name).toBe("same-origin");
      expect(request.init.cache, PROBES[index].name).toBe("no-store");
    }
  });

  it("carries an Idempotency-Key on every mutation the API guards, and on no read", async () => {
    const requests = await sweep();

    for (const [index, request] of requests.entries()) {
      const probe = PROBES[index];
      const key = header(request, "Idempotency-Key");
      if (probe.needsKey) {
        // Without it the API answers 400 and the mutation never runs at all.
        expect(key, probe.name).toBeTypeOf("string");
        expect((key ?? "").length, probe.name).toBeGreaterThan(0);
      } else {
        expect(key, probe.name).toBeUndefined();
      }
    }
  });

  it("sets Content-Type only when there is a body to describe", async () => {
    const requests = await sweep();

    for (const [index, request] of requests.entries()) {
      const contentType = header(request, "Content-Type");
      if (request.init.body === undefined) {
        expect(contentType, PROBES[index].name).toBeUndefined();
      } else {
        expect(contentType, PROBES[index].name).toBe("application/json");
      }
      // Every call asks for JSON back, whether or not it sent any.
      expect(header(request, "Accept"), PROBES[index].name).toBe("application/json");
    }
  });

  it("sends no body at all on a read, rather than an empty one", async () => {
    const requests = await sweep();

    for (const [index, request] of requests.entries()) {
      if (PROBES[index].method === "GET") {
        expect(request.init.body, PROBES[index].name).toBeUndefined();
      }
    }
  });

  it("gives each mutation a fresh key, so two calls are not deduplicated into one", async () => {
    replyWith(jsonResponse(BASKET, 201), jsonResponse(BASKET, 201));

    await settle(api.createBasket());
    await settle(api.createBasket());

    const [first, second] = sent();
    expect(header(first, "Idempotency-Key")).not.toBe(header(second, "Idempotency-Key"));
  });

  it("uses a caller's key verbatim, so a retry replays instead of buying twice", async () => {
    replyWith(jsonResponse(BASKET, 201), jsonResponse(BASKET, 201));

    await settle(api.createBasket("retry-me-01a070e0"));
    await settle(api.createBasket("retry-me-01a070e0"));

    const [first, second] = sent();
    expect(header(first, "Idempotency-Key")).toBe("retry-me-01a070e0");
    expect(header(second, "Idempotency-Key")).toBe("retry-me-01a070e0");
  });

  it("echoes the exact bytes the buyer saw when approving, and nothing else", async () => {
    replyWith(jsonResponse({ ...CANCEL_ALLOWED }, 200));

    await settle(api.approve(CARD, "key-12"));

    // The server compares these three against the version it holds, which is how consent
    // is bound to what was on screen. An extra field here, or a missing one, turns that
    // comparison into something the buyer did not agree to.
    expect(onlyRequest().body).toEqual({
      content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
      amount_minor: 57995,
      currency: "INR",
    });
  });

  it("sends the content hash and a reason when rejecting", async () => {
    replyWith(jsonResponse({ ...CANCEL_ALLOWED }, 200));

    await settle(api.reject(CARD, "too_expensive", "key-13"));

    expect(onlyRequest().body).toEqual({
      content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
      reason: "too_expensive",
    });
  });

  it("percent-encodes an identifier rather than pasting it into the path", async () => {
    replyWith(jsonResponse(BASKET, 200));

    await settle(api.setLine(BASKET_ID, "SKU/../../v1/orders", 1, "key-14"));

    // A SKU is merchant data. Unencoded it would address a different route entirely.
    expect(onlyRequest().url).toBe(
      `/api/backend/v1/baskets/${BASKET_ID}/lines/SKU%2F..%2F..%2Fv1%2Forders`,
    );
  });
});

/* ============================================================ query building */

describe("the query string", () => {
  it("drops a null, an undefined and an empty value instead of sending the word", async () => {
    replyWith(jsonResponse({}, 200));

    await settle(
      api.products({ category: undefined, cursor: undefined, limit: 50, listed: undefined }),
    );

    const { url } = onlyRequest();
    expect(url).toBe("/api/backend/v1/catalogue/products?limit=50");
    // The bug this prevents: `?category=undefined`, which the API reads as the string
    // "undefined" and answers with an empty catalogue rather than an error.
    expect(url).not.toContain("undefined");
    expect(url).not.toContain("null");
  });

  it("drops an empty string, which is how a cleared filter arrives from a form", async () => {
    replyWith(jsonResponse({}, 200));

    await settle(api.orders({ status: "", cursor: "" }));

    expect(onlyRequest().url).toBe("/api/backend/v1/orders?limit=25");
  });

  it("keeps `false`, which is a filter and not an absence", async () => {
    replyWith(jsonResponse({}, 200));

    await settle(api.products({ listed: false, available: true, limit: 10 }));

    // `listed=false` asks for the delisted products. Dropping it because it is falsy
    // would silently answer a different question.
    expect(onlyRequest().url).toBe(
      "/api/backend/v1/catalogue/products?listed=false&available=true&limit=10",
    );
  });

  it("keeps a zero, for the same reason", async () => {
    replyWith(jsonResponse({}, 200));

    await settle(api.search("doodh", { limit: 0 }));

    expect(onlyRequest().url).toBe("/api/backend/v1/catalogue/search?q=doodh&limit=0");
  });

  it("encodes a query the buyer typed, including Devanagari and reserved characters", async () => {
    replyWith(jsonResponse({}, 200));

    await settle(api.search("दूध 50% & अंडे", { locale: "hi-IN" }));

    const { url } = onlyRequest();
    expect(url).toContain("q=%E0%A4%A6%E0%A5%82%E0%A4%A7+50%25+%26+%E0%A4%85%E0%A4%82%E0%A4%A1%E0%A5%87");
    expect(url).toContain("locale=hi-IN");
    // Read back, it is the string the buyer typed and not a truncation at the `&`.
    expect(new URLSearchParams(url.split("?")[1]).get("q")).toBe("दूध 50% & अंडे");
  });
});

/* ======================================================= idempotency keys */

describe("newIdempotencyKey", () => {
  it("uses crypto.randomUUID when the platform has one", () => {
    const randomUUID = vi.fn(() => "01a070e0-df4b-770a-a096-805a08b646d4" as const);
    vi.stubGlobal("crypto", { randomUUID });

    expect(newIdempotencyKey()).toBe("01a070e0-df4b-770a-a096-805a08b646d4");
    expect(randomUUID).toHaveBeenCalledTimes(1);
  });

  it("falls back when randomUUID is missing, which is every non-secure context", () => {
    // `crypto.randomUUID` is gated on a secure context. A demo served over plain http on
    // a LAN address has `crypto` and no `randomUUID`, and a mutation with no key is a
    // 400 — so the fallback is the difference between a working demo and a dead one.
    vi.stubGlobal("crypto", { getRandomValues: vi.fn() });

    const key = newIdempotencyKey();

    expect(key).toMatch(/^k-\d+-[a-z0-9]+$/);
  });

  it("falls back when there is no crypto object at all", () => {
    vi.stubGlobal("crypto", undefined);

    expect(newIdempotencyKey()).toMatch(/^k-\d+-[a-z0-9]+$/);
  });

  it("does not collide, even with the clock frozen", () => {
    // Frozen time removes the `Date.now()` half of the key, so this tests the half that
    // actually has to be unique. Two mutations sharing a key inside the same millisecond
    // would have the second one replay the first one's result — a quantity change that
    // silently answers with the previous quantity.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-05T09:23:00.000Z"));
    vi.stubGlobal("crypto", undefined);

    const keys = new Set<string>();
    for (let index = 0; index < 1000; index += 1) keys.add(newIdempotencyKey());

    expect(keys.size).toBe(1000);
  });

  it("reaches the wire as the header the API reads", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "01a070e0-0000-7000-8000-000000000000" });
    replyWith(jsonResponse(BASKET, 201));

    await settle(api.createBasket());

    expect(header(onlyRequest(), "Idempotency-Key")).toBe("01a070e0-0000-7000-8000-000000000000");
  });
});

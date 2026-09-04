import { describe, expect, it, vi } from "vitest";

import { createLiveClient } from "./client";
import { ApiError, isApiError, parseProblem, problemFromBody } from "./problem";

function response(body: string, init: { status: number; contentType?: string; statusText?: string }): Response {
  return new Response(body, {
    status: init.status,
    statusText: init.statusText ?? "",
    headers: init.contentType ? { "content-type": init.contentType } : {},
  });
}

describe("RFC 9457 problem parsing", () => {
  it("parses a problem+json document into a typed ApiError", async () => {
    const error = await parseProblem(
      response(
        JSON.stringify({
          type: "https://errors.example/stale-checkout",
          title: "Checkout is stale",
          status: 409,
          detail: "Version 1 was invalidated",
          instance: "/v1/checkouts/chk_1",
          code: "REAPPROVAL_REQUIRED",
          next_version: 2,
        }),
        { status: 409, contentType: "application/problem+json" },
      ),
    );
    expect(isApiError(error)).toBe(true);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.type).toBe("https://errors.example/stale-checkout");
    expect(error.title).toBe("Checkout is stale");
    expect(error.detail).toBe("Version 1 was invalidated");
    expect(error.instance).toBe("/v1/checkouts/chk_1");
    expect(error.code).toBe("REAPPROVAL_REQUIRED");
    expect(error.problem.extensions).toEqual({ next_version: 2 });
    expect(error.message).toBe("Checkout is stale: Version 1 was invalidated");
  });

  it("falls back to HTTP status and statusText when the body is not a problem document", async () => {
    const error = await parseProblem(response("<html>Bad gateway</html>", { status: 502, contentType: "text/html", statusText: "Bad Gateway" }));
    expect(error.status).toBe(502);
    expect(error.type).toBe("about:blank");
    expect(error.title).toBe("Bad Gateway");
    expect(error.detail).toBe("<html>Bad gateway</html>");
  });

  it("does not trust a non-numeric status member over the HTTP status", () => {
    const problem = problemFromBody({ title: "Odd", status: "500" }, 422, "Unprocessable");
    expect(problem.status).toBe(422);
    expect(problem.title).toBe("Odd");
  });

  it("accepts recovery_code as an alias for code and survives an empty body", async () => {
    const aliased = problemFromBody({ title: "Denied", recovery_code: "SAFE_MODE_ACTIVE" }, 403, "");
    expect(aliased.code).toBe("SAFE_MODE_ACTIVE");
    const empty = await parseProblem(response("", { status: 500, contentType: "application/json" }));
    expect(empty.status).toBe(500);
    expect(empty.title).toBe("HTTP 500");
  });
});

describe("live client transport", () => {
  it("throws ApiError on non-2xx, sends Idempotency-Key only on mutations, and reuses it on a 401 retry", async () => {
    const calls: { url: string; method: string; headers: Headers }[] = [];
    let first = true;
    const fetchImpl: typeof fetch = async (input, init) => {
      const url = String(input);
      const headers = new Headers(init?.headers);
      calls.push({ url, method: init?.method ?? "GET", headers });
      if (url.endsWith("/v1/config")) {
        return response(JSON.stringify({ type: "about:blank", title: "Boom", status: 500 }), { status: 500, contentType: "application/problem+json" });
      }
      if (first) {
        first = false;
        return response(JSON.stringify({ title: "No session", status: 401 }), { status: 401, contentType: "application/problem+json" });
      }
      return response(
        JSON.stringify({ basket_id: "bsk_1", lines: [], code: "OK", quote: null, unavailable: [], freshness: { source: "s", catalogue_revision: 1, observed_at: "2026-09-04T00:00:00Z" }, stale: false }),
        { status: 200, contentType: "application/json" },
      );
    };
    const onUnauthorized = vi.fn(async () => true);
    const client = createLiveClient({ baseUrl: "http://api.test", fetchImpl, onUnauthorized });

    await expect(client.getConfig()).rejects.toMatchObject({ name: "ApiError", status: 500, title: "Boom" });
    expect(calls[0].headers.has("idempotency-key")).toBe(false);

    const basket = await client.createBasket();
    expect(basket.basket_id).toBe("bsk_1");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    const mutations = calls.filter((call) => call.method === "POST");
    expect(mutations).toHaveLength(2);
    const firstKey = mutations[0].headers.get("idempotency-key");
    expect(firstKey).toMatch(/^[0-9a-f-]{36}$/);
    expect(mutations[1].headers.get("idempotency-key")).toBe(firstKey);
  });

  it("surfaces a 2xx body that violates the provisional contract", async () => {
    const fetchImpl: typeof fetch = async () => response(JSON.stringify({ unexpected: true }), { status: 200, contentType: "application/json" });
    const client = createLiveClient({ baseUrl: "http://api.test", fetchImpl });
    await expect(client.getConfig()).rejects.toMatchObject({ name: "ApiError", type: "urn:buyer-web:contract-mismatch" });
  });
});

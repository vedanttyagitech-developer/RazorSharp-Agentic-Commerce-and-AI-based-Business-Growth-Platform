/**
 * The proxy that holds the only credential in this app.
 *
 * Everything else on this storefront can be wrong and be a rendering bug. This module can
 * be wrong and hand a genuine, valid token bound to somebody else's `buyer_ref` to
 * whoever asked for it — which is not hypothetical, it is what an earlier version of this
 * file did. It read a bare base64 cookie, believed the identity inside it, and on an
 * upstream 401 minted a fresh session for that identity. Writing a cookie by hand was
 * therefore enough to read another buyer's orders, and `httpOnly` was no defence at all:
 * it stops a script *reading* a cookie, not an attacker *writing* one.
 *
 * So the tests below are organised by attack rather than by function. Each one names the
 * thing that goes wrong if the assertion stops holding:
 *
 *  - **It does not invent a response.** When the API is down the proxy answers a 503
 *    problem document. The assertion is not that the status is 503 but that the body
 *    contains no product, no price and no checkout — a storefront that quietly serves
 *    fixtures shows a buyer a number no kernel ever agreed to.
 *  - **The 401 replay is anonymous.** The mint that follows an upstream 401 must not take
 *    a `buyer_ref` from the caller's cookie. A replay that reads its identity from the
 *    request is a way of asking to be somebody else.
 *  - **A forged cookie is discarded whole**, not repaired and not partly believed.
 *  - **A write must prove it came from this origin.** `SameSite=lax` is no defence,
 *    because this handler will mint a session for a caller that presents no cookie at all.
 *  - **The token never comes back down.** `/api/backend/session` answers who the browser
 *    is and nothing that could be replayed against the API from a tab.
 *  - **Only an allowlist crosses.** A caller's own `Authorization` or `X-Scenario-Key` is
 *    dropped rather than smuggled through.
 *
 * `SESSION_COOKIE_SECRET` is fixed before the module is imported, because the module
 * reads it once at import time and otherwise randomises per process. With it fixed, a
 * correctly-signed cookie and a tampered one are both constructible here, which is the
 * only way to test the two paths apart.
 */
import { createHmac } from "node:crypto";

import { NextRequest } from "next/server";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------- the wiring */

/** Fixed so the tests can forge both a valid tag and an invalid one. */
const SECRET = "test-cookie-secret-01a070e0";
/** Deliberately not the default, so the 503 body has to name a configured address. */
const API_BASE = "http://127.0.0.1:8123";
const ORIGIN = "http://storefront.test";
const COOKIE = "acr_session";

process.env.SESSION_COOKIE_SECRET = SECRET;
process.env.COMMERCE_API_URL = API_BASE;
process.env.NEXT_PUBLIC_TENANT_SLUG = "demo";

let route: typeof import("./route");

beforeAll(async () => {
  route = await import("./route");
});

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const fetchMock = vi.fn<Fetch>();

function stubFetch(): void {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** One request the proxy made upstream, unpacked. */
interface Upstream {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string;
}

function upstreamCalls(): Upstream[] {
  return fetchMock.mock.calls.map(([input, rawInit]) => {
    const init = rawInit ?? {};
    const headers: Record<string, string> = {};
    if (init.headers instanceof Headers) {
      init.headers.forEach((value, name) => {
        headers[name.toLowerCase()] = value;
      });
    } else {
      for (const [name, value] of Object.entries((init.headers ?? {}) as Record<string, string>)) {
        headers[name.toLowerCase()] = value;
      }
    }
    let body = "";
    if (typeof init.body === "string") body = init.body;
    else if (init.body instanceof ArrayBuffer) body = Buffer.from(init.body).toString("utf8");
    return { url: String(input), method: (init.method ?? "GET").toUpperCase(), headers, body };
  });
}

const MINT_URL = `${API_BASE}/v1/demo/sessions`;

function mints(): Upstream[] {
  return upstreamCalls().filter((call) => call.url === MINT_URL);
}

function forwards(): Upstream[] {
  return upstreamCalls().filter((call) => call.url !== MINT_URL);
}

interface Session {
  token: string;
  session_id: string;
  tenant_id: string;
  merchant_id: string;
  buyer_ref: string;
  actor_type: string;
  capabilities: string[];
  expires_at: string;
}

/** The shape `POST /v1/demo/sessions` answers with. Captured 2026-09-05, fields renamed. */
function session(overrides: Partial<Session> = {}): Session {
  return {
    token: "tok-cookie-0000",
    session_id: "01a070e0-c001-7000-8000-000000000001",
    tenant_id: "01a070e0-7e11-7000-8000-000000000002",
    merchant_id: "01a070e0-3e11-7000-8000-000000000003",
    buyer_ref: "buyer-in-the-cookie",
    actor_type: "BUYER",
    capabilities: ["basket.write", "checkout.approve"],
    expires_at: "2026-09-05T17:23:00.000000Z",
    ...overrides,
  };
}

/** The proxy's own signing, reproduced: base64url of the JSON, then an HMAC over that. */
function sign(value: Session): string {
  const payload = Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
  const mac = createHmac("sha256", SECRET).update(payload).digest("base64url");
  return `${payload}.${mac}`;
}

/** A cookie whose payload decodes cleanly and whose tag is wrong. The forgery. */
function forge(value: Session, tag = "not-the-real-tag"): string {
  const payload = Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
  return `${payload}.${tag}`;
}

interface RequestOptions {
  method?: string;
  cookie?: string;
  headers?: Record<string, string>;
  body?: string;
  search?: string;
}

function request(path: string[], options: RequestOptions = {}): NextRequest {
  const { method = "GET", cookie, headers = {}, body, search = "" } = options;
  const all: Record<string, string> = { host: "storefront.test", ...headers };
  if (cookie) all.cookie = `${COOKIE}=${cookie}`;
  return new NextRequest(`${ORIGIN}/api/backend/${path.join("/")}${search}`, {
    method,
    headers: all,
    body,
  });
}

function call(path: string[], options: RequestOptions = {}): Promise<Response> {
  const method = (options.method ?? "GET").toUpperCase();
  const handler =
    method === "POST"
      ? route.POST
      : method === "PUT"
        ? route.PUT
        : method === "DELETE"
          ? route.DELETE
          : route.GET;
  return handler(request(path, options), { params: Promise.resolve({ path }) });
}

function jsonUpstream(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A write that a page of this storefront would send: the browser sets this header. */
const FROM_THIS_PAGE = { "sec-fetch-site": "same-origin" };

/* ============================================== the API is not there at all */

describe("when the API is unreachable", () => {
  it("answers a 503 problem document naming the address it could not reach", async () => {
    stubFetch();
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const response = await call(["v1", "orders"], { cookie: sign(session()) });

    expect(response.status).toBe(503);
    expect(response.headers.get("content-type")).toBe("application/problem+json");
    const body = await response.json();
    expect(body.title).toBe("The store is not reachable");
    // Naming the address is the difference between a bug report and a shrug.
    expect(body.detail).toContain(API_BASE);
  });

  it("invents nothing: the body carries no product, no price and no checkout", async () => {
    stubFetch();
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));

    const response = await call(["v1", "catalogue", "products"], { cookie: sign(session()) });
    const text = await response.text();

    // This is the assertion, not the status code. A storefront that falls back to
    // fixtures when the kernel is down shows a buyer a price nothing agreed to, and the
    // buyer cannot tell the difference. Whatever this body is, it is not a catalogue.
    expect(JSON.parse(text)).toEqual({
      type: "about:blank",
      title: "The store is not reachable",
      status: 503,
      detail: `No response from ${API_BASE}.`,
    });
    for (const forbidden of [
      "products",
      "sku",
      "amount_minor",
      "total_minor",
      "unit_price",
      "checkout_id",
      "basket_id",
      "currency",
      "INR",
      "quote",
    ]) {
      expect(text, forbidden).not.toContain(forbidden);
    }
  });

  it("says the same thing when it is the mint that cannot be reached", async () => {
    stubFetch();
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));

    // No cookie at all, so the very first thing it tries is the mint.
    const response = await call(["v1", "orders"]);

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body.title).toBe("The store is not reachable");
    expect(body.detail).toContain(API_BASE);
    expect(mints()).toHaveLength(1);
  });

  it("does not hand the browser a session cookie it could not obtain", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(new Response("nope", { status: 500 }));

    const response = await call(["v1", "orders"]);

    expect(response.status).toBe(503);
    expect(response.headers.get("set-cookie")).toBeNull();
  });
});

/* =========================================== the 401 replay, and its identity */

describe("a 401 after the API restarted", () => {
  /** Upstream rejects the cookie's token once, then accepts the freshly minted one. */
  function restartThenRecover(fresh: Session, payload: unknown): void {
    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream({ title: "Not authenticated" }, 401));
    fetchMock.mockResolvedValueOnce(jsonUpstream(fresh));
    fetchMock.mockResolvedValueOnce(jsonUpstream(payload));
  }

  it("mints once, replays the request, and the replay succeeds", async () => {
    const fresh = session({ token: "tok-fresh-1111", buyer_ref: "anonymous-buyer" });
    restartThenRecover(fresh, { orders: [], next_cursor: null, limit: 25, scope: "session", counts: {} });

    const response = await call(["v1", "orders"], {
      cookie: sign(session({ token: "tok-stale-9999" })),
      search: "?limit=25",
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ orders: [] });
    expect(mints()).toHaveLength(1);
    const sentUpstream = forwards();
    expect(sentUpstream).toHaveLength(2);
    expect(sentUpstream[0].headers.authorization).toBe("Bearer tok-stale-9999");
    expect(sentUpstream[1].headers.authorization).toBe("Bearer tok-fresh-1111");
    // The replay is the same request, not a different one.
    expect(sentUpstream[1].url).toBe(sentUpstream[0].url);
    expect(sentUpstream[1].method).toBe(sentUpstream[0].method);
  });

  it("mints anonymously: the mint body carries no identity from the caller's cookie", async () => {
    const fresh = session({ token: "tok-fresh-2222", buyer_ref: "anonymous-buyer" });
    restartThenRecover(fresh, { orders: [] });

    await call(["v1", "orders"], {
      cookie: sign(session({ token: "tok-stale-9999", buyer_ref: "victim-buyer-ref" })),
    });

    // This is the identity-takeover the module was rewritten to stop. The mint asks for
    // whoever the API wants to give it; it does not ask to be the buyer the request
    // named, because a request can name anyone.
    const [mint] = mints();
    expect(JSON.parse(mint.body)).toEqual({ tenant_slug: "demo", actor_type: "BUYER" });
    expect(Object.keys(JSON.parse(mint.body))).not.toContain("buyer_ref");
    expect(mint.body).not.toContain("victim-buyer-ref");
    expect(mint.headers.authorization).toBeUndefined();
  });

  it("never puts the caller's claimed buyer_ref on any wire at all", async () => {
    const fresh = session({ token: "tok-fresh-3333", buyer_ref: "anonymous-buyer" });
    restartThenRecover(fresh, { orders: [] });

    await call(["v1", "orders"], {
      cookie: sign(session({ token: "tok-stale-9999", buyer_ref: "victim-buyer-ref" })),
    });

    for (const sentUpstream of upstreamCalls()) {
      expect(sentUpstream.body).not.toContain("victim-buyer-ref");
      expect(JSON.stringify(sentUpstream.headers)).not.toContain("victim-buyer-ref");
    }
  });

  it("replaces the cookie with the session it actually got, not the one it was shown", async () => {
    const fresh = session({ token: "tok-fresh-4444", buyer_ref: "anonymous-buyer" });
    restartThenRecover(fresh, { orders: [] });

    const response = await call(["v1", "orders"], {
      cookie: sign(session({ token: "tok-stale-9999", buyer_ref: "victim-buyer-ref" })),
    });

    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain(`${COOKIE}=`);
    expect(setCookie).toContain("HttpOnly");
    const stored = readSetCookie(setCookie);
    expect(stored?.token).toBe("tok-fresh-4444");
    expect(stored?.buyer_ref).toBe("anonymous-buyer");
  });

  it("gives up rather than looping when the mint fails during the replay", async () => {
    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream({ title: "Not authenticated" }, 401));
    fetchMock.mockResolvedValueOnce(new Response("no", { status: 500 }));

    const response = await call(["v1", "orders"], { cookie: sign(session()) });

    // The 401 is passed through as the API's own answer. One mint, one replay attempt
    // that never happened, and no third request.
    expect(response.status).toBe(401);
    expect(mints()).toHaveLength(1);
    expect(forwards()).toHaveLength(1);
  });
});

/** Decode the session out of a `Set-Cookie` the proxy wrote. */
function readSetCookie(setCookie: string): Session | null {
  const value = /acr_session=([^;]+)/.exec(setCookie)?.[1];
  if (!value) return null;
  const payload = value.slice(0, value.lastIndexOf("."));
  return JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as Session;
}

/* ================================================================ the cookie */

describe("the cookie's signature", () => {
  it("reuses a correctly-signed cookie: no mint, and its token is what goes upstream", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ orders: [] }));

    const response = await call(["v1", "orders"], {
      cookie: sign(session({ token: "tok-cookie-5555" })),
    });

    expect(response.status).toBe(200);
    expect(mints()).toHaveLength(0);
    expect(forwards()).toHaveLength(1);
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-cookie-5555");
    // Nothing was re-issued, so the browser keeps the cookie it already had.
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("discards a tampered cookie whole and mints a fresh anonymous session", async () => {
    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream(session({ token: "tok-minted-6666", buyer_ref: "anonymous-buyer" })));
    fetchMock.mockResolvedValueOnce(jsonUpstream({ orders: [] }));

    // A perfectly well-formed payload — valid base64url, valid JSON, a plausible token
    // and somebody else's `buyer_ref`. Everything about it is right except the tag.
    const response = await call(["v1", "orders"], {
      cookie: forge(session({ token: "tok-forged-attacker", buyer_ref: "victim-buyer-ref" })),
    });

    expect(response.status).toBe(200);
    expect(mints()).toHaveLength(1);
    // Not repaired, not partly believed: the forged token was never presented upstream
    // and the forged identity was never asked for.
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-minted-6666");
    for (const sentUpstream of upstreamCalls()) {
      expect(sentUpstream.body).not.toContain("victim-buyer-ref");
      expect(sentUpstream.body).not.toContain("tok-forged-attacker");
      expect(JSON.stringify(sentUpstream.headers)).not.toContain("tok-forged-attacker");
    }
  });

  it("rejects a payload spliced onto a tag that was valid for a different payload", async () => {
    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream(session({ token: "tok-minted-7777" })));
    fetchMock.mockResolvedValueOnce(jsonUpstream({ orders: [] }));

    // The attacker holds one genuine cookie of their own and swaps the payload for one
    // naming the victim, keeping the tag. The HMAC is over the payload, so it fails.
    const genuine = sign(session({ token: "tok-mine", buyer_ref: "attacker" }));
    const stolenTag = genuine.slice(genuine.lastIndexOf(".") + 1);
    const spliced = forge(session({ token: "tok-mine", buyer_ref: "victim-buyer-ref" }), stolenTag);

    await call(["v1", "orders"], { cookie: spliced });

    expect(mints()).toHaveLength(1);
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-minted-7777");
  });

  it("discards a cookie with no tag at all rather than reading the payload anyway", async () => {
    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream(session({ token: "tok-minted-8888" })));
    fetchMock.mockResolvedValueOnce(jsonUpstream({ orders: [] }));

    // The bare base64 blob the old version of this handler used to believe.
    const bare = Buffer.from(
      JSON.stringify(session({ token: "tok-bare", buyer_ref: "victim-buyer-ref" })),
      "utf8",
    ).toString("base64url");

    await call(["v1", "orders"], { cookie: bare });

    expect(mints()).toHaveLength(1);
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-minted-8888");
  });

  it("is signed under a key this process chose, so another process's cookie is worthless", async () => {
    // With no `SESSION_COOKIE_SECRET` configured the module picks a random key at import.
    // A restart therefore invalidates every outstanding cookie, which is the safe default
    // for a secret nobody chose: each browser is minted a new anonymous session instead.
    delete process.env.SESSION_COOKIE_SECRET;
    vi.resetModules();
    const isolated = await import("./route");
    process.env.SESSION_COOKIE_SECRET = SECRET;

    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream(session({ token: "tok-minted-9999" })));
    fetchMock.mockResolvedValueOnce(jsonUpstream({ orders: [] }));

    const path = ["v1", "orders"];
    const response = await isolated.GET(
      request(path, { cookie: sign(session({ token: "tok-from-the-old-process" })) }),
      { params: Promise.resolve({ path }) },
    );

    expect(response.status).toBe(200);
    expect(mints()).toHaveLength(1);
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-minted-9999");
  });
});

/* ==================================================== cross-site write refusal */

describe("cross-site writes", () => {
  it("refuses a POST that carries neither Sec-Fetch-Site nor Origin", async () => {
    stubFetch();

    const response = await call(["v1", "baskets"], { method: "POST", body: "{}" });

    expect(response.status).toBe(403);
    expect(response.headers.get("content-type")).toBe("application/problem+json");
    expect((await response.json()).title).toBe("Cross-site write refused");
    // Refused before anything happened: no session was minted for the caller and nothing
    // reached the API. A refusal that still mints is a refusal that still costs.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a POST from a foreign Origin", async () => {
    stubFetch();

    const response = await call(["v1", "baskets"], {
      method: "POST",
      body: "{}",
      headers: { origin: "https://evil.example" },
      cookie: sign(session()),
    });

    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a POST the browser itself labelled cross-site", async () => {
    stubFetch();

    const response = await call(["v1", "baskets"], {
      method: "POST",
      body: "{}",
      headers: { "sec-fetch-site": "cross-site", origin: ORIGIN },
      cookie: sign(session()),
    });

    // `Sec-Fetch-Site` is set by the browser and cannot be forged by the page, so it wins
    // over an `Origin` the attacker controls.
    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses PUT and DELETE on the same terms", async () => {
    for (const method of ["PUT", "DELETE"]) {
      stubFetch();
      const response = await call(["v1", "baskets", "b1", "lines", "AMUL-DAIRY-001"], {
        method,
        body: method === "PUT" ? '{"quantity":2}' : undefined,
      });
      expect(response.status, method).toBe(403);
      expect(fetchMock, method).not.toHaveBeenCalled();
      vi.unstubAllGlobals();
    }
  });

  it("accepts a POST from a page of this storefront", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ basket_id: "b1" }, 201));

    const response = await call(["v1", "baskets"], {
      method: "POST",
      body: "{}",
      headers: { ...FROM_THIS_PAGE, "idempotency-key": "k-1" },
      cookie: sign(session({ token: "tok-write" })),
    });

    expect(response.status).toBe(201);
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-write");
  });

  it("accepts a POST that proves itself with a matching Origin instead", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ basket_id: "b1" }, 201));

    const response = await call(["v1", "baskets"], {
      method: "POST",
      body: "{}",
      headers: { origin: ORIGIN },
      cookie: sign(session()),
    });

    expect(response.status).toBe(201);
  });

  it("does not gate reads: a GET with the same headers goes through", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ orders: [] }));

    // Neither header, and a foreign origin. Reads are not the thing being protected here
    // — nothing a cross-site read could reach is a mutation, and the response is not
    // readable cross-origin anyway.
    const bare = await call(["v1", "orders"], { cookie: sign(session()) });
    expect(bare.status).toBe(200);

    const foreign = await call(["v1", "orders"], {
      headers: { origin: "https://evil.example" },
      cookie: sign(session()),
    });
    expect(foreign.status).toBe(200);
  });

  it("forwards the body of an accepted write, byte for byte", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ basket_id: "b1" }));

    await call(["v1", "baskets", "b1", "lines", "AMUL-DAIRY-001"], {
      method: "PUT",
      body: '{"quantity":2}',
      headers: { ...FROM_THIS_PAGE, "content-type": "application/json" },
      cookie: sign(session()),
    });

    expect(forwards()[0].body).toBe('{"quantity":2}');
    expect(forwards()[0].method).toBe("PUT");
  });
});

/* ============================================================= /session */

describe("GET /api/backend/session", () => {
  it("answers from the cookie without asking the API anything", async () => {
    stubFetch();

    const response = await call(["session"], {
      cookie: sign(session({ token: "tok-must-not-leak-abcdef" })),
    });

    expect(response.status).toBe(200);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(await response.json()).toEqual({
      session_id: "01a070e0-c001-7000-8000-000000000001",
      tenant_id: "01a070e0-7e11-7000-8000-000000000002",
      merchant_id: "01a070e0-3e11-7000-8000-000000000003",
      buyer_ref: "buyer-in-the-cookie",
      actor_type: "BUYER",
      capabilities: ["basket.write", "checkout.approve"],
      expires_at: "2026-09-05T17:23:00.000000Z",
    });
  });

  it("does not contain the bearer token anywhere in the body", async () => {
    stubFetch();
    const token = "tok-must-not-leak-abcdef";

    const response = await call(["session"], { cookie: sign(session({ token })) });
    const text = await response.text();

    // The point of the proxy. A token in this body would be readable by any script in
    // the page, which is exactly what the httpOnly cookie exists to prevent.
    expect(text).not.toContain(token);
    expect(Object.keys(JSON.parse(text))).not.toContain("token");
    // And not smuggled out through a header either.
    expect(JSON.stringify([...response.headers.entries()])).not.toContain(token);
  });

  it("mints when there is no cookie, and still withholds the token from the body", async () => {
    stubFetch();
    fetchMock.mockResolvedValueOnce(jsonUpstream(session({ token: "tok-brand-new-xyz" })));

    const response = await call(["session"]);
    const text = await response.text();

    expect(response.status).toBe(200);
    expect(mints()).toHaveLength(1);
    expect(text).not.toContain("tok-brand-new-xyz");
    // It goes into the cookie instead, which is httpOnly and therefore unreadable here.
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("HttpOnly");
    expect(readSetCookie(setCookie)?.token).toBe("tok-brand-new-xyz");
  });

  it("assembles the reply field by field, so a new upstream field is withheld", async () => {
    stubFetch();
    fetchMock.mockResolvedValueOnce(
      jsonUpstream({
        ...session({ token: "tok-new-fields" }),
        refresh_token: "refresh-must-not-leak",
        internal_principal_id: "prn_0001",
      }),
    );

    const response = await call(["session"]);
    const text = await response.text();

    // Omission is the safe default; subtraction is not. A field the API starts sending
    // tomorrow does not reach the page until somebody decides it belongs there.
    expect(text).not.toContain("refresh-must-not-leak");
    expect(text).not.toContain("prn_0001");
    expect(Object.keys(JSON.parse(text)).sort()).toEqual([
      "actor_type",
      "buyer_ref",
      "capabilities",
      "expires_at",
      "merchant_id",
      "session_id",
      "tenant_id",
    ]);
  });

  it("is not cached, because it is the identity of this browser", async () => {
    stubFetch();

    const response = await call(["session"], { cookie: sign(session()) });

    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

/* ==================================================== the header allowlist */

describe("the header allowlist", () => {
  it("drops a caller's own Authorization instead of forwarding it", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ orders: [] }));

    await call(["v1", "orders"], {
      cookie: sign(session({ token: "tok-real-session" })),
      headers: { authorization: "Bearer stolen-operator-token" },
    });

    // Replaced, not appended to, and not preferred. Forwarding this would let a page
    // present any token it liked and use the proxy as an open relay.
    expect(forwards()[0].headers.authorization).toBe("Bearer tok-real-session");
    expect(JSON.stringify(forwards()[0].headers)).not.toContain("stolen-operator-token");
  });

  it("drops X-Scenario-Key, which is the key to the fault-injection routes", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ orders: [] }));

    await call(["v1", "orders"], {
      cookie: sign(session()),
      headers: { "x-scenario-key": "guessed-scenario-key" },
    });

    expect(forwards()[0].headers["x-scenario-key"]).toBeUndefined();
    expect(JSON.stringify(forwards()[0].headers)).not.toContain("guessed-scenario-key");
  });

  it("drops the browser's cookie header, so upstream never sees this app's session cookie", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ orders: [] }));

    await call(["v1", "orders"], { cookie: sign(session({ token: "tok-cookie-only" })) });

    expect(forwards()[0].headers.cookie).toBeUndefined();
  });

  it("forwards only the small allowlist, and nothing a caller invented", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ basket_id: "b1" }));

    await call(["v1", "baskets"], {
      method: "POST",
      body: "{}",
      cookie: sign(session({ token: "tok-allow" })),
      headers: {
        ...FROM_THIS_PAGE,
        accept: "application/json",
        "content-type": "application/json",
        "idempotency-key": "k-allow-1",
        "accept-language": "hi-IN",
        "x-correlation-id": "corr-1",
        "x-forwarded-for": "10.0.0.9",
        "x-real-ip": "10.0.0.9",
        "x-admin-override": "true",
        referer: "https://evil.example/attack",
      },
    });

    expect(Object.keys(forwards()[0].headers).sort()).toEqual([
      "accept",
      "accept-language",
      "authorization",
      "content-type",
      "idempotency-key",
      "x-correlation-id",
    ]);
  });

  it("carries the Idempotency-Key through, because the API refuses a mutation without it", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ basket_id: "b1" }, 201));

    await call(["v1", "baskets"], {
      method: "POST",
      body: "{}",
      cookie: sign(session()),
      headers: { ...FROM_THIS_PAGE, "idempotency-key": "01a070e0-key" },
    });

    expect(forwards()[0].headers["idempotency-key"]).toBe("01a070e0-key");
  });

  it("returns only the allowlisted response headers, and always no-store", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ basket_id: "b1" }), {
        status: 200,
        headers: {
          "content-type": "application/json",
          "idempotent-replayed": "true",
          etag: 'W/"abc"',
          "retry-after": "3",
          "set-cookie": "upstream_session=leak; Path=/",
          "x-internal-trace": "svc-7",
        },
      }),
    );

    const response = await call(["v1", "baskets", "b1"], { cookie: sign(session()) });

    expect(response.headers.get("idempotent-replayed")).toBe("true");
    expect(response.headers.get("etag")).toBe('W/"abc"');
    expect(response.headers.get("retry-after")).toBe("3");
    expect(response.headers.get("cache-control")).toBe("no-store");
    // Upstream must not be able to set a cookie on this origin or leak its own tracing.
    expect(response.headers.get("x-internal-trace")).toBeNull();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("passes an upstream failure through as itself rather than rewriting it", async () => {
    stubFetch();
    const problem = {
      type: "about:blank",
      title: "Product not found",
      status: 404,
      detail: "No product with that SKU exists in this merchant's catalogue.",
      instance: "/v1/baskets/.../lines/NOPE-SKU-999",
      sku: "NOPE-SKU-999",
    };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(problem), {
        status: 404,
        headers: { "content-type": "application/problem+json" },
      }),
    );

    const response = await call(["v1", "baskets", "b1", "lines", "NOPE-SKU-999"], {
      method: "PUT",
      body: '{"quantity":1}',
      headers: { ...FROM_THIS_PAGE },
      cookie: sign(session()),
    });

    // The proxy is a pipe for the API's own sentence, extension members and all.
    expect(response.status).toBe(404);
    expect(response.headers.get("content-type")).toBe("application/problem+json");
    expect(await response.json()).toEqual(problem);
  });
});

/* ============================================================ the path */

describe("the path it addresses", () => {
  it("joins the suffix onto the configured base and forwards the query string", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({ orders: [] }));

    await call(["v1", "orders"], {
      cookie: sign(session()),
      search: "?limit=25&cursor=abc&status=PAID",
    });

    const url = new URL(forwards()[0].url);
    expect(url.origin).toBe(API_BASE);
    expect(url.pathname).toBe("/v1/orders");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.get("cursor")).toBe("abc");
    expect(url.searchParams.get("status")).toBe("PAID");
  });

  it("cannot be walked out of the API base with .. segments", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({}));

    await call(["v1", "..", "..", "..", "..", "evil.example", "steal"], {
      cookie: sign(session()),
    });

    // The segments normalise, but they normalise inside the origin: the worst a caller
    // gets is a different path on the same API, which its own authorization still guards.
    const url = new URL(forwards()[0].url);
    expect(url.origin).toBe(API_BASE);
    expect(url.host).toBe("127.0.0.1:8123");
    expect(url.href.startsWith(`${API_BASE}/`)).toBe(true);
  });

  it("cannot be walked out with encoded .. segments either", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({}));

    // Next decodes a catch-all segment once before the handler sees it, so this is what
    // arrives from a URL that wrote the dots double-encoded: `/api/backend/v1/%252e%252e`.
    // The handler decodes again, which is what makes the second layer worth a test.
    await call(["v1", "%2e%2e", "%2e%2e", "admin", "keys"], { cookie: sign(session()) });

    expect(new URL(forwards()[0].url).origin).toBe(API_BASE);
  });

  it("cannot be turned into a different host by a segment that looks like a URL", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({}));

    // A segment carrying a scheme and an authority. The `:` is percent-encoded on the way
    // through, so what survives is `/https%3A//attacker.example/...` — a path on this API,
    // not an authority. `attacker.example` still appears in the string; the assertion is
    // that it appears where it cannot do anything, which is why this checks the parsed
    // host rather than a substring.
    await call(["https:", "", "attacker.example", "v1", "orders"], { cookie: sign(session()) });

    const url = new URL(forwards()[0].url);
    expect(url.origin).toBe(API_BASE);
    expect(url.host).toBe("127.0.0.1:8123");
    expect(url.protocol).toBe("http:");
    expect(url.pathname.startsWith("/https%3A/")).toBe(true);
  });

  it("cannot smuggle a slash through an encoded segment", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({}));

    // The client sends `SKU%2F..%2F..%2Fv1%2Forders`; Next splits the catch-all on real
    // slashes before decoding, so the handler receives this as one segment with the
    // slashes already back in it. Re-encoding must put it back as one segment, not three.
    await call(["v1", "baskets", "b1", "lines", "SKU/../../v1/orders"], {
      cookie: sign(session()),
    });

    const url = new URL(forwards()[0].url);
    expect(url.origin).toBe(API_BASE);
    expect(url.pathname).toBe("/v1/baskets/b1/lines/SKU%2F..%2F..%2Fv1%2Forders");
  });

  it("preserves a legitimate identifier through the decode-and-re-encode round trip", async () => {
    stubFetch();
    fetchMock.mockResolvedValue(jsonUpstream({}));

    await call(["v1", "checkouts", "01a070e0-df64-7a40-b08e-a69974e53fac", "payment"], {
      cookie: sign(session()),
    });

    expect(forwards()[0].url).toBe(
      `${API_BASE}/v1/checkouts/01a070e0-df64-7a40-b08e-a69974e53fac/payment`,
    );
  });
});

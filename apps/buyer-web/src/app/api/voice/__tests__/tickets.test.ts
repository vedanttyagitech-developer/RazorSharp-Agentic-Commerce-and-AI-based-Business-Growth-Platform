/**
 * The ticket route, which is the only reason a browser on this storefront can speak.
 *
 * Two properties carry the design and both are asserted here rather than assumed. The
 * bearer must go OUT to the gateway and must never come BACK to the page -- the whole
 * point of minting server-side is that the browser holds no credential. And a page on
 * another site must not be able to open a voice session on this storefront's behalf, which
 * `SameSite=lax` does not prevent, because this route will mint a session for a caller
 * that presents no cookie at all.
 */
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const BEARER = "tok_buyer_secret_value";
const GATEWAY = "http://127.0.0.1:8100";

const SESSION = {
  token: BEARER,
  session_id: "sess_1",
  tenant_id: "ten_1",
  merchant_id: "mer_1",
  buyer_ref: "buyer_1",
  actor_type: "BUYER",
  capabilities: ["cart.read"],
  expires_at: "2099-01-01T00:00:00Z",
};

const TICKET = {
  ticket: "tkt_opaque_handle",
  expires_in_s: 60,
  session_id: "vs_1",
  speech_available: true,
};

/** Import after the env is stubbed: the route reads the gateway URL at module load. */
async function loadRoute() {
  vi.resetModules();
  return import("../tickets/route");
}

function request(headers: Record<string, string> = {}): NextRequest {
  return new NextRequest("http://localhost:3000/api/voice/tickets", {
    method: "POST",
    headers: { "sec-fetch-site": "same-origin", ...headers },
  });
}

interface Call {
  url: string;
  init: RequestInit | undefined;
}

/** Answers the demo-session mint and the gateway mint, recording both. */
function stubUpstreams(gateway: () => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith("/v1/demo/sessions")) {
      return new Response(JSON.stringify(SESSION), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return gateway();
  });
  return calls;
}

function ok(): Response {
  return new Response(JSON.stringify(TICKET), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  vi.stubEnv("VOICE_GATEWAY_URL", GATEWAY);
  vi.stubEnv("SESSION_COOKIE_SECRET", "test-secret-for-the-cookie-tag");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("POST /api/voice/tickets", () => {
  it("refuses a request that did not come from a page on this origin", async () => {
    const calls = stubUpstreams(ok);
    const { POST } = await loadRoute();

    const response = await POST(request({ "sec-fetch-site": "cross-site" }));

    expect(response.status).toBe(403);
    // Not merely refused: nothing upstream was touched, so a cross-site caller cannot even
    // cause a session to be minted.
    expect(calls).toHaveLength(0);
  });

  it("refuses a caller that sends neither Sec-Fetch-Site nor a matching Origin", async () => {
    stubUpstreams(ok);
    const { POST } = await loadRoute();

    const bare = new NextRequest("http://localhost:3000/api/voice/tickets", { method: "POST" });
    expect((await POST(bare)).status).toBe(403);
  });

  it("sends the buyer's bearer to the gateway and returns only the opaque ticket", async () => {
    const calls = stubUpstreams(ok);
    const { POST } = await loadRoute();

    const response = await POST(request());
    expect(response.status).toBe(200);

    const gatewayCall = calls.find((call) => call.url.startsWith(GATEWAY));
    expect(gatewayCall?.url).toBe(`${GATEWAY}/v1/voice/tickets`);
    expect(new Headers(gatewayCall?.init?.headers).get("authorization")).toBe(`Bearer ${BEARER}`);

    const body = await response.json();
    expect(body).toEqual({
      ticket: "tkt_opaque_handle",
      expires_in_s: 60,
      session_id: "vs_1",
      speech_available: true,
    });
  });

  it("never lets the bearer reach the page, in any header or any field", async () => {
    stubUpstreams(ok);
    const { POST } = await loadRoute();

    const response = await POST(request());
    const text = await response.clone().text();

    expect(text).not.toContain(BEARER);
    // The cookie is the one place the token legitimately travels, and it is httpOnly.
    const cookie = response.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("HttpOnly");
    for (const [name, value] of response.headers) {
      if (name.toLowerCase() === "set-cookie") continue;
      expect(value).not.toContain(BEARER);
    }
  });

  it("assembles the reply field by field, withholding anything the gateway adds later", async () => {
    stubUpstreams(() =>
      new Response(
        JSON.stringify({ ...TICKET, internal_bearer: BEARER, some_new_field: "surprise" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { POST } = await loadRoute();

    const body = await (await POST(request())).json();

    expect(Object.keys(body).sort()).toEqual([
      "expires_in_s",
      "session_id",
      "speech_available",
      "ticket",
    ]);
  });

  it("mints a fresh session and replays once when the gateway rejects the bearer", async () => {
    let attempts = 0;
    const calls = stubUpstreams(() => {
      attempts += 1;
      return attempts === 1 ? new Response("{}", { status: 403 }) : ok();
    });
    const { POST } = await loadRoute();

    const response = await POST(request());

    expect(response.status).toBe(200);
    expect(attempts).toBe(2);
    // Two demo-session mints: the first for the missing cookie, the second for the replay.
    expect(calls.filter((call) => call.url.endsWith("/v1/demo/sessions"))).toHaveLength(2);
  });

  it("answers 502 when the gateway refuses, without passing its detail through", async () => {
    stubUpstreams(() => new Response("speech is not configured for project foo", { status: 500 }));
    const { POST } = await loadRoute();

    const response = await POST(request());
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(response.headers.get("content-type")).toContain("application/problem+json");
    expect(JSON.stringify(body)).not.toContain("project foo");
  });

  it("answers 503 when the gateway is not running at all", async () => {
    stubUpstreams(() => {
      throw new Error("ECONNREFUSED");
    });
    const { POST } = await loadRoute();

    const response = await POST(request());
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body.detail).toContain(GATEWAY);
  });

  it("answers 503 when no session can be opened to mint against", async () => {
    vi.stubGlobal("fetch", async (input: string | URL) => {
      if (String(input).endsWith("/v1/demo/sessions")) return new Response("{}", { status: 503 });
      return ok();
    });
    const { POST } = await loadRoute();

    expect((await POST(request())).status).toBe(503);
  });

  it("rejects a reply that parses but carries no ticket", async () => {
    stubUpstreams(() =>
      new Response(JSON.stringify({ expires_in_s: 60 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { POST } = await loadRoute();

    expect((await POST(request())).status).toBe(502);
  });

  it("is never cached, because a ticket is spent by whoever reads it first", async () => {
    stubUpstreams(ok);
    const { POST } = await loadRoute();

    const response = await POST(request());
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

/**
 * The only place this console holds a credential, and it holds two.
 *
 * The browser talks to `/api/backend/...` on its own origin. This handler mints an
 * **operator** session against the Commerce API and attaches the bearer token on the way
 * through, plus `X-Scenario-Key` on the routes that are actually gated or widened by it.
 *
 * Why both, and why neither reaches the browser
 * ---------------------------------------------
 * The bearer token says *whose tenant*; the scenario key says *you may operate this
 * apparatus*. On the read routes the key widens the scope from the caller's own rows to
 * the whole tenant, which is the difference between a console and a buyer's order
 * history, and an operator session cannot even be minted without it (see
 * `commerce_api.routers.demo.mint_session`). A key that reached the browser would be a
 * key any visitor could lift out of a network panel and replay against the scenario
 * controller, so it lives in this process and is written onto the outbound request here.
 *
 * Three rules keep that from being a distinction without a difference:
 *
 *  - **The key goes on an allowlist of paths, never on everything.** It used to be set on
 *    whatever path the browser asked for, and `/v1/demo/sessions` is such a path: posting
 *    `{"actor_type":"OPERATOR"}` through this handler answered 200 with a raw operator
 *    bearer token in a readable JSON body. Minting is this file's own private business, so
 *    `/v1/demo/` is refused outright from the browser-facing handler and the key is
 *    attached only where the API asks for it.
 *  - **A write must prove it came from a page of this console.** Nothing here is a
 *    credential the browser must present, so `SameSite` protects nothing: without an
 *    origin check, a hostile page in an operator's browser could revive an outbox command
 *    or throw the Safe Mode switch with a CORS-simple POST that needs no preflight and no
 *    cookie.
 *  - **The session cookie is signed.** Otherwise "a write needs a session" would be
 *    satisfied by any forged blob, and the 401 replay below would mint a genuine operator
 *    session to execute the write with.
 *
 * What this handler deliberately does NOT do, exactly as the storefront's does not:
 *
 *  - It never invents a response. An unreachable API is a 503 problem document. A console
 *    that painted a plausible queue depth over a failed request would be worse than no
 *    console, because an operator would act on it.
 *  - It never forwards an arbitrary header. Only the allowlist below crosses, so a page
 *    in this app cannot smuggle its own `Authorization` or `X-Scenario-Key` through and
 *    the two credentials always come from this file's environment.
 *  - It never proxies anything but the configured API base: the path is joined onto that
 *    origin and re-parsed, so `..` segments cannot walk out of it.
 */
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

import { NextResponse, type NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const API_BASE = process.env.COMMERCE_API_URL ?? "http://localhost:8000";
const TENANT_SLUG = process.env.NEXT_PUBLIC_TENANT_SLUG ?? "demo";
const SCENARIO_KEY = process.env.SCENARIO_KEY ?? "local-demo-scenario-key";
const COOKIE = "acr_operator";
const UPSTREAM_TIMEOUT_MS = 15_000;

/**
 * The key the cookie's tag is computed under. Configured where more than one process
 * serves this console; random per process otherwise, so a restart invalidates outstanding
 * cookies and each browser is minted a new operator session on its next read.
 */
const COOKIE_SECRET = process.env.OPERATOR_COOKIE_SECRET ?? randomBytes(32).toString("hex");

/** Headers a browser may influence. Everything else is dropped. */
const FORWARD_REQUEST = ["accept", "content-type", "idempotency-key", "accept-language", "x-correlation-id"];
const FORWARD_RESPONSE = ["content-type", "idempotent-replayed", "etag", "retry-after"];

/**
 * The paths the scenario key is attached to, and nothing else.
 *
 * `/v1/ops/` and `/v1/scenario/` are gated on it outright: the API refuses them without
 * it. The rest are the routes where `routers.evidence.scenario_key_ok` *widens* a read
 * from this session's own rows to the tenant's, which is what makes the console's order
 * list, refund list, inspector and proof pages show more than one buyer. `/v1/config` and
 * `/v1/catalogue/` are not here because the key changes nothing about them.
 */
const SCENARIO_KEY_PATHS = [
  "v1/ops/",
  "v1/scenario/",
  "v1/orders",
  "v1/refunds",
  "v1/inspector/",
  "v1/merchants/",
  "v1/audit/",
  "v1/checkouts/",
];

interface MintedSession {
  token: string;
  session_id: string;
  tenant_id: string;
  merchant_id: string;
  buyer_ref: string;
  actor_type: string;
  capabilities: string[];
  expires_at: string;
}

function problem(status: number, title: string, detail?: string): NextResponse {
  return NextResponse.json(
    { type: "about:blank", title, status, detail },
    { status, headers: { "Content-Type": "application/problem+json", "Cache-Control": "no-store" } },
  );
}

function needsScenarioKey(suffix: string): boolean {
  return SCENARIO_KEY_PATHS.some((prefix) => suffix === prefix || suffix.startsWith(prefix));
}

/**
 * A write that did not come from a page on this origin is refused.
 *
 * `Sec-Fetch-Site` is set by the browser and cannot be written by a page. For the browsers
 * that omit it, an `Origin` matching this host is accepted instead. A request carrying
 * neither is refused rather than trusted, which is stricter than a CSRF token would be:
 * a same-origin `fetch` always sends one of the two, so the callers this turns away are
 * exactly the ones that are not a page of this console.
 */
function sameOriginWrite(request: NextRequest): boolean {
  const site = request.headers.get("sec-fetch-site");
  if (site) return site === "same-origin" || site === "none";
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    return new URL(origin).host === (request.headers.get("host") ?? request.nextUrl.host);
  } catch {
    return false;
  }
}

/**
 * Mint an OPERATOR session.
 *
 * The scenario key is on this request as well as on the operating routes: the demo router
 * refuses an `OPERATOR` actor to a caller that does not already hold it, so "anyone who
 * can reach the demo router" never becomes "anyone who can read every order in the
 * tenant". This function is the only caller of that route in this process, and the
 * handler below refuses to proxy it on the browser's behalf.
 */
async function mint(): Promise<MintedSession | null> {
  try {
    const response = await fetch(`${API_BASE}/v1/demo/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Scenario-Key": SCENARIO_KEY },
      body: JSON.stringify({ tenant_slug: TENANT_SLUG, actor_type: "OPERATOR" }),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    if (!response.ok) return null;
    return (await response.json()) as MintedSession;
  } catch {
    return null;
  }
}

function tag(payload: string): string {
  return createHmac("sha256", COOKIE_SECRET).update(payload).digest("base64url");
}

/** Constant-time, and false rather than a throw when the two are different lengths. */
function tagMatches(expected: string, supplied: string): boolean {
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(supplied, "utf8");
  return a.length === b.length && timingSafeEqual(a, b);
}

/** Read the session this browser already has, if this process is the one that issued it. */
function readCookie(request: NextRequest): MintedSession | null {
  const raw = request.cookies.get(COOKIE)?.value;
  if (!raw) return null;
  const separator = raw.lastIndexOf(".");
  if (separator <= 0) return null;
  const payload = raw.slice(0, separator);
  if (!tagMatches(tag(payload), raw.slice(separator + 1))) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as MintedSession;
    return session?.token ? session : null;
  } catch {
    return null;
  }
}

function writeCookie(response: NextResponse, session: MintedSession): void {
  const payload = Buffer.from(JSON.stringify(session), "utf8").toString("base64url");
  response.cookies.set({
    name: COOKIE,
    value: `${payload}.${tag(payload)}`,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 8,
  });
}

async function forward(
  url: URL,
  suffix: string,
  method: string,
  headers: Headers,
  body: ArrayBuffer | undefined,
  token: string,
  signal: AbortSignal | null,
): Promise<Response> {
  const outbound = new Headers(headers);
  outbound.set("Authorization", `Bearer ${token}`);
  if (needsScenarioKey(suffix)) outbound.set("X-Scenario-Key", SCENARIO_KEY);
  const timeout = AbortSignal.timeout(UPSTREAM_TIMEOUT_MS);
  return fetch(url, {
    method,
    headers: outbound,
    body,
    cache: "no-store",
    redirect: "manual",
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
}

async function handle(request: NextRequest, context: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await context.params;
  const suffix = path.map((segment) => encodeURIComponent(decodeURIComponent(segment))).join("/");
  const method = request.method.toUpperCase();
  const reads = method === "GET" || method === "HEAD";

  // Minting is this file's business and no page's. Refused as 404 rather than 403 because
  // from the browser's side of this origin the route genuinely does not exist.
  if (suffix === "v1/demo" || suffix.startsWith("v1/demo/")) {
    return problem(
      404,
      "No such endpoint",
      "Sessions are minted by this console's own server, not on a page's behalf.",
    );
  }

  if (!reads && !sameOriginWrite(request)) {
    return problem(
      403,
      "Cross-site write refused",
      "This endpoint accepts writes only from a page served by this console.",
    );
  }

  let session = readCookie(request);
  let minted = false;
  if (!session) {
    // A read may open a session; a write may not. An operator write that arrives with no
    // session of its own is a write from something that never loaded this console, and
    // minting one for it is how the absence of a credential becomes a fast path to one.
    if (!reads) {
      return problem(
        401,
        "No operator session",
        "This browser holds no operator session for this console. Reload the console and try again.",
      );
    }
    session = await mint();
    minted = true;
    if (!session) {
      return problem(
        503,
        "The platform is not reachable",
        `Could not mint an operator session against ${API_BASE}. Start the API with \`make demo\`, ` +
          "and check that SCENARIO_KEY here matches the one the API was started with.",
      );
    }
  }

  // `/api/backend/session` answers from the cookie: who this console is, minus the token.
  // The response is built field by field rather than by removing one key from the session,
  // so a field added to the minted session later cannot reach the browser by default.
  if (suffix === "session") {
    const response = NextResponse.json(
      {
        session_id: session.session_id,
        tenant_id: session.tenant_id,
        merchant_id: session.merchant_id,
        buyer_ref: session.buyer_ref,
        actor_type: session.actor_type,
        capabilities: session.capabilities,
        expires_at: session.expires_at,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
    if (minted) writeCookie(response, session);
    return response;
  }

  const url = new URL(`${API_BASE}/${suffix}`);
  if (url.origin !== new URL(API_BASE).origin) {
    return problem(400, "Bad path", "That path does not address this API.");
  }
  request.nextUrl.searchParams.forEach((value, key) => url.searchParams.set(key, value));

  const headers = new Headers();
  for (const name of FORWARD_REQUEST) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const body = reads ? undefined : await request.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await forward(url, suffix, method, headers, body, session.token, request.signal);
    // The API restarted, or the session aged out: mint once and replay.
    if (upstream.status === 401) {
      const fresh = await mint();
      if (fresh) {
        session = fresh;
        minted = true;
        upstream = await forward(url, suffix, method, headers, body, session.token, request.signal);
      }
    }
  } catch {
    return problem(503, "The platform is not reachable", `No response from ${API_BASE}.`);
  }

  const responseHeaders = new Headers({ "Cache-Control": "no-store" });
  for (const name of FORWARD_RESPONSE) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  const response = new NextResponse(upstream.body, { status: upstream.status, headers: responseHeaders });
  if (minted) writeCookie(response, session);
  return response;
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;

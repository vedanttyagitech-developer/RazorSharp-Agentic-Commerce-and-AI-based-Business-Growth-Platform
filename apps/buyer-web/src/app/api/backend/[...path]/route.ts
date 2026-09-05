/**
 * The only place this app holds a credential.
 *
 * The browser talks to `/api/backend/...` on its own origin and never sees a bearer
 * token. This handler mints a demo session against the Commerce API, keeps the token in
 * an `httpOnly`, `sameSite=lax` cookie, and attaches it as `Authorization` on the way
 * through. An XSS that reaches `document.cookie` therefore still cannot read it, and a
 * screenshot of the network tab does not contain it.
 *
 * The cookie is signed, and that is not decoration. It used to be a bare base64 JSON blob
 * that this handler parsed and believed, including the `buyer_ref` inside it, and on an
 * upstream 401 it minted a fresh session for whatever identity that blob named. Presenting
 * a hand-written cookie was therefore enough to be issued a genuine, valid token bound to
 * somebody else's `buyer_ref` and to read their orders. `httpOnly` never protected against
 * that: it stops a script reading a cookie, not an attacker writing one. So the cookie now
 * carries an HMAC over its own bytes and is dropped whole when the tag does not verify,
 * and the 401 replay mints anonymously rather than taking an identity from the request.
 *
 * What it deliberately does NOT do:
 *
 *  - It does not invent a response when the API is unreachable. A storefront that
 *    silently falls back to fixtures shows a buyer a price no kernel ever agreed to.
 *    An unreachable API is a 503 problem document, and the UI says so.
 *  - It does not forward arbitrary headers. Only the small allowlist below crosses, so a
 *    caller cannot smuggle its own `Authorization` or `X-Scenario-Key` through.
 *  - It does not proxy anything but the configured API base. The path is joined onto that
 *    origin and re-parsed, so `..` segments cannot walk out of it.
 *  - It does not accept a write from another site. `SameSite=lax` is no defence here,
 *    because this handler will happily mint a session for a caller that presents no
 *    cookie at all, so a mutation has to prove it came from this origin.
 */
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

import { NextResponse, type NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const API_BASE = process.env.COMMERCE_API_URL ?? "http://localhost:8000";
const TENANT_SLUG = process.env.NEXT_PUBLIC_TENANT_SLUG ?? "demo";
const COOKIE = "acr_session";
const UPSTREAM_TIMEOUT_MS = 10_000;

/**
 * The key the cookie's tag is computed under.
 *
 * Configured in a deployment that has more than one process; random per process otherwise,
 * which is the safe default rather than a convenient one. A restart then invalidates every
 * outstanding cookie and each browser is minted a new anonymous session on its next read,
 * which costs a demo nothing and is the correct behaviour for a secret nobody chose.
 */
const COOKIE_SECRET = process.env.SESSION_COOKIE_SECRET ?? randomBytes(32).toString("hex");

/** Headers a browser may influence. Everything else is dropped. */
const FORWARD_REQUEST = ["accept", "content-type", "idempotency-key", "accept-language", "x-correlation-id"];
const FORWARD_RESPONSE = ["content-type", "idempotent-replayed", "etag", "retry-after"];

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

/**
 * A write that did not come from a page on this origin is refused.
 *
 * `Sec-Fetch-Site` is the primary signal and is set by the browser, not by the page. For
 * the browsers that omit it, an `Origin` that matches this host is accepted instead. A
 * request carrying neither is refused rather than trusted: a same-origin `fetch` always
 * sends at least one of them, so the only callers this turns away are the ones that are
 * not a page of this app.
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

async function mint(): Promise<MintedSession | null> {
  try {
    const response = await fetch(`${API_BASE}/v1/demo/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_slug: TENANT_SLUG, actor_type: "BUYER" }),
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

/**
 * Read the session this browser already has, if this process is the one that issued it.
 *
 * A cookie that fails the tag check is not repaired and not partly believed; it is
 * discarded, and the caller mints a fresh anonymous session as though there had been no
 * cookie at all. There is nothing in a forged one worth keeping.
 */
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
  method: string,
  headers: Headers,
  body: ArrayBuffer | undefined,
  token: string,
  signal: AbortSignal | null,
): Promise<Response> {
  const outbound = new Headers(headers);
  outbound.set("Authorization", `Bearer ${token}`);
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

  // Minting is this handler's own private business, never a page's -- exactly as the
  // console proxy refuses it. `POST /v1/demo/sessions` answers 201 with a *raw bearer
  // token* in a readable JSON body, and it honours a caller-supplied `buyer_ref`, so
  // proxying it for the browser hands page JavaScript a working token bound to an
  // identity the caller chose. That is precisely the "browser never sees a token"
  // property this file is built around; an XSS could otherwise read a durable, buyer_ref
  // -pickable credential straight out of a `fetch` response. The handler mints on its own
  // behalf through `mint()` (a direct upstream call, not this path), so nothing legitimate
  // is lost. 404 because from the browser's side of this origin the route does not exist.
  // The compare is lower-cased so a future case-insensitive upstream route cannot be
  // reached past the guard by asking for `v1/Demo/sessions`.
  const demoPath = suffix.toLowerCase();
  if (demoPath === "v1/demo" || demoPath.startsWith("v1/demo/")) {
    return problem(
      404,
      "No such endpoint",
      "Sessions are minted by this storefront's own server, not on a page's behalf.",
    );
  }

  if (!reads && !sameOriginWrite(request)) {
    return problem(
      403,
      "Cross-site write refused",
      "This endpoint accepts writes only from a page served by this storefront.",
    );
  }

  let session = readCookie(request);
  let minted = false;
  if (!session) {
    session = await mint();
    minted = true;
    if (!session) {
      return problem(
        503,
        "The store is not reachable",
        `Could not open a session against ${API_BASE}. Start the API with \`make demo\`.`,
      );
    }
  }

  // `/api/backend/session` answers from the cookie: who this browser is, minus the token.
  //
  // The reply is assembled field by field rather than by deleting `token` from the record,
  // so a field the API starts sending tomorrow is withheld until somebody decides it
  // belongs in the page. Omission is the safe default; subtraction is not.
  if (suffix === "session") {
    const safe = {
      session_id: session.session_id,
      tenant_id: session.tenant_id,
      merchant_id: session.merchant_id,
      buyer_ref: session.buyer_ref,
      actor_type: session.actor_type,
      capabilities: session.capabilities,
      expires_at: session.expires_at,
    };
    const response = NextResponse.json(safe, { headers: { "Cache-Control": "no-store" } });
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
    upstream = await forward(url, method, headers, body, session.token, request.signal);
    // The API restarted, or the session aged out: mint once and replay. Anonymously, and
    // never for the identity the request named -- a replay that reads its `buyer_ref` from
    // the caller is a way of asking to be somebody else.
    if (upstream.status === 401) {
      const fresh = await mint();
      if (fresh) {
        session = fresh;
        minted = true;
        upstream = await forward(url, method, headers, body, session.token, request.signal);
      }
    }
  } catch {
    return problem(503, "The store is not reachable", `No response from ${API_BASE}.`);
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

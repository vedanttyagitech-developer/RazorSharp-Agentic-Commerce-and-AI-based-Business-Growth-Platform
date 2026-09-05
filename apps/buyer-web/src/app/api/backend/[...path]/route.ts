/**
 * The only place this app puts a credential on an outbound request.
 *
 * The browser talks to `/api/backend/...` on its own origin and never sees a bearer
 * token. `lib/server/session` mints a demo session against the Commerce API and keeps the
 * token in a signed, `httpOnly`, `sameSite=lax` cookie; this handler attaches it as
 * `Authorization` on the way through. An XSS that reaches `document.cookie` therefore
 * still cannot read it, and a screenshot of the network tab does not contain it.
 *
 * Why the cookie is signed, and what a forged one does not buy, is written where the
 * signing is -- `lib/server/session`. The voice ticket route reads the same cookie
 * through the same verifier.
 *
 * What this handler deliberately does NOT do:
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
import { NextResponse, type NextRequest } from "next/server";

import {
  API_BASE,
  UPSTREAM_TIMEOUT_MS,
  mintSession,
  problem,
  readSessionCookie,
  sameOriginWrite,
  writeSessionCookie,
} from "@/lib/server/session";

export const dynamic = "force-dynamic";

/** Headers a browser may influence. Everything else is dropped. */
const FORWARD_REQUEST = ["accept", "content-type", "idempotency-key", "accept-language", "x-correlation-id"];
const FORWARD_RESPONSE = ["content-type", "idempotent-replayed", "etag", "retry-after"];

/**
 * A model-backed agent turn makes several round trips to Vertex and was measured at
 * 10.5 s for three of them, which is past the 10 s ceiling every other proxied call keeps.
 * Only the turn route gets the longer ceiling: a basket or checkout call that takes a
 * minute is a fault, and the short timeout is what surfaces it.
 */
const AGENT_TURN_TIMEOUT_MS = 60_000;

function timeoutFor(url: URL): number {
  return url.pathname.endsWith("/v1/agent/turn") ? AGENT_TURN_TIMEOUT_MS : UPSTREAM_TIMEOUT_MS;
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
  const timeout = AbortSignal.timeout(timeoutFor(url));
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
  /*
   * Encoded once, not decoded and re-encoded.
   *
   * Next has already decoded these catch-all segments before the handler runs, so the
   * `decodeURIComponent` that used to sit inside this line was a second decode, and it
   * was wrong twice. An identifier containing a literal `%` arrives here as a bare `%`
   * and `decodeURIComponent` throws `URIError` — outside every `try` in this module, so
   * the one route that is careful to answer every failure with a problem document
   * answered that one with a bare Next 500 and no content type at all. And an identifier
   * whose own characters spell a percent sequence, `A%20B`, was quietly decoded into
   * `A B` and forwarded as a different identifier than the browser asked for.
   *
   * Encoding the already-decoded segment is the whole job: it keeps `/` and `:` escaped,
   * which is what stops a segment from walking out of the API base below.
   */
  const suffix = path.map((segment) => encodeURIComponent(segment)).join("/");
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

  let session = readSessionCookie(request);
  let minted = false;
  if (!session) {
    session = await mintSession();
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
    if (minted) writeSessionCookie(response, session);
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
      const fresh = await mintSession();
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
  if (minted) writeSessionCookie(response, session);
  return response;
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;

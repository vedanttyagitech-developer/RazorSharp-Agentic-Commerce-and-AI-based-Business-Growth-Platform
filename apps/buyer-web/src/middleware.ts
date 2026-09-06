/**
 * Per-request security headers and a fresh CSP nonce.
 *
 * The nonce goes on the *forwarded request* as well as on the response, because that is
 * where Next reads it from: it parses an inbound `Content-Security-Policy`, lifts the
 * nonce out and stamps it onto the inline scripts it emits. `x-nonce` is this app's own
 * convention and Next has never heard of it, so setting only that left every inline
 * script unstamped.
 *
 * There is no per-route surgery on the policy any more. An earlier version handed
 * prerendered routes a weaker policy on the theory that a build-time document cannot
 * carry a per-request nonce -- true, but the reason those routes broke was
 * `'strict-dynamic'` voiding the `'self'` allowlist, and that is fixed at the source in
 * `lib/security/csp`. One policy for every route is easier to reason about and easier to
 * audit, and Razorpay's origins are still added only on the checkout path.
 */
import { NextResponse, type NextRequest } from "next/server";

import { SECURITY_HEADERS, checkoutPolicy, defaultPolicy, isCheckoutPath, newNonce } from "@/lib/security/csp";

export function middleware(request: NextRequest): NextResponse {
  const nonce = newNonce();
  const policy = isCheckoutPath(request.nextUrl.pathname) ? checkoutPolicy(nonce) : defaultPolicy(nonce);

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  // The header Next actually parses. Without it the nonce is a number nobody used.
  headers.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", policy);
  response.headers.set("x-nonce", nonce);
  for (const [name, value] of SECURITY_HEADERS) response.headers.set(name, value);
  return response;
}

export const config = {
  // Everything except Next's own internals and the local product imagery.
  //
  // `_next` is excluded WHOLESALE rather than by listing `_next/static` and `_next/image`,
  // because the one that was missing from that list is `_next/hmr` -- the dev server's
  // WebSocket -- and running middleware on a WebSocket upgrade breaks it: `NextResponse.next()`
  // answers with an ordinary HTTP response, so the handshake dies as `ERR_INVALID_HTTP_RESPONSE`.
  // Turbopack's dev runtime drives module loading over that socket, not just Fast Refresh, so
  // losing it means the app never hydrates at all: the page server-renders correctly, every
  // chunk returns 200, and then no button responds and every fetch-on-mount shelf stays a
  // skeleton. Nothing names CSP or middleware in the console, which is what made this
  // expensive to find -- and it is a permanent, not intermittent, `npm run dev` failure.
  //
  // Nothing under `_next` needs a per-request nonce: these are Next's own generated assets
  // and its dev channel, never a document that embeds an inline script of ours.
  matcher: ["/((?!_next|favicon.ico|products|brand|categories|fonts|infographics|subcategories).*)"],
};

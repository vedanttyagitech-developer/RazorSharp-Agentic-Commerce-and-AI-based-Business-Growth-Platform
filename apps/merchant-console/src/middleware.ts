/**
 * Per-request security headers and a fresh CSP nonce for the console.
 *
 * The nonce goes on the *forwarded request* as well as on the response, because that is
 * where Next reads it from: it parses an inbound `Content-Security-Policy`, lifts the nonce
 * out and stamps it onto the inline scripts it emits. `x-nonce` is this app's own
 * convention and Next has never heard of it, so setting only that would leave every inline
 * script unstamped and every page in this console dead behind an enforced policy.
 *
 * Setting that request header is also what stops these pages being served from a build-time
 * prerender: a nonce baked into a document rendered before the request existed is not a
 * secret, so Next renders them per request instead. That costs this console nothing. Every
 * page here is a live read of the platform and already answers `no-store`; a cached
 * operations screen is a screen that lies about the state of the queue.
 *
 * One policy for every route, because there is no route here that loads anything the
 * others do not. The storefront varies its policy on `/checkout` only to name a payment
 * provider, and this console never talks to one.
 *
 * A `Content-Security-Policy` set here replaces the one `next.config.ts` declares
 * statically rather than joining it -- verified against a production build, which answers
 * a single policy header carrying this nonce. The static headers beside it, which do not
 * vary per request, are re-set here so that this file states the whole security posture of
 * a response in one place instead of half of it.
 */
import { NextResponse, type NextRequest } from "next/server";

import { SECURITY_HEADERS, consolePolicy, newNonce } from "@/lib/security/csp";

export function middleware(request: NextRequest): NextResponse {
  const nonce = newNonce();
  const policy = consolePolicy(nonce);

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
  // Next's own static output carries no inline script of ours and is served from this
  // origin under the same `'self'`, so re-signing it per request would buy nothing.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

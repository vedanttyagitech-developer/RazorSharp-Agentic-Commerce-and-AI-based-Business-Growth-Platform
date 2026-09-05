/**
 * The buyer's session cookie, and the only code that is allowed to believe one.
 *
 * This lived inside `app/api/backend/[...path]/route.ts` until a second route needed it.
 * It was lifted out rather than copied, because the thing it does -- decide whether a
 * cookie was issued by this process -- is the whole of the storefront's authentication,
 * and two implementations of that would eventually disagree about which one is stricter.
 * The route that proxies the API and the route that mints a voice ticket now read the
 * same bytes through the same verifier.
 *
 * The cookie is signed, and that is not decoration. It used to be a bare base64 JSON blob
 * that the proxy parsed and believed, including the `buyer_ref` inside it, and on an
 * upstream 401 it minted a fresh session for whatever identity that blob named. Presenting
 * a hand-written cookie was therefore enough to be issued a genuine, valid token bound to
 * somebody else's `buyer_ref` and to read their orders. `httpOnly` never protected against
 * that: it stops a script reading a cookie, not an attacker writing one. So the cookie
 * carries an HMAC over its own bytes and is dropped whole when the tag does not verify,
 * and a 401 replay mints anonymously rather than taking an identity from the request.
 */
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

import { NextResponse, type NextRequest } from "next/server";

export const API_BASE = process.env.COMMERCE_API_URL ?? "http://localhost:8000";
export const UPSTREAM_TIMEOUT_MS = 10_000;

const TENANT_SLUG = process.env.NEXT_PUBLIC_TENANT_SLUG ?? "demo";
const COOKIE = "acr_session";

/**
 * The key the cookie's tag is computed under.
 *
 * Configured in a deployment that has more than one process; random per process otherwise,
 * which is the safe default rather than a convenient one. A restart then invalidates every
 * outstanding cookie and each browser is minted a new anonymous session on its next read,
 * which costs a demo nothing and is the correct behaviour for a secret nobody chose.
 */
const COOKIE_SECRET = process.env.SESSION_COOKIE_SECRET ?? randomBytes(32).toString("hex");

export interface MintedSession {
  token: string;
  session_id: string;
  tenant_id: string;
  merchant_id: string;
  buyer_ref: string;
  actor_type: string;
  capabilities: string[];
  expires_at: string;
}

export function problem(status: number, title: string, detail?: string): NextResponse {
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
export function sameOriginWrite(request: NextRequest): boolean {
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

export async function mintSession(): Promise<MintedSession | null> {
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
export function readSessionCookie(request: NextRequest): MintedSession | null {
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

export function writeSessionCookie(response: NextResponse, session: MintedSession): void {
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

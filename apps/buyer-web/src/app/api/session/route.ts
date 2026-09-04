/**
 * Session route handler.
 *
 * POST mints a pseudonymous buyer session by calling POST /v1/demo/sessions server-side
 * and stores the bearer token in an HttpOnly cookie. The token is never returned to the
 * browser, so no JavaScript-accessible storage ever holds it (spec 21.1, 21.5).
 *
 * This handler is routing only. It grants nothing: FastAPI verifies the session on every
 * proxied request.
 */
import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { DemoSessionSchema, type SessionInfo } from "@/lib/api/types";
import { SESSION_COOKIE, apiBase, apiMode, isProduction } from "@/lib/server/env";

export const dynamic = "force-dynamic";

function info(partial: Partial<SessionInfo>): SessionInfo {
  return { active: false, session_id: null, expires_at: null, mode: apiMode(), ...partial };
}

export async function GET(): Promise<NextResponse<SessionInfo>> {
  const jar = await cookies();
  const active = jar.get(SESSION_COOKIE)?.value ? true : false;
  return NextResponse.json(info({ active }), { headers: { "Cache-Control": "no-store" } });
}

export async function POST(): Promise<NextResponse> {
  if (apiMode() === "mock") {
    return NextResponse.json(info({ active: true, session_id: "mock" }), { headers: { "Cache-Control": "no-store" } });
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/v1/demo/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", "Idempotency-Key": crypto.randomUUID() },
      // PROVISIONAL: reconcile with OpenAPI after commerce-api lands.
      body: JSON.stringify({ actor_type: "BUYER" }),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { type: "about:blank", title: "Commerce API unreachable", status: 503, detail: `Could not reach ${apiBase()}` },
      { status: 503, headers: { "Content-Type": "application/problem+json", "Cache-Control": "no-store" } },
    );
  }
  if (!upstream.ok) {
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/problem+json", "Cache-Control": "no-store" },
    });
  }
  const parsed = DemoSessionSchema.safeParse(await upstream.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      { type: "urn:buyer-web:contract-mismatch", title: "Session response did not match the provisional contract", status: 502 },
      { status: 502, headers: { "Content-Type": "application/problem+json", "Cache-Control": "no-store" } },
    );
  }
  const session = parsed.data;
  const expires = new Date(session.expires_at);
  const maxAge = Number.isNaN(expires.getTime()) ? 3600 : Math.max(60, Math.floor((expires.getTime() - Date.now()) / 1000));
  const response = NextResponse.json(info({ active: true, session_id: session.session_id, expires_at: session.expires_at }), {
    headers: { "Cache-Control": "no-store" },
  });
  response.cookies.set({
    name: SESSION_COOKIE,
    value: session.token,
    httpOnly: true,
    sameSite: "lax",
    secure: isProduction(),
    path: "/",
    maxAge,
  });
  return response;
}

export async function DELETE(): Promise<NextResponse> {
  const response = NextResponse.json(info({ active: false }), { headers: { "Cache-Control": "no-store" } });
  response.cookies.set({ name: SESSION_COOKIE, value: "", httpOnly: true, sameSite: "lax", secure: isProduction(), path: "/", maxAge: 0 });
  return response;
}

/**
 * Same-origin pass-through to the commerce API.
 *
 * Attaches the bearer token from the HttpOnly session cookie and forwards the request
 * verbatim: method, JSON body, Idempotency-Key, Accept and Last-Event-ID. Responses stream
 * back unchanged, which is what lets EventSource consume /v1/checkouts/{id}/events.
 *
 * This is routing, not authorization (spec 21.5): it adds a header and nothing else. It
 * refuses paths outside the buyer surface so the browser can never reach the scenario
 * controller, the webhook inbox or operator routes through this origin.
 */
import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE, apiBase, apiMode } from "@/lib/server/env";

export const dynamic = "force-dynamic";

const ALLOWED: ReadonlyArray<{ method: string; pattern: RegExp }> = [
  { method: "GET", pattern: /^v1\/catalogue\/(search|products\/[^/]+)$/ },
  { method: "POST", pattern: /^v1\/baskets$/ },
  { method: "GET", pattern: /^v1\/baskets\/[^/]+$/ },
  { method: "PUT", pattern: /^v1\/baskets\/[^/]+\/lines\/[^/]+$/ },
  { method: "POST", pattern: /^v1\/baskets\/[^/]+\/checkout$/ },
  { method: "GET", pattern: /^v1\/checkouts\/[^/]+(\/(payment|timeline|events|proof))?$/ },
  { method: "POST", pattern: /^v1\/checkouts\/[^/]+\/versions\/\d+\/(approve|reject|submit)$/ },
  { method: "POST", pattern: /^v1\/checkouts\/[^/]+\/cancel$/ },
  { method: "POST", pattern: /^v1\/payments\/verify$/ },
  { method: "GET", pattern: /^v1\/orders\/[^/]+$/ },
  { method: "POST", pattern: /^v1\/orders\/[^/]+\/refunds$/ },
  { method: "GET", pattern: /^v1\/inspector\/payment-attempts\/[^/]+$/ },
  { method: "GET", pattern: /^v1\/audit\/streams\/[^/]+\/[^/]+\/verify$/ },
  { method: "GET", pattern: /^v1\/config$/ },
  { method: "GET", pattern: /^v1\/ops\/safe-mode$/ },
];

const FORWARD_REQUEST_HEADERS = ["accept", "content-type", "idempotency-key", "last-event-id", "accept-language"];
const FORWARD_RESPONSE_HEADERS = ["content-type", "idempotent-replayed", "etag", "location", "retry-after"];

function problem(status: number, title: string, detail?: string): NextResponse {
  return NextResponse.json(
    { type: "about:blank", title, status, detail },
    { status, headers: { "Content-Type": "application/problem+json", "Cache-Control": "no-store" } },
  );
}

async function handle(request: NextRequest, context: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await context.params;
  const upstreamPath = path.map(decodeURIComponent).join("/");
  const method = request.method.toUpperCase();
  if (!ALLOWED.some((rule) => rule.method === method && rule.pattern.test(upstreamPath))) {
    return problem(404, "Not routed", `${method} /${upstreamPath} is not part of the buyer surface`);
  }
  if (apiMode() === "mock") {
    return problem(503, "Mock mode", "NEXT_PUBLIC_API_MODE=mock: the browser client answers locally; nothing is proxied");
  }

  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return problem(401, "No session", "Create a session with POST /api/session first");

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("Authorization", `Bearer ${token}`);

  const url = new URL(`${apiBase()}/${upstreamPath}`);
  request.nextUrl.searchParams.forEach((value, key) => {
    // EventSource cannot set headers on its first request; the client passes the resume
    // point as a query parameter and it becomes the real Last-Event-ID upstream.
    if (key === "last_event_id") headers.set("Last-Event-ID", value);
    else url.searchParams.set(key, value);
  });

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method,
      headers,
      body: method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
      signal: request.signal,
    });
  } catch {
    return problem(503, "Commerce API unreachable", `Could not reach ${apiBase()}`);
  }

  const responseHeaders = new Headers({ "Cache-Control": "no-store" });
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  if ((upstream.headers.get("content-type") ?? "").includes("text/event-stream")) {
    responseHeaders.set("X-Accel-Buffering", "no");
    responseHeaders.set("Connection", "keep-alive");
  }
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;

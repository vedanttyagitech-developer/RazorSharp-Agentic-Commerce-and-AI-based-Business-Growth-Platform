/**
 * Server-side Route Handler proxy for Merchant Console.
 *
 * Rule 5: No secret in frontend code.
 * Injects X-Scenario-Key and operator authentication server-side before
 * forwarding requests to the live Commerce API (http://localhost:8000).
 */
import { NextResponse, type NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const COMMERCE_API_BASE = process.env.COMMERCE_API_URL || "http://localhost:8000";
const SCENARIO_KEY = process.env.SCENARIO_KEY || "local-demo-scenario-key";

const FORWARD_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "idempotency-key",
  "authorization",
  "accept-language",
];

const FORWARD_RESPONSE_HEADERS = [
  "content-type",
  "idempotent-replayed",
  "etag",
  "location",
  "retry-after",
];

function problem(status: number, title: string, detail?: string): NextResponse {
  return NextResponse.json(
    { type: "about:blank", title, status, detail },
    {
      status,
      headers: {
        "Content-Type": "application/problem+json",
        "Cache-Control": "no-store",
      },
    }
  );
}

async function handle(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { path } = await context.params;
  const upstreamPath = path.map(decodeURIComponent).join("/");
  const method = request.method.toUpperCase();

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  // Inject operator scenario key server-side
  headers.set("X-Scenario-Key", SCENARIO_KEY);

  // When mock mode is enabled, avoid attempting connection to unreachable port 8000
  if (process.env.NEXT_PUBLIC_API_MODE === "mock") {
    if (upstreamPath === "v1/scenario/injections" && method === "POST") {
      return NextResponse.json({
        injection_id: `inj_mock_${Date.now()}`,
        kind: "SCENARIO_INJECTION",
        label: "SCENARIO_INJECTION (Mock Proxy)",
        new_catalogue_revision: 14,
      });
    }
    return problem(
      503,
      "Mock Mode Active",
      "NEXT_PUBLIC_API_MODE=mock: browser client answers locally with transparent fixtures."
    );
  }

  const url = new URL(`${COMMERCE_API_BASE}/${upstreamPath}`);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  let upstream: Response;
  try {
    const timeoutSignal = AbortSignal.timeout(800);
    const combinedSignal = request.signal
      ? AbortSignal.any([request.signal, timeoutSignal])
      : timeoutSignal;

    upstream = await fetch(url, {
      method,
      headers,
      body: method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
      signal: combinedSignal,
    });
  } catch {
    return problem(
      503,
      "Commerce API Unreachable",
      `Could not reach backend at ${COMMERCE_API_BASE}. Console is displaying transparent fallback data.`
    );
  }

  const responseHeaders = new Headers({ "Cache-Control": "no-store" });
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;

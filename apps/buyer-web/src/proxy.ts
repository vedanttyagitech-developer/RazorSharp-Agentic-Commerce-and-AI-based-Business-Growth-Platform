/**
 * Next.js 16 proxy (formerly middleware). Routing and headers only, never authorization
 * (spec 21.5). It mints a per-request nonce and sets the Content-Security-Policy so that
 * Next's own inline scripts carry the nonce and nothing else inline may run.
 */
import { NextResponse, type NextRequest } from "next/server";

import { buildCsp, isPaymentRoute } from "@/lib/security/csp";

export function proxy(request: NextRequest): NextResponse {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = buildCsp({
    nonce,
    paymentRoute: isPaymentRoute(request.nextUrl.pathname),
    dev: process.env.NODE_ENV !== "production",
  });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|txt|xml)$).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};

/**
 * Mint a voice ticket for the browser, using a bearer the browser is never given.
 *
 * This route exists because of a gap that has no other honest bridge. The voice gateway's
 * `POST /v1/voice/tickets` authenticates with `Authorization: Bearer <buyer token>`, and
 * that token lives in this storefront's signed `httpOnly` cookie -- deliberately out of
 * reach of page script (`lib/server/session`). A browser therefore cannot mint its own
 * ticket against the gateway, in development or anywhere else: it has nothing to present.
 * So the mint is same-origin and server-side. The buyer's page asks this route, this route
 * asks the gateway with the bearer, and the browser receives an opaque single-use handle
 * that carries no authority of its own.
 *
 * That is also why `defaultTicketUrl` in `features/voice/session.ts` is same-origin in
 * every environment, while `defaultVoiceUrl` is not: the ticket needs a credential and the
 * socket needs only the ticket.
 *
 * WHAT CROSSES, AND WHAT DOES NOT
 * -------------------------------
 * Out of this route goes `{ticket, expires_in_s, session_id, speech_available}` and
 * nothing else, assembled field by field rather than by forwarding the gateway's body. A
 * field the gateway starts sending tomorrow is withheld until somebody decides it belongs
 * in a page -- the same rule `/api/backend/session` follows, for the same reason.
 *
 * The bearer goes only outbound, to the gateway, over the loopback or the deployment's own
 * network. The gateway holds it server-side for the socket's lifetime and every
 * consequential call it makes carries it back to the trusted server, so tenancy and every
 * capability check happen exactly where they do for typed input. Nothing this route hands
 * back can approve, pay, refund or cancel: a ticket buys one WebSocket and no authority.
 */
import { NextResponse, type NextRequest } from "next/server";

import {
  UPSTREAM_TIMEOUT_MS,
  mintSession,
  problem,
  readSessionCookie,
  sameOriginWrite,
  writeSessionCookie,
  type MintedSession,
} from "@/lib/server/session";

export const dynamic = "force-dynamic";

/**
 * Where THIS SERVER reaches the gateway.
 *
 * Distinct from `NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN`, which is where the BROWSER reaches it
 * to open the socket, and the two are genuinely different addresses in a deployment: this
 * one may be an internal name that no browser can resolve, which is the point of minting
 * here rather than there.
 */
const GATEWAY_URL = (process.env.VOICE_GATEWAY_URL ?? "http://127.0.0.1:8100").replace(/\/+$/, "");

interface GatewayTicket {
  ticket: string;
  expires_in_s: number;
  session_id: string;
  speech_available: boolean;
}

async function mintTicket(token: string, signal: AbortSignal | null): Promise<Response> {
  const timeout = AbortSignal.timeout(UPSTREAM_TIMEOUT_MS);
  return fetch(`${GATEWAY_URL}/v1/voice/tickets`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    cache: "no-store",
    redirect: "manual",
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
}

export async function POST(request: NextRequest): Promise<Response> {
  // A POST, so the cross-site guard applies. It is not ceremony here: this route will mint
  // a session for a caller that presents no cookie at all, so another site could otherwise
  // open a voice socket that speaks as a fresh buyer on this storefront's behalf.
  if (!sameOriginWrite(request)) {
    return problem(
      403,
      "Cross-site write refused",
      "A voice ticket is minted only for a page served by this storefront.",
    );
  }

  let session: MintedSession | null = readSessionCookie(request);
  let minted = false;
  if (!session) {
    session = await mintSession();
    minted = true;
    if (!session) {
      return problem(
        503,
        "The store is not reachable",
        "Could not open a session to mint a voice ticket against.",
      );
    }
  }

  let upstream: Response;
  try {
    upstream = await mintTicket(session.token, request.signal);
    // 401 is "no usable bearer" and 403 is the gateway's answer when the trusted server
    // would not resolve this session -- an API restart makes both of them mean the same
    // thing. Mint once and replay, anonymously: a replay that took its identity from the
    // request would be a way of asking to be somebody else, here as much as in the proxy.
    if (upstream.status === 401 || upstream.status === 403) {
      const fresh = await mintSession();
      if (fresh) {
        session = fresh;
        minted = true;
        upstream = await mintTicket(session.token, request.signal);
      }
    }
  } catch {
    return problem(
      503,
      "The voice gateway is not reachable",
      `No response from ${GATEWAY_URL}. Start it with \`uv run --no-sync python -m voice_runtime.gateway\`.`,
    );
  }

  if (!upstream.ok) {
    // The gateway's own detail is not passed through. It is written for an operator and
    // may name the trusted server's answer; the buyer is told the truth at the altitude
    // the panel can act on, which is that speech is unavailable and typing still works.
    return problem(
      502,
      "The voice gateway refused this session",
      `The gateway answered ${upstream.status} when asked for a ticket.`,
    );
  }

  let body: GatewayTicket;
  try {
    body = (await upstream.json()) as GatewayTicket;
  } catch {
    return problem(502, "The voice gateway sent an unreadable ticket");
  }
  if (typeof body?.ticket !== "string" || body.ticket.length === 0) {
    return problem(502, "The voice gateway sent no ticket");
  }

  const response = NextResponse.json(
    {
      ticket: body.ticket,
      expires_in_s: body.expires_in_s,
      session_id: body.session_id,
      speech_available: body.speech_available,
    },
    // `no-store` twice over: a ticket is single-use, and a cached one is a ticket that has
    // already been spent by whoever read the cache.
    { headers: { "Cache-Control": "no-store" } },
  );
  if (minted) writeSessionCookie(response, session);
  return response;
}

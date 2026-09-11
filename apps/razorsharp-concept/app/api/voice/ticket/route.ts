// The voice ticket, minted where the credential lives.
//
// `POST /v1/voice/tickets` on the gateway wants the buyer's bearer, and the browser does
// not have one -- that is the whole point of the commerce bridge, which keeps the token in
// an HttpOnly cookie the page cannot read. So the ticket is minted here, server-side, and
// only the ticket crosses to the browser.
//
// A ticket is not a credential: random bytes, sixty seconds, consumed on first connection,
// and it carries no authority of its own (`voice_runtime.wire.tickets`). That is what makes
// it safe to put in a query string, which is the only place a browser can put anything on a
// WebSocket handshake -- browsers cannot set headers on an upgrade.
//
// The gateway's own origin check is the second lock: it admits the storefront's origin and
// refuses the socket otherwise, so a ticket that did leak is useless from anywhere else.

import { publicOrigin } from '@/lib/public-origin';

const GATEWAY = process.env.VOICE_GATEWAY_URL || 'http://127.0.0.1:8100';

/**
 * The socket the browser should open.
 *
 * Same-origin whenever a proxy is in front, and that is the correction. It used to be
 * derived from `VOICE_GATEWAY_URL`, which is right on a laptop -- where that is
 * `127.0.0.1:8100` and the browser can reach it -- and wrong the moment the gateway is a
 * container: the address becomes `voice:8100`, a name only the compose network resolves,
 * and the browser is handed a socket it cannot open.
 *
 * Reading it off the request instead means the socket follows the host the page was
 * actually served from, with no second place to configure. That matters more than it
 * sounds: this deployment answers on two hostnames, and a socket pinned to one of them
 * would fail the gateway's own origin check when the page came from the other.
 */
function socketUrl(request: Request): string {
  const origin = publicOrigin(request);
  // A proxy in front means /v1/voice/stream is published on this same origin; without one
  // there is no proxy to route through and the gateway's own address is the only answer.
  const proxied = request.headers.get('x-forwarded-proto') !== null;
  const url = new URL('/v1/voice/stream', proxied ? origin : GATEWAY);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export async function POST(request: Request): Promise<Response> {
  if (process.env.NODE_ENV === 'production' && process.env.RESERVE_LOCAL_DEMO !== 'true')
    return Response.json({ detail: 'Local voice bridge is disabled.' }, { status: 404 });

  const origin = request.headers.get('origin');
  // The browser's origin, not this process's: behind a proxy terminating TLS they differ
  // by scheme, and comparing against the inner one refuses every mint.
  if (origin && origin !== publicOrigin(request))
    return Response.json({detail:'Cross-origin voice request refused.'},{status:403});

  const cookies = Object.fromEntries(
    (request.headers.get('cookie') || '').split(';').map((v) => v.trim().split('=')),
  );
  const token = cookies.rs_buyer_token;
  if (!token)
    // Deliberately not minting a session here. The commerce bridge owns identity, and a
    // second minting path would be a second place a buyer can be created -- with its own
    // idea of who they are. Load the shop first; that is what puts the cookie there.
    return Response.json(
      {
        detail: 'No shopping session yet. Open the shop, then start a voice conversation.',
        title: 'Voice needs a session',
      },
      { status: 409 },
    );

  let minted: Response;
  try {
    minted = await fetch(`${GATEWAY}/v1/voice/tickets`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      signal: request.signal,
    });
  } catch {
    return Response.json(
      {
        detail: 'The voice gateway is not reachable. Typing still works.',
        title: 'Voice unavailable',
      },
      { status: 503 },
    );
  }

  const body = await minted.text();
  if (!minted.ok)
    return new Response(body, {
      status: minted.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    });

  // The socket URL travels with the ticket so the page never has to know where the gateway
  // is -- one place configures it, and it is the server.
  const ticket = JSON.parse(body) as Record<string, unknown>;
  return Response.json(
    { ...ticket, socket_url: socketUrl(request) },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}

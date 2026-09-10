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

const GATEWAY = process.env.VOICE_GATEWAY_URL || 'http://127.0.0.1:8100';

/** The socket the browser should open, derived from the gateway's own address. */
function socketUrl(): string {
  const url = new URL('/v1/voice/stream', GATEWAY);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export async function POST(request: Request): Promise<Response> {
  if (process.env.NODE_ENV === 'production' && process.env.RESERVE_LOCAL_DEMO !== 'true')
    return Response.json({ detail: 'Local voice bridge is disabled.' }, { status: 404 });

  const origin = request.headers.get('origin');
  if (origin && origin !== new URL(request.url).origin)
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
    { ...ticket, socket_url: socketUrl() },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}

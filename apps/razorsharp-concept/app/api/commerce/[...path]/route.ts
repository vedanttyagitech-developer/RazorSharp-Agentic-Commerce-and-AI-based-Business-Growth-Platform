// Local development bridge to the commerce API.
//
// Credentials stay in an HttpOnly cookie and never reach UI state. This is NOT production
// authentication: it mints a demo buyer session on first use, which only the development
// profile of the backend offers. `RESERVE_LOCAL_DEMO` must be set to keep it alive in a
// production build, and it answers 404 otherwise.
//
// The allowlist below is a table rather than one regex because it has to carry a *method*
// per route: a proxy that forwards any verb to a matched path is an open proxy with extra
// steps. Three rules govern what may be added to it:
//
//   1. Buyer surface only. `/v1/ops`, `/v1/scenario` and `/v1/merchant` are operator and
//      merchant surfaces and are deliberately absent -- a browser parameter must never be
//      able to select a privileged role.
//   2. `reserve/simulator/{id}` is NEVER here. It is the protected fixture that chooses a
//      simulated provider outcome, it is guarded by the scenario key on the backend, and
//      exposing it through a buyer proxy would let the buyer decide whether their own
//      payment succeeded.
//   3. Money moves only through routes the Kernel admits. This bridge adds no capability;
//      it forwards a request the backend would have accepted anyway.

import { publicOrigin } from '@/lib/public-origin';

const base = process.env.COMMERCE_API_URL || 'http://127.0.0.1:8000';

/** 400 days: the ceiling browsers place on a cookie's life, and so the longest "keep me". */
const BUYER_REF_MAX_AGE_SECONDS = 400 * 24 * 60 * 60;

type Rule = { pattern: RegExp; methods: readonly string[] };

const UUID = '[0-9a-fA-F-]{36}';
const SKU = '[A-Za-z0-9._-]{1,64}';

const ALLOWED: readonly Rule[] = [
  // --- catalogue: grounded discovery -------------------------------------------------
  { pattern: /^catalogue\/search$/, methods: ['GET'] },
  { pattern: /^catalogue\/products$/, methods: ['GET'] },
  { pattern: new RegExp(`^catalogue/products/${SKU}$`), methods: ['GET'] },

  // --- cart ---------------------------------------------------------------------------
  { pattern: /^carts$/, methods: ['POST'] },
  { pattern: /^carts\/current$/, methods: ['GET'] },
  { pattern: new RegExp(`^carts/${UUID}$`), methods: ['GET'] },
  { pattern: new RegExp(`^carts/${UUID}/lines/${SKU}$`), methods: ['PUT'] },
  { pattern: new RegExp(`^carts/${UUID}/checkout$`), methods: ['POST'] },

  // --- checkout and the trusted approval surface ---------------------------------------
  // The buyer's own checkouts. Scoped to them by the backend, which answers `own` for a
  // session with no operator key; this bridge cannot widen it, because it forwards no
  // scenario key and the backend reads scope from the request rather than the query.
  { pattern: /^checkouts$/, methods: ['GET'] },
  { pattern: new RegExp(`^checkouts/${UUID}$`), methods: ['GET'] },
  { pattern: new RegExp(`^checkouts/${UUID}/payment$`), methods: ['GET'] },
  { pattern: new RegExp(`^checkouts/${UUID}/timeline$`), methods: ['GET'] },
  { pattern: new RegExp(`^checkouts/${UUID}/proof$`), methods: ['GET'] },
  { pattern: new RegExp(`^checkouts/${UUID}/events$`), methods: ['GET'] },
  { pattern: new RegExp(`^checkouts/${UUID}/cancel$`), methods: ['POST'] },
  {
    pattern: new RegExp(`^checkouts/${UUID}/versions/\\d+/(approve|approve-and-pay|hold|reject|submit)$`),
    methods: ['POST'],
  },

  // --- payment handoff and the client-return report -------------------------------------
  { pattern: /^payments\/(verify|reconcile)$/, methods: ['POST'] },

  // --- orders, refund visibility and buyer support --------------------------------------
  { pattern: /^orders$/, methods: ['GET'] },
  { pattern: new RegExp(`^orders/${UUID}$`), methods: ['GET'] },
  { pattern: new RegExp(`^orders/${UUID}/(refundable|policy|resolution|payment-acknowledgement)$`), methods: ['GET'] },
  { pattern: new RegExp(`^orders/${UUID}/support-cases$`), methods: ['GET', 'POST'] },
  { pattern: /^support\/cases$/, methods: ['GET'] },
  { pattern: new RegExp(`^support/cases/${UUID}$`), methods: ['GET'] },

  // --- the copilot ------------------------------------------------------------------------
  { pattern: /^agent\/turn$/, methods: ['POST'] },
  { pattern: /^agent\/capabilities$/, methods: ['GET'] },

  // --- Reserve Pay. `simulator` is absent on purpose; see rule 2 above. -------------------
  { pattern: /^reserve\/verification-keys$/, methods: ['GET'] },
  { pattern: new RegExp(`^reserve/authorities/${UUID}/proof$`), methods: ['GET'] },
  { pattern: /^reserve\/authorities$/, methods: ['GET', 'POST'] },
  { pattern: new RegExp(`^reserve/authorities/${UUID}$`), methods: ['GET'] },
  { pattern: new RegExp(`^reserve/authorities/${UUID}/revoke$`), methods: ['POST'] },
  { pattern: new RegExp(`^reserve/checkouts/${UUID}/versions/\\d+/pay$`), methods: ['POST'] },
  { pattern: new RegExp(`^reserve/payments/${UUID}$`), methods: ['GET'] },
];

function permitted(path: string, method: string): boolean {
  return ALLOWED.some((rule) => rule.pattern.test(path) && rule.methods.includes(method));
}

async function forward(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const url = new URL(request.url);
  if (process.env.NODE_ENV === 'production' && process.env.RESERVE_LOCAL_DEMO !== 'true')
    return Response.json({ detail: 'Local commerce bridge is disabled.' }, { status: 404 });

  // A same-origin check on writes. The cookie is SameSite=Strict, so this is the second
  // lock rather than the only one.
  if (
    request.method !== 'GET' &&
    request.headers.get('origin') &&
    request.headers.get('origin') !== publicOrigin(request)
  )
    return Response.json({ detail: 'Cross-origin write refused.' }, { status: 403 });

  const path = (await context.params).path.join('/');
  if (!permitted(path, request.method))
    return Response.json(
      { detail: 'Route unavailable through this bridge.', title: 'Not proxied' },
      { status: 404 },
    );

  const cookies = Object.fromEntries(
    (request.headers.get('cookie') || '').split(';').map((v) => v.trim().split('=')),
  );
  let token = cookies.rs_buyer_token;
  // The buyer's pseudonymous LABEL, kept in its own cookie beside the token.
  //
  // Identity used to live only inside the token, so a session the backend no longer had a
  // row for took the buyer's whole history with it: minting again produced a fresh random
  // `buyer-xxxxxxxx`, and their orders and Reserve permissions -- still in the database,
  // under the old label -- became unreachable. `POST /v1/demo/sessions` has always accepted
  // a `buyer_ref` for exactly this ("so a reloaded demo can rejoin its own cart"); nothing
  // was passing it.
  //
  // It is a label, not a credential: it authorises nothing on its own, and a caller who set
  // one by hand could equally call `/v1/demo/sessions` themselves with any label they liked.
  // Rule 3 above still holds -- this bridge adds no capability the backend would not grant.
  let buyerRef = cookies.rs_buyer_ref;
  let minted = false;

  /** Mint a session, rejoining `buyerRef` when there is one. False if the backend refuses. */
  const mint = async (): Promise<boolean> => {
    const session = await fetch(`${base}/v1/demo/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_slug: process.env.COMMERCE_TENANT_SLUG || 'demo',
        actor_type: 'BUYER',
        ...(buyerRef ? { buyer_ref: buyerRef } : {}),
      }),
    });
    if (!session.ok) return false;
    const body = (await session.json()) as { token: string; buyer_ref: string | null };
    token = body.token;
    if (body.buyer_ref) buyerRef = body.buyer_ref;
    minted = true;
    return true;
  };

  try {
    if (!token && !(await mint()))
      return Response.json(
        { detail: 'Backend demo session unavailable. Check API and tenant configuration.' },
        { status: 503 },
      );

    // Read once: a request body is a stream, and the retry below would find it consumed.
    const payload = request.method === 'GET' ? undefined : await request.text();

    const send = () => {
      const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
      if (request.method !== 'GET') headers['Content-Type'] = 'application/json';
      // Forwarded verbatim: a retry must carry the key of the request it is retrying, or the
      // backend replays somebody else's answer -- or worse, executes twice.
      const key = request.headers.get('Idempotency-Key');
      if (key) headers['Idempotency-Key'] = key;
      const correlation = request.headers.get('X-Correlation-Id');
      if (correlation) headers['X-Correlation-Id'] = correlation;
      if (request.headers.get('accept')?.includes('text/event-stream'))
        headers.Accept = 'text/event-stream';
      const lastEvent = request.headers.get('Last-Event-ID');
      if (lastEvent) headers['Last-Event-ID'] = lastEvent;
      return fetch(`${base}/v1/${path}${url.search}`, {
        method: request.method,
        headers,
        body: payload,
        // Bound payment HTTP waits without timing out agent/SSE conversations.
        // A timeout is unknown, never a failed transaction; the same key is retained.
        signal: /^(payments|checkouts)\//.test(path) && !headers.Accept
          ? AbortSignal.any([request.signal, AbortSignal.timeout(20_000)])
          : request.signal,
      });
    };

    let result = await send();
    // One retry, for one status, once.
    //
    // 401 is the backend saying it does not recognise this token, and it decides that in
    // authentication -- before any handler runs -- so nothing executed under it. Without
    // this the buyer was stranded: the cookie is HttpOnly, so nothing in the page could
    // clear it, the bridge only minted when the cookie was ABSENT, and every retry answered
    // 401 again. The screen said "the session has expired; mint a new one" beside a button
    // that could not.
    //
    // The rule this replaces -- never remint on a failed write, because the old token may
    // own a payment whose outcome is unresolved -- was right about a write that FAILED, and
    // does not describe this: a token the backend has no row for owns nothing it could lose
    // sight of, and the new session rejoins the same `buyer_ref`, so anything the old one
    // did own is still in front of the buyer. The Idempotency-Key rides along unchanged, so
    // a resent write cannot execute twice either.
    //
    // `!minted` is the guard against looping: if the token we just minted is refused, the
    // fault is not staleness and a second mint would not help.
    if (result.status === 401 && !minted && (await mint())) result = await send();

    const upstream = result.headers.get('content-type') || 'application/json';
    const out = new Headers({ 'Content-Type': upstream, 'Cache-Control': 'no-store' });
    if (minted) {
      // The token is a CREDENTIAL and stays a session cookie: it should not outlive the
      // browser, and it does not need to -- the label below is enough to mint another one
      // for the same person.
      out.append('Set-Cookie', `rs_buyer_token=${token}; HttpOnly; SameSite=Strict; Path=/`);
      // The label is not a credential and outlives the browser on purpose.
      //
      // It authorises nothing: a caller who set one by hand could equally call
      // `/v1/demo/sessions` with any label they liked. What it does is answer "who is this
      // shopper" -- and their orders, their Reserve permissions and their unfinished
      // checkouts are all keyed to it. A session cookie made this identity last exactly as
      // long as a browser window, so closing the browser silently turned a returning buyer
      // into a new one with an empty history, and no amount of recovering a lost token
      // could help: the bridge would mint a session for a person who had never existed.
      //
      // 400 days is not a preference. It is the longest a cookie can be asked to live --
      // browsers clamp anything larger (RFC 6265bis; Chrome enforces it) -- so this is the
      // nearest thing to "keep this identity" the platform can say today. Real durability
      // arrives with platform authentication, where the buyer is a person the platform
      // knows rather than a label their browser is holding for them.
      if (buyerRef)
        out.append(
          'Set-Cookie',
          `rs_buyer_ref=${buyerRef}; Max-Age=${BUYER_REF_MAX_AGE_SECONDS}; HttpOnly; ` +
            'SameSite=Strict; Path=/',
        );
    }

    // Server-sent events are streamed rather than buffered: `text()` would wait for a
    // stream that never ends.
    if (upstream.includes('text/event-stream') && result.body)
      return new Response(result.body, { status: result.status, headers: out });

    return new Response(await result.text(), { status: result.status, headers: out });
  } catch (cause) {
    if ((cause as Error)?.name === 'AbortError')
      return new Response(null, { status: 499, headers: { 'Cache-Control': 'no-store' } });
    return Response.json(
      { detail: 'Commerce backend is unavailable. No payment confirmation received.' },
      { status: 503 },
    );
  }
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;

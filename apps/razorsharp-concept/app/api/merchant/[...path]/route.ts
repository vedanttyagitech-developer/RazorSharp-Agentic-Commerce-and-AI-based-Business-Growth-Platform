// The merchant workspace's bridge. No sign-in: a visitor arrives and the workspace opens.
//
// It used to demand the platform's demo scenario key through a form, because the backend
// would not mint a MERCHANT session without one. That is no longer true, and the form was
// never the protection it looked like -- the same key mints an OPERATOR, so anyone given
// it to see the shop was also handed Safe Mode, the outbox and scenario injections.
//
// The boundary that matters is untouched and lives in the backend, not here: a merchant
// holds Registry D and nothing more. They cannot approve a checkout or execute a payment,
// because those capabilities exist in no registry at all. This file decides who may open
// the workspace; it grants nothing inside it. Browser-selected roles still cannot elevate
// anyone: the actor is chosen here, on the server, and is always MERCHANT.
const base = process.env.COMMERCE_API_URL || 'http://127.0.0.1:8000';
const uuid = '[0-9a-fA-F-]{36}';
const rules: [RegExp, string[]][] = [
  [/^merchant\/policy$/, ['GET']],
  [/^merchant\/insights$/, ['GET']],
  [/^merchant\/actions$/, ['GET', 'POST']],
  [new RegExp(`^merchant/actions/${uuid}$`), ['GET', 'PUT']],
  [
    new RegExp(
      `^merchant/actions/${uuid}/(submit|approve|reject|cancel|execute)$`,
    ),
    ['POST'],
  ],
  [/^support\/cases$/, ['GET']],
  [new RegExp(`^support/cases/${uuid}$`), ['GET']],
  [new RegExp(`^support/cases/${uuid}/advance$`), ['POST']],
  [/^orders$/, ['GET']],
  [new RegExp(`^orders/${uuid}/refundable$`), ['GET']],
  [new RegExp(`^orders/${uuid}/refunds$`), ['POST']],
  [new RegExp(`^orders/${uuid}$`), ['GET']],
];
async function forward(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  if (
    process.env.NODE_ENV === 'production' &&
    process.env.RESERVE_LOCAL_DEMO !== 'true'
  )
    return Response.json(
      { detail: 'Local merchant bridge disabled.' },
      { status: 404 },
    );
  const origin = new URL(request.url).origin;
  if (request.method !== 'GET' && request.headers.get('origin') !== origin)
    return Response.json(
      { detail: 'Same-origin merchant request required.' },
      { status: 403 },
    );
  const path = (await context.params).path.join('/');
  const secure = new URL(request.url).protocol === 'https:' ? '; Secure' : '';
  try {
    // The sign-in and sign-out routes are gone. `POST session` took the platform's demo
    // scenario key out of a browser form and put it in a cookie; the backend no longer
    // asks for one to mint a MERCHANT (routers/demo.py, PRIVILEGED_ACTORS), so the only
    // thing that route still did was carry a key that also mints an OPERATOR through the
    // one place it should never be -- a visitor's machine.
    if (
      !rules.some(
        ([pattern, methods]) =>
          pattern.test(path) && methods.includes(request.method),
      )
    )
      return Response.json(
        { detail: 'Merchant route unavailable.' },
        { status: 404 },
      );
    const cookies = Object.fromEntries(
      (request.headers.get('cookie') || '').split(';').map((x) => {
        const i = x.indexOf('=');
        return [x.slice(0, i).trim(), decodeURIComponent(x.slice(i + 1))];
      }),
    );
    const responseCookies = new Headers();

    /** Open a MERCHANT session. No key: the backend stopped asking for one. */
    const mint = async (): Promise<string | Response> => {
      const answer = await fetch(`${base}/v1/demo/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_slug: process.env.COMMERCE_TENANT_SLUG || 'demo',
          actor_type: 'MERCHANT',
        }),
        signal: request.signal,
      });
      if (!answer.ok)
        return Response.json(
          { detail: 'The merchant workspace could not reach the store.' },
          { status: answer.status },
        );
      const session = (await answer.json()) as { token: string };
      responseCookies.append(
        'Set-Cookie',
        `rs_merchant_token=${encodeURIComponent(session.token)}; HttpOnly; SameSite=Strict; Path=/api/merchant${secure}`,
      );
      return session.token;
    };

    let token = cookies.rs_merchant_token;
    if (!token) {
      const minted = await mint();
      if (minted instanceof Response) return minted;
      token = minted;
    }

    // Read once: a request body is a stream, and the retry below would find it consumed.
    const payload = request.method === 'GET' ? undefined : await request.text();

    const send = (bearer: string) => {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${bearer}`,
        'Content-Type': 'application/json',
      };
      // Server-held, and never a cookie. A browser must not carry the platform's demo
      // key: the same key mints an OPERATOR -- Safe Mode, the outbox, scenario
      // injections. The routes below need a merchant, and a merchant is what the token
      // already is.
      if (process.env.SCENARIO_KEY) headers['X-Scenario-Key'] = process.env.SCENARIO_KEY;
      const idempotency = request.headers.get('Idempotency-Key');
      if (idempotency) headers['Idempotency-Key'] = idempotency;
      return fetch(`${base}/v1/${path}${new URL(request.url).search}`, {
        method: request.method,
        headers,
        body: payload,
        signal: request.signal,
      });
    };

    let result = await send(token);
    // Sessions expire. With no sign-in screen there is nobody to ask for a new one, and a
    // workspace that dies after an hour behind a 401 the visitor cannot clear is worse
    // than the screen this replaced. Re-mint and send again -- once, inside this request:
    // a second 401 is a real refusal and is passed through rather than retried.
    if (result.status === 401) {
      const minted = await mint();
      if (minted instanceof Response) return minted;
      result = await send(minted);
    }

    responseCookies.set(
      'Content-Type',
      result.headers.get('Content-Type') || 'application/json',
    );
    responseCookies.set('Cache-Control', 'no-store');
    return new Response(await result.text(), {
      status: result.status,
      headers: responseCookies,
    });
  } catch {
    return Response.json(
      {
        detail:
          'Merchant service unavailable. Check recorded state before retrying a change.',
      },
      { status: 503 },
    );
  }
}
export const GET = forward;
export const POST = forward;
export const PUT = forward;

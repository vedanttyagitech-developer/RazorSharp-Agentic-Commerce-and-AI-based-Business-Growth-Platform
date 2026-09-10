// Local demo merchant bridge. Login must prove possession of the backend scenario key;
// no buyer cookie, browser-selected role, or ambient server key can elevate a buyer.
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
    if (path === 'session' && request.method === 'POST') {
      const body = (await request.json()) as { key?: unknown };
      if (typeof body.key !== 'string' || !body.key || body.key.length > 4096)
        return Response.json(
          { detail: 'Enter the local demo scenario key.' },
          { status: 400 },
        );
      const result = await fetch(`${base}/v1/demo/sessions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Scenario-Key': body.key,
        },
        body: JSON.stringify({
          tenant_slug: process.env.COMMERCE_TENANT_SLUG || 'demo',
          actor_type: 'MERCHANT',
        }),
        signal: request.signal,
      });
      if (!result.ok)
        return Response.json(
          { detail: 'Merchant authentication refused.' },
          { status: result.status },
        );
      const session = (await result.json()) as { token: string };
      const headers = new Headers({ 'Cache-Control': 'no-store' });
      headers.append(
        'Set-Cookie',
        `rs_merchant_token=${encodeURIComponent(session.token)}; HttpOnly; SameSite=Strict; Path=/api/merchant${secure}`,
      );
      headers.append(
        'Set-Cookie',
        `rs_merchant_key=${encodeURIComponent(body.key)}; HttpOnly; SameSite=Strict; Path=/api/merchant${secure}`,
      );
      return Response.json({ authenticated: true }, { headers });
    }
    if (path === 'logout' && request.method === 'POST') {
      const headers = new Headers();
      for (const name of ['rs_merchant_token', 'rs_merchant_key'])
        headers.append(
          'Set-Cookie',
          `${name}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/api/merchant${secure}`,
        );
      return Response.json({ authenticated: false }, { headers });
    }
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
    if (!cookies.rs_merchant_token || !cookies.rs_merchant_key)
      return Response.json(
        { detail: 'Sign in to the merchant workspace.' },
        { status: 401 },
      );
    const headers: Record<string, string> = {
      Authorization: `Bearer ${cookies.rs_merchant_token}`,
      'X-Scenario-Key': cookies.rs_merchant_key,
      'Content-Type': 'application/json',
    };
    const key = request.headers.get('Idempotency-Key');
    if (key) headers['Idempotency-Key'] = key;
    const result = await fetch(
      `${base}/v1/${path}${new URL(request.url).search}`,
      {
        method: request.method,
        headers,
        body: request.method === 'GET' ? undefined : await request.text(),
        signal: request.signal,
      },
    );
    return new Response(await result.text(), {
      status: result.status,
      headers: {
        'Content-Type':
          result.headers.get('Content-Type') || 'application/json',
        'Cache-Control': 'no-store',
      },
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

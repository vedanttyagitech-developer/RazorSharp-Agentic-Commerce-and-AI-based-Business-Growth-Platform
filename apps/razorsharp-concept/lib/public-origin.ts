// The origin the browser actually used, which behind a TLS-terminating proxy is not the
// one this process can see.
//
// THE DEFECT THIS EXISTS TO REMOVE
// --------------------------------
// Both bridges refuse a non-GET whose `Origin` header does not match the request's own
// origin. That is a same-site check and it is worth having. It is also computed from
// `new URL(request.url)`, which behind a reverse proxy terminating TLS reports the scheme
// of the *inner* hop: the browser sends `Origin: https://host` and this process reconstructs
// `http://host`, so every write is refused with 403.
//
// On a laptop nothing terminates TLS and the two agree, so this was invisible until the
// first deployment: the storefront rendered, every page was 200, and the first POST -- a
// cart write, a checkout, a payment, a copilot turn -- came back "Same-origin request
// required". The `Secure` cookie flag had the same fault from the same line, so the
// merchant session cookie was being set without it over a connection that was, in fact,
// HTTPS.
//
// WHY TRUSTING THE HEADER IS SAFE *HERE*
// --------------------------------------
// `X-Forwarded-Proto` is set by whatever speaks to this process last, so a client that
// could reach the app directly could claim any scheme. In this deployment it cannot: the
// container publishes `127.0.0.1:3000` and only the proxy shares its network. The header is
// therefore as trustworthy as the proxy, and the proxy is the thing that knows.
//
// It also does not weaken the check it feeds. The comparison is still against the browser's
// own `Origin`, which a browser sets and a page cannot forge; spoofing the scheme here lets
// somebody claim their request arrived over TLS, not that it came from this site.

/**
 * The scheme-and-host a browser used to reach this route.
 *
 * Falls back to the request's own origin when no proxy header is present, which is the
 * local case and the one every test exercises.
 */
export function publicOrigin(request: Request): string {
  const direct = new URL(request.url);
  // A proxy chain appends rather than replaces, so `https, http` is possible; the first
  // entry is the hop closest to the browser and the only one that speaks for it.
  const first = (value: string | null): string | null =>
    value ? (value.split(',')[0] ?? '').trim() || null : null;
  const proto = first(request.headers.get('x-forwarded-proto'));
  const host =
    first(request.headers.get('x-forwarded-host')) ?? first(request.headers.get('host'));
  if (!proto || !host) return direct.origin;
  // Only the two schemes a browser can have used. Anything else is a header worth ignoring
  // rather than reflecting back into a comparison.
  if (proto !== 'http' && proto !== 'https') return direct.origin;
  return `${proto}://${host}`;
}

/** The `; Secure` a cookie should carry, decided by the browser's connection and not ours. */
export function secureCookieSuffix(request: Request): string {
  return publicOrigin(request).startsWith('https:') ? '; Secure' : '';
}

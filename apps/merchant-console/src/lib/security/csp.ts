/**
 * The console's Content-Security-Policy, built per request because the nonce cannot be
 * static.
 *
 * This is the higher-privilege of the two surfaces. The proxy behind it holds an operator
 * bearer token and the scenario key, and every page renders rows an operator did not write
 * -- SKUs, notes on injections, problem details, whole inspector documents echoed key by
 * key. A script that got into one of those rows on a console with no policy could read the
 * tenant's orders and post them anywhere it liked; `script-src 'self'` with a nonce is what
 * says it may not run at all, and `connect-src 'self'` is what says that even code that did
 * run has nowhere to send what it read.
 *
 * Three decisions here are load-bearing, and each one was learned from the storefront:
 *
 *  - **No `'strict-dynamic'`.** It tells the browser to ignore every host in the list and
 *    trust only what a nonced script loads, which is the right trade for an app that pulls
 *    in third-party bundles. This one loads its own chunks from its own origin and nothing
 *    else at all. With `'strict-dynamic'` the `'self'` beside it stops meaning anything,
 *    Next's chunks are refused, and the console renders server HTML with no JavaScript
 *    behind it: a nav that does not navigate and panels that never resolve.
 *  - **No nonce in `style-src`.** A nonce anywhere in that directive makes the browser
 *    ignore the `'unsafe-inline'` beside it, and React writes `style` attributes that carry
 *    no nonce and never will.
 *  - **No payment provider anywhere.** The storefront names Razorpay's script, API and
 *    frame origins on its checkout route. Nothing in this console talks to a payment
 *    provider -- approve, pay and refund are the buyer's surface and the kernel's -- so no
 *    third-party origin appears in any directive, and `frame-src` is shut outright.
 */

/** 128 bits, base64. Fresh for every response; never reused across requests. */
export function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}

function serialise(directives: Record<string, string[]>): string {
  return Object.entries(directives)
    .map(([name, values]) => (values.length ? `${name} ${values.join(" ")}` : name))
    .join("; ");
}

/** The one policy this console serves, on every route. */
export function consolePolicy(nonce: string): string {
  // Turbopack serves its dev chunks through `eval`, and React Refresh injects inline
  // scripts it does not nonce. Relaxing those two in development only is honest: the
  // policy that ships is the one below with `development` false.
  const development = process.env.NODE_ENV !== "production";
  return serialise({
    "default-src": ["'self'"],
    "script-src": development
      ? ["'self'", `'nonce-${nonce}'`, "'unsafe-inline'", "'unsafe-eval'"]
      : ["'self'", `'nonce-${nonce}'`],
    "style-src": ["'self'", "'unsafe-inline'"],
    // The console's only images are its own drawn mark and whatever a data URI carries.
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'", "data:"],
    // Every read this app performs goes through `/api/backend/...` on this same origin.
    // Naming the Commerce API here instead would let a page in this console reach it
    // directly, which is precisely the path that would carry no operator credential and
    // would tempt someone to put one in the browser.
    "connect-src": ["'self'"],
    "frame-src": ["'none'"],
    "frame-ancestors": ["'none'"],
    "form-action": ["'self'"],
    "base-uri": ["'none'"],
    "object-src": ["'none'"],
    "worker-src": ["'self'", "blob:"],
    "manifest-src": ["'self'"],
    "upgrade-insecure-requests": [],
  });
}

/**
 * Static headers that do not vary per request.
 *
 * Tighter than the storefront's in the two places the surfaces differ: `payment=()`
 * because this console never opens a payment sheet, and `Cross-Origin-Opener-Policy:
 * same-origin` because it never opens a provider popup that needs to talk back.
 */
export const SECURITY_HEADERS: ReadonlyArray<readonly [string, string]> = [
  ["Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"],
  ["Referrer-Policy", "strict-origin-when-cross-origin"],
  ["X-Content-Type-Options", "nosniff"],
  ["X-Frame-Options", "DENY"],
  ["Cross-Origin-Opener-Policy", "same-origin"],
  ["Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=(), bluetooth=()"],
];

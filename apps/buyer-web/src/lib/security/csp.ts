/**
 * Content-Security-Policy, built per request because the nonce cannot be static.
 *
 * Two things shape this policy:
 *
 *  - **Product imagery is local.** Every image under `public/products` was downloaded
 *    into this repository. `img-src` therefore does not name any external CDN: a
 *    storefront whose pictures come from someone else's origin stops working the day
 *    that origin changes a path, and on a payments submission it also means a third
 *    party sees a request for every product a buyer looks at.
 *  - **Razorpay's checkout only loads on the checkout route.** Its script and frame
 *    origins are added by `checkoutPolicy` and nowhere else, so the surface that can
 *    embed a payment iframe is exactly the surface that needs to.
 */

const RAZORPAY_SCRIPT = "https://checkout.razorpay.com";

//: The voice gateway's SOCKET in local development. Behind a reverse proxy in a
//: deployment, where `'self'` covers it and this does not appear in the policy at all.
//:
//: Its http origin used to be named beside this one, for a browser that minted its own
//: ticket against the gateway. No browser does: minting needs the buyer's bearer, the
//: bearer lives in an `httpOnly` cookie, and the mint therefore happens same-origin in
//: `/api/voice/tickets`. A `connect-src` entry for a request nobody makes is a permission
//: granted for nothing, so it was removed rather than left to look load-bearing.
const VOICE_GATEWAY_WS = "ws://127.0.0.1:8100";

/**
 * The websocket origin this policy admits, which must be the one the client dials.
 *
 * `features/voice/session.ts` resolves the gateway from `NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN`
 * and falls back to :8100 outside production. This read the fallback and nothing else, so
 * setting that variable pointed the client at an origin the policy refused -- and a CSP
 * refusal on a WebSocket surfaces as a close with no reason, which is indistinguishable
 * from the gateway being down. The file's own comment warned against exactly this ("not a
 * client that quietly points somewhere the policy would refuse") while the mismatch sat
 * one constant away.
 *
 * Both now read the same variable. An explicitly configured origin is honoured in any
 * environment, because naming one is a deliberate act by whoever deploys the gateway
 * somewhere other than behind this app's own proxy; with nothing configured the policy is
 * `'self'` alone in production and the local gateway in development, as before.
 */
function voiceGatewaySocketOrigins(development: boolean): string[] {
  const configured = process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
  if (configured) return [configured.replace(/\/+$/, "").replace(/^http/, "ws")];
  return development ? [VOICE_GATEWAY_WS] : [];
}
const RAZORPAY_API = "https://api.razorpay.com";
const RAZORPAY_FRAME = "https://api.razorpay.com https://checkout.razorpay.com";

//: Razorpay Checkout is not one origin, and finding that out cost a blank modal.
//:
//: `checkout.razorpay.com` serves only the loader, and the loader runs in *this*
//: document: from here it pulls a risk-detection bundle from `cdn.razorpay.com` and posts
//: its telemetry to `lumberjack.razorpay.com`. Naming just the loader let it load and
//: then blocked both of those, and the visible result was the modal iframe mounted at
//: full size with nothing drawn in it -- a dead payment box, with the actual cause only
//: in the console. A policy that admits a script but not what that script must fetch is
//: not a stricter policy, it is a broken one.
//:
//: The modal's own interface is a document on `api.razorpay.com` under that origin's
//: policy, not this one, which is why nothing here has to admit its images or fonts.
const RAZORPAY_CDN = "https://cdn.razorpay.com";
const RAZORPAY_TELEMETRY = "https://lumberjack.razorpay.com";

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

function base(nonce: string): Record<string, string[]> {
  // Turbopack serves its dev chunks through `eval`, and React Refresh injects inline
  // scripts it does not nonce. Relaxing those two in development only is honest: the
  // policy that ships is the one below with `development` false.
  const development = process.env.NODE_ENV !== "production";
  return {
    "default-src": ["'self'"],
    // `'self'` and a nonce, deliberately WITHOUT `'strict-dynamic'`.
    //
    // `'strict-dynamic'` tells the browser to ignore every host in this list and trust
    // only what a nonced script loads. That is the right trade for an app that pulls in
    // third-party bundles; this one loads its own chunks from its own origin and exactly
    // one external script, Razorpay's, on exactly one route. With `'strict-dynamic'` the
    // `'self'` beside it stopped meaning anything, Next's un-nonced chunks were refused,
    // and the storefront rendered its server HTML with no JavaScript behind it at all --
    // an empty basket, a dead RazorAI button, and skeletons that never resolved. The
    // host allowlist is both stricter here and true to how this app actually loads code.
    "script-src": development
      ? ["'self'", `'nonce-${nonce}'`, "'unsafe-inline'", "'unsafe-eval'"]
      : ["'self'", `'nonce-${nonce}'`],
    // No nonce on styles. A nonce anywhere in `style-src` makes the browser ignore the
    // `'unsafe-inline'` beside it, and React writes `style` attributes that carry no
    // nonce and never will -- which silently erased the card shadows and the ring that
    // marks a trusted surface apart from anything RazorAI draws.
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'", "data:"],
    // `'self'` plus, in development only, the voice gateway's socket origin.
    //
    // The gateway is a separate ASGI process on :8100 and the microphone stream is a
    // websocket to it. In a deployment it sits behind the same reverse proxy as this app
    // and `'self'` covers it, which is why the client defaults to a same-origin
    // `wss://.../api/voice/stream` and why that default is not changed here.
    //
    // Locally there is no reverse proxy, so the choice was to name the origin or to write
    // a websocket proxy inside a Next route handler. Naming it is the smaller lie: a
    // hand-written upgrade proxy would be a second implementation of the transport whose
    // failures would look like the gateway's, and it would exist only in development,
    // which is the worst place to keep code nobody runs in production. That reasoning
    // still holds, and `app/api/voice/stream/route.ts` is what stands at the same-origin
    // path instead -- an explanation of who serves it, not a relay.
    //
    // Everything else voice needs is already same-origin: the ticket is minted at
    // `/api/voice/tickets` because only this app's server holds the bearer to mint with.
    "connect-src": ["'self'", ...voiceGatewaySocketOrigins(development)],
    "frame-src": ["'none'"],
    "frame-ancestors": ["'none'"],
    "form-action": ["'self'"],
    "base-uri": ["'none'"],
    "object-src": ["'none'"],
    "worker-src": ["'self'", "blob:"],
    "manifest-src": ["'self'"],
    "upgrade-insecure-requests": [],
  };
}

/** The policy for every page that is not the payment surface. */
export function defaultPolicy(nonce: string): string {
  return serialise(base(nonce));
}

/**
 * The policy for `/checkout/*` only: Razorpay's script, its API and its iframe.
 *
 * Scoped rather than global on purpose. If the whole site could frame a payment provider,
 * then any injected markup anywhere could draw a convincing payment box; here only the
 * route the buyer deliberately navigated to can.
 */
export function checkoutPolicy(nonce: string): string {
  const directives = base(nonce);
  directives["script-src"] = [...directives["script-src"], RAZORPAY_SCRIPT, RAZORPAY_CDN];
  directives["connect-src"] = [
    ...directives["connect-src"],
    RAZORPAY_API,
    RAZORPAY_SCRIPT,
    RAZORPAY_CDN,
    RAZORPAY_TELEMETRY,
  ];
  directives["frame-src"] = RAZORPAY_FRAME.split(" ");
  return serialise(directives);
}

/** Static headers that do not vary per request (specification 21.5). */
export const SECURITY_HEADERS: ReadonlyArray<readonly [string, string]> = [
  ["Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"],
  ["Referrer-Policy", "strict-origin-when-cross-origin"],
  ["X-Content-Type-Options", "nosniff"],
  ["X-Frame-Options", "DENY"],
  ["Cross-Origin-Opener-Policy", "same-origin-allow-popups"],
  [
    "Permissions-Policy",
    'camera=(), geolocation=(self), microphone=(self), payment=(self "https://api.razorpay.com" "https://checkout.razorpay.com"), usb=(), bluetooth=()',
  ],
];

/** True for the routes that may load the payment provider. */
export function isCheckoutPath(pathname: string): boolean {
  return pathname === "/checkout" || pathname.startsWith("/checkout/");
}

/**
 * True for a path that must be entered as a **new document**, not by a client-side push.
 *
 * A Content-Security-Policy is a property of a document, not of a URL. Next's router
 * changes the URL and swaps the tree without fetching a document, so a buyer who reaches
 * the checkout the way buyers actually do -- basket, then "Proceed to checkout" -- keeps
 * whatever policy `/basket` was served with. That policy is the strict one: no
 * `checkout.razorpay.com` in `script-src` and `frame-src 'none'`. The payment script is
 * refused, and because the refusal is a console line rather than a network error the
 * surface just reports that the provider could not be reached.
 *
 * A direct load of the same URL worked fine, which is exactly what made this survive: the
 * policy was right, the middleware was right, and the only broken thing was the path a
 * real buyer takes. The scoped policy in `checkoutPolicy` is worth keeping -- it is what
 * stops any other surface from drawing a payment box -- and the price of keeping it is
 * that its route has to be its own document. So the two client-side entrances to
 * `/checkout/*` do a document navigation, and this predicate is where that rule is
 * written down rather than being two unexplained `window.location` calls.
 */
export function requiresOwnDocument(pathname: string): boolean {
  return isCheckoutPath(pathname);
}

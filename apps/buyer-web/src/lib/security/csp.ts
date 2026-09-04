/**
 * Nonce-based Content Security Policy (spec 21.5).
 *
 * The Razorpay Standard Checkout origins are allowed only on the payment route
 * (/checkout/*). Everywhere else no third-party script or frame is permitted, and
 * frame-ancestors is 'none' on every route. Inline scripts run only with Next's nonce.
 */
export const RAZORPAY_SCRIPT_ORIGIN = "https://checkout.razorpay.com";
export const RAZORPAY_API_ORIGIN = "https://api.razorpay.com";

export interface CspInput {
  nonce: string;
  /** True on routes that may launch Razorpay Standard Checkout. */
  paymentRoute: boolean;
  /** Development needs eval for source maps and a websocket for HMR. */
  dev: boolean;
}

export function buildCsp({ nonce, paymentRoute, dev }: CspInput): string {
  const scriptSrc = [
    "'self'",
    `'nonce-${nonce}'`,
    "'strict-dynamic'",
    ...(paymentRoute ? [RAZORPAY_SCRIPT_ORIGIN] : []),
    ...(dev ? ["'unsafe-eval'"] : []),
  ];
  const connectSrc = ["'self'", ...(paymentRoute ? [RAZORPAY_API_ORIGIN, "https://lumberjack.razorpay.com"] : []), ...(dev ? ["ws:", "wss:"] : [])];
  const frameSrc = paymentRoute ? [RAZORPAY_API_ORIGIN, RAZORPAY_SCRIPT_ORIGIN] : ["'none'"];
  // Product imagery is served from apps/buyer-web/public, so no third-party image host is
  // permitted. Razorpay's own origins are allowed only on the payment route, where its
  // checkout iframe renders card-network and bank logos.
  const imgSrc = [
    "'self'",
    "data:",
    "blob:",
    ...(paymentRoute ? [RAZORPAY_API_ORIGIN, "https://cdn.razorpay.com"] : []),
  ];

  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "base-uri": ["'self'"],
    "object-src": ["'none'"],
    "frame-ancestors": ["'none'"],
    "form-action": ["'self'"],
    "script-src": scriptSrc,
    // Tailwind ships one compiled stylesheet; the inline allowance covers Next's runtime
    // style injection in development and the style attributes React sets on elements.
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": imgSrc,
    "font-src": ["'self'", "data:"],
    "connect-src": connectSrc,
    "frame-src": frameSrc,
    "worker-src": ["'self'", "blob:"],
    "manifest-src": ["'self'"],
    ...(dev ? {} : { "upgrade-insecure-requests": [] }),
  };
  return Object.entries(directives)
    .map(([name, values]) => (values.length ? `${name} ${values.join(" ")}` : name))
    .join("; ");
}

export function isPaymentRoute(pathname: string): boolean {
  return pathname === "/checkout" || pathname.startsWith("/checkout/");
}

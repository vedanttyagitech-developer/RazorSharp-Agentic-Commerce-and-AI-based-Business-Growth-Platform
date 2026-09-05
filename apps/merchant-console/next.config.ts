import type { NextConfig } from "next";

/**
 * The console is an internal instrument, not a public storefront, so its headers are
 * tighter than the buyer app's: no payment permission, no framing, and nothing here is
 * ever cached. Every page reads live rows, and a cached operations screen is a screen
 * that lies about the state of the queue.
 */

/**
 * The console's Content-Security-Policy, and the one promise it deliberately does not make.
 *
 * This is the higher-privilege of the two surfaces — the proxy behind it holds an operator
 * bearer token and the scenario key — and until now it shipped no policy at all, so a
 * script injected into an operator-supplied row could reach any origin it liked. The
 * policy below closes that: `'self'` is the only host that may serve a script, `connect-src
 * 'self'` means nothing this page loads can post what it reads to somebody else's server,
 * `frame-ancestors 'none'` refuses clickjacking, and `base-uri` and `object-src` are shut.
 * The console never talks to Razorpay, so no provider origin appears anywhere in it.
 *
 * It is written here, statically, rather than built per request behind a nonce, because
 * every page in this app is prerendered at build time (`next build` marks all eight ○) and
 * a nonce stamped into a document rendered before the request existed is not a secret. The
 * buyer app carries a nonce on the routes it genuinely renders per request; claiming one
 * here would buy nothing but the appearance of having bought something, and would break
 * every page the moment `'strict-dynamic'` came with it. What that costs is inline-script
 * containment, which is stated rather than papered over: `'unsafe-inline'` is in the list.
 */
const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "frame-src 'none'",
  "frame-ancestors 'none'",
  "form-action 'self'",
  "base-uri 'none'",
  "object-src 'none'",
  "worker-src 'self' blob:",
  "manifest-src 'self'",
  "upgrade-insecure-requests",
].join("; ");

const SECURITY_HEADERS = [
  { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), geolocation=(), microphone=(), payment=(), usb=(), bluetooth=()",
  },
  { key: "Cache-Control", value: "no-store, must-revalidate" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  agentRules: false,
  poweredByHeader: false,
  async headers() {
    return [{ source: "/(.*)", headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;

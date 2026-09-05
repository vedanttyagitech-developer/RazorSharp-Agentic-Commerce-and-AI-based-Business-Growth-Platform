import type { NextConfig } from "next";

/**
 * The console is an internal instrument, not a public storefront, so its headers are
 * tighter than the buyer app's: no payment permission, no framing, and nothing here is
 * ever cached. Every page reads live rows, and a cached operations screen is a screen
 * that lies about the state of the queue.
 */

/**
 * Where the Content-Security-Policy is, and why it is not here.
 *
 * A static policy used to live in this file, on the reasoning that every page in the
 * console was prerendered at build time and a nonce baked into a document rendered before
 * the request existed is not a secret. Both halves of that stopped being true: the root
 * layout now forces dynamic rendering and `src/middleware.ts` builds a fresh nonce per
 * request, so the policy is assembled in `src/lib/security/csp.ts` and set on every
 * response there. Leaving the old one declared here would ship a second, weaker header —
 * `script-src 'self' 'unsafe-inline'` — that reads like the console's real posture to
 * anyone who opens this file, which is the same kind of stale claim the rest of this
 * codebase refuses to render on screen.
 *
 * What remains below is the set of headers that genuinely do not vary per request, kept
 * here so they also cover the paths the middleware's matcher skips (`_next/static` and
 * friends). The middleware re-sets them on the routes it does match, with the same values.
 */
const SECURITY_HEADERS = [
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

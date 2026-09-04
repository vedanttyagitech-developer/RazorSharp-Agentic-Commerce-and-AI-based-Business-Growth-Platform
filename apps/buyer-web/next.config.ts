import type { NextConfig } from "next";

/**
 * Static security headers (spec 21.5). The nonce-based Content-Security-Policy is set per
 * request in src/proxy.ts because a nonce cannot be static; the Razorpay origins are
 * allowed there only on /checkout/*. Transactional routes are uncached.
 */
const SECURITY_HEADERS = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin-allow-popups" },
  {
    key: "Permissions-Policy",
    value: 'camera=(), geolocation=(self), microphone=(self), payment=(self "https://api.razorpay.com" "https://checkout.razorpay.com"), usb=(), bluetooth=()',
  },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [
      { source: "/(.*)", headers: SECURITY_HEADERS },
      { source: "/checkout/:path*", headers: [{ key: "Cache-Control", value: "no-store, must-revalidate" }] },
      { source: "/orders/:path*", headers: [{ key: "Cache-Control", value: "no-store, must-revalidate" }] },
      { source: "/basket", headers: [{ key: "Cache-Control", value: "no-store, must-revalidate" }] },
    ];
  },
};

export default nextConfig;

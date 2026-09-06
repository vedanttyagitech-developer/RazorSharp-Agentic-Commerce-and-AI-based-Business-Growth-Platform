import type { Metadata, Viewport } from "next";
import "./globals.css";

/**
 * Every route in this console renders per request, and that is what makes its CSP nonce
 * real.
 *
 * Next stamps the nonce from the inbound `Content-Security-Policy` onto the inline scripts
 * it emits -- but only while it is rendering. Left to prerender, the routes were built once
 * at build time and served with inline bootstrap scripts carrying no nonce at all, so the
 * policy `middleware.ts` sets refused them and the console hydrated into nothing: six
 * `Executing inline script violates...` refusals and React error #412 on a screen that
 * still looked half-drawn.
 *
 * The alternative was to keep `'unsafe-inline'` in `script-src`, which on the surface that
 * holds an operator bearer token is the one concession worth paying to avoid. Nothing is
 * lost by rendering per request: every page here is a live read of the platform, the whole
 * app answers `no-store`, and a cached screen is a screen that lies about the state of the
 * platform.
 *
 * There is no shell here at present. The console's frame was the old top-nav instrument,
 * and it was removed with the screens it framed; the workspace that replaces it brings its
 * own three-column frame, and wrapping the interim page in half of a dead one would only
 * make the rebuild harder to see.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "RazorSharp — Agentic Commerce & Merchant Growth Platform",
  description: "The merchant workspace for the agentic commerce platform.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#0b0d10",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

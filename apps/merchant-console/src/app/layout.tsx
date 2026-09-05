import type { Metadata, Viewport } from "next";
import { Shell } from "@/components/Shell";
import "./globals.css";

/**
 * Every route in this console renders per request, and that is what makes its CSP nonce
 * real.
 *
 * Next stamps the nonce from the inbound `Content-Security-Policy` onto the inline scripts
 * it emits -- but only while it is rendering. Left to prerender, all eight routes were
 * built once at build time and served with inline bootstrap scripts carrying no nonce at
 * all, so the policy `middleware.ts` sets refused them and the console hydrated into
 * nothing: six `Executing inline script violates...` refusals and React error #412 on a
 * screen that still looked half-drawn.
 *
 * The alternative was to keep `'unsafe-inline'` in `script-src`, which on the surface that
 * holds an operator bearer token is the one concession worth paying to avoid. Nothing is
 * lost by rendering per request: every page here is a live read of the platform, the whole
 * app answers `no-store`, and a cached operations screen is a screen that lies about the
 * state of the queue.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "RazorSharp — Agentic Commerce & Merchant Growth Platform",
  description:
    "The operations instrument for the agentic commerce platform: safe mode, the durable outbox, orders, refunds, the catalogue, retained-revenue evidence and the payment-attempt inspector.",
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
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}

/**
 * The application shell: one header, one basket context, one footer, on every route.
 *
 * There is no inline script here, so the per-request nonce that `src/middleware.ts`
 * publishes on `x-nonce` is never read. That is the point of not having one -- reading
 * the header would opt this layout out of static rendering for a tag it does not need.
 * A route that genuinely must inline a script should read the nonce itself.
 */
/**
 * Every route renders per request, because every response carries its own CSP nonce.
 *
 * `middleware.ts` mints a fresh nonce per response and Next stamps it onto the inline
 * bootstrap scripts it emits. A prerendered document is built before any request exists, so
 * there is no nonce to stamp into it -- and the browser then refuses every inline script
 * under `script-src 'self' 'nonce-...'`, leaving the server HTML on screen with nothing
 * running behind it. The page looks fine and does nothing.
 *
 * Measured under `next start`, not reasoned about: `/basket` logged three CSP violations
 * and React error #412 and never hydrated, while `/checkout` beside it was perfect. Static
 * and dynamic routes were failing differently under a policy that applies to both, which is
 * why this survived -- and `next dev` hides it completely, because the development policy
 * carries `'unsafe-inline'`.
 *
 * Declared here rather than on each page for two reasons: the constraint belongs to the
 * whole app rather than to any route, and a `"use client"` page cannot carry route segment
 * config at all -- `/search` is one, so a per-page fix would have silently missed it and
 * left exactly the kind of gap this comment exists to prevent.
 *
 * The cost is small. These pages read what they show from the API in the browser, so there
 * was little to prerender beyond the shell.
 */
export const dynamic = "force-dynamic";

import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "./globals.css";

import { Footer } from "@/components/footer";
import { Header } from "@/components/header";
import { Providers } from "@/components/providers";
import { RazorAILauncher } from "@/features/agent/launcher";

/*
 * The tab title used to carry a real quick-commerce slogan promising last-minute delivery.
 * The header underneath it now says outright that nothing here is delivered, and the trust
 * strip on the home page was rewritten to claim only what the kernel can back, so the
 * title was the last delivery promise left on the surface -- and the first thing a buyer
 * reads, before a single row has loaded. A facsimile may copy a look; it may not make a
 * promise about the buyer's order that no response in this platform carries.
 */
export const metadata: Metadata = {
  title: {
    default: "RazorSharp Quick Commerce — a governed commerce demonstration",
    template: "%s · RazorSharp Quick Commerce",
  },
  description:
    "A demonstration quick-commerce storefront for the Razorpay AI Buildathon: an agent proposes, a deterministic transaction kernel authorises, and a stale approval is refused in the open.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#ffffff",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="flex min-h-full flex-col bg-[var(--page)]">
        <Providers>
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-[var(--r-md)] focus:bg-white focus:px-4 focus:py-2 focus:text-[14px] focus:font-semibold focus:text-[var(--ink)]"
          >
            Skip to content
          </a>
          <Header />
          <main id="main" className="flex-1">
            {children}
          </main>
          <Footer />
          {/*
            RazorAI is mounted here, outside <main>, because it is reachable from every
            route but is not part of any page's document. It sits inside <Providers> so
            it can read the basket the buyer has open, and it reads the checkout it is
            looking at from the path. It proposes and hands off; every control that
            commits money lives on the page beneath it.
          */}
          <RazorAILauncher />
        </Providers>
      </body>
    </html>
  );
}

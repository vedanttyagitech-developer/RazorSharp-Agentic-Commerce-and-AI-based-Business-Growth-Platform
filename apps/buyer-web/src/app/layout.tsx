/**
 * The application shell: one header, one basket context, one footer, on every route.
 *
 * There is no inline script here, so the per-request nonce that `src/middleware.ts`
 * publishes on `x-nonce` is never read. That is the point of not having one -- reading
 * the header would opt this layout out of static rendering for a tag it does not need.
 * A route that genuinely must inline a script should read the nonce itself.
 */
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
    default: "Blinkit — a governed commerce demonstration",
    template: "%s · Blinkit",
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

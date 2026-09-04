import type { Metadata } from "next";
import type React from "react";

import "./globals.css";

import { AppHeader } from "@/components/app-header";
import { DegradationBanner } from "@/components/degradation-banner";
import { AppProviders } from "@/components/providers";

export const metadata: Metadata = {
  title: { default: "Demo Grocery Store", template: "%s · Demo Grocery Store" },
  description: "Buyer storefront for a governed agentic-commerce platform (Razorpay AI Buildathon, Track 1).",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="flex min-h-full flex-col">
        <AppProviders>
          <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2">
            Skip to content
          </a>
          <DegradationBanner />
          <AppHeader />
          <main id="main" className="mx-auto w-full max-w-5xl flex-1 px-4 py-6">
            {children}
          </main>
          <footer className="border-t border-line px-4 py-4 text-center text-xs text-muted">
            Money facts are server-rendered. The browser never computes a total and never marks an order paid.
          </footer>
        </AppProviders>
      </body>
    </html>
  );
}

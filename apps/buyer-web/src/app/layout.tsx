import { Suspense, type ReactNode } from "react";
import type { Metadata } from "next";

import "./globals.css";

import { AppHeader } from "@/components/app-header";
import { DegradationBanner } from "@/components/degradation-banner";
import { AppProviders } from "@/components/providers";

export const metadata: Metadata = {
  title: { default: "Zepto Clone Demo", template: "%s · Zepto Clone Demo" },
  description: "Zepto Clone Demo — Quick commerce grocery delivery UI clone for demonstration purposes.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" />
      </head>
      <body className="flex min-h-full flex-col">
        <AppProviders>
          <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2">
            Skip to content
          </a>
          <DegradationBanner />
          <Suspense fallback={<header className="sticky top-0 z-40 bg-surface border-b border-line h-24" />}>
            <AppHeader />
          </Suspense>
          <main id="main" className="mx-auto w-full max-w-[1440px] flex-1 px-4 sm:px-6 lg:px-8 py-5">
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

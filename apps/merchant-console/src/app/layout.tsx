import type { Metadata } from "next";
import "./globals.css";
import { Navigation } from "@/components/navigation";

export const metadata: Metadata = {
  title: "Merchant Console · Governed Agentic Commerce",
  description: "Razorpay AI Buildathon Track 1 — Merchant Console, Revenue Protection & Protocol Inspector",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="flex min-h-full flex-col bg-[#0d0a14] text-[#f6f5f9]">
        <Navigation />
        <main className="flex-1 mx-auto w-full max-w-7xl px-4 sm:px-6 lg:px-8 py-6">
          {children}
        </main>
        <footer className="border-t border-[#2d2242] py-4 text-center text-xs text-[#a49cb5]">
          Governed Agentic Commerce Platform · Merchant Operations &amp; Retained Revenue Engine
        </footer>
      </body>
    </html>
  );
}

import Link from "next/link";

import { API_MODE } from "@/lib/api";

import { BasketLink } from "./basket-link";

export function AppHeader() {
  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div>
          <Link href="/" className="text-lg font-semibold">Demo Grocery Store</Link>
          <p className="text-xs text-muted">A Zepto-class quick-commerce journey on a governed agentic-commerce platform. Synthetic products; Razorpay test mode.</p>
        </div>
        <nav aria-label="Primary" className="flex items-center gap-3 text-sm">
          <Link href="/" className="underline-offset-4 hover:underline">Store</Link>
          <BasketLink />
          <span className={`rounded border px-2 py-0.5 text-xs ${API_MODE === "mock" ? "border-amber-400 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100" : "border-line"}`}>
            {API_MODE === "mock" ? "Mock mode (no backend)" : "Live API"}
          </span>
        </nav>
      </div>
    </header>
  );
}

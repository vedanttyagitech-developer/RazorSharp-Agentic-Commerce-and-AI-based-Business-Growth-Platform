"use client";

import Link from "next/link";
import { Check, Flame } from "lucide-react";

export function PromoBanners({ onSelectCategory }: { onSelectCategory?: (category: string) => void }) {
  return (
    <section aria-label="Featured promotions" className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      {/* Banner 1: ALL NEW ZEPTO EXPERIENCE (Left, ~58% width) */}
      <div className="relative flex flex-col justify-between rounded-3xl bg-banner-lavender p-5 sm:p-6 lg:col-span-7 shadow-xs overflow-hidden border border-purple-200/40 transition-colors">
        <div className="space-y-4">
          <div className="text-center sm:text-left">
            <p className="text-xs sm:text-sm font-black uppercase tracking-wider text-brand-purple">
              ALL <span className="text-foreground">NEW ZEPTO EXPERIENCE</span>
            </p>
            <h2 className="sr-only">Storefront Zero Fees Promotion</h2>
          </div>

          {/* Feature Highlight Cards */}
          <div className="grid grid-cols-2 gap-3">
            {/* Card 1: Zero Fees */}
            <div className="flex items-center gap-3 rounded-2xl bg-surface p-3 sm:p-3.5 shadow-2xs border border-line transition-colors">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-brand-purple text-white shadow-xs">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z" />
                  <line x1="3" y1="6" x2="21" y2="6" />
                  <path d="M16 10a4 4 0 0 1-8 0" />
                </svg>
              </div>
              <div>
                <span className="block text-xl sm:text-2xl font-black text-brand-purple leading-none">
                  ₹0 FEES
                </span>
                <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
                  No hidden charges
                </span>
              </div>
            </div>

            {/* Card 2: Everyday Low Prices */}
            <div className="flex items-center gap-3 rounded-2xl bg-surface p-3 sm:p-3.5 shadow-2xs border border-line transition-colors">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-brand-purple text-white shadow-xs">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <polyline points="23 18 13.5 8.5 8.5 13.5 1 6" />
                  <polyline points="17 18 23 18 23 12" />
                </svg>
              </div>
              <div>
                <span className="block text-[10px] font-black uppercase text-muted tracking-wider">
                  EVERYDAY
                </span>
                <strong className="text-sm sm:text-base font-black text-brand-purple leading-tight block">
                  LOW PRICES*
                </strong>
              </div>
            </div>
          </div>
        </div>

        {/* Green Badges Row */}
        <div className="mt-4 pt-3 border-t border-line/60">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-bold text-foreground">
            <span className="inline-flex items-center gap-1.5">
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-600 text-white"><Check className="h-2.5 w-2.5 stroke-[3]" /></span>
              <span>₹0 Handling Fee</span>
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-600 text-white"><Check className="h-2.5 w-2.5 stroke-[3]" /></span>
              <span>₹0 Delivery Fee*</span>
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-600 text-white"><Check className="h-2.5 w-2.5 stroke-[3]" /></span>
              <span>₹0 Rain &amp; Surge Fee</span>
            </span>
          </div>
          <p className="mt-1 text-[10px] text-muted text-center sm:text-left">
            *T&amp;C Apply. Above specific minimum order value.
          </p>
        </div>
      </div>

      {/* Banner 2: PAAN CORNER (Right, ~42% width) */}
      <div className="relative flex flex-col justify-between rounded-3xl bg-banner-teal p-5 sm:p-6 lg:col-span-5 shadow-xs overflow-hidden border border-teal-200/40 transition-colors">
        <div className="space-y-2 z-10 max-w-[62%]">
          <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-emerald-950 dark:text-emerald-100">
            PAAN CORNER
          </h2>
          <p className="text-xs sm:text-sm font-semibold text-emerald-900/90 dark:text-emerald-200 leading-snug">
            Get smoking accessories, fresheners &amp; more delivered in minutes!
          </p>
        </div>

        <div className="mt-4 flex items-center justify-between z-10">
          {onSelectCategory ? (
            <button
              type="button"
              onClick={() => onSelectCategory("snacks")}
              className="inline-flex items-center gap-1.5 rounded-full bg-foreground text-background px-5 py-2.5 text-xs sm:text-sm font-bold shadow-sm hover:opacity-90 active:scale-95 transition cursor-pointer"
            >
              <span>Order now</span>
              <span aria-hidden="true">›</span>
            </button>
          ) : (
            <Link
              href="/?category=snacks"
              className="inline-flex items-center gap-1.5 rounded-full bg-foreground text-background px-5 py-2.5 text-xs sm:text-sm font-bold shadow-sm hover:opacity-90 active:scale-95 transition"
            >
              <span>Order now</span>
              <span aria-hidden="true">›</span>
            </Link>
          )}
        </div>

        {/* Right Product Collage Representation */}
        <div className="absolute right-3 -bottom-2 sm:bottom-2 flex items-end gap-1.5 opacity-90 select-none pointer-events-none" aria-hidden="true">
          <div className="w-16 h-24 sm:w-20 sm:h-28 rounded-xl bg-surface/90 p-1.5 shadow-sm flex flex-col justify-between border border-line transform rotate-3">
            <span className="text-[9px] font-black text-foreground uppercase">stash-pro</span>
            <div className="w-full h-10 bg-amber-100 dark:bg-amber-900/40 rounded flex items-center justify-center text-xs font-bold text-amber-900 dark:text-amber-200">
              6 Brown
            </div>
            <span className="text-[7px] text-muted">cones</span>
          </div>
          <div className="w-14 h-20 sm:w-16 sm:h-24 rounded-xl bg-teal-800 p-1.5 shadow-sm text-white flex flex-col justify-between transform -rotate-6">
            <span className="text-[8px] font-bold">JETTY</span>
            <Flame className="h-6 w-6 text-amber-300 self-center" />
            <span className="text-[7px] text-teal-200">lighter</span>
          </div>
        </div>
      </div>
    </section>
  );
}

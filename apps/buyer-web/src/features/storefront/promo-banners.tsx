"use client";

import { Check } from "lucide-react";
import { AiShoppingSlider } from "./ai-shopping-slider";

export function PromoBanners({ onSelectCategory }: { onSelectCategory?: (category: string) => void }) {
  return (
    <section aria-label="Featured promotions" className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      {/* Banner 1: Blinkit Yellow & Green Experience Promo (Left, ~58% width) */}
      <div className="relative flex flex-col justify-between rounded-3xl bg-[#fffde6] dark:bg-[#231f10] p-5 sm:p-6 lg:col-span-7 shadow-xs overflow-hidden border border-[#f8cb46]/40 transition-colors">
        <div className="space-y-4">
          <div className="text-center sm:text-left">
            <p className="text-xs sm:text-sm font-black uppercase tracking-wider text-[#0c831f]">
              ALL <span className="text-foreground">NEW ZEPTO EXPERIENCE</span>
            </p>
            <h2 className="sr-only">Storefront Zero Fees Promotion</h2>
          </div>

          {/* Feature Highlight Cards */}
          <div className="grid grid-cols-2 gap-3">
            {/* Card 1: Zero Fees */}
            <div className="flex items-center gap-3 rounded-2xl bg-white dark:bg-surface p-3 sm:p-3.5 shadow-2xs border border-[#e8e8e8] dark:border-line transition-colors">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-[#0c831f] text-white shadow-xs">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z" />
                  <line x1="3" y1="6" x2="21" y2="6" />
                  <path d="M16 10a4 4 0 0 1-8 0" />
                </svg>
              </div>
              <div>
                <span className="block text-xl sm:text-2xl font-black text-[#0c831f] leading-none">
                  ₹0 FEES
                </span>
                <span className="text-[10px] font-bold text-muted uppercase tracking-wider">
                  No hidden charges
                </span>
              </div>
            </div>

            {/* Card 2: Everyday Low Prices */}
            <div className="flex items-center gap-3 rounded-2xl bg-white dark:bg-surface p-3 sm:p-3.5 shadow-2xs border border-[#e8e8e8] dark:border-line transition-colors">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-[#0c831f] text-white shadow-xs">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <polyline points="23 18 13.5 8.5 8.5 13.5 1 6" />
                  <polyline points="17 18 23 18 23 12" />
                </svg>
              </div>
              <div>
                <span className="block text-[10px] font-black uppercase text-muted tracking-wider">
                  EVERYDAY
                </span>
                <strong className="text-sm sm:text-base font-black text-[#0c831f] leading-tight block">
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
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-[#0c831f] text-white">
                <Check className="h-2.5 w-2.5 stroke-[3]" />
              </span>
              <span>₹0 Handling Fee</span>
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-[#0c831f] text-white">
                <Check className="h-2.5 w-2.5 stroke-[3]" />
              </span>
              <span>₹0 Delivery Fee*</span>
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-[#0c831f] text-white">
                <Check className="h-2.5 w-2.5 stroke-[3]" />
              </span>
              <span>₹0 Rain &amp; Surge Fee</span>
            </span>
          </div>
          <p className="mt-1 text-[10px] text-muted text-center sm:text-left">
            *T&amp;C Apply. Above specific minimum order value.
          </p>
        </div>
      </div>

      {/* Banner 2: AI-Assisted Shopping Sliding Animation Bar (Right, ~42% width) */}
      <AiShoppingSlider />
    </section>
  );
}

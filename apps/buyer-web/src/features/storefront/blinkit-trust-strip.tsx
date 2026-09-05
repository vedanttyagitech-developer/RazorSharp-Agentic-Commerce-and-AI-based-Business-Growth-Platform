"use client";

import Image from "next/image";

const TRUST_ITEMS = [
  {
    id: "delivery",
    title: "Superfast 10-Min Delivery",
    description: "Packed and delivered in minutes from your nearest Blinkit dark store.",
    icon: "/infographics/delivery-10-min.svg",
    tag: "⏱ 8-10 MINS",
  },
  {
    id: "prices",
    title: "Best Prices & Offers",
    description: "Cheaper than local supermarkets with daily price cuts and zero surge.",
    icon: "/infographics/best-prices.svg",
    tag: "UP TO 50% OFF",
  },
  {
    id: "assortment",
    title: "Wide Assortment",
    description: "5,000+ grocery essentials, dairy, snacks and top derma skincare.",
    icon: "/infographics/wide-assortment.svg",
    tag: "5,000+ SKUS",
  },
  {
    id: "doorstep",
    title: "Open Box Verification",
    description: "Inspect items right at your doorstep before accepting. Instant returns.",
    icon: "/infographics/doorstep-return.svg",
    tag: "100% VERIFIED",
  },
];

export function BlinkitTrustStrip() {
  return (
    <section
      aria-label="Blinkit Trust & Promises"
      className="rounded-3xl border border-[#0c831f]/20 bg-gradient-to-br from-[#f6fff7] via-white to-[#fffde6] p-4 sm:p-5 shadow-2xs"
    >
      <div className="flex items-center justify-between pb-3 border-b border-[#0c831f]/10 mb-3">
        <div className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full bg-[#0c831f] animate-pulse" />
          <h3 className="text-xs sm:text-sm font-black text-[#1c1c1c] tracking-tight uppercase">
            Why Shop On <span className="text-[#0c831f]">Blink</span><span className="text-[#f8cb46] bg-[#1c1c1c] px-1 py-0.5 rounded-sm ml-0.5">it</span>?
          </h3>
        </div>
        <span className="text-[11px] font-bold text-[#0c831f] bg-[#eaf7ec] px-2.5 py-0.5 rounded-full border border-[#0c831f]/20">
          Guaranteed Fast &amp; Authentic
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {TRUST_ITEMS.map((item) => (
          <div
            key={item.id}
            className="flex items-start gap-3 rounded-2xl bg-white dark:bg-stone-900/50 p-3 border border-stone-200/70 hover:border-[#0c831f]/50 hover:shadow-xs transition-all group"
          >
            <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-xl bg-stone-50 p-1 flex items-center justify-center border border-stone-100 group-hover:scale-105 transition-transform">
              <img
                src={item.icon}
                alt={item.title}
                width={48}
                height={48}
                className="h-full w-full object-contain"
                loading="lazy"
              />
            </div>
            <div className="space-y-0.5 min-w-0">
              <div className="flex items-center gap-1.5 flex-wrap">
                <h4 className="text-xs font-bold text-[#1f1f1f] dark:text-stone-100 leading-tight">
                  {item.title}
                </h4>
              </div>
              <p className="text-[11px] text-stone-500 dark:text-stone-400 leading-tight line-clamp-2">
                {item.description}
              </p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

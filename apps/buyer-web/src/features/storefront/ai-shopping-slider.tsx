"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  Sparkles,
  ChevronLeft,
  ChevronRight,
  Bot,
  Utensils,
  ShieldCheck,
  CheckCircle2,
  Clock,
  ArrowRight,
  Zap,
} from "lucide-react";

export interface AiFeatureCard {
  id: string;
  stepNumber: string;
  badge: string;
  title: string;
  description: string;
  easeHighlights: [string, string];
  actionLabel: string;
  prompt: string;
  icon: typeof Bot;
  accentGlow: string;
}

export const AI_FEATURE_CARDS: AiFeatureCard[] = [
  {
    id: "conversational-cart",
    stepNumber: "01 / 05",
    badge: "NATURAL LANGUAGE",
    title: "Talk or Type — Cart Built in Seconds",
    description:
      "Say '2 packets milk and brown bread' or paste dinner lists. Zepto AI understands English, Hindi & Hinglish, maps items to live inventory, and stages your basket in 1 tap.",
    easeHighlights: ["⚡ 1-Tap Staging vs 15 Manual Searches", "🗣️ Colloquial Hindi & Hinglish"],
    actionLabel: 'Try: "2 packet doodh add karo"',
    prompt: "2 packet doodh add karo",
    icon: Bot,
    accentGlow: "from-emerald-400/20 to-yellow-400/20",
  },
  {
    id: "recipe-dietary",
    stepNumber: "02 / 05",
    badge: "DIET & ALLERGENS",
    title: "Dietary Reasoning & Complete Recipe Kits",
    description:
      "Looking for vegan dairy, gluten-free snacks, or high-protein munchies? Zepto AI reasons across 247 verified SKUs, filtering by allergen and building complete recipe baskets.",
    easeHighlights: ["🥗 Vegan & Gluten-Free Filters", "🍲 Zero Missing Recipe Ingredients"],
    actionLabel: 'Try: "High protein snacks"',
    prompt: "High protein snacks",
    icon: Utensils,
    accentGlow: "from-emerald-400/20 to-teal-400/20",
  },
  {
    id: "price-shift-shield",
    stepNumber: "03 / 05",
    badge: "KERNEL INTEGRITY",
    title: "Zero Surprise Charges or Price Bumps",
    description:
      "If supplier prices change or surge pricing updates mid-checkout, the Transaction Assurance Kernel detects the delta, refuses stale charges, and presents the exact itemized difference.",
    easeHighlights: ["🛡️ SHA-256 State Verification", "✋ Auto-Refusal on Price Jumps"],
    actionLabel: "Simulate Price Shift Refusal",
    prompt: "Simulate price change refusal hero",
    icon: ShieldCheck,
    accentGlow: "from-yellow-400/20 to-amber-500/20",
  },
  {
    id: "sovereign-checkout",
    stepNumber: "04 / 05",
    badge: "HUMAN GOVERNANCE",
    title: "AI Drafts Proposals, You Retain Authority",
    description:
      "AI assistants compile stock quotas and generate verified checkout drafts, but they cannot debit your money. You retain unilateral sovereign control to approve via Razorpay.",
    easeHighlights: ["💳 1-Click Governed Approval Card", "🔐 Autonomous Payment Prevention"],
    actionLabel: "Propose Checkout Draft",
    prompt: "propose checkout for current basket",
    icon: CheckCircle2,
    accentGlow: "from-blue-400/20 to-emerald-400/20",
  },
  {
    id: "hyperlocal-fulfillment",
    stepNumber: "05 / 05",
    badge: "10-MIN FULFILLMENT",
    title: "From Chat Approval to Doorstep in 10 Mins",
    description:
      "The instant your cryptographic approval is logged, your order routes directly to hyper-local dark-store pickers with cold-chain monitoring, arriving fresh in under 10 minutes.",
    easeHighlights: ["⏱️ Hyper-Local 10-Minute Dispatch", "❄️ Cold-Chain Dairy & Produce Care"],
    actionLabel: "Explore 10-Min Fast Delivery",
    prompt: "Amul Taaza Toned Milk 500 ml",
    icon: Clock,
    accentGlow: "from-yellow-400/20 to-emerald-400/20",
  },
];

export function AiShoppingSlider({ onOpenAi }: { onOpenAi?: (prompt?: string) => void }) {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [isPaused, setIsPaused] = useState(false);
  const touchStartX = useRef<number | null>(null);

  const nextSlide = useCallback(() => {
    setCurrentIndex((prev) => (prev + 1) % AI_FEATURE_CARDS.length);
  }, []);

  const prevSlide = useCallback(() => {
    setCurrentIndex((prev) => (prev - 1 + AI_FEATURE_CARDS.length) % AI_FEATURE_CARDS.length);
  }, []);

  // Auto-advance sliding timer (4.5s)
  useEffect(() => {
    if (isPaused) return;
    const interval = setInterval(nextSlide, 4500);
    return () => clearInterval(interval);
  }, [isPaused, nextSlide]);

  const handleTriggerAi = (promptText: string) => {
    if (onOpenAi) {
      onOpenAi(promptText);
    } else if (typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("open-zepto-ai", {
          detail: { prompt: promptText },
        })
      );
    }
  };

  // Touch swipe handling for mobile
  const handleTouchStart = (e: React.TouchEvent) => {
    touchStartX.current = e.touches[0].clientX;
  };

  const handleTouchEnd = (e: React.TouchEvent) => {
    if (touchStartX.current === null) return;
    const deltaX = e.changedTouches[0].clientX - touchStartX.current;
    if (deltaX > 40) {
      prevSlide();
    } else if (deltaX < -40) {
      nextSlide();
    }
    touchStartX.current = null;
  };

  const activeCard = AI_FEATURE_CARDS[currentIndex];

  return (
    <div
      role="region"
      aria-label="How AI-Assisted Shopping Works"
      onMouseEnter={() => setIsPaused(true)}
      onMouseLeave={() => setIsPaused(false)}
      onTouchStart={handleTouchStart}
      onTouchEnd={handleTouchEnd}
      className="relative flex flex-col justify-between rounded-3xl bg-gradient-to-br from-[#063319] via-[#094d25] to-[#0c6b32] text-white p-5 sm:p-6 lg:col-span-5 shadow-xs overflow-hidden border border-emerald-500/30 transition-all select-none"
    >
      {/* Dynamic Background Glow */}
      <div
        className={`absolute -right-12 -top-12 w-48 h-48 rounded-full bg-gradient-to-br ${activeCard.accentGlow} blur-3xl pointer-events-none transition-all duration-700`}
        aria-hidden="true"
      />

      {/* Top Header Bar */}
      <div className="flex items-center justify-between z-10 gap-2 mb-3">
        <div className="inline-flex items-center gap-1.5 rounded-full bg-white/10 backdrop-blur-md px-3 py-1 text-[11px] font-bold text-emerald-100 border border-white/15 shadow-2xs">
          <Sparkles className="h-3.5 w-3.5 text-[#f8cb46] fill-[#f8cb46] animate-pulse" aria-hidden="true" />
          <span className="tracking-wide uppercase text-[10px] sm:text-[11px]">How Zepto AI Works</span>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="text-[11px] font-mono font-bold text-emerald-200/90 mr-1">
            {activeCard.stepNumber}
          </span>
          <button
            type="button"
            onClick={prevSlide}
            aria-label="Previous slide"
            className="h-7 w-7 rounded-full bg-white/10 hover:bg-white/20 active:scale-95 flex items-center justify-center text-emerald-100 transition border border-white/10 cursor-pointer"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={nextSlide}
            aria-label="Next slide"
            className="h-7 w-7 rounded-full bg-white/10 hover:bg-white/20 active:scale-95 flex items-center justify-center text-emerald-100 transition border border-white/10 cursor-pointer"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Sliding Viewport Container */}
      <div className="overflow-hidden w-full relative z-10 my-auto">
        <div
          className="flex w-full transition-transform duration-500 ease-out"
          style={{ transform: `translateX(-${currentIndex * 100}%)` }}
        >
          {AI_FEATURE_CARDS.map((card) => {
            const Icon = card.icon;
            return (
              <div key={card.id} className="w-full flex-shrink-0 flex flex-col justify-between pr-1">
                <div>
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="inline-flex items-center gap-1 text-[10px] font-mono font-extrabold uppercase tracking-wider text-[#052e16] bg-[#f8cb46] px-2 py-0.5 rounded-full border border-[#f8cb46]/40">
                      <Zap className="h-2.5 w-2.5 fill-[#052e16]" aria-hidden="true" />
                      {card.badge}
                    </span>
                  </div>

                  <h3 className="text-lg sm:text-xl font-black tracking-tight text-white leading-snug flex items-center gap-2">
                    <span className="p-1 rounded-lg bg-white/10 text-[#f8cb46] shrink-0 inline-flex">
                      <Icon className="h-4 w-4" aria-hidden="true" />
                    </span>
                    <span>{card.title}</span>
                  </h3>

                  <p className="mt-2 text-xs sm:text-[13px] text-emerald-100/90 leading-relaxed font-normal">
                    {card.description}
                  </p>
                </div>

                {/* Ease-of-Work Benefit Highlights */}
                <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                  {card.easeHighlights.map((highlight, i) => (
                    <div
                      key={i}
                      className="flex items-center gap-1.5 rounded-xl bg-white/10 backdrop-blur-xs px-2.5 py-1.5 text-[11px] font-semibold text-emerald-50 border border-white/10"
                    >
                      <span className="leading-tight">{highlight}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Bottom Bar: Action Trigger & Slide Progress Indicators */}
      <div className="mt-4 pt-3 border-t border-white/15 flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 z-10">
        <button
          type="button"
          aria-label="Test in Zepto AI"
          onClick={() => handleTriggerAi(activeCard.prompt)}
          className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-[#f8cb46] to-[#ecc32c] hover:from-[#ecc32c] hover:to-[#dfb51c] text-[#052e16] px-4 py-2 text-xs font-black shadow-md hover:shadow-lg active:scale-95 transition cursor-pointer group"
        >
          <Sparkles className="h-3.5 w-3.5 fill-[#052e16]" aria-hidden="true" />
          <span>{activeCard.actionLabel}</span>
          <ArrowRight className="h-3.5 w-3.5 group-hover:translate-x-0.5 transition-transform" aria-hidden="true" />
        </button>

        {/* Step Indicators with Active Animation */}
        <div className="flex items-center justify-center gap-1.5 py-1" aria-label="Slide pagination">
          {AI_FEATURE_CARDS.map((card, idx) => (
            <button
              key={card.id}
              type="button"
              onClick={() => setCurrentIndex(idx)}
              aria-label={`Go to slide ${idx + 1}: ${card.title}`}
              className={`h-2 rounded-full transition-all duration-300 cursor-pointer ${
                currentIndex === idx
                  ? "w-6 bg-[#f8cb46] shadow-xs"
                  : "w-2 bg-white/25 hover:bg-white/40"
              }`}
            />
          ))}
          {isPaused && (
            <span className="ml-1 text-[9px] font-mono text-emerald-200/80 uppercase tracking-widest hidden sm:inline">
              (Paused)
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

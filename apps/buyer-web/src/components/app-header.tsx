"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, type FormEvent } from "react";
import {
  Search,
  X,
  MapPin,
  ChevronDown,
  User,
  ShoppingCart,
  Zap,
  LayoutGrid,
  Milk,
  Wheat,
  Carrot,
  Cookie,
  Coffee,
  Croissant,
  Sparkles,
  HeartPulse,
  Flame,
  CupSoda,
  type LucideIcon,
} from "lucide-react";

import { useBasketRef } from "./providers";

interface NavTab {
  id: string;
  label: string;
  icon: LucideIcon;
  category: string;
}

const TOP_NAV_TABS: NavTab[] = [
  { id: "all", label: "All", icon: LayoutGrid, category: "all" },
  { id: "dairy", label: "Dairy, Bread & Eggs", icon: Milk, category: "dairy" },
  { id: "staples", label: "Atta, Rice & Dal", icon: Wheat, category: "staples" },
  { id: "fresh", label: "Fresh Vegetables", icon: Carrot, category: "produce" },
  { id: "snacks", label: "Snacks & Munchies", icon: Cookie, category: "snacks" },
  { id: "beverages", label: "Cold Drinks & Juices", icon: CupSoda, category: "beverages" },
  { id: "bakery", label: "Bakery & Biscuits", icon: Croissant, category: "bakery" },
  { id: "household", label: "Cleaning & Household", icon: Sparkles, category: "household" },
  { id: "personal_care", label: "Personal Care", icon: HeartPulse, category: "personal_care" },
  { id: "condiments", label: "Masalas & Spices", icon: Flame, category: "condiments" },
];

const SEARCH_PLACEHOLDERS = [
  'Search "bread"',
  'Search "milk"',
  'Search "soft drinks"',
  'Search "coca cola"',
  'Search "bisleri"',
];

export function AppHeader() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { lineCount } = useBasketRef();

  const currentQuery = searchParams.get("q") ?? "";
  const currentCategory = searchParams.get("category") ?? "all";
  const showSubNav = pathname === "/" && currentCategory === "all" && !currentQuery;

  const [userInput, setUserInput] = useState<string | null>(null);
  const searchVal = userInput ?? currentQuery;
  const [placeholderIndex, setPlaceholderIndex] = useState(0);
  const [showLocationModal, setShowLocationModal] = useState(false);
  const [showLoginModal, setShowLoginModal] = useState(false);
  const isClient = useSyncExternalStore(() => () => {}, () => true, () => false);
  const safeLineCount = isClient ? lineCount : 0;

  // Gentle placeholder rotation
  useEffect(() => {
    const timer = setInterval(() => {
      setPlaceholderIndex((prev) => (prev + 1) % SEARCH_PLACEHOLDERS.length);
    }, 4000);
    return () => clearInterval(timer);
  }, []);

  function handleSearchSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const params = new URLSearchParams(searchParams.toString());
    if (searchVal.trim()) {
      params.set("q", searchVal.trim());
    } else {
      params.delete("q");
    }
    router.push(`/?${params.toString()}`);
  }

  function handleTabClick(category: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("q");
    setUserInput("");
    if (category === "all") {
      params.delete("category");
    } else {
      params.set("category", category);
    }
    router.push(`/?${params.toString()}`);
  }

  return (
    <header className="sticky top-0 z-40 bg-white dark:bg-[#160f22] border-b border-[#e8e8e8] dark:border-line transition-colors">
      {/* Primary Top Bar */}
      <div>
        <div className="mx-auto flex w-full max-w-[1440px] flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 px-4 sm:px-6 lg:px-8 py-2.5 sm:py-3">
          {/* Row 1: Logo + Delivery Info + Mobile Cart */}
          <div className="flex items-center justify-between gap-3 sm:gap-6">
            <div className="flex items-center gap-3 sm:gap-6">
              {/* Blinkit Wordmark Logo */}
              <Link
                href="/"
                className="group flex items-baseline focus-visible:ring-2 focus-visible:ring-[#0c831f] rounded-lg px-1 select-none"
                aria-label="Blinkit home"
              >
                <span className="text-2xl sm:text-3xl font-black tracking-tight leading-none">
                  <span className="text-[#f8cb46]">blink</span>
                  <span className="text-[#0c831f]">it</span>
                </span>
              </Link>

              {/* Delivery Location Pill */}
              <button
                type="button"
                onClick={() => setShowLocationModal(true)}
                className="flex flex-col justify-center text-left group min-h-[44px] focus-visible:ring-2 focus-visible:ring-[#0c831f] rounded-lg px-1.5 cursor-pointer hover:bg-stone-50 transition"
                aria-label="Delivery location: TOWER-C, Nirvana Country"
              >
                <span className="text-xs sm:text-sm font-extrabold text-[#1f1f1f] dark:text-foreground leading-tight">
                  Delivery in 8 minutes
                </span>
                <div className="flex items-center gap-1 text-[11px] font-medium text-stone-500 group-hover:text-stone-900 transition leading-tight">
                  <span className="truncate max-w-[130px] sm:max-w-[190px]">TOWER-C, Nirvana Country, Sec...</span>
                  <ChevronDown className="h-3 w-3 text-stone-500 shrink-0" aria-hidden="true" />
                </div>
              </button>
            </div>

            {/* Mobile-only Account & Cart */}
            <div className="flex sm:hidden items-center gap-2 shrink-0">
              <button
                type="button"
                onClick={() => setShowLoginModal(true)}
                className="text-xs font-bold text-[#1f1f1f] hover:text-[#0c831f] px-2 py-1.5"
                aria-label="Login to account"
              >
                Login
              </button>

              <Link
                href="/basket"
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold shadow-xs transition ${
                  safeLineCount > 0 ? "bg-[#0c831f] text-white" : "bg-[#f3f3f3] text-[#1f1f1f]"
                }`}
                aria-label={`Shopping cart with ${safeLineCount} items`}
              >
                <ShoppingCart className="h-4 w-4" />
                <span>{safeLineCount > 0 ? safeLineCount : "Cart"}</span>
              </Link>
            </div>
          </div>

          {/* Search Bar: Full-width on mobile row 2, centered on sm+ */}
          <div className="w-full sm:flex-1 sm:max-w-2xl">
            <form onSubmit={handleSearchSubmit} role="search" className="relative w-full">
              <span
                aria-hidden="true"
                className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-stone-400"
              >
                <Search className="h-4 w-4" />
              </span>
              <input
                type="search"
                value={searchVal}
                onChange={(e) => setUserInput(e.target.value)}
                placeholder={SEARCH_PLACEHOLDERS[placeholderIndex]}
                className="w-full min-h-[44px] rounded-xl border border-[#e8e8e8] bg-[#f4f4f4] dark:bg-surface-raised pl-10 pr-9 py-2 text-xs sm:text-sm font-medium text-foreground placeholder:text-stone-400 focus:bg-white focus:border-[#0c831f] focus:ring-1 focus:ring-[#0c831f] focus:outline-none transition shadow-2xs"
                autoComplete="off"
                aria-label="Search catalogue"
              />
              {searchVal && (
                <button
                  type="button"
                  onClick={() => {
                    setUserInput("");
                    const params = new URLSearchParams(searchParams.toString());
                    params.delete("q");
                    router.push(`/?${params.toString()}`);
                  }}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 min-h-[36px] min-w-[36px] flex items-center justify-center text-muted hover:text-foreground cursor-pointer transition"
                  aria-label="Clear search"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </form>
          </div>

          {/* Desktop-only Account & Cart Actions */}
          <div className="hidden sm:flex items-center gap-4 sm:gap-6 shrink-0">
            {/* Login Button */}
            <button
              type="button"
              onClick={() => setShowLoginModal(true)}
              className="text-sm font-extrabold text-[#1f1f1f] dark:text-foreground hover:text-[#0c831f] transition cursor-pointer px-1 py-1"
              aria-label="Login to account"
            >
              Login
            </button>

            {/* Cart Button (Blinkit Green Pill) */}
            <Link
              href="/basket"
              className={`flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs sm:text-sm font-extrabold shadow-xs transition ${
                safeLineCount > 0
                  ? "bg-[#0c831f] text-white hover:bg-[#0a721b]"
                  : "bg-[#f3f3f3] text-[#1f1f1f] hover:bg-[#e8e8e8]"
              }`}
              aria-label={`Shopping cart with ${safeLineCount} items`}
            >
              <ShoppingCart className="h-4 w-4" />
              <span>{safeLineCount > 0 ? `${safeLineCount} Items` : "My Cart"}</span>
            </Link>
          </div>
        </div>
      </div>

      {/* Sub-Header: Top Category Navigation Tabs (only shown on homepage root view) */}
      {showSubNav && (
        <div className="border-t border-[#e8e8e8] bg-white dark:bg-surface transition-colors">
          <nav
            aria-label="Top categories"
            className="mx-auto flex w-full max-w-[1440px] items-center gap-6 sm:gap-8 overflow-x-auto px-4 sm:px-6 lg:px-8 py-2 scrollbar-none"
          >
            {TOP_NAV_TABS.map((tab) => {
              const isActive =
                (tab.category === "all" && currentCategory === "all") ||
                (tab.category !== "all" && currentCategory === tab.category);

              const Icon = tab.icon;

              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => handleTabClick(tab.category)}
                  className={`flex shrink-0 items-center gap-2 pb-1 text-xs sm:text-sm font-bold transition-all relative select-none cursor-pointer ${
                    isActive
                      ? "text-[#0c831f]"
                      : "text-stone-500 hover:text-stone-900"
                  }`}
                >
                  <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span>{tab.label}</span>
                  {isActive && (
                    <span className="absolute bottom-0 left-0 right-0 h-0.5 rounded-full bg-[#0c831f]" />
                  )}
                </button>
              );
            })}
          </nav>
        </div>
      )}

      {/* Location Modal */}
      {showLocationModal && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="location-modal-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-xs"
        >
          <div className="w-full max-w-sm rounded-3xl bg-white p-6 shadow-xl border border-stone-200 space-y-4 animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between">
              <h2 id="location-modal-title" className="text-base font-black text-[#1f1f1f] flex items-center gap-2">
                <MapPin className="h-4 w-4 text-[#0c831f]" />
                <span>Delivery Location</span>
              </h2>
              <button
                type="button"
                onClick={() => setShowLocationModal(false)}
                className="text-stone-400 hover:text-stone-900 p-1 rounded-lg transition"
                aria-label="Close dialog"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="rounded-2xl bg-[#f8f8f8] p-4 space-y-1">
              <p className="font-extrabold text-sm text-[#1f1f1f]">TOWER-C, Nirvana Country, Sec 50</p>
              <p className="text-xs text-stone-500">Gurugram, Haryana · 122018</p>
            </div>
            <p className="text-xs text-stone-500">
              Delivery in 8 minutes guaranteed from local dark store warehouse.
            </p>
            <button
              type="button"
              onClick={() => setShowLocationModal(false)}
              className="w-full h-11 rounded-xl bg-[#0c831f] font-bold text-white text-xs shadow-xs hover:bg-[#0a721b] transition cursor-pointer"
            >
              Confirm Location
            </button>
          </div>
        </div>
      )}

      {/* Login Demo Modal */}
      {showLoginModal && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="login-modal-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-xs"
        >
          <div className="w-full max-w-sm rounded-3xl bg-white p-6 shadow-xl border border-stone-200 space-y-4 text-center animate-in fade-in zoom-in-95 duration-150">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-[#f0f9f2] text-[#0c831f]">
              <User className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <h2 id="login-modal-title" className="text-base font-black text-[#1f1f1f]">
                Blinkit User Session
              </h2>
              <p className="text-xs text-stone-500">
                Logged in as Verified Buyer (governed agentic session).
              </p>
            </div>
            <button
              type="button"
              onClick={() => setShowLoginModal(false)}
              className="w-full h-11 rounded-xl bg-[#0c831f] font-bold text-white text-xs shadow-xs hover:bg-[#0a721b] transition cursor-pointer"
            >
              Got it
            </button>
          </div>
        </div>
      )}
    </header>
  );
}

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
  { id: "dairy", label: "Dairy & Eggs", icon: Milk, category: "dairy" },
  { id: "staples", label: "Atta, Rice & Oil", icon: Wheat, category: "staples" },
  { id: "fresh", label: "Fresh Vegetables", icon: Carrot, category: "produce" },
  { id: "snacks", label: "Snacks & Munchies", icon: Cookie, category: "snacks" },
  { id: "beverages", label: "Tea & Cold Drinks", icon: Coffee, category: "beverages" },
  { id: "bakery", label: "Bakery & Bread", icon: Croissant, category: "bakery" },
  { id: "household", label: "Cleaning & Household", icon: Sparkles, category: "household" },
  { id: "personal_care", label: "Personal Care", icon: HeartPulse, category: "personal_care" },
  { id: "condiments", label: "Masalas & Spices", icon: Flame, category: "condiments" },
];

const SEARCH_PLACEHOLDERS = [
  'Search for "amul butter"',
  'Search for "cheese slices"',
  'Search for "chocolate box"',
  'Search for "toned milk"',
  'Search for "aashirvaad atta"',
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

  // Gentle placeholder rotation matching Zepto's live feel
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
    <header className="sticky top-0 z-40 bg-surface/95 backdrop-blur-md transition-colors">
      {/* Primary Top Bar */}
      <div className="border-b border-line">
        <div className="mx-auto flex w-full max-w-[1440px] flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 px-4 sm:px-6 lg:px-8 py-2.5 sm:py-3">
          {/* Row 1 on mobile: Logo + Delivery Info + Right utility actions */}
          <div className="flex items-center justify-between gap-3 sm:gap-6">
            <div className="flex items-center gap-3 sm:gap-6">
              {/* Zepto Wordmark Logo */}
              <Link
                href="/"
                className="group flex items-baseline focus-visible:ring-2 focus-visible:ring-brand-purple rounded-lg px-1"
                aria-label="Zepto home"
              >
                <span className="text-2xl sm:text-3xl font-black text-[#950EDB] group-hover:opacity-90 transition tracking-tight">
                  zepto
                </span>
                <span className="ml-2 hidden rounded-md bg-purple-50 px-1.5 py-0.5 text-[10px] font-bold text-[#950EDB] sm:inline-block border border-purple-100 dark:bg-purple-950/60 dark:border-purple-800 dark:text-purple-300">
                  clone demo
                </span>
              </Link>

              {/* Delivery Location Pill with >=44px tap target */}
              <button
                type="button"
                onClick={() => setShowLocationModal(true)}
                className="flex flex-col justify-center text-left group min-h-[44px] focus-visible:ring-2 focus-visible:ring-brand-purple rounded-lg px-1.5 cursor-pointer hover:bg-surface-raised transition"
                aria-label="Delivery location: Select Location"
              >
                <div className="flex items-center gap-1.5 text-xs font-black text-foreground leading-tight">
                  <Zap className="h-3.5 w-3.5 fill-amber-400 text-amber-500 shrink-0" aria-hidden="true" />
                  <span>10 Mins*</span>
                </div>
                <div className="flex items-center gap-1 text-[11px] font-semibold text-muted group-hover:text-foreground transition leading-tight">
                  <span className="truncate max-w-[110px] sm:max-w-none">Select Location</span>
                  <ChevronDown className="h-3 w-3 text-muted shrink-0" aria-hidden="true" />
                </div>
              </button>
            </div>

            {/* Mobile-only Account & Cart */}
            <div className="flex sm:hidden items-center gap-2 shrink-0">
              <button
                type="button"
                onClick={() => setShowLoginModal(true)}
                className="flex min-h-[44px] min-w-[44px] flex-col items-center justify-center text-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-brand-purple rounded-xl cursor-pointer transition"
                aria-label="Login to account"
              >
                <User className="h-5 w-5" />
                <span className="text-[10px] font-bold mt-0.5">Login</span>
              </button>

              <Link
                href="/basket"
                className="flex min-h-[44px] min-w-[44px] flex-col items-center justify-center text-muted hover:text-foreground relative focus-visible:ring-2 focus-visible:ring-brand-purple rounded-xl transition"
                aria-label={`Shopping cart with ${safeLineCount} items`}
              >
                <div className="relative flex h-5 w-5 items-center justify-center">
                  <ShoppingCart className="h-5 w-5" />
                  {safeLineCount > 0 && (
                    <span className="absolute -top-1.5 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-[#ff3269] px-1 text-[10px] font-black text-white shadow-xs">
                      {safeLineCount}
                    </span>
                  )}
                </div>
                <span className="text-[10px] font-bold mt-0.5">Cart</span>
              </Link>
            </div>
          </div>

          {/* Search Bar: Full-width on mobile row 2, centered on sm+ */}
          <div className="w-full sm:flex-1 sm:max-w-2xl">
            <form onSubmit={handleSearchSubmit} role="search" className="relative w-full">
              <span
                aria-hidden="true"
                className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-muted"
              >
                <Search className="h-4 w-4" />
              </span>
              <input
                type="search"
                value={searchVal}
                onChange={(e) => setUserInput(e.target.value)}
                placeholder={SEARCH_PLACEHOLDERS[placeholderIndex]}
                className="w-full min-h-[44px] rounded-xl border border-line bg-surface-raised pl-10 pr-9 py-2 text-xs sm:text-sm font-medium text-foreground placeholder:text-muted focus:bg-surface focus:border-[#950EDB] focus:ring-1 focus:ring-[#950EDB] focus:outline-none transition shadow-2xs"
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
              className="flex flex-col items-center justify-center text-muted hover:text-foreground group focus:outline-none cursor-pointer transition"
              aria-label="Login to account"
            >
              <div className="flex h-5 w-5 sm:h-6 sm:w-6 items-center justify-center">
                <User className="h-5 w-5" />
              </div>
              <span className="text-[10px] sm:text-[11px] font-bold mt-0.5">Login</span>
            </button>

            {/* Cart Button */}
            <Link
              href="/basket"
              className="flex flex-col items-center justify-center text-muted hover:text-foreground relative group focus:outline-none transition"
              aria-label={`Shopping cart with ${safeLineCount} items`}
            >
              <div className="relative flex h-5 w-5 sm:h-6 sm:w-6 items-center justify-center">
                <ShoppingCart className="h-5 w-5" />
                {safeLineCount > 0 && (
                  <span className="absolute -top-1.5 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-[#ff3269] px-1 text-[10px] font-black text-white shadow-xs">
                    {safeLineCount}
                  </span>
                )}
              </div>
              <span className="text-[10px] sm:text-[11px] font-bold mt-0.5">Cart</span>
            </Link>
          </div>
        </div>
      </div>

      {/* Sub-Header: Top Category Navigation Tabs (only shown on homepage root view) */}
      {showSubNav && (
        <div className="border-b border-line bg-surface transition-colors">
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
                      ? "text-[#950EDB]"
                      : "text-muted hover:text-foreground"
                  }`}
                >
                  <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span>{tab.label}</span>
                  {isActive && (
                    <span className="absolute bottom-0 left-0 right-0 h-0.5 rounded-full bg-[#950EDB]" />
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
          <div className="w-full max-w-sm rounded-3xl bg-surface p-6 shadow-xl border border-line space-y-4 animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between">
              <h2 id="location-modal-title" className="text-base font-black text-foreground flex items-center gap-2">
                <MapPin className="h-4 w-4 text-brand-purple" />
                <span>Delivery Location</span>
              </h2>
              <button
                type="button"
                onClick={() => setShowLocationModal(false)}
                className="text-muted hover:text-foreground p-1 rounded-lg transition"
                aria-label="Close dialog"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="rounded-2xl bg-surface-raised p-4 space-y-1">
              <p className="font-bold text-sm text-foreground">Central Mumbai · 400001</p>
              <p className="text-xs text-muted">Quick-commerce test service simulation</p>
            </div>
            <p className="text-xs text-muted">
              Deliveries and inventory are bound to the merchant simulator test warehouse.
            </p>
            <button
              type="button"
              onClick={() => setShowLocationModal(false)}
              className="w-full h-11 rounded-xl bg-[#ff3269] font-bold text-white text-xs shadow-xs hover:bg-[#e0285a] transition cursor-pointer"
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
          <div className="w-full max-w-sm rounded-3xl bg-surface p-6 shadow-xl border border-line space-y-4 text-center animate-in fade-in zoom-in-95 duration-150">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-brand-purple-light text-brand-purple">
              <User className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <h2 id="login-modal-title" className="text-base font-black text-foreground">
                Buyer Session
              </h2>
              <p className="text-xs text-muted">
                Authenticated as Test Buyer (governed agentic session).
              </p>
            </div>
            <button
              type="button"
              onClick={() => setShowLoginModal(false)}
              className="w-full h-11 rounded-xl bg-[#950EDB] font-bold text-white text-xs shadow-xs hover:bg-[#7b0bb7] transition cursor-pointer"
            >
              Got it
            </button>
          </div>
        </div>
      )}
    </header>
  );
}

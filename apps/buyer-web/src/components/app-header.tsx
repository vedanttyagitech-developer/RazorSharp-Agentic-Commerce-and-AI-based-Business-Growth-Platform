"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { useBasketRef } from "./providers";

const TOP_NAV_TABS = [
  { id: "all", label: "All", icon: "🛍️", category: "all" },
  { id: "dairy", label: "Dairy & Eggs", icon: "🥛", category: "dairy" },
  { id: "staples", label: "Atta, Rice & Oil", icon: "🌾", category: "staples" },
  { id: "fresh", label: "Fresh Vegetables", icon: "🥦", category: "produce" },
  { id: "snacks", label: "Snacks & Munchies", icon: "🍪", category: "snacks" },
  { id: "beverages", label: "Tea & Cold Drinks", icon: "☕", category: "beverages" },
  { id: "bakery", label: "Bakery & Bread", icon: "🍞", category: "bakery" },
  { id: "household", label: "Cleaning & Household", icon: "🧼", category: "household" },
  { id: "personal_care", label: "Personal Care", icon: "🧴", category: "personal_care" },
  { id: "condiments", label: "Masalas & Spices", icon: "🌶️", category: "condiments" },
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
    setUserInput(null);
    router.push(`/?${params.toString()}`);
  }

  function handleTabClick(tabCategory: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (tabCategory !== "all") {
      params.set("category", tabCategory);
    } else {
      params.delete("category");
    }
    router.push(`/?${params.toString()}`);
  }

  return (
    <header className="sticky top-0 z-40 bg-white shadow-xs">
      {/* Main Top Header Bar — Clean White with Zepto Branding */}
      <div className="border-b border-stone-200/80 bg-white">
        <div className="mx-auto flex w-full max-w-[1440px] items-center justify-between gap-3 sm:gap-6 px-4 sm:px-6 lg:px-8 py-2.5">
          {/* Left: Zepto Wordmark & Delivery Location */}
          <div className="flex items-center gap-4 sm:gap-6 shrink-0">
            <Link
              href="/"
              className="flex items-center tracking-tighter lowercase group"
              aria-label="Zepto Clone Demo Homepage"
            >
              <span className="text-2xl sm:text-3xl font-black text-[#950EDB] group-hover:opacity-90 transition">
                zepto
              </span>
              <span className="ml-2 hidden rounded-md bg-purple-50 px-1.5 py-0.5 text-[10px] font-bold text-[#950EDB] sm:inline-block border border-purple-100">
                clone demo
              </span>
            </Link>

            {/* Delivery Location Pill */}
            <button
              type="button"
              onClick={() => setShowLocationModal(true)}
              className="flex flex-col text-left group focus:outline-none cursor-pointer"
              aria-label="Delivery location: Select Location"
            >
              <div className="flex items-center gap-1 text-xs font-black text-stone-900 leading-tight">
                <span className="text-amber-500 font-bold" aria-hidden="true">⚡</span>
                <span>Delivery in minutes*</span>
              </div>
              <div className="flex items-center gap-1 text-[11px] font-semibold text-stone-500 group-hover:text-stone-800 transition leading-tight">
                <span>Select Location</span>
                <span className="text-[10px] leading-none text-stone-400">⌵</span>
              </div>
            </button>
          </div>

          {/* Center: Search Bar */}
          <div className="flex-1 max-w-2xl">
            <form onSubmit={handleSearchSubmit} role="search" className="relative w-full">
              <span
                aria-hidden="true"
                className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-stone-400 text-sm"
              >
                🔍
              </span>
              <input
                type="search"
                value={searchVal}
                onChange={(e) => setUserInput(e.target.value)}
                placeholder={SEARCH_PLACEHOLDERS[placeholderIndex]}
                className="w-full rounded-xl border border-line bg-surface-raised pl-10 pr-9 py-2 text-xs sm:text-sm font-medium text-foreground placeholder:text-muted focus:bg-surface focus:border-[#950EDB] focus:outline-none transition shadow-2xs"
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
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-xs font-bold text-stone-400 hover:text-stone-600"
                  aria-label="Clear search"
                >
                  ✕
                </button>
              )}
            </form>
          </div>

          {/* Right: Login & Cart Actions */}
          <div className="flex items-center gap-4 sm:gap-6 shrink-0">
            {/* Login Button */}
            <button
              type="button"
              onClick={() => setShowLoginModal(true)}
              className="flex flex-col items-center justify-center text-muted hover:text-foreground group focus:outline-none cursor-pointer transition"
              aria-label="Login to account"
            >
              <div className="flex h-5 w-5 sm:h-6 sm:w-6 items-center justify-center">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                  <circle cx="12" cy="7" r="4" />
                </svg>
              </div>
              <span className="text-[10px] sm:text-[11px] font-bold mt-0.5">Login</span>
            </button>

            {/* Cart Button */}
            <Link
              href="/basket"
              className="flex flex-col items-center justify-center text-muted hover:text-foreground relative group focus:outline-none transition"
              aria-label={`Shopping cart with ${lineCount} items`}
            >
              <div className="relative flex h-5 w-5 sm:h-6 sm:w-6 items-center justify-center">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="9" cy="21" r="1" />
                  <circle cx="20" cy="21" r="1" />
                  <path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6" />
                </svg>
                {lineCount > 0 && (
                  <span className="absolute -top-1.5 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-[#ff3269] px-1 text-[10px] font-black text-white shadow-xs">
                    {lineCount}
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

              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => handleTabClick(tab.category)}
                  className={`flex shrink-0 items-center gap-1.5 pb-1 text-xs sm:text-sm font-bold transition-all relative select-none cursor-pointer ${
                    isActive
                      ? "text-[#950EDB]"
                      : "text-muted hover:text-foreground"
                  }`}
                >
                  <span aria-hidden="true">{tab.icon}</span>
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
          <div className="w-full max-w-sm rounded-3xl bg-surface p-6 shadow-xl border border-line space-y-4">
            <div className="flex items-center justify-between">
              <h2 id="location-modal-title" className="text-base font-black text-foreground flex items-center gap-2">
                <span>📍</span>
                <span>Delivery Location</span>
              </h2>
              <button
                type="button"
                onClick={() => setShowLocationModal(false)}
                className="text-stone-400 hover:text-stone-900 text-sm font-bold"
                aria-label="Close dialog"
              >
                ✕
              </button>
            </div>
            <div className="rounded-2xl bg-surface-raised p-4 space-y-1">
              <p className="font-bold text-sm text-foreground">Central Mumbai · 400001</p>
              <p className="text-xs text-stone-500">Quick-commerce test service simulation</p>
            </div>
            <p className="text-xs text-stone-500">
              Deliveries and inventory are bound to the merchant simulator test warehouse.
            </p>
            <button
              type="button"
              onClick={() => setShowLocationModal(false)}
              className="w-full h-10 rounded-xl bg-[#ff3269] font-bold text-white text-xs shadow-xs hover:bg-[#e0285a] transition cursor-pointer"
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
          <div className="w-full max-w-sm rounded-3xl bg-surface p-6 shadow-xl border border-line space-y-4 text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-brand-purple-light text-2xl">
              👤
            </div>
            <div className="space-y-1">
              <h2 id="login-modal-title" className="text-base font-black text-foreground">
                Buyer Session
              </h2>
              <p className="text-xs text-stone-500">
                Authenticated as Test Buyer (governed agentic session).
              </p>
            </div>
            <button
              type="button"
              onClick={() => setShowLoginModal(false)}
              className="w-full h-10 rounded-xl bg-[#950EDB] font-bold text-white text-xs shadow-xs hover:bg-[#7b0bb7] transition cursor-pointer"
            >
              Got it
            </button>
          </div>
        </div>
      )}
    </header>
  );
}

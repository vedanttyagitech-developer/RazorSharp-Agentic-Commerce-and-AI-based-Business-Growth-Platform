"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AvailabilityBadge, FreshnessLine } from "@/components/availability";
import { useClient } from "@/components/providers";
import { Alert, Button, Spinner, StatusPill } from "@/components/ui";
import type { SearchHit, SearchResponse } from "@/lib/api/types";
import { JOURNEY_META } from "@/lib/journey";
import { formatMinor } from "@/lib/money";

import { CategoryGrid } from "./category-grid";
import { CategoryView } from "./category-view";
import { PromoBanners } from "./promo-banners";
import { useBasketActions } from "./use-basket-actions";
import { SafeImage } from "@/components/product-img";
import {
  getDiscountDisplay,
  getMockMrp,
  getProductImage,
} from "@/lib/product-images";

const CATEGORIES = [
  { id: "all", label_en: "All Items", label_hi: "सभी उत्पाद", icon: "🛒" },
  { id: "dairy", label_en: "Dairy & Breakfast", label_hi: "डेयरी और नाश्ता", icon: "🥛" },
  { id: "staples", label_en: "Atta, Rice & Dal", label_hi: "आटा, चावल और दाल", icon: "🌾" },
  { id: "produce", label_en: "Fresh Vegetables", label_hi: "ताज़ी सब्ज़ियाँ", icon: "🥦" },
  { id: "snacks", label_en: "Snacks & Munchies", label_hi: "स्नैक्स और नमकीन", icon: "🍪" },
  { id: "bakery", label_en: "Bakery & Eggs", label_hi: "बेकरी और अंडे", icon: "🍞" },
  { id: "beverages", label_en: "Tea & Beverages", label_hi: "चाय और पेय", icon: "☕" },
] as const;

const LOCALES = [
  { value: "en-IN", label: "English" },
  { value: "hi-IN", label: "हिन्दी" },
  { value: "hi-Latn-IN", label: "Hinglish" },
];

function ProductVisual({ sku, category, isAvailable }: { sku?: string; category: string; isAvailable: boolean }) {
  const cat = CATEGORIES.find((c) => c.id === category);
  const icon = cat?.icon ?? "📦";
  const imageUrl = sku ? getProductImage(sku) : null;

  return (
    <div className="relative aspect-square w-full rounded-2xl bg-[#f8f8fa] flex items-center justify-center p-2.5 overflow-hidden">
      <SafeImage
        src={imageUrl}
        alt={category}
        fallbackEmoji={icon}
        className="w-full h-full object-contain transition-transform duration-300 group-hover:scale-105"
      />
      {!isAvailable ? (
        <div className="absolute inset-0 bg-stone-900/40 backdrop-blur-[1px] flex items-center justify-center">
          <span className="rounded-full bg-stone-900/80 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-white">
            Out of Stock
          </span>
        </div>
      ) : null}
    </div>
  );
}

export function SearchPanel() {
  const client = useClient();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const {
    addOne,
    setQuantity,
    busySku,
    isMutating,
    error: basketError,
    feedback,
    lastBasket,
    lastFailedAction,
    retryLastAction,
  } = useBasketActions();

  // Derive query, category, and locale directly from URL searchParams
  const query = searchParams.get("q") ?? "";
  const selectedCategory = searchParams.get("category") ?? "all";
  const locale = searchParams.get("locale") ?? "en-IN";

  const [phase, setPhase] = useState<"idle" | "searching" | "checked">("idle");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const updateUrl = useCallback(
    (q: string, category: string, loc: string) => {
      const params = new URLSearchParams();
      if (q.trim()) params.set("q", q.trim());
      if (category && category !== "all") params.set("category", category);
      if (loc && loc !== "en-IN") params.set("locale", loc);
      const queryString = params.toString();
      router.replace(`${pathname}${queryString ? `?${queryString}` : ""}`, { scroll: false });
    },
    [pathname, router],
  );

  useEffect(() => {
    let cancelled = false;
    client
      .search({ q: query, locale, limit: 100 })
      .then((response) => {
        if (!cancelled) {
          setResults(response);
          setPhase("checked");
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Search failed");
          setPhase("idle");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, locale, query, reloadKey]);

  function onSelectCategory(catId: string) {
    if (catId === "all") {
      updateUrl(query, "all", locale);
    } else {
      updateUrl(query, catId, locale);
    }
  }

  function onClearFilters() {
    updateUrl("", "all", locale);
  }

  const filteredHits = useMemo((): SearchHit[] => {
    if (!results) return [];
    if (selectedCategory === "all") return results.hits;
    return results.hits.filter((hit) => hit.category === selectedCategory);
  }, [results, selectedCategory]);

  const journey =
    phase === "searching"
      ? JOURNEY_META.SEARCHING
      : phase === "checked"
        ? JOURNEY_META.AVAILABILITY_CHECKED
        : null;

  return (
    <section aria-labelledby="storefront-heading" className="space-y-7">
      <h1 id="storefront-heading" className="sr-only">
        Grocery Shopping Discovery
      </h1>

      {/* Screen-reader live region for basket notifications */}
      <div role="status" aria-live="polite" className="sr-only">
        {feedback?.message}
      </div>

      {/* Main Content Area: CategoryView when a category is selected, otherwise Homepage Banners + Grid */}
      {selectedCategory !== "all" && !query ? (
        <CategoryView
          category={selectedCategory}
          hits={filteredHits}
          isLoading={!results}
          onSelectCategory={onSelectCategory}
        />
      ) : (
        <>
          {/* 1. Dual Hero Banners (Exact Zepto Screenshot Layout) */}
          <PromoBanners onSelectCategory={onSelectCategory} />

          {/* 2. Two-Row 20-Category Exploration Grid (Exact Zepto Screenshot Layout) */}
          <CategoryGrid activeCategory={selectedCategory} onSelectCategory={onSelectCategory} />

          {/* Alerts & Feedback */}
          {error ? (
            <Alert tone="danger" title="Search failed" role="alert">
              <p className="mb-2">{error}</p>
              <Button variant="secondary" onClick={() => setReloadKey((k) => k + 1)}>
                Retry search
              </Button>
            </Alert>
          ) : null}

          {basketError ? (
            <Alert tone="danger" title="Basket update failed" role="alert">
              <p className="mb-2">{basketError}</p>
              {lastFailedAction ? (
                <Button variant="secondary" onClick={() => void retryLastAction()}>
                  Retry action
                </Button>
              ) : null}
            </Alert>
          ) : null}

          {feedback ? (
            <div
              role="status"
              className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-emerald-300 bg-emerald-50 px-4 py-2.5 text-xs font-semibold text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950 dark:text-emerald-100 shadow-xs"
            >
              <div className="flex items-center gap-2">
                <span aria-hidden="true" className="font-bold">✓</span>
                <span>{feedback.message}</span>
              </div>
              {lastBasket?.quote ? (
                <span className="tabular-nums">
                  Basket Total: <strong>{formatMinor(lastBasket.quote.total_minor, lastBasket.quote.currency)}</strong>
                </span>
              ) : null}
            </div>
          ) : null}

          {/* 3. Catalogue Header & Filter Strip */}
          <div className="pt-2 border-t border-line/80 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-0.5">
                <h2 className="text-xl sm:text-2xl font-black text-foreground tracking-tight">
                  {query
                    ? `Results for "${query}"`
                    : "Trending Grocery Essentials"}
                </h2>
                <p className="text-xs text-muted">
                  Live catalogue stock and pricing verified by deterministic quote engine.
                </p>
              </div>

              <div className="flex items-center gap-3">
                {/* Display Language Selector */}
                <div className="flex items-center gap-1.5 text-xs">
                  <label htmlFor="locale-dropdown" className="font-medium text-stone-500">Language:</label>
                  <select
                    id="locale-dropdown"
                    value={locale}
                    onChange={(event) => {
                      updateUrl(query, selectedCategory, event.target.value);
                    }}
                    className="rounded-lg border border-line bg-surface px-2.5 py-1 text-xs font-semibold text-foreground focus:border-accent focus:outline-none"
                    aria-label="Display language"
                  >
                    {LOCALES.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>

                {selectedCategory !== "all" || query ? (
                  <button
                    type="button"
                    onClick={onClearFilters}
                    className="rounded-full bg-stone-100 px-3 py-1 text-xs font-bold text-stone-700 hover:bg-stone-200 dark:bg-stone-800 dark:text-stone-300 transition"
                  >
                    Clear filters ✕
                  </button>
                ) : null}

                <div role="status" aria-live="polite">
                  {journey ? (
                    <StatusPill tone={journey.tone} glyph={journey.glyph} label={journey.label} />
                  ) : (
                    <span className="text-xs font-medium text-muted">Real-time stock</span>
                  )}
                </div>
              </div>
            </div>

            {/* Quick Category Bar Filter */}
            <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none" role="tablist">
              {CATEGORIES.map((cat) => {
                const isSelected = selectedCategory === cat.id;
                return (
                  <button
                    key={cat.id}
                    role="tab"
                    type="button"
                    aria-selected={isSelected}
                    onClick={() => onSelectCategory(cat.id)}
                    className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-bold transition focus:outline-none focus:ring-2 focus:ring-accent ${
                      isSelected
                        ? "bg-[#3c0065] text-white shadow-sm dark:bg-purple-600 dark:text-white"
                        : "border border-line bg-surface text-foreground hover:border-stone-400 dark:hover:border-stone-600"
                    }`}
                  >
                    <span aria-hidden="true">{cat.icon}</span>
                    <span>{locale === "hi-IN" ? cat.label_hi : cat.label_en}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Loading state */}
          {phase === "searching" && !results ? (
            <div className="py-12 text-center">
              <Spinner label="Searching the live catalogue..." />
            </div>
          ) : null}

          {/* 4. Product Grid Results (Zepto Proportions) */}
          {results ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-medium text-muted">
                <p>
                  Showing <strong className="text-foreground">{filteredHits.length}</strong> product{filteredHits.length === 1 ? "" : "s"}
                </p>
                <FreshnessLine freshness={results.freshness} />
              </div>

              {filteredHits.length === 0 ? (
                <div className="rounded-3xl border border-line bg-surface p-12 text-center space-y-4 shadow-2xs">
                  <div className="text-4xl select-none" aria-hidden="true">🔍</div>
                  <p className="text-base font-bold text-foreground">No products match your search criteria.</p>
                  <p className="text-xs text-muted max-w-md mx-auto">
                    Only grounded catalogue products are returned. Listings reflect actual merchant inventory records.
                  </p>
                  <div>
                    <Button variant="secondary" onClick={onClearFilters}>
                      Clear filters & view all products
                    </Button>
                  </div>
                </div>
              ) : (
                <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-4" role="list">
                  {filteredHits.map((hit) => {
                    const isUnavailable = !hit.is_available;
                    const inBasketLine = lastBasket?.lines.find((line) => line.sku === hit.sku);
                    const currentQty = inBasketLine?.quantity ?? 0;
                    const isItemBusy = busySku === hit.sku || isMutating;

                    const mrpMinor = getMockMrp(hit.unit_price_minor);
                    const discount = getDiscountDisplay(hit.unit_price_minor, mrpMinor);

                    return (
                      <li
                        key={hit.sku}
                        className={`group flex flex-col justify-between rounded-3xl border p-3 sm:p-3.5 transition-all duration-200 shadow-2xs ${
                          isUnavailable
                            ? "border-stone-200 bg-stone-50/70 opacity-75"
                            : "border-stone-200 bg-white hover:shadow-md hover:border-stone-300"
                        }`}
                      >
                        <div className="space-y-2">
                          {/* Product Visual Container with Real Image */}
                          <Link
                            href={`/products/${encodeURIComponent(hit.sku)}`}
                            className="block focus:outline-none focus:ring-2 focus:ring-[#950EDB] rounded-2xl"
                            tabIndex={0}
                            aria-label={`View ${hit.display_name}`}
                          >
                            <ProductVisual sku={hit.sku} category={hit.category} isAvailable={hit.is_available} />
                          </Link>

                          {/* Category & Availability Metadata */}
                          <div className="flex items-center justify-between gap-1 pt-1">
                            <span className="rounded bg-purple-50 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-[#950EDB] border border-purple-100">
                              {hit.category}
                            </span>
                            <AvailabilityBadge product={hit} />
                          </div>

                          {/* Pack Size / Unit Label */}
                          <p className="text-[11px] font-medium text-stone-500">
                            1 pack ({hit.unit_label})
                          </p>

                          {/* Product Title */}
                          <div>
                            <Link
                              href={`/products/${encodeURIComponent(hit.sku)}`}
                              className="font-bold text-xs sm:text-sm text-stone-900 line-clamp-2 leading-snug group-hover:text-[#950EDB] transition-colors focus:outline-none focus:underline"
                            >
                              {hit.display_name}
                            </Link>
                            <p className="mt-0.5 text-[11px] text-stone-500 line-clamp-1">
                              {hit.display_name === hit.name_en ? hit.name_hi : hit.name_en}
                            </p>
                          </div>

                          <FreshnessLine freshness={hit.freshness} />
                        </div>

                        {/* Price and In-Place Stepper Action */}
                        <div className="mt-3.5 pt-2.5 border-t border-stone-100 flex items-center justify-between gap-1.5">
                          <div className="flex flex-col">
                            <div className="flex items-baseline gap-1.5 flex-wrap">
                              <span className="rounded bg-[#168753] px-1.5 py-0.5 text-xs font-black text-white tabular-nums">
                                {formatMinor(hit.unit_price_minor, hit.currency)}
                              </span>
                              <span className="text-[11px] text-stone-400 font-medium line-through tabular-nums">
                                {formatMinor(mrpMinor, hit.currency)}
                              </span>
                            </div>
                            {discount && (
                              <span className="text-[10px] font-bold text-emerald-700 mt-0.5">
                                {discount.text}
                              </span>
                            )}
                          </div>

                          <div className="shrink-0">
                            {hit.is_available ? (
                              currentQty > 0 ? (
                                /* In-place Stepper when item is in basket */
                                <div
                                  className="h-8 rounded-lg bg-[#ff3269] text-white flex items-center justify-between px-1.5 shadow-xs font-bold text-xs"
                                  role="group"
                                  aria-label={`Quantity controls for ${hit.display_name}`}
                                >
                                  <button
                                    type="button"
                                    disabled={isItemBusy}
                                    onClick={() => void setQuantity(hit.sku, currentQty - 1, hit.display_name)}
                                    className="w-6 h-full flex items-center justify-center text-sm font-bold hover:opacity-80 active:scale-90 disabled:opacity-50 cursor-pointer"
                                    aria-label={`Decrease quantity of ${hit.display_name}`}
                                  >
                                    −
                                  </button>
                                  <span
                                    className="min-w-5 text-center font-bold tabular-nums text-xs"
                                    aria-live="polite"
                                    aria-label={`${currentQty} units`}
                                  >
                                    {busySku === hit.sku ? "…" : currentQty}
                                  </span>
                                  <button
                                    type="button"
                                    disabled={isItemBusy}
                                    onClick={() => void setQuantity(hit.sku, currentQty + 1, hit.display_name)}
                                    className="w-6 h-full flex items-center justify-center text-sm font-bold hover:opacity-80 active:scale-90 disabled:opacity-50 cursor-pointer"
                                    aria-label={`Increase quantity of ${hit.display_name}`}
                                  >
                                    +
                                  </button>
                                </div>
                              ) : (
                                /* Crisp Zepto-style ADD button */
                                <button
                                  type="button"
                                  disabled={isItemBusy}
                                  onClick={() => void addOne(hit.sku, hit.display_name)}
                                  className="h-8 px-3.5 rounded-lg border border-[#ff3269] bg-white text-xs font-black text-[#ff3269] tracking-wider hover:bg-[#ff3269]/10 transition active:scale-95 flex items-center gap-1 shadow-xs disabled:opacity-50 cursor-pointer"
                                  aria-label={`Add ${hit.display_name} to basket`}
                                >
                                  <span>ADD</span>
                                  <span className="text-sm font-bold leading-none">+</span>
                                </button>
                              )
                            ) : (
                              /* Unavailable / Sold Out pill */
                              <span className="inline-flex h-8 items-center rounded-lg border border-stone-200 bg-stone-100 px-2 text-[10px] font-bold uppercase tracking-wider text-stone-400">
                                {hit.is_listed ? "Sold Out" : "Unavailable"}
                              </span>
                            )}
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          ) : null}
        </>
      )}

      {/* Floating Cart Entry Pill when items exist */}
      {lastBasket && lastBasket.lines.length > 0 ? (
        <div className="sticky bottom-4 z-30 flex justify-center pointer-events-none pt-2">
          <Link
            href="/basket"
            className="pointer-events-auto flex items-center justify-between gap-4 rounded-2xl bg-[#ff3269] px-5 py-3 text-white shadow-xl hover:bg-[#e0265b] transition active:scale-95"
            aria-label={`View cart: ${lastBasket.lines.length} items`}
          >
            <div className="flex items-center gap-2 text-sm font-bold">
              <span aria-hidden="true">🛍️</span>
              <span>
                {lastBasket.lines.length} {lastBasket.lines.length === 1 ? "Item" : "Items"}
              </span>
              {lastBasket.quote ? (
                <>
                  <span aria-hidden="true" className="opacity-60">|</span>
                  <span className="tabular-nums font-black">
                    {formatMinor(lastBasket.quote.total_minor, lastBasket.quote.currency)}
                  </span>
                </>
              ) : null}
            </div>
            <span className="text-xs font-black uppercase tracking-wider bg-white/20 px-2.5 py-1 rounded-lg">
              View Cart →
            </span>
          </Link>
        </div>
      ) : null}
    </section>
  );
}

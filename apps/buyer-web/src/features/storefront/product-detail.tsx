"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AvailabilityBadge, FreshnessLine } from "@/components/availability";
import { SafeImage } from "@/components/product-img";
import { useClient } from "@/components/providers";
import { Alert, Button, Spinner } from "@/components/ui";
import type { Product } from "@/lib/api/types";
import { formatBasisPoints, formatMinor } from "@/lib/money";
import {
  getDiscountDisplay,
  getMockMrp,
  getProductImage,
  getProductImages,
  getProductMeta,
} from "@/lib/product-images";

import { useBasketActions } from "./use-basket-actions";

export function ProductDetail({ sku, initialProduct }: { sku: string; initialProduct?: Product | null }) {
  const client = useClient();
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

  const [product, setProduct] = useState<Product | null>(initialProduct ?? null);
  const [error, setError] = useState<string | null>(null);
  const [activeImageIdx, setActiveImageIdx] = useState(0);
  const [isComparing, setIsComparing] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [activeTab, setActiveTab] = useState<"about" | "brandImages" | "specifications" | "all">("about");

  useEffect(() => {
    let cancelled = false;
    client
      .getProduct(sku)
      .then((result) => {
        if (!cancelled) {
          setProduct(result);
          setError(null);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setProduct((prev) => {
            if (!prev) {
              setError(cause instanceof Error ? cause.message : "Product unavailable");
            }
            return prev;
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, sku, reloadKey]);

  if (error) {
    return (
      <Alert tone="danger" title="Product could not be loaded" role="alert">
        <p className="mb-2">{error}</p>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => setReloadKey((k) => k + 1)}>
            Retry loading
          </Button>
          <Link href="/" className="inline-flex items-center px-3 py-1.5 text-sm underline">
            Back to search
          </Link>
        </div>
      </Alert>
    );
  }

  if (!product) {
    return (
      <div className="py-16 text-center">
        <Spinner label="Loading product and live availability..." />
      </div>
    );
  }

  const isAvailable = product.is_available;
  const isBusy = busySku === product.sku || isMutating;
  const inBasketLine = lastBasket?.lines.find((line) => line.sku === product.sku);
  const currentQty = inBasketLine?.quantity ?? 0;

  // Real images list with multiple angles
  const productImages = getProductImages(product.sku);
  const primaryImage = getProductImage(product.sku);
  const allImages = productImages.length > 0 ? productImages : primaryImage ? [primaryImage] : [];
  const productMeta = getProductMeta(product.sku, product.category, product.display_name, product.unit_label);

  // Derive brand name
  const brand =
    product.display_name.includes("Apple") || product.display_name.includes("iPhone")
      ? "Apple"
      : product.display_name.includes("Freedom")
        ? "Freedom"
        : product.display_name.includes("Fortune")
          ? "Fortune"
          : product.display_name.includes("Mr.Gold")
            ? "Mr.Gold"
            : product.display_name.includes("Sunpure")
              ? "Sunpure"
              : product.category === "dairy"
                ? "Amul"
                : product.category === "staples"
                  ? product.display_name.includes("India Gate")
                    ? "India Gate"
                    : product.display_name.includes("Aashirvaad")
                      ? "Aashirvaad"
                      : "Fortune"
                  : product.category === "snacks"
                    ? product.display_name.includes("Maggi")
                      ? "Maggi"
                      : product.display_name.includes("Parle")
                        ? "Parle"
                        : "Snacks"
                    : product.category === "bakery"
                      ? "Britannia"
                      : product.category === "beverages"
                        ? product.display_name.includes("Tata")
                          ? "Tata Tea"
                          : "Bisleri"
                        : product.category;

  const categoryEmoji =
    product.category === "dairy"
      ? "🥛"
      : product.category === "staples"
        ? "🌾"
        : product.category === "produce"
          ? "🥦"
          : product.category === "snacks"
            ? "🍪"
            : product.category === "bakery"
              ? "🍞"
              : product.category === "beverages"
                ? "☕"
                : "📱";

  const mrpMinor = getMockMrp(product.unit_price_minor);
  const discount = getDiscountDisplay(product.unit_price_minor, mrpMinor);

  // EMI calculation simulation
  const monthlyEmi = Math.round(product.unit_price_minor / 300);

  return (
    <article className="space-y-6">
      {/* Breadcrumb Navigation matching Screenshot 3 */}
      <nav aria-label="Breadcrumb" className="rounded-2xl border border-line bg-surface px-4 py-2.5 text-xs font-semibold text-muted shadow-2xs transition-colors">
        <ol className="flex items-center gap-1.5 flex-wrap">
          <li>
            <Link href="/" className="hover:text-foreground transition">
              Home
            </Link>
          </li>
          <li aria-hidden="true" className="text-stone-400">›</li>
          <li>
            <Link href={`/?category=${product.category}`} className="hover:text-foreground transition capitalize">
              {product.category === "electronics" ? "Mobile Phones" : product.category}
            </Link>
          </li>
          <li aria-hidden="true" className="text-stone-400">›</li>
          <li className="font-bold text-foreground line-clamp-1 max-w-sm sm:max-w-md" aria-current="page">
            {product.name_en}
          </li>
        </ol>
      </nav>

      {/* Screen-reader live region */}
      <div role="status" aria-live="polite" className="sr-only">
        {feedback?.message}
      </div>

      {/* Error and feedback alerts */}
      {basketError && (
        <Alert tone="danger" title="Basket action failed" role="alert">
          <p className="mb-2 text-xs">{basketError}</p>
          {lastFailedAction && (
            <Button variant="secondary" onClick={() => void retryLastAction()}>
              Retry action
            </Button>
          )}
        </Alert>
      )}

      {feedback && (
        <div
          role="status"
          className="rounded-2xl border border-emerald-300 bg-emerald-50 px-4 py-3 text-xs font-semibold text-emerald-900 shadow-xs flex items-center justify-between"
        >
          <span>✓ {feedback.message}</span>
          {lastBasket?.quote && (
            <span className="tabular-nums">
              Basket Total: <strong>{formatMinor(lastBasket.quote.total_minor, lastBasket.quote.currency)}</strong>
            </span>
          )}
        </div>
      )}

      {/* Main 2-Column Split matching Screenshot 3 */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 lg:gap-10 items-start">
        {/* Left Column: Vertical Thumbnails Strip + Showcase Box + Add to Cart strictly beneath */}
        <div className="lg:col-span-6 flex gap-3 sm:gap-4 items-start">
          {/* Vertical Thumbnail Strip on Left matching Screenshot 3 */}
          <div className="flex flex-col gap-2 shrink-0">
            {allImages.length > 0 ? (
              allImages.slice(0, 6).map((imgUrl, idx) => {
                const isActive = activeImageIdx === idx;
                return (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => setActiveImageIdx(idx)}
                    aria-label={`View image angle ${idx + 1}`}
                    className={`h-14 w-14 sm:h-16 sm:w-16 rounded-2xl border flex items-center justify-center p-1.5 transition-all bg-surface cursor-pointer ${
                      isActive
                        ? "border-2 border-stone-900 shadow-xs scale-102"
                        : "border-line hover:border-line opacity-80 hover:opacity-100"
                    }`}
                  >
                    <SafeImage
                      src={imgUrl}
                      alt={`${product.name_en} thumb ${idx + 1}`}
                      fallbackEmoji={categoryEmoji}
                      className="w-full h-full object-contain"
                    />
                  </button>
                );
              })
            ) : (
              <div className="h-14 w-14 rounded-2xl border border-line flex items-center justify-center text-2xl">
                {categoryEmoji}
              </div>
            )}

            {allImages.length > 1 && (
              <button
                type="button"
                onClick={() => setActiveImageIdx((idx) => (idx + 1) % allImages.length)}
                className="h-6 w-full flex items-center justify-center text-xs font-bold text-stone-400 hover:text-foreground transition cursor-pointer"
                aria-label="Next angle"
              >
                ▼
              </button>
            )}
          </div>

          {/* Right part of Left Column: Large Showcase Image + Full-Width CTA strictly beneath it */}
          <div className="flex-1 space-y-4">
            <div className="relative aspect-square rounded-3xl border border-line bg-surface flex items-center justify-center p-8 sm:p-12 shadow-xs overflow-hidden transition-colors">
              <SafeImage
                src={allImages[activeImageIdx] ?? primaryImage}
                alt={product.name_en}
                fallbackEmoji={categoryEmoji}
                className="w-full h-full object-contain transition-transform duration-300 hover:scale-105"
              />

              {/* Bottom-right next angle circular arrow button matching Screenshot 3 */}
              <button
                type="button"
                onClick={() => setActiveImageIdx((idx) => (idx + 1) % Math.max(1, allImages.length))}
                className="absolute right-3.5 bottom-3.5 flex h-8 w-8 items-center justify-center rounded-full border border-line bg-surface text-muted shadow-xs hover:bg-surface-raised transition cursor-pointer"
                aria-label="Next angle"
              >
                ›
              </button>

              {/* Availability tag */}
              <div className="absolute top-3.5 right-3.5">
                <AvailabilityBadge product={product} />
              </div>
            </div>

            {/* Prominent Full-Width Berry-Pink Add to Cart CTA strictly beneath showcase image box (Screenshot 3) */}
            <div>
              {isAvailable ? (
                currentQty > 0 ? (
                  <div
                    className="w-full h-12 sm:h-14 rounded-2xl bg-[#ff3269] text-white flex items-center justify-between px-6 shadow-md font-black text-base"
                    role="group"
                    aria-label={`Quantity controls for ${product.name_en}`}
                  >
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => void setQuantity(product.sku, currentQty - 1, product.name_en)}
                      className="w-10 h-full flex items-center justify-center text-2xl font-black hover:opacity-80 active:scale-90 disabled:opacity-50 cursor-pointer"
                      aria-label={`Decrease quantity of ${product.name_en}`}
                    >
                      −
                    </button>
                    <span
                      className="font-black tabular-nums text-lg"
                      aria-live="polite"
                      aria-label={`${currentQty} units in cart`}
                    >
                      {busySku === product.sku ? "…" : `${currentQty} in cart`}
                    </span>
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => void setQuantity(product.sku, currentQty + 1, product.name_en)}
                      className="w-10 h-full flex items-center justify-center text-2xl font-black hover:opacity-80 active:scale-90 disabled:opacity-50 cursor-pointer"
                      aria-label={`Increase quantity of ${product.name_en}`}
                    >
                      +
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    disabled={isBusy}
                    onClick={() => void addOne(product.sku, product.name_en)}
                    className="w-full h-12 sm:h-14 rounded-2xl bg-[#ff3269] hover:bg-[#e0285a] active:scale-[0.99] text-white font-black text-base sm:text-lg shadow-md transition flex items-center justify-center gap-2 cursor-pointer disabled:opacity-50"
                    aria-label={`Add ${product.name_en} to basket`}
                  >
                    {isBusy ? (
                      <span>Adding to Cart…</span>
                    ) : (
                      <span>Add to Cart</span>
                    )}
                  </button>
                )
              ) : (
                <button
                  type="button"
                  disabled
                  className="w-full h-12 sm:h-14 rounded-2xl bg-stone-200 text-muted font-bold text-base cursor-not-allowed"
                >
                  Currently Out of Stock
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Right Column: Title, Prices, EMI, Assurances, Open Box Verification */}
        <div className="lg:col-span-6 rounded-3xl border border-line bg-surface p-6 sm:p-7 space-y-5 shadow-2xs transition-colors">
          {/* Open Box Verification Pill & Brand link + Compare Checkbox */}
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="rounded bg-sky-50 border border-sky-200/80 px-2 py-0.5 text-[11px] font-bold text-sky-700">
                Open Box Verification
              </span>
              <span className="text-xs font-bold text-muted hover:text-foreground cursor-pointer">
                {brand} ›
              </span>
            </div>

            <div className="flex items-center gap-3">
              <label className="flex items-center gap-1.5 text-xs font-semibold text-muted cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={isComparing}
                  onChange={(e) => setIsComparing(e.target.checked)}
                  className="rounded border-stone-300 text-[#950EDB] focus:ring-[#950EDB]"
                />
                <span>Compare</span>
              </label>
              <button
                type="button"
                className="text-muted hover:text-foreground p-1 cursor-pointer"
                aria-label="Share product"
                onClick={() => {
                  if (typeof navigator !== "undefined" && navigator.clipboard) {
                    void navigator.clipboard.writeText(window.location.href);
                  }
                }}
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="18" cy="5" r="3" />
                  <circle cx="6" cy="12" r="3" />
                  <circle cx="18" cy="19" r="3" />
                  <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
                  <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
                </svg>
              </button>
            </div>
          </div>

          {/* Product Title */}
          <div className="space-y-1">
            <h1 className="text-xl sm:text-2xl lg:text-3xl font-black text-foreground tracking-tight leading-tight">
              {product.name_en}
            </h1>
            {product.name_hi && product.name_hi !== product.name_en && (
              <p className="text-sm font-semibold text-muted">
                {product.name_hi}
              </p>
            )}
          </div>

          {/* Net Qty & Star Rating */}
          <div className="flex items-center gap-3 text-xs font-semibold text-muted">
            <span>Net Qty: {product.unit_label}</span>
            <span>•</span>
            <div className="flex items-center gap-1 rounded bg-emerald-50 px-2 py-0.5 font-bold text-emerald-800 border border-emerald-200">
              <span className="text-amber-500">★</span>
              <span>4.8</span>
              <span className="text-stone-400 font-normal">(32 reviews)</span>
            </div>
          </div>

          {/* Large Authoritative Price & MRP with Discount tag matching Screenshot 3 */}
          <div className="space-y-1 pt-1 border-t border-stone-100">
            <div className="flex items-baseline gap-2">
              <span className="text-3xl sm:text-4xl font-black text-foreground tracking-tight tabular-nums">
                {formatMinor(product.unit_price_minor, product.currency)}
              </span>
            </div>
            <div className="flex items-center gap-2 text-xs font-medium text-muted flex-wrap">
              <span>MRP</span>
              <span className="line-through tabular-nums text-stone-400">
                {formatMinor(mrpMinor, product.currency)}
              </span>
              <span>(incl. of all taxes)</span>
              {discount && (
                <span className="font-black text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.2 rounded text-[11px]">
                  {discount.text}
                </span>
              )}
            </div>
          </div>

          {/* EMI Card matching Screenshot 3 */}
          <div className="rounded-2xl border border-emerald-200 bg-[#f4fbf7] p-3.5 flex items-center justify-between gap-3 shadow-2xs">
            <div className="flex items-center gap-3">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-700 text-white font-black text-sm">
                %
              </div>
              <div>
                <p className="text-xs font-black text-foreground">
                  ₹0 Platform Fee with Instant UPI
                </p>
                <p className="text-[11px] font-semibold text-emerald-700">
                  ₹{monthlyEmi}/month* with EMI · No Cost EMI available
                </p>
              </div>
            </div>
            <span className="text-xs font-bold text-stone-400">›</span>
          </div>

          {/* Assurance Badges Row matching Screenshot 3 */}
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-2xl border border-line bg-surface-raised p-3 flex items-center gap-3 shadow-2xs">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-stone-100 text-foreground/90 text-lg">
                🛡️
              </div>
              <div>
                <p className="text-xs font-bold text-foreground leading-tight">
                  7 Days Quality Guarantee
                </p>
                <p className="text-[10px] text-muted">
                  Service Centre Repair
                </p>
              </div>
            </div>

            <div className="rounded-2xl border border-line bg-surface-raised p-3 flex items-center gap-3 shadow-2xs">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-stone-100 text-foreground/90 text-lg">
                ⚡
              </div>
              <div>
                <p className="text-xs font-bold text-foreground leading-tight">
                  Fast Delivery
                </p>
                <p className="text-[10px] text-muted">
                  In minutes to your door
                </p>
              </div>
            </div>
          </div>

          {/* Open Box Verification Card matching Screenshot 3 */}
          <div className="rounded-2xl border border-line bg-surface p-4 space-y-2.5 shadow-2xs">
            <div className="flex items-start justify-between gap-3">
              <div className="space-y-1 max-w-[80%]">
                <h3 className="text-xs sm:text-sm font-black text-foreground">
                  Open Box Verification
                </h3>
                <p className="text-xs text-muted leading-relaxed">
                  Your item will be opened at delivery for you to check the physical condition. Accept it if you&apos;re satisfied or return it on the spot.
                </p>
              </div>
              <div className="text-2xl sm:text-3xl select-none" aria-hidden="true">
                📦
              </div>
            </div>
            <div className="pt-1">
              <span className="text-xs font-black text-[#ff3269] hover:underline cursor-pointer flex items-center gap-1">
                <span>View details</span>
                <span>›</span>
              </span>
            </div>
          </div>

          {/* Technical & Commercial Facts (preserving architectural fidelity) */}
          <div className="rounded-2xl border border-line bg-surface-raised p-4 space-y-2 text-xs">
            <h3 className="font-bold text-foreground">Governed Product Facts</h3>
            <p className="text-foreground/90 font-semibold">
              Applicable GST: {formatBasisPoints(product.tax_bp)}
            </p>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-muted">
              <dt className="text-muted">SKU:</dt>
              <dd className="font-mono font-medium text-foreground">{product.sku}</dd>

              <dt className="text-muted">Category:</dt>
              <dd className="capitalize text-foreground">{product.category}</dd>

              <dt className="text-muted">Stock Units:</dt>
              <dd className="tabular-nums text-foreground">{product.stock_units}</dd>
            </dl>
            <div className="pt-2 border-t border-line">
              <FreshnessLine freshness={product.freshness} />
            </div>
          </div>
        </div>
      </div>

      {/* Zepto Authenticity Tabs Navigation Strip */}
      <div className="pt-6 border-t border-line">
        <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "about"}
            onClick={() => setActiveTab("about")}
            className={`px-4 py-2 rounded-full text-xs font-bold transition whitespace-nowrap cursor-pointer ${
              activeTab === "about"
                ? "bg-[#3c0065] text-white shadow-xs"
                : "border border-line bg-surface text-foreground/90 hover:border-stone-400"
            }`}
          >
            About & Highlights
          </button>

          {productMeta.brandImages.length > 0 && (
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === "brandImages"}
              onClick={() => setActiveTab("brandImages")}
              className={`px-4 py-2 rounded-full text-xs font-bold transition whitespace-nowrap cursor-pointer flex items-center gap-1.5 ${
                activeTab === "brandImages"
                  ? "bg-[#3c0065] text-white shadow-xs"
                  : "border border-line bg-surface text-foreground/90 hover:border-stone-400"
              }`}
            >
              <span>Brand Images</span>
              <span className="rounded-full bg-surface/20 px-1.5 py-0.2 text-[10px] font-black">
                {productMeta.brandImages.length}
              </span>
            </button>
          )}

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "specifications"}
            onClick={() => setActiveTab("specifications")}
            className={`px-4 py-2 rounded-full text-xs font-bold transition whitespace-nowrap cursor-pointer ${
              activeTab === "specifications"
                ? "bg-[#3c0065] text-white shadow-xs"
                : "border border-line bg-surface text-foreground/90 hover:border-stone-400"
            }`}
          >
            Specifications & Details
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "all"}
            onClick={() => setActiveTab("all")}
            className={`px-4 py-2 rounded-full text-xs font-bold transition whitespace-nowrap cursor-pointer ${
              activeTab === "all"
                ? "bg-[#3c0065] text-white shadow-xs"
                : "border border-line bg-surface text-foreground/90 hover:border-stone-400"
            }`}
          >
            All Details
          </button>
        </div>
      </div>

      {/* Product Description / About the Product Section */}
      {(activeTab === "about" || activeTab === "all") && (
        <section id="product-description-section" className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-xl sm:text-2xl font-black text-foreground tracking-tight">
              Product Description
            </h2>
            <span className="text-xs font-bold text-emerald-800 bg-emerald-50 border border-emerald-200 px-2.5 py-0.5 rounded-full flex items-center gap-1">
              <span>✓</span>
              <span>100% Genuine</span>
            </span>
          </div>
          <div className="rounded-3xl border border-line bg-surface p-6 sm:p-8 shadow-xs space-y-3">
            <h3 className="text-xs font-black uppercase tracking-wider text-stone-400">
              About the Product
            </h3>
            <p className="text-sm sm:text-base text-foreground/90 leading-relaxed">
              {productMeta.description}
            </p>
          </div>
        </section>
      )}

      {/* Highlights & Key Features Section */}
      {(activeTab === "about" || activeTab === "all") && (
        <section id="product-highlights-section" className="space-y-4">
          <h2 className="text-xl sm:text-2xl font-black text-foreground tracking-tight">
            Key Features & Highlights
          </h2>
          <div className="rounded-3xl border border-line bg-surface p-6 sm:p-8 shadow-xs space-y-4">
            <h3 className="text-xs font-black uppercase tracking-wider text-stone-400">
              Highlights
            </h3>
            <div className="divide-y divide-stone-100">
              {productMeta.highlights.map((item, idx) => (
                <div
                  key={idx}
                  className="flex flex-col sm:flex-row sm:items-center justify-between py-3.5 gap-1"
                >
                  <span className="text-xs sm:text-sm font-medium text-[#5A6477] w-full sm:w-1/3 capitalize">
                    {item.label}
                  </span>
                  <span className="text-xs sm:text-sm font-semibold text-[#101418] w-full sm:w-2/3">
                    {item.value}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      {/* Brand Images Showcase Section */}
      {(activeTab === "brandImages" || activeTab === "all") && productMeta.brandImages.length > 0 && (
        <section id="brand-images-section" className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-xl sm:text-2xl font-black text-foreground tracking-tight">
                Brand Images
              </h2>
              <p className="text-xs text-muted">Official product visual highlights from Zepto</p>
            </div>
            <span className="text-xs font-bold text-stone-400">
              {productMeta.brandImages.length} Visual Cards
            </span>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {productMeta.brandImages.map((imgUrl, idx) => (
              <div
                key={idx}
                className="group overflow-hidden rounded-3xl border border-line bg-surface p-4 shadow-xs hover:shadow-md transition duration-300"
              >
                <div className="relative aspect-square w-full rounded-2xl bg-surface-raised overflow-hidden flex items-center justify-center">
                  <SafeImage
                    src={imgUrl}
                    alt={`${product.name_en} brand asset ${idx + 1}`}
                    fallbackEmoji={categoryEmoji}
                    className="h-full w-full object-contain transition-transform duration-300 group-hover:scale-105"
                  />
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Specifications & Product Information Section */}
      {(activeTab === "specifications" || activeTab === "all") && (
        <section id="product-specifications-section" className="space-y-4">
          <h2 className="text-xl sm:text-2xl font-black text-foreground tracking-tight">
            Information & Specifications
          </h2>
          <div className="overflow-hidden rounded-3xl border border-line bg-surface shadow-xs">
            <div className="p-5 sm:p-6 border-b border-stone-100 bg-surface-raised/50">
              <h3 className="text-xs font-black uppercase tracking-wider text-stone-400">
                Specifications & Regulatory Details
              </h3>
            </div>
            <div className="divide-y divide-stone-100 px-6">
              {productMeta.specifications.map((spec, idx) => (
                <div
                  key={idx}
                  className="flex flex-col sm:flex-row sm:items-start justify-between py-3.5 gap-1.5"
                >
                  <span className="text-xs sm:text-sm font-medium text-[#5A6477] w-full sm:w-1/3 shrink-0 capitalize">
                    {spec.label}
                  </span>
                  <span className="text-xs sm:text-sm font-semibold text-[#101418] w-full sm:w-2/3 leading-relaxed">
                    {spec.value}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      {/* Disclaimer & Customer Support Card matching Zepto Bottom */}
      <section className="rounded-3xl border border-line bg-surface p-6 space-y-3 text-xs text-muted leading-relaxed shadow-2xs">
        <h4 className="font-bold text-foreground uppercase tracking-wider text-[11px]">Disclaimer</h4>
        <p>{productMeta.disclaimer}</p>
        <div className="pt-3 border-t border-stone-100 flex flex-wrap items-center justify-between gap-2 text-muted">
          <span>{productMeta.customerCare}</span>
          <span className="font-semibold text-emerald-700">✓ 100% Genuine Quality Guaranteed</span>
        </div>
      </section>
    </article>
  );
}

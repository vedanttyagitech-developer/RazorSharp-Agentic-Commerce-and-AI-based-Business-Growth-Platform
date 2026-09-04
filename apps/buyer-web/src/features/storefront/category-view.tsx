"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { FreshnessLine } from "@/components/availability";
import {
  Wheat,
  Milk,
  Carrot,
  Cookie,
  Coffee,
  Croissant,
  Droplet,
  Droplets,
  Package,
  Sprout,
  Sparkles,
  Gift,
  Check,
  Star,
  Flame,
  Layers,
  Square,
  Egg,
  Apple,
  Popcorn,
  Heart,
  Nut,
  CupSoda,
  Wine,
  Cake,
  type LucideIcon,
} from "lucide-react";
import { SafeImage } from "@/components/product-img";
import { Alert, Button, Spinner } from "@/components/ui";
import type { SearchHit } from "@/lib/api/types";
import { formatMinor } from "@/lib/money";
import {
  getDiscountDisplay,
  getMockMrp,
  getProductImage,
  SUBCATEGORY_IMAGES,
} from "@/lib/product-images";

import { useBasketActions } from "./use-basket-actions";

export interface SubCategoryItem {
  id: string;
  name: string;
  icon: LucideIcon;
  imageKey?: string;
  category: string;
}

const SIDEBAR_SUBCATEGORIES: Record<string, SubCategoryItem[]> = {
  staples: [
    { id: "staples_picks", name: "Healthy Picks", icon: Sparkles, imageKey: "healthy_picks", category: "staples" },
    { id: "staples_olive", name: "Olive & Cold Pressed", icon: Droplets, imageKey: "olive_cold", category: "staples" },
    { id: "staples_oil", name: "Oil", icon: Flame, imageKey: "oil", category: "staples" },
    { id: "staples_atta", name: "Atta", icon: Wheat, imageKey: "atta", category: "staples" },
    { id: "staples_millets", name: "Millets & Other Grains", icon: Sprout, imageKey: "millets", category: "staples" },
    { id: "staples_besan", name: "Besan, Sooji & Maida", icon: Package, imageKey: "besan", category: "staples" },
    { id: "staples_healthy_atta", name: "Healthy Atta & Flours", icon: Wheat, imageKey: "healthy_atta", category: "staples" },
    { id: "staples_ghee", name: "Healthy Ghee", icon: Layers, imageKey: "ghee", category: "staples" },
  ],
  oil: [
    { id: "oil_picks", name: "Healthy Picks", icon: Sparkles, imageKey: "healthy_picks", category: "oil" },
    { id: "oil_olive", name: "Olive & Cold Pressed", icon: Droplets, imageKey: "olive_cold", category: "oil" },
    { id: "oil_cooking", name: "Oil", icon: Flame, imageKey: "oil", category: "oil" },
    { id: "oil_atta", name: "Atta", icon: Wheat, imageKey: "atta", category: "staples" },
    { id: "oil_millets", name: "Millets & Other Grains", icon: Sprout, imageKey: "millets", category: "staples" },
    { id: "oil_besan", name: "Besan, Sooji & Maida", icon: Package, imageKey: "besan", category: "staples" },
    { id: "oil_healthy_atta", name: "Healthy Atta & Flours", icon: Wheat, imageKey: "healthy_atta", category: "staples" },
    { id: "oil_ghee", name: "Healthy Ghee", icon: Layers, imageKey: "ghee", category: "staples" },
  ],
  dairy: [
    { id: "dairy_milk", name: "Milk & Curd", icon: Milk, category: "dairy" },
    { id: "dairy_butter", name: "Butter & Cheese", icon: Layers, category: "dairy" },
    { id: "dairy_paneer", name: "Paneer & Tofu", icon: Square, category: "dairy" },
    { id: "dairy_eggs", name: "Eggs & Breakfast", icon: Egg, category: "bakery" },
    { id: "dairy_bread", name: "Breads & Buns", icon: Croissant, category: "bakery" },
  ],
  produce: [
    { id: "produce_fresh", name: "Fresh Vegetables", icon: Carrot, category: "produce" },
    { id: "produce_roots", name: "Potatoes & Onions", icon: Carrot, category: "produce" },
    { id: "produce_herbs", name: "Herbs & Chillies", icon: Sprout, category: "produce" },
    { id: "produce_fruits", name: "Fresh Fruits", icon: Apple, category: "produce" },
  ],
  snacks: [
    { id: "snacks_munchies", name: "Munchies & Chips", icon: Popcorn, category: "snacks" },
    { id: "snacks_biscuits", name: "Biscuits & Cookies", icon: Cookie, category: "snacks" },
    { id: "snacks_sweets", name: "Chocolates & Sweets", icon: Heart, category: "snacks" },
    { id: "snacks_namkeen", name: "Indian Namkeen", icon: Nut, category: "snacks" },
  ],
  beverages: [
    { id: "bev_tea", name: "Tea & Chai", icon: CupSoda, category: "beverages" },
    { id: "bev_coffee", name: "Coffee & Brews", icon: Coffee, category: "beverages" },
    { id: "bev_cold", name: "Cold Drinks & Soda", icon: Wine, category: "beverages" },
    { id: "bev_juices", name: "Fruit Juices", icon: CupSoda, category: "beverages" },
  ],
  bakery: [
    { id: "bakery_bread", name: "Fresh Breads", icon: Croissant, category: "bakery" },
    { id: "bakery_eggs", name: "Farm Fresh Eggs", icon: Egg, category: "bakery" },
    { id: "bakery_cakes", name: "Cakes & Rusk", icon: Cake, category: "bakery" },
  ],
};

const DEFAULT_SIDEBAR: SubCategoryItem[] = [
  { id: "all_staples", name: "Atta, Rice & Oil", icon: Wheat, imageKey: "atta", category: "staples" },
  { id: "all_dairy", name: "Dairy & Eggs", icon: Milk, category: "dairy" },
  { id: "all_produce", name: "Fresh Vegetables", icon: Carrot, category: "produce" },
  { id: "all_snacks", name: "Snacks & Munchies", icon: Cookie, category: "snacks" },
  { id: "all_beverages", name: "Tea & Beverages", icon: Coffee, category: "beverages" },
  { id: "all_bakery", name: "Bakery & Breads", icon: Croissant, category: "bakery" },
];

export function CategoryView({
  category,
  hits,
  isLoading,
  onSelectCategory,
}: {
  category: string;
  hits: SearchHit[];
  isLoading?: boolean;
  onSelectCategory: (cat: string) => void;
}) {
  const router = useRouter();
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

  const sidebarItems = SIDEBAR_SUBCATEGORIES[category] ?? DEFAULT_SIDEBAR;
  const [activeSubId, setActiveSubId] = useState(sidebarItems[2]?.id ?? sidebarItems[0]?.id ?? "");

  const categoryTitles: Record<string, string> = {
    staples: "Cooking Oil",
    oil: "Cooking Oil",
    dairy: "Dairy, Bread & Eggs",
    produce: "Fresh Fruits & Vegetables",
    snacks: "Snacks, Biscuits & Munchies",
    beverages: "Tea, Coffee & Cold Beverages",
    bakery: "Bakery, Eggs & Buns",
    all: "All Grocery Items",
  };

  const title = categoryTitles[category] ?? "Cooking Oil";

  return (
    <div className="space-y-4">
      {/* Breadcrumb Navigation matching Screenshot 2: Home > Grocery > Oil */}
      <nav aria-label="Breadcrumb" className="text-xs font-semibold text-stone-500">
        <ol className="flex items-center gap-1.5">
          <li>
            <button
              type="button"
              onClick={() => onSelectCategory("all")}
              className="hover:text-stone-900 transition cursor-pointer"
            >
              Home
            </button>
          </li>
          <li aria-hidden="true" className="text-stone-400">›</li>
          <li>
            <button
              type="button"
              onClick={() => onSelectCategory("all")}
              className="hover:text-stone-900 transition cursor-pointer"
            >
              Grocery
            </button>
          </li>
          <li aria-hidden="true" className="text-stone-400">›</li>
          <li className="font-bold text-stone-900 capitalize" aria-current="page">
            {category === "staples" ? "Oil" : category}
          </li>
        </ol>
      </nav>

      {/* Screen-reader live feedback */}
      <div role="status" aria-live="polite" className="sr-only">
        {feedback?.message}
      </div>

      {/* Basket error and feedback alerts */}
      {basketError ? (
        <Alert tone="danger" title="Basket update failed" role="alert">
          <p className="mb-2 text-xs">{basketError}</p>
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
          className="rounded-2xl border border-emerald-300 bg-emerald-50 px-4 py-2.5 text-xs font-semibold text-emerald-900 shadow-xs flex items-center justify-between"
        >
          <span className="inline-flex items-center gap-1.5"><Check className="h-3.5 w-3.5 stroke-[2.5]" /><span>{feedback.message}</span></span>
          {lastBasket?.quote ? (
            <span className="tabular-nums">
              Total: <strong>{formatMinor(lastBasket.quote.total_minor, lastBasket.quote.currency)}</strong>
            </span>
          ) : null}
        </div>
      ) : null}

      {/* Split layout: Subcategory sidebar on left + product grid on right */}
      <div className="grid grid-cols-1 md:grid-cols-12 gap-5 sm:gap-6 items-start">
        {/* Left Sidebar Navigation: horizontal pill bar on mobile, vertical sidebar on md+ */}
        <aside className="md:col-span-3 lg:col-span-2 py-1 w-full overflow-hidden">
          <nav aria-label="Subcategories" className="flex md:flex-col overflow-x-auto md:overflow-visible gap-1.5 md:gap-0.5 scrollbar-none pb-2 md:pb-0">
            {sidebarItems.map((item) => {
              const isActive = activeSubId === item.id || category === item.category;
              const subImg = item.imageKey ? SUBCATEGORY_IMAGES[item.imageKey] : undefined;

              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    setActiveSubId(item.id);
                    onSelectCategory(item.category);
                  }}
                  className={`shrink-0 md:shrink md:w-full flex items-center gap-2 px-3 py-2 text-left transition-all group cursor-pointer rounded-xl focus-visible:ring-2 focus-visible:ring-brand-purple ${
                    isActive
                      ? "bg-brand-purple-light text-[#950EDB] border border-brand-purple md:border-transparent md:border-l-[3px] font-black md:rounded-r-xl md:rounded-l-none"
                      : "border border-line md:border-transparent bg-surface hover:bg-surface-raised text-foreground/80 font-medium"
                  }`}
                >
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-transparent overflow-hidden group-hover:scale-105 transition-transform p-0.5">
                    {(() => {
                      const ItemIcon = item.icon;
                      return subImg ? (
                        <SafeImage
                          src={subImg}
                          alt={item.name}
                          fallbackIcon={<ItemIcon className="h-5 w-5 text-stone-500" />}
                          className="w-full h-full object-contain"
                        />
                      ) : (
                        <ItemIcon className="h-5 w-5 text-stone-500" />
                      );
                    })()}
                  </div>
                  <span className="text-xs leading-snug whitespace-nowrap md:whitespace-normal line-clamp-1 md:line-clamp-2">
                    {item.name}
                  </span>
                </button>
              );
            })}
          </nav>
        </aside>

        {/* Main Content Area */}
        <section className="md:col-span-9 lg:col-span-10 space-y-5">
          <header className="space-y-3">
            <h1 className="text-2xl sm:text-3xl font-black text-stone-900 tracking-tight">
              {title}
            </h1>

            {/* Category Promo Banner matching Screenshot 2: Olive Oil Store */}
            <div className="relative rounded-2xl bg-gradient-to-r from-[#fed858] via-[#ffd343] to-[#febf26] p-5 sm:p-6 shadow-xs overflow-hidden flex items-center justify-between min-h-[170px]">
              <div className="space-y-1.5 z-10 max-w-[65%]">
                <span className="text-[10px] font-black uppercase tracking-wider text-[#a73507]">
                  UP TO 60% OFF
                </span>
                <h2 className="text-2xl sm:text-3xl font-black text-[#a73507] tracking-tight leading-none uppercase">
                  {category === "oil" || category === "staples" ? "OLIVE OIL STORE" : `${category.toUpperCase()} STORE`}
                </h2>
                <div className="pt-1">
                  <span className="inline-block bg-[#f38118] text-white text-[11px] font-bold px-3 py-1 rounded-full shadow-xs">
                    {category === "oil" || category === "staples"
                      ? "Get 15% off upto ₹150 on Pomace Oil. Use code: POMACE15"
                      : "Guaranteed authentic quality & lightning 10-minute delivery."}
                  </span>
                </div>
                <div className="pt-2">
                  <span className="inline-flex items-center gap-1 text-xs font-black text-stone-900 bg-white px-6 py-1.5 rounded-full shadow-xs hover:shadow transition cursor-pointer">
                    Explore
                  </span>
                </div>
              </div>

              {/* Real Zepto Oil Bottles Showcase on Green Platform matching Screenshot 2 */}
              <div className="hidden sm:flex items-end justify-center relative shrink-0 pr-4">
                <div className="flex items-end -space-x-4 drop-shadow-md z-10">
                  <div className="w-14 h-24 relative overflow-hidden">
                    <SafeImage
                      src="/subcategories/olive_cold.webp"
                      alt="Oil bottle"
                      fallbackIcon={<Droplet className="w-8 h-8 text-muted/40" />}
                      className="w-full h-full object-contain"
                    />
                  </div>
                  <div className="w-16 h-28 relative overflow-hidden z-20">
                    <SafeImage
                      src="/brand/brand_OIL-MUS-001_1.webp"
                      alt="Mustard oil bottle"
                      fallbackIcon={<Droplet className="w-8 h-8 text-muted/40" />}
                      className="w-full h-full object-contain"
                    />
                  </div>
                  <div className="w-14 h-24 relative overflow-hidden">
                    <SafeImage
                      src="/brand/brand_OIL-SUN-001_1.webp"
                      alt="Sunflower oil bottle"
                      fallbackIcon={<Droplet className="w-8 h-8 text-muted/40" />}
                      className="w-full h-full object-contain"
                    />
                  </div>
                </div>
                {/* Emerald pedestal */}
                <div className="absolute -bottom-2 inset-x-0 h-4 bg-[#1b4d3e] rounded-t-lg shadow-sm" />
              </div>
            </div>
          </header>

          {/* 6-Column Product Grid matching Screenshot 2 */}
          {isLoading ? (
            <div className="py-16 text-center">
              <Spinner label="Loading catalogue products..." />
            </div>
          ) : hits.length === 0 ? (
            <div className="rounded-3xl border border-stone-200 bg-white p-12 text-center space-y-3 shadow-2xs">
              <p className="text-base font-bold text-stone-900">No products available in this subcategory.</p>
              <p className="text-xs text-stone-500">
                Catalogue data reflects live merchant inventory.
              </p>
              <div>
                <Button variant="secondary" onClick={() => onSelectCategory("all")}>
                  View all items
                </Button>
              </div>
            </div>
          ) : (
            <ul
              className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2.5 sm:gap-3"
              role="list"
            >
              {[...hits]
                .sort((a, b) => {
                  const aIsOil = a.sku.startsWith("OIL-") || a.display_name.toLowerCase().includes("oil");
                  const bIsOil = b.sku.startsWith("OIL-") || b.display_name.toLowerCase().includes("oil");
                  if (aIsOil && !bIsOil) return -1;
                  if (!aIsOil && bIsOil) return 1;
                  return 0;
                })
                .map((hit) => {
                  const isUnavailable = !hit.is_available;
                  const inBasketLine = lastBasket?.lines.find((line) => line.sku === hit.sku);
                  const currentQty = inBasketLine?.quantity ?? 0;
                  const isItemBusy = busySku === hit.sku || isMutating;
                  const imageUrl = getProductImage(hit.sku);
                  const mrpMinor = getMockMrp(hit.unit_price_minor);
                  const discount = getDiscountDisplay(hit.unit_price_minor, mrpMinor);

                  const isRiceBran = hit.display_name.toLowerCase().includes("rice bran");
                  const isSunflower = hit.display_name.toLowerCase().includes("sunflower");

                  return (
                    <li
                      key={hit.sku}
                      onClick={(e) => {
                        if ((e.target as HTMLElement).closest('button, input, [role="group"]')) return;
                        router.push(`/products/${encodeURIComponent(hit.sku)}`);
                      }}
                      className={`group flex flex-col justify-between rounded-2xl border p-2.5 transition-all duration-200 shadow-2xs bg-surface cursor-pointer ${
                        isUnavailable
                          ? "border-line bg-surface-raised/70 opacity-75"
                          : "border-line hover:shadow-md hover:border-brand-purple/40"
                      }`}
                    >
                      <div className="space-y-1">
                        {/* Product Image Stage with Pack Label and ADD button matching Screenshot 2 */}
                        <div className="relative aspect-square w-full rounded-xl bg-white flex items-center justify-center p-2 overflow-hidden border border-stone-100/60">
                          <Link
                            href={`/products/${encodeURIComponent(hit.sku)}`}
                            className="block w-full h-full focus:outline-none focus:ring-2 focus:ring-[#950EDB] rounded-lg"
                            aria-label={`View ${hit.display_name}`}
                          >
                            <SafeImage
                              src={imageUrl}
                              alt={hit.display_name}
                              fallbackIcon={<Package className="h-8 w-8 text-muted/40 stroke-1" />}
                              className="w-full h-full object-contain group-hover:scale-105 transition-transform"
                            />
                          </Link>

                          {/* Top corner pack label badge matching Screenshot 2 */}
                          <div className="absolute top-1 right-1 pointer-events-none flex flex-col items-end gap-0.5 z-10">
                            <span className="rounded bg-[#007038] text-white text-[9px] font-black px-1.5 py-0.5 uppercase tracking-wider shadow-2xs">
                              {hit.unit_label}
                            </span>
                          </div>

                          {/* Overlapping ADD button in bottom-right corner matching Screenshot 2 */}
                          <div className="absolute bottom-1.5 right-1.5 z-10">
                            {hit.is_available ? (
                              currentQty > 0 ? (
                                <div
                                  className="h-8 rounded-lg bg-[#ff3269] text-white flex items-center justify-between px-1 shadow-sm font-black text-xs"
                                  role="group"
                                  aria-label={`Quantity controls for ${hit.display_name}`}
                                >
                                  <button
                                    type="button"
                                    disabled={isItemBusy}
                                    onClick={() => void setQuantity(hit.sku, currentQty - 1, hit.display_name)}
                                    className="w-7 h-full flex items-center justify-center text-sm font-black hover:opacity-80 active:scale-90 disabled:opacity-50 cursor-pointer touch-manipulation"
                                    aria-label={`Decrease quantity of ${hit.display_name}`}
                                  >
                                    −
                                  </button>
                                  <span
                                    className="min-w-4 text-center font-black tabular-nums text-[11px]"
                                    aria-live="polite"
                                    aria-label={`${currentQty} units`}
                                  >
                                    {busySku === hit.sku ? "…" : currentQty}
                                  </span>
                                  <button
                                    type="button"
                                    disabled={isItemBusy}
                                    onClick={() => void setQuantity(hit.sku, currentQty + 1, hit.display_name)}
                                    className="w-7 h-full flex items-center justify-center text-sm font-black hover:opacity-80 active:scale-90 disabled:opacity-50 cursor-pointer touch-manipulation"
                                    aria-label={`Increase quantity of ${hit.display_name}`}
                                  >
                                    +
                                  </button>
                                </div>
                              ) : (
                                <button
                                  type="button"
                                  disabled={isItemBusy}
                                  onClick={() => void addOne(hit.sku, hit.display_name)}
                                  className="h-8 px-3.5 rounded-lg border-2 border-[#ff3269] bg-white dark:bg-stone-900 text-[11px] font-black text-[#ff3269] tracking-wider hover:bg-[#ff3269]/10 transition active:scale-95 flex items-center gap-1 shadow-xs cursor-pointer touch-manipulation focus-visible:ring-2 focus-visible:ring-[#ff3269]"
                                  aria-label={`Add ${hit.display_name} to basket`}
                                >
                                  <span>ADD</span>
                                </button>
                              )
                            ) : (
                              <span className="h-6 px-1.5 rounded-md border border-stone-200 bg-stone-100 text-[9px] font-bold text-stone-400 flex items-center">
                                Out
                              </span>
                            )}
                          </div>
                        </div>

                        {/* Authoritative Price with Green Badge + MRP strikethrough + Discount tag matching Screenshot 2 */}
                        <div className="pt-1 flex items-baseline gap-1.5 flex-wrap">
                          <span className="rounded bg-[#168753] px-1.5 py-0.5 text-[11px] font-black text-white tabular-nums">
                            {formatMinor(hit.unit_price_minor, hit.currency)}
                          </span>
                          <span className="text-[10px] text-stone-400 font-medium line-through tabular-nums">
                            {formatMinor(mrpMinor, hit.currency)}
                          </span>
                          {discount && (
                            <span className="text-[10px] font-black text-[#168753]">
                              {discount.text}
                            </span>
                          )}
                        </div>

                        {/* Product Title */}
                        <Link
                          href={`/products/${encodeURIComponent(hit.sku)}`}
                          className="font-bold text-xs text-stone-900 line-clamp-2 leading-tight group-hover:text-[#950EDB] transition-colors block"
                        >
                          {hit.display_name}
                        </Link>

                        {/* Unit / Pack Size */}
                        <p className="text-[11px] font-medium text-stone-500">
                          1 pack ({hit.unit_label})
                        </p>

                        {/* Sub-tag if present */}
                        {isRiceBran && (
                          <div>
                            <span className="inline-block text-[9px] font-semibold text-[#0284c7] bg-[#f0f9ff] border border-sky-100 px-1 py-0.2 rounded">
                              Rice Bran Oil
                            </span>
                          </div>
                        )}
                        {isSunflower && !isRiceBran && (
                          <div>
                            <span className="inline-block text-[9px] font-semibold text-amber-700 bg-amber-50 border border-amber-100 px-1 py-0.2 rounded">
                              Sunflower Oil
                            </span>
                          </div>
                        )}

                        {/* Rating pill matching Screenshot 2 */}
                        <div className="flex items-center gap-1 text-[11px] font-bold text-[#168753]">
                          <span className="inline-flex items-center text-[#168753]"><Star className="h-3 w-3 fill-[#168753] text-[#168753] inline" aria-hidden="true" /><span className="sr-only">★</span></span>
                          <span>4.8</span>
                          <span className="text-stone-400 font-normal">(348.8k)</span>
                        </div>

                        <FreshnessLine freshness={hit.freshness} />
                      </div>
                    </li>
                  );
                })}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

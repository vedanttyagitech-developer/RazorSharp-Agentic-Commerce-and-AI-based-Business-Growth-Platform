"use client";

import {
  Carrot,
  Milk,
  Wheat,
  Croissant,
  Flame,
  UtensilsCrossed,
  Coffee,
  GlassWater,
  Sparkles,
  Cookie,
  Gift,
  Droplet,
  Home,
  HeartPulse,
  Snowflake,
  type LucideIcon,
} from "lucide-react";

import { SafeImage } from "@/components/product-img";
import { CATEGORY_IMAGES } from "@/lib/product-images";

export interface CategoryItem {
  id: string;
  name: string;
  icon: LucideIcon;
  imageUrl?: string;
  badge?: string;
  filterKey: string;
}

export const ZEPTO_CATEGORIES_ROW_1: CategoryItem[] = [
  { id: "produce", name: "Fruits &\nVegetables", icon: Carrot, imageUrl: CATEGORY_IMAGES.produce, filterKey: "produce" },
  { id: "dairy", name: "Dairy, Milk\n& Curd", icon: Milk, imageUrl: CATEGORY_IMAGES.dairy, filterKey: "dairy" },
  { id: "staples", name: "Atta, Rice\n& Dals", icon: Wheat, imageUrl: CATEGORY_IMAGES.staples, filterKey: "staples" },
  { id: "bakery", name: "Breads &\nEggs", icon: Croissant, imageUrl: CATEGORY_IMAGES.meats, filterKey: "bakery" },
  { id: "masalas", name: "Masala, Spices\n& Pickles", icon: Flame, imageUrl: CATEGORY_IMAGES.masalas, filterKey: "condiments" },
  { id: "packaged", name: "Instant Food\n& Noodles", icon: UtensilsCrossed, imageUrl: CATEGORY_IMAGES.packaged, filterKey: "snacks" },
  { id: "tea_coffee", name: "Tea, Coffee\n& Sips", icon: Coffee, imageUrl: CATEGORY_IMAGES.tea_coffee, filterKey: "beverages" },
  { id: "drinks", name: "Cold Drinks\n& Juices", icon: GlassWater, imageUrl: CATEGORY_IMAGES.drinks, filterKey: "beverages" },
];

export const ZEPTO_CATEGORIES_ROW_2: CategoryItem[] = [
  { id: "munchies", name: "Chips &\nNamkeen", icon: Sparkles, imageUrl: CATEGORY_IMAGES.munchies, filterKey: "snacks" },
  { id: "biscuits", name: "Biscuits &\nCookies", icon: Cookie, imageUrl: CATEGORY_IMAGES.biscuits, filterKey: "snacks" },
  { id: "sweets", name: "Sweet Bites\n& Chocolates", icon: Gift, imageUrl: CATEGORY_IMAGES.sweets, filterKey: "snacks" },
  { id: "oil_ghee", name: "Cooking Oils\n& Ghee", icon: Droplet, imageUrl: CATEGORY_IMAGES.breakfast, filterKey: "staples" },
  { id: "household", name: "Cleaning &\nHousehold", icon: Home, imageUrl: CATEGORY_IMAGES.lifestyle, filterKey: "household" },
  { id: "personal_care", name: "Personal Care\n& Hygiene", icon: HeartPulse, imageUrl: CATEGORY_IMAGES.beauty_care, filterKey: "personal_care" },
  { id: "cafe", name: "Zepto Cafe\nSpecials", icon: Coffee, imageUrl: CATEGORY_IMAGES.cafe, filterKey: "beverages" },
  { id: "frozen", name: "Ready to Cook\n& Serve", icon: Snowflake, imageUrl: CATEGORY_IMAGES.frozen, filterKey: "snacks" },
];

export function CategoryGrid({
  activeCategory,
  onSelectCategory,
}: {
  activeCategory: string;
  onSelectCategory: (category: string) => void;
}) {
  const renderRow = (items: CategoryItem[]) => (
    <div className="grid grid-cols-4 sm:grid-cols-6 md:grid-cols-8 gap-2.5 sm:gap-3">
      {items.map((cat) => {
        const isSelected = activeCategory === cat.filterKey;
        const Icon = cat.icon;

        return (
          <button
            key={cat.id}
            type="button"
            onClick={() => onSelectCategory(cat.filterKey)}
            className="group flex flex-col items-center text-center focus:outline-none focus:ring-2 focus:ring-brand-purple rounded-2xl p-1 transition cursor-pointer"
            aria-label={`Browse ${cat.name.replace("\n", " ")}`}
          >
            {/* Square Container matching Zepto design */}
            <div
              className={`relative aspect-square w-full rounded-2xl flex items-center justify-center p-2 transition-all duration-200 group-hover:scale-105 group-hover:shadow-md ${
                isSelected
                  ? "bg-brand-purple-light border-2 border-brand-purple shadow-sm"
                  : "bg-category-card border border-category-card-border hover:border-line hover:bg-surface"
              }`}
            >
              {cat.imageUrl ? (
                <div className="w-full h-full flex items-center justify-center p-1">
                  <SafeImage
                    src={cat.imageUrl}
                    alt={cat.name.replace("\n", " ")}
                    fallbackIcon={<Icon className="w-6 h-6 text-brand-purple" />}
                    className="w-full h-full object-contain"
                  />
                </div>
              ) : (
                <div className="flex items-center justify-center text-brand-purple transition-transform group-hover:scale-110">
                  <Icon className="w-7 h-7 stroke-[1.75]" />
                </div>
              )}

              {cat.badge ? (
                <span className="absolute -bottom-1 left-1/2 -translate-x-1/2 rounded-full bg-[#7a12b8] px-2 py-0.5 text-[8px] font-black uppercase tracking-wider text-white shadow-xs">
                  {cat.badge}
                </span>
              ) : null}
            </div>

            {/* Category Name */}
            <span
              className={`mt-1.5 text-[10px] sm:text-[11px] font-bold leading-tight line-clamp-2 transition-colors whitespace-pre-line ${
                isSelected
                  ? "text-brand-purple font-black"
                  : "text-foreground/90 group-hover:text-foreground"
              }`}
            >
              {cat.name}
            </span>
          </button>
        );
      })}
    </div>
  );

  return (
    <section aria-label="Explore Categories" className="space-y-3">
      {renderRow(ZEPTO_CATEGORIES_ROW_1)}
      {renderRow(ZEPTO_CATEGORIES_ROW_2)}
    </section>
  );
}

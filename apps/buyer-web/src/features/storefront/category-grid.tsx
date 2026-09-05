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
  Gift,
  Droplet,
  Home,
  Baby,
  Pill,
  Sparkle,
  type LucideIcon,
} from "lucide-react";

import { SafeImage } from "@/components/product-img";

export interface CategoryItem {
  id: string;
  name: string;
  icon: LucideIcon;
  imageUrl?: string;
  badge?: string;
  filterKey: string;
}

export const BLINKIT_CATEGORIES_ROW_1: CategoryItem[] = [
  { id: "produce", name: "Vegetables &\nFruits", icon: Carrot, imageUrl: "/categories/blinkit_produce.webp", filterKey: "produce" },
  { id: "dairy", name: "Dairy, Bread\n& Eggs", icon: Milk, imageUrl: "/categories/blinkit_dairy.webp", filterKey: "dairy" },
  { id: "munchies", name: "Munchies &\nSnacks", icon: Sparkles, imageUrl: "/categories/blinkit_snacks.webp", filterKey: "snacks" },
  { id: "beverages", name: "Cold Drinks\n& Juices", icon: GlassWater, imageUrl: "/categories/blinkit_beverages.webp", filterKey: "beverages" },
  { id: "packaged", name: "Instant &\nFrozen Food", icon: UtensilsCrossed, imageUrl: "/categories/blinkit_packaged.webp", filterKey: "snacks" },
  { id: "tea_coffee", name: "Tea, Coffee\n& Health", icon: Coffee, imageUrl: "/categories/blinkit_tea_coffee.webp", filterKey: "beverages" },
  { id: "bakery", name: "Bakery &\nBiscuits", icon: Croissant, imageUrl: "/categories/blinkit_bakery.webp", filterKey: "bakery" },
  { id: "sweets", name: "Sweet Tooth\n& Chocolates", icon: Gift, imageUrl: "/categories/blinkit_sweets.webp", filterKey: "snacks" },
];

export const BLINKIT_CATEGORIES_ROW_2: CategoryItem[] = [
  { id: "staples", name: "Atta, Rice\n& Dal", icon: Wheat, imageUrl: "/categories/blinkit_staples.webp", filterKey: "staples" },
  { id: "masalas", name: "Masala, Oil\n& More", icon: Flame, imageUrl: "/categories/blinkit_masalas.webp", filterKey: "condiments" },
  { id: "sauces", name: "Sauces &\nSpreads", icon: Droplet, imageUrl: "/categories/blinkit_sauces.webp", filterKey: "condiments" },
  { id: "skincare", name: "Derma &\nSkin Care", icon: Sparkle, imageUrl: "/categories/blinkit_personal_care.webp", filterKey: "personal_care", badge: "NEW" },
  { id: "cleaning", name: "Cleaning\nEssentials", icon: Home, imageUrl: "/categories/blinkit_household.webp", filterKey: "household" },
  { id: "pharma", name: "Pharma &\nWellness", icon: Pill, imageUrl: "/categories/blinkit_pharma.webp", filterKey: "personal_care" },
  { id: "baby", name: "Baby Care\nEssentials", icon: Baby, imageUrl: "/categories/blinkit_baby_care.webp", filterKey: "household" },
  { id: "home_office", name: "Home &\nOffice", icon: Home, imageUrl: "/categories/blinkit_home_office.webp", filterKey: "household" },
];

// Backwards compatibility aliases
export const ZEPTO_CATEGORIES_ROW_1 = BLINKIT_CATEGORIES_ROW_1;
export const ZEPTO_CATEGORIES_ROW_2 = BLINKIT_CATEGORIES_ROW_2;

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
            className="group flex flex-col items-center text-center focus:outline-none focus:ring-2 focus:ring-[#0c831f] rounded-3xl p-1.5 transition cursor-pointer"
            aria-label={`Browse ${cat.name.replace("\n", " ")}`}
          >
            {/* Square Container matching Blinkit rounded-3xl design */}
            <div
              className={`relative aspect-square w-full rounded-3xl flex items-center justify-center p-2 transition-all duration-200 group-hover:scale-105 group-hover:shadow-md ${
                isSelected
                  ? "bg-[#f7fff9] border-2 border-[#0c831f] shadow-sm"
                  : "bg-white border border-[#e8e8e8] hover:border-[#0c831f]/40 hover:bg-[#fcfdfc]"
              }`}
            >
              {cat.imageUrl ? (
                <div className="w-full h-full flex items-center justify-center p-1">
                  <SafeImage
                    src={cat.imageUrl}
                    alt={cat.name.replace("\n", " ")}
                    fallbackIcon={<Icon className="w-6 h-6 text-[#0c831f]" />}
                    className="w-full h-full object-contain"
                  />
                </div>
              ) : (
                <div className="flex items-center justify-center text-[#0c831f] transition-transform group-hover:scale-110">
                  <Icon className="w-7 h-7 stroke-[1.75]" />
                </div>
              )}

              {cat.badge ? (
                <span className="absolute -bottom-1 left-1/2 -translate-x-1/2 rounded-full bg-[#0c831f] px-2 py-0.5 text-[8px] font-black uppercase tracking-wider text-white shadow-xs">
                  {cat.badge}
                </span>
              ) : null}
            </div>

            {/* Category Name */}
            <span
              className={`mt-1.5 text-[10px] sm:text-[11px] font-bold leading-tight line-clamp-2 transition-colors whitespace-pre-line ${
                isSelected
                  ? "text-[#0c831f] font-black"
                  : "text-[#1f1f1f] group-hover:text-[#0c831f]"
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
      {renderRow(BLINKIT_CATEGORIES_ROW_1)}
      {renderRow(BLINKIT_CATEGORIES_ROW_2)}
    </section>
  );
}

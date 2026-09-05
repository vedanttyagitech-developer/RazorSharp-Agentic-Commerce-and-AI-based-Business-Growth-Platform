"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

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
  Home,
  HeartPulse,
  Smartphone,
  Clock,
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
  getProductMeta,
  SUBCATEGORY_IMAGES,
} from "@/lib/product-images";

import { useBasketActions } from "./use-basket-actions";

export interface SubCategoryItem {
  id: string;
  name: string;
  icon: LucideIcon;
  imageKey?: string;
  image?: string;
  category: string;
  keywords?: string[];
}

const SIDEBAR_SUBCATEGORIES: Record<string, SubCategoryItem[]> = {
  staples: [
    { id: "staples_picks", name: "Healthy Picks", icon: Sparkles, imageKey: "healthy_picks", image: "/products/GRO-STPL-OIL-001.webp", category: "staples" },
    { id: "staples_olive", name: "Olive & Cold Pressed", icon: Droplets, imageKey: "olive_cold", image: "/products/GRO-STPL-OIL-003.webp", category: "staples", keywords: ["olive", "cold"] },
    { id: "staples_oil", name: "Oil", icon: Flame, imageKey: "oil", image: "/products/GRO-STPL-OIL-002.webp", category: "staples", keywords: ["oil"] },
    { id: "staples_atta", name: "Atta", icon: Wheat, imageKey: "atta", image: "/products/GRO-STPL-ATT-001.webp", category: "staples", keywords: ["atta", "flour"] },
    { id: "staples_millets", name: "Millets & Other Grains", icon: Sprout, imageKey: "millets", image: "/products/GRO-STPL-RIC-001.webp", category: "staples", keywords: ["millet", "grain", "poha", "rice"] },
    { id: "staples_besan", name: "Besan, Sooji & Maida", icon: Package, imageKey: "besan", image: "/products/GRO-STPL-DAL-001.webp", category: "staples", keywords: ["besan", "sooji", "maida", "chana dal", "toor dal", "dal"] },
    { id: "staples_healthy_atta", name: "Healthy Atta & Flours", icon: Wheat, imageKey: "healthy_atta", image: "/products/GRO-STPL-ATT-002.webp", category: "staples", keywords: ["atta", "wheat"] },
    { id: "staples_ghee", name: "Healthy Ghee", icon: Layers, imageKey: "ghee", image: "/products/GRO-DAIRY-005.webp", category: "staples", keywords: ["ghee"] },
  ],
  oil: [
    { id: "oil_picks", name: "Healthy Picks", icon: Sparkles, imageKey: "healthy_picks", image: "/products/GRO-STPL-OIL-001.webp", category: "oil" },
    { id: "oil_olive", name: "Olive & Cold Pressed", icon: Droplets, imageKey: "olive_cold", image: "/products/GRO-STPL-OIL-003.webp", category: "oil", keywords: ["olive", "cold"] },
    { id: "oil_cooking", name: "Oil", icon: Flame, imageKey: "oil", image: "/products/GRO-STPL-OIL-002.webp", category: "oil", keywords: ["oil"] },
    { id: "oil_atta", name: "Atta", icon: Wheat, imageKey: "atta", image: "/products/GRO-STPL-ATT-001.webp", category: "staples", keywords: ["atta"] },
    { id: "oil_millets", name: "Millets & Other Grains", icon: Sprout, imageKey: "millets", image: "/products/GRO-STPL-RIC-001.webp", category: "staples", keywords: ["millet"] },
    { id: "oil_besan", name: "Besan, Sooji & Maida", icon: Package, imageKey: "besan", image: "/products/GRO-STPL-DAL-001.webp", category: "staples", keywords: ["besan"] },
    { id: "oil_healthy_atta", name: "Healthy Atta & Flours", icon: Wheat, imageKey: "healthy_atta", image: "/products/GRO-STPL-ATT-002.webp", category: "staples", keywords: ["atta"] },
    { id: "oil_ghee", name: "Healthy Ghee", icon: Layers, imageKey: "ghee", image: "/products/GRO-DAIRY-005.webp", category: "staples", keywords: ["ghee"] },
  ],
  dairy: [
    { id: "dairy_all", name: "All Dairy", icon: Milk, image: "/products/GRO-DAIRY-001.webp", category: "dairy" },
    { id: "dairy_milk", name: "Milk & Cream", icon: Milk, image: "/products/GRO-DAIRY-001.webp", category: "dairy", keywords: ["milk", "cream", "taaza", "gold", "toned"] },
    { id: "dairy_curd", name: "Curd & Yogurts", icon: Milk, image: "/products/GRO-DAIRY-004.webp", category: "dairy", keywords: ["curd", "dahi", "yogurt", "shrikhand", "buttermilk", "chaas"] },
    { id: "dairy_butter", name: "Butter & Cheese", icon: Layers, image: "/products/GRO-DAIRY-003.webp", category: "dairy", keywords: ["butter", "cheese"] },
    { id: "dairy_paneer", name: "Paneer & Tofu", icon: Square, image: "/products/GRO-DAIRY-006.webp", category: "dairy", keywords: ["paneer", "tofu"] },
    { id: "dairy_ghee", name: "Ghee & Fats", icon: Flame, image: "/products/GRO-DAIRY-005.webp", category: "dairy", keywords: ["ghee"] },
  ],
  produce: [
    { id: "produce_all", name: "All Fresh Produce", icon: Carrot, image: "/products/GRO-VEG-003.webp", category: "produce" },
    { id: "produce_veggies", name: "Fresh Vegetables", icon: Carrot, image: "/products/GRO-VEG-003.webp", category: "produce", keywords: ["tomato", "tamatar", "carrot", "cucumber", "capsicum", "cabbage", "cauliflower", "gourd", "lauki", "karela", "bhindi", "lady finger"] },
    { id: "produce_roots", name: "Potatoes & Onions", icon: Carrot, image: "/products/GRO-VEG-001.webp", category: "produce", keywords: ["onion", "potato", "aloo", "pyaz", "ginger", "garlic"] },
    { id: "produce_herbs", name: "Leafy & Herbs", icon: Sprout, image: "/products/GRO-VEG-005.webp", category: "produce", keywords: ["spinach", "palak", "coriander", "dhaniya", "mint", "pudina", "methi", "chilli", "mirch", "lemon", "nimbu"] },
    { id: "produce_fruits", name: "Fresh Fruits", icon: Apple, image: "/products/GRO-VEG-008.webp", category: "produce", keywords: ["apple", "banana", "orange", "pomegranate", "anar", "coconut", "nariyal"] },
  ],
  snacks: [
    { id: "snacks_all", name: "All Snacks & Munchies", icon: Popcorn, image: "/products/GRO-SNCK-001.webp", category: "snacks" },
    { id: "snacks_chips", name: "Chips & Crisps", icon: Popcorn, image: "/products/GRO-SNCK-001.webp", category: "snacks", keywords: ["chips", "crisps", "lays", "kurkure", "bingo", "wafers"] },
    { id: "snacks_namkeen", name: "Namkeen & Bhujia", icon: Nut, image: "/products/GRO-SNCK-003.webp", category: "snacks", keywords: ["namkeen", "bhujia", "sev", "khatta meetha", "mixture", "haldiram", "bikaji"] },
    { id: "snacks_biscuits", name: "Biscuits & Cookies", icon: Cookie, image: "/products/GRO-SNCK-002.webp", category: "snacks", keywords: ["biscuit", "cookie", "parle", "britannia", "oreo", "sunfeast", "rusk"] },
    { id: "snacks_noodles", name: "Instant Noodles", icon: Package, image: "/products/GRO-SNCK-004.webp", category: "snacks", keywords: ["noodle", "maggi", "yippee", "hakka", "vermicelli"] },
    { id: "snacks_sweets", name: "Chocolates & Sweets", icon: Heart, image: "/products/GRO-SNCK-005.webp", category: "snacks", keywords: ["chocolate", "dairy milk", "kitkat", "sweet"] },
  ],
  beverages: [
    { id: "bev_all", name: "Beverages Gift Packs", icon: Gift, image: "/subcategories/blinkit_bev_gift.webp", category: "beverages" },
    { id: "bev_soft", name: "Soft Drinks", icon: CupSoda, image: "/subcategories/blinkit_soft_drinks.webp", category: "beverages", keywords: ["coca", "cola", "sprite", "thums", "pepsi", "limca", "fizz", "soda"] },
    { id: "bev_fruit", name: "Fruit Juice", icon: Wine, image: "/subcategories/blinkit_fruit_juice.webp", category: "beverages", keywords: ["real", "juice", "fruit power"] },
    { id: "bev_mango", name: "Mango Drinks", icon: Wine, image: "/subcategories/blinkit_mango_drinks.webp", category: "beverages", keywords: ["frooti", "maaza", "aamras", "mango"] },
    { id: "bev_pure", name: "Pure Juices", icon: Wine, image: "/products/GRO-BEVG-016.webp", category: "beverages", keywords: ["mixed fruit", "juice", "paper boat"] },
    { id: "bev_concentrates", name: "Concentrates & Syrups", icon: Wine, image: "/subcategories/blinkit_syrups.webp", category: "beverages", keywords: ["rooh", "sharbat", "syrup"] },
    { id: "bev_energy", name: "Energy Drinks", icon: Flame, image: "/subcategories/blinkit_energy.webp", category: "beverages", keywords: ["red bull", "energy"] },
    { id: "bev_water", name: "Water & Soda", icon: Droplets, image: "/products/GRO-BEVG-005.webp", category: "beverages", keywords: ["water", "bisleri", "kinley", "coconut", "club soda"] },
    { id: "bev_tea", name: "Tea & Chai", icon: Coffee, image: "/products/GRO-BEVG-001.webp", category: "beverages", keywords: ["tea", "chai", "taj", "tata", "wagh", "red label"] },
    { id: "bev_coffee", name: "Coffee & Brews", icon: Coffee, image: "/products/GRO-BEVG-002.webp", category: "beverages", keywords: ["coffee", "nescafe", "bru"] },
    { id: "bev_health", name: "Health Drinks", icon: Package, image: "/products/GRO-BEVG-023.webp", category: "beverages", keywords: ["horlicks", "bournvita"] },
  ],
  bakery: [
    { id: "bakery_all", name: "All Bakery", icon: Croissant, image: "/products/GRO-BAK-001.webp", category: "bakery" },
    { id: "bakery_bread", name: "Fresh Breads & Pav", icon: Croissant, image: "/products/GRO-BAK-001.webp", category: "bakery", keywords: ["bread", "bun", "pav", "loaf", "brown bread", "white bread"] },
    { id: "bakery_eggs", name: "Farm Fresh Eggs", icon: Egg, image: "/products/GRO-BAK-003.webp", category: "bakery", keywords: ["egg", "eggoz", "tray"] },
    { id: "bakery_cakes", name: "Cakes & Rusk", icon: Cake, image: "/products/GRO-BAK-004.webp", category: "bakery", keywords: ["cake", "muffin", "croissant", "rusk"] },
  ],
  condiments: [
    { id: "cond_all", name: "All Masalas & Spices", icon: Flame, image: "/products/GRO-COND-001.webp", category: "condiments" },
    { id: "cond_whole", name: "Whole & Ground Spices", icon: Flame, image: "/products/GRO-COND-001.webp", category: "condiments", keywords: ["turmeric", "haldi", "chilli", "mirch", "coriander", "dhaniya", "jeera", "garam"] },
    { id: "cond_pickles", name: "Pickles & Chutneys", icon: Sprout, image: "/products/GRO-COND-003.webp", category: "condiments", keywords: ["pickle", "achar", "mango pickle", "chutney"] },
    { id: "cond_sauces", name: "Sauces & Pastes", icon: Droplet, image: "/products/GRO-COND-002.webp", category: "condiments", keywords: ["ketchup", "sauce", "kissan", "ginger garlic"] },
  ],
  household: [
    { id: "house_all", name: "All Cleaning & Home", icon: Home, image: "/products/GRO-HOU-001.webp", category: "household" },
    { id: "house_laundry", name: "Laundry Detergents", icon: Droplets, image: "/products/GRO-HOU-001.webp", category: "household", keywords: ["surf", "ariel", "detergent", "rin", "comfort"] },
    { id: "house_dish", name: "Dishwashing", icon: Package, image: "/products/GRO-HOU-003.webp", category: "household", keywords: ["vim", "dishwash", "bar", "gel"] },
    { id: "house_cleaners", name: "Cleaners & Hygiene", icon: Sparkles, image: "/products/GRO-HOU-002.webp", category: "household", keywords: ["harpic", "lizol", "dettol", "colin"] },
    { id: "house_repellents", name: "Repellents & Freshness", icon: Sprout, image: "/products/GRO-HOU-005.webp", category: "household", keywords: ["all out", "good knight", "godrej", "odonil"] },
    { id: "house_essentials", name: "Foil & Tissues", icon: Square, image: "/products/GRO-HOU-006.webp", category: "household", keywords: ["origami", "freshwrap", "foil", "tissue"] },
  ],
  personal_care: [
    { id: "pc_all", name: "All Personal Care", icon: HeartPulse, image: "/products/GRO-PC-001.webp", category: "personal_care" },
    { id: "pc_bath", name: "Soaps & Bodywash", icon: Droplets, image: "/products/GRO-PC-001.webp", category: "personal_care", keywords: ["dettol", "dove", "pears", "lifebuoy", "soap"] },
    { id: "pc_oral", name: "Oral Care & Toothpaste", icon: Sparkles, image: "/products/GRO-PC-002.webp", category: "personal_care", keywords: ["colgate", "sensodyne", "close up", "brush"] },
    { id: "pc_hair", name: "Hair Shampoos & Oils", icon: Droplet, image: "/products/GRO-PC-003.webp", category: "personal_care", keywords: ["head", "shoulders", "clinic plus", "pantene", "parachute", "oil"] },
    { id: "pc_skin", name: "Skincare & Creams", icon: Heart, image: "/products/GRO-PC-004.webp", category: "personal_care", keywords: ["nivea", "vaseline", "ponds", "lotion", "cream"] },
    { id: "pc_grooming", name: "Shaving & Grooming", icon: Package, image: "/products/GRO-PC-005.webp", category: "personal_care", keywords: ["gillette", "razor", "foam", "blade"] },
  ],
  electronics: [
    { id: "elec_all", name: "All Electronics", icon: Smartphone, image: "/products/ELEC-IPHONE-16.webp", category: "electronics" },
    { id: "elec_audio", name: "Earphones & Audio", icon: CupSoda, image: "/products/ELEC-BOAT-131.webp", category: "electronics", keywords: ["boat", "airpods", "earbuds", "audio"] },
    { id: "elec_cables", name: "Chargers & Cables", icon: Package, image: "/products/ELEC-PORT-CABLE.webp", category: "electronics", keywords: ["cable", "charger", "adapter", "usb"] },
    { id: "elec_power", name: "Power Banks", icon: Layers, image: "/products/ELEC-MI-PB-10K.webp", category: "electronics", keywords: ["power", "bank", "mi"] },
    { id: "elec_phones", name: "Smartphones", icon: Smartphone, image: "/products/ELEC-IPHONE-16.webp", category: "electronics", keywords: ["iphone", "apple", "phone"] },
  ],
};

export function CategoryView({
  category,
  hits,
  isLoading,
  initialSubId,
  onSelectCategory,
}: {
  category: string;
  hits: SearchHit[];
  isLoading?: boolean;
  initialSubId?: string;
  onSelectCategory: (category: string) => void;
}) {
  const router = useRouter();
  const { addOne, setQuantity, busySku, isMutating, lastBasket } = useBasketActions();

  const sidebarItems = SIDEBAR_SUBCATEGORIES[category] || [
    { id: "all", name: "All Items", icon: Package, category },
  ];

  const [activeSubId, setActiveSubId] = useState<string>(
    initialSubId || sidebarItems[0]?.id || "all"
  );

  const categoryTitles: Record<string, string> = {
    staples: "Cooking Oil & Staples",
    oil: "Cooking Oil & Staples",
    dairy: "Dairy, Bread & Eggs",
    produce: "Fresh Fruits & Vegetables",
    snacks: "Snacks & Munchies",
    beverages: "Cold Drinks & Juices",
    bakery: "Bakery, Cakes & Dairy",
    condiments: "Masalas, Spices & Pickles",
    household: "Cleaning & Home Essentials",
    personal_care: "Personal Care, Bath & Hygiene",
    electronics: "Electronics, Audio & Accessories",
    all: "All Grocery Items",
  };

  const title = categoryTitles[category] ?? "All Grocery Items";

  // Filter hits based on active subcategory keywords if available
  const activeSubItem = sidebarItems.find((s) => s.id === activeSubId);
  const displayedHits = useMemo(() => {
    let list = hits;
    if (activeSubItem?.keywords && activeSubItem.keywords.length > 0) {
      const kws = activeSubItem.keywords.map((k) => k.toLowerCase());
      const filtered = hits.filter((h) => {
        const text = `${h.display_name} ${h.sku} ${h.name_en} ${h.name_hi}`.toLowerCase();
        return kws.some((kw) => text.includes(kw));
      });
      if (filtered.length > 0) {
        list = filtered;
      }
    }

    // Preserve oil sort prioritization for staples & oil as required by tests
    if (category === "staples" || category === "oil") {
      return [...list].sort((a, b) => {
        const aIsOil = a.sku.startsWith("OIL-") || a.display_name.toLowerCase().includes("oil");
        const bIsOil = b.sku.startsWith("OIL-") || b.display_name.toLowerCase().includes("oil");
        if (aIsOil && !bIsOil) return -1;
        if (!aIsOil && bIsOil) return 1;
        return 0;
      });
    }

    return list;
  }, [hits, activeSubItem, category]);

  return (
    <div className="space-y-4">
      {/* Breadcrumb Navigation: Home > Grocery > Oil / Cold Drinks & Juices */}
      <nav aria-label="Breadcrumb" className="text-xs font-semibold text-stone-500">
        <ol className="flex items-center gap-1.5 flex-wrap">
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
          <li className="text-stone-900 font-bold">
            {category === "staples" || category === "oil" ? "Oil" : title}
          </li>
          {activeSubItem && activeSubItem.id !== "all" && activeSubItem.id !== "bev_all" && activeSubItem.id !== "staples_picks" && activeSubItem.id !== "oil_picks" && (
            <>
              <li aria-hidden="true" className="text-stone-400">›</li>
              <li className="text-stone-700 font-semibold">{activeSubItem.name}</li>
            </>
          )}
        </ol>
      </nav>

      {/* Blinkit Split Layout: Compact Subcategory Sidebar (Left) + Product Grid (Right) */}
      <div className="flex flex-col md:flex-row gap-0 rounded-3xl border border-[#e8e8e8] bg-white overflow-hidden shadow-2xs">
        {/* Left Subcategory Sidebar (Blinkit Vertical Pill Layout) */}
        <aside className="w-full md:w-[92px] lg:w-[100px] shrink-0 border-b md:border-b-0 md:border-r border-[#e8e8e8] py-2 bg-white flex md:flex-col overflow-x-auto md:overflow-y-auto scrollbar-none items-center gap-1">
          <nav aria-label="Subcategories" className="flex md:flex-col items-center w-full gap-1">
            {sidebarItems.map((item) => {
              const isActive = activeSubId === item.id;
              const subImg = item.image ?? (item.imageKey ? SUBCATEGORY_IMAGES[item.imageKey] : undefined);
              const ItemIcon = item.icon;

              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    setActiveSubId(item.id);
                    if (item.category !== category && !item.keywords) {
                      onSelectCategory(item.category);
                    }
                  }}
                  className={`w-auto md:w-full shrink-0 flex flex-col items-center py-2 px-2 md:px-1 relative transition-colors group cursor-pointer ${
                    isActive
                      ? "border-b-2 md:border-b-0 md:border-r-[3px] border-[#0c831f] bg-[#f8fff9]"
                      : "hover:bg-[#fafafa]"
                  }`}
                >
                  <div className="w-12 h-12 sm:w-13 sm:h-13 rounded-2xl bg-[#f8f8f8] flex items-center justify-center p-1.5 overflow-hidden border border-stone-100 group-hover:scale-105 transition-transform shadow-2xs">
                    {subImg ? (
                      <SafeImage
                        src={subImg}
                        alt={item.name}
                        fallbackIcon={<ItemIcon className="h-5 w-5 text-stone-500" />}
                        className="w-full h-full object-contain"
                      />
                    ) : (
                      <ItemIcon className="h-5 w-5 text-stone-500" />
                    )}
                  </div>
                  <span
                    className={`text-[10px] sm:text-[11px] text-center leading-tight line-clamp-2 mt-1.5 px-0.5 ${
                      isActive ? "font-bold text-[#1f1f1f]" : "font-medium text-[#666]"
                    }`}
                  >
                    {item.name}
                  </span>
                </button>
              );
            })}
          </nav>
        </aside>

        {/* Main Products Section (Right) */}
        <section className="flex-1 p-3 sm:p-4 md:p-5 space-y-4 bg-white min-w-0">
          <header className="space-y-3">
            <div className="flex items-center justify-between border-b border-[#e8e8e8] pb-3">
              <div>
                <h1 className="text-lg sm:text-xl font-black text-[#1f1f1f] tracking-tight">
                  {title}
                </h1>
                <p className="text-[11px] text-stone-500">
                  {displayedHits.length} {displayedHits.length === 1 ? "product" : "products"} available
                </p>
              </div>

              {/* Delivery ETA Pill */}
              <div className="inline-flex items-center gap-1.5 rounded-full bg-[#f4f9f4] border border-[#0c831f]/20 px-3 py-1 text-xs font-bold text-[#0c831f]">
                <Clock className="h-3.5 w-3.5" />
                <span>Delivery in 8 minutes</span>
              </div>
            </div>

            {/* Category Promo Store Banner (Preserved for staples & oil as required by tests) */}
            {(category === "oil" || category === "staples") && (
              <div className="relative rounded-2xl bg-gradient-to-r from-[#fed858] via-[#ffd343] to-[#febf26] p-5 sm:p-6 shadow-xs overflow-hidden flex items-center justify-between min-h-[160px]">
                <div className="space-y-1.5 z-10 max-w-[65%]">
                  <span className="text-[10px] font-black uppercase tracking-wider text-[#a73507]">
                    UP TO 60% OFF
                  </span>
                  <h2 className="text-2xl sm:text-3xl font-black text-[#a73507] tracking-tight leading-none uppercase">
                    OLIVE OIL STORE
                  </h2>
                  <div className="pt-1">
                    <span className="inline-block bg-[#f38118] text-white text-[11px] font-bold px-3 py-1 rounded-full shadow-xs">
                      Get 15% off upto ₹150 on Pomace Oil. Use code: POMACE15
                    </span>
                  </div>
                  <div className="pt-2">
                    <span className="inline-flex items-center gap-1 text-xs font-black text-stone-900 bg-white px-6 py-1.5 rounded-full shadow-xs hover:shadow transition cursor-pointer">
                      Explore
                    </span>
                  </div>
                </div>

                {/* Oil bottles showcase */}
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
                </div>
              </div>
            )}
          </header>

          {/* Product Grid (Blinkit 6-Column Layout with Downloaded Images) */}
          {isLoading ? (
            <div className="py-16 text-center">
              <Spinner label="Loading catalogue products..." />
            </div>
          ) : displayedHits.length === 0 ? (
            <div className="rounded-3xl border border-stone-200 bg-white p-12 text-center space-y-3 shadow-2xs">
              <p className="text-base font-bold text-stone-900">No products available in this subcategory.</p>
              <p className="text-xs text-stone-500">
                Catalogue data reflects live merchant inventory.
              </p>
              <div>
                <Button variant="secondary" onClick={() => setActiveSubId(sidebarItems[0]?.id ?? "")}>
                  View all {title}
                </Button>
              </div>
            </div>
          ) : (
            <ul
              className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-2.5 sm:gap-3"
              role="list"
            >
              {displayedHits.map((hit) => {
                const isUnavailable = !hit.is_available;
                const inBasketLine = lastBasket?.lines.find((line) => line.sku === hit.sku);
                const currentQty = inBasketLine?.quantity ?? 0;
                const isItemBusy = busySku === hit.sku || isMutating;
                const imageUrl = getProductImage(hit.sku);
                const mrpMinor = getMockMrp(hit.unit_price_minor);
                const percentOff = mrpMinor > hit.unit_price_minor ? Math.round(((mrpMinor - hit.unit_price_minor) / mrpMinor) * 100) : 0;
                const productMeta = getProductMeta(hit.sku, hit.category, hit.display_name, hit.unit_label);

                return (
                  <li
                    key={hit.sku}
                    onClick={(e) => {
                      if ((e.target as HTMLElement).closest('button, input, [role="group"]')) return;
                      router.push(`/products/${encodeURIComponent(hit.sku)}`);
                    }}
                    className={`group flex flex-col justify-between rounded-3xl border bg-white p-3 sm:p-3.5 transition-all duration-150 relative overflow-hidden cursor-pointer shadow-xs hover:shadow-md ${
                      isUnavailable
                        ? "border-stone-200 bg-stone-50/70 opacity-75"
                        : "border-[#e8e8e8] hover:shadow-md hover:border-[#0c831f]/40"
                    }`}
                  >
                    <div>
                      {/* Top Badges: Discount badge in top-left matching Blinkit */}
                      <div className="flex items-start justify-between relative z-10 -mt-1.5 -mx-1.5 mb-1">
                        {percentOff > 0 ? (
                          <span className="bg-[#256fef] text-white text-[9px] font-black px-1.5 py-0.5 rounded-br-xl shadow-2xs tracking-wide">
                            {percentOff}% OFF
                          </span>
                        ) : (
                          <span />
                        )}
                      </div>

                      {/* Product Image Stage (using authentic downloaded images) */}
                      <div className="relative aspect-square w-full rounded-2xl bg-[#f8f8f8] flex items-center justify-center p-2 mb-1.5 overflow-hidden border border-stone-100/80">
                        <Link
                          href={`/products/${encodeURIComponent(hit.sku)}`}
                          className="block w-full h-full focus:outline-none focus:ring-2 focus:ring-[#0c831f] rounded-lg"
                          aria-label={`View ${hit.display_name}`}
                        >
                          <SafeImage
                            src={imageUrl}
                            alt={hit.display_name}
                            fallbackIcon={<Package className="h-8 w-8 text-muted/40 stroke-1" />}
                            className="w-full h-full object-contain group-hover:scale-105 transition-transform"
                          />
                        </Link>
                      </div>

                      {/* Delivery Time Badge: 8 MINS with clock icon */}
                      <div className="inline-flex items-center gap-1 text-[9px] font-extrabold text-[#333] bg-[#f5f5f5] px-2 py-0.5 rounded-full w-fit">
                        <Clock className="h-2.5 w-2.5 text-[#333]" />
                        <span>8 MINS</span>
                      </div>

                      {/* Product Title (2-line clamped) */}
                      <h3 className="text-xs font-bold text-[#1f1f1f] leading-snug line-clamp-2 min-h-[32px] mt-1">
                        {hit.display_name}
                      </h3>

                      {/* Unit / Weight Label & SKU Badge */}
                      <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                        <span className="text-[11px] text-[#666] font-medium">
                          {hit.unit_label}
                        </span>
                        <span className="font-mono text-[9px] font-bold text-[#0c831f] bg-[#eefaf0] px-2 py-0.5 rounded-full border border-[#0c831f]/20">
                          SKU: {hit.sku}
                        </span>
                      </div>

                      {/* Authentic Product Description Snippet */}
                      {productMeta?.description && (
                        <p className="text-[10px] text-stone-500 leading-tight line-clamp-2 mt-1 min-h-[22px]">
                          {productMeta.description}
                        </p>
                      )}
                    </div>

                    {/* Price & Blinkit Signature ADD Button Row */}
                    <div className="flex items-center justify-between mt-2 pt-2 border-t border-stone-100">
                      <div className="flex items-baseline gap-1">
                        <span className="text-xs sm:text-sm font-extrabold text-[#1f1f1f]">
                          {formatMinor(hit.unit_price_minor, hit.currency)}
                        </span>
                        {mrpMinor > hit.unit_price_minor && (
                          <span className="text-[10px] text-[#888] line-through">
                            {formatMinor(mrpMinor, hit.currency)}
                          </span>
                        )}
                      </div>

                      {hit.is_available ? (
                        currentQty > 0 ? (
                          <div
                            className="h-8.5 rounded-xl bg-[#0c831f] text-white flex items-center justify-between px-2 shadow-xs font-bold text-xs min-w-[70px]"
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
                            <span className="min-w-4 text-center font-bold tabular-nums text-xs">
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
                          <button
                            type="button"
                            disabled={isItemBusy}
                            onClick={() => void addOne(hit.sku, hit.display_name)}
                            className="min-w-[62px] min-h-[32px] border border-[#0c831f] text-[#0c831f] bg-[#f7fff9] hover:bg-[#0c831f] hover:text-white rounded-xl px-3.5 py-1.5 font-extrabold text-xs tracking-wider transition-colors active:scale-95 cursor-pointer disabled:opacity-50 shadow-2xs"
                            aria-label={`Add ${hit.display_name} to basket`}
                          >
                            ADD
                          </button>
                        )
                      ) : (
                        <span className="inline-flex h-7 items-center rounded-lg border border-stone-200 bg-stone-100 px-2 text-[10px] font-bold uppercase text-stone-400">
                          Sold Out
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      {/* Freshness Proof Line */}
      {displayedHits.length > 0 && displayedHits[0].freshness ? (
        <div className="pt-2 flex justify-end">
          <FreshnessLine freshness={displayedHits[0].freshness} />
        </div>
      ) : null}
    </div>
  );
}

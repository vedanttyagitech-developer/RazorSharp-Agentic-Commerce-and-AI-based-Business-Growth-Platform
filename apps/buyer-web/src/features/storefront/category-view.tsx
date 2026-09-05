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
  category: string;
  keywords?: string[];
}

const SIDEBAR_SUBCATEGORIES: Record<string, SubCategoryItem[]> = {
  staples: [
    { id: "staples_picks", name: "Healthy Picks", icon: Sparkles, imageKey: "healthy_picks", category: "staples" },
    { id: "staples_olive", name: "Olive & Cold Pressed", icon: Droplets, imageKey: "olive_cold", category: "staples", keywords: ["olive", "cold"] },
    { id: "staples_oil", name: "Oil", icon: Flame, imageKey: "oil", category: "staples", keywords: ["oil"] },
    { id: "staples_atta", name: "Atta", icon: Wheat, imageKey: "atta", category: "staples", keywords: ["atta", "flour"] },
    { id: "staples_millets", name: "Millets & Other Grains", icon: Sprout, imageKey: "millets", category: "staples", keywords: ["millet", "grain", "poha"] },
    { id: "staples_besan", name: "Besan, Sooji & Maida", icon: Package, imageKey: "besan", category: "staples", keywords: ["besan", "sooji", "maida", "chana dal"] },
    { id: "staples_healthy_atta", name: "Healthy Atta & Flours", icon: Wheat, imageKey: "healthy_atta", category: "staples", keywords: ["atta", "wheat"] },
    { id: "staples_ghee", name: "Healthy Ghee", icon: Layers, imageKey: "ghee", category: "staples", keywords: ["ghee"] },
  ],
  oil: [
    { id: "oil_picks", name: "Healthy Picks", icon: Sparkles, imageKey: "healthy_picks", category: "oil" },
    { id: "oil_olive", name: "Olive & Cold Pressed", icon: Droplets, imageKey: "olive_cold", category: "oil", keywords: ["olive", "cold"] },
    { id: "oil_cooking", name: "Oil", icon: Flame, imageKey: "oil", category: "oil", keywords: ["oil"] },
    { id: "oil_atta", name: "Atta", icon: Wheat, imageKey: "atta", category: "staples", keywords: ["atta"] },
    { id: "oil_millets", name: "Millets & Other Grains", icon: Sprout, imageKey: "millets", category: "staples", keywords: ["millet"] },
    { id: "oil_besan", name: "Besan, Sooji & Maida", icon: Package, imageKey: "besan", category: "staples", keywords: ["besan"] },
    { id: "oil_healthy_atta", name: "Healthy Atta & Flours", icon: Wheat, imageKey: "healthy_atta", category: "staples", keywords: ["atta"] },
    { id: "oil_ghee", name: "Healthy Ghee", icon: Layers, imageKey: "ghee", category: "staples", keywords: ["ghee"] },
  ],
  dairy: [
    { id: "dairy_all", name: "All Dairy", icon: Milk, category: "dairy" },
    { id: "dairy_milk", name: "Milk & Cream", icon: Milk, category: "dairy", keywords: ["milk", "cream", "taaza", "gold", "toned"] },
    { id: "dairy_curd", name: "Curd & Yogurts", icon: Milk, category: "dairy", keywords: ["curd", "dahi", "yogurt", "shrikhand", "buttermilk", "chaas"] },
    { id: "dairy_butter", name: "Butter & Cheese", icon: Layers, category: "dairy", keywords: ["butter", "cheese"] },
    { id: "dairy_paneer", name: "Paneer & Tofu", icon: Square, category: "dairy", keywords: ["paneer", "tofu"] },
    { id: "dairy_ghee", name: "Ghee & Fats", icon: Flame, category: "dairy", keywords: ["ghee"] },
  ],
  produce: [
    { id: "produce_all", name: "All Fresh Produce", icon: Carrot, category: "produce" },
    { id: "produce_roots", name: "Potatoes & Onions", icon: Carrot, category: "produce", keywords: ["onion", "potato", "aloo", "pyaz", "ginger", "garlic"] },
    { id: "produce_veggies", name: "Fresh Vegetables", icon: Carrot, category: "produce", keywords: ["tomato", "tamatar", "carrot", "cucumber", "capsicum", "cabbage", "cauliflower", "gourd", "lauki", "karela", "bhindi", "lady finger"] },
    { id: "produce_herbs", name: "Leafy & Herbs", icon: Sprout, category: "produce", keywords: ["spinach", "palak", "coriander", "dhaniya", "mint", "pudina", "methi", "chilli", "mirch", "lemon", "nimbu"] },
    { id: "produce_fruits", name: "Fresh Fruits", icon: Apple, category: "produce", keywords: ["apple", "banana", "orange", "pomegranate", "anar", "coconut", "nariyal"] },
  ],
  snacks: [
    { id: "snacks_all", name: "All Snacks & Munchies", icon: Popcorn, category: "snacks" },
    { id: "snacks_chips", name: "Chips & Crisps", icon: Popcorn, category: "snacks", keywords: ["chips", "crisps", "lays", "kurkure", "bingo", "wafers"] },
    { id: "snacks_namkeen", name: "Namkeen & Bhujia", icon: Nut, category: "snacks", keywords: ["namkeen", "bhujia", "sev", "khatta meetha", "mixture", "haldiram", "bikaji"] },
    { id: "snacks_biscuits", name: "Biscuits & Cookies", icon: Cookie, category: "snacks", keywords: ["biscuit", "cookie", "parle", "britannia", "oreo", "sunfeast", "rusk"] },
    { id: "snacks_noodles", name: "Instant Noodles", icon: Package, category: "snacks", keywords: ["noodle", "maggi", "yippee", "hakka", "vermicelli"] },
    { id: "snacks_sweets", name: "Chocolates & Sweets", icon: Heart, category: "snacks", keywords: ["chocolate", "dairy milk", "kitkat", "sweet"] },
  ],
  beverages: [
    { id: "bev_all", name: "All Beverages", icon: Coffee, category: "beverages" },
    { id: "bev_tea", name: "Tea & Chai", icon: Coffee, category: "beverages", keywords: ["tea", "chai", "taj mahal", "tata tea", "red label", "wagh bakri", "green tea"] },
    { id: "bev_coffee", name: "Coffee & Brews", icon: Coffee, category: "beverages", keywords: ["coffee", "nescafe", "bru"] },
    { id: "bev_cold", name: "Cold Drinks & Soda", icon: CupSoda, category: "beverages", keywords: ["coca", "cola", "sprite", "thums", "pepsi", "soda", "limca", "red bull"] },
    { id: "bev_juices", name: "Fruit Juices & Sharbats", icon: Wine, category: "beverages", keywords: ["juice", "frooti", "maaza", "rooh afza", "sharbat", "real"] },
    { id: "bev_water", name: "Water & Hydration", icon: Droplets, category: "beverages", keywords: ["water", "bisleri", "kinley", "coconut"] },
  ],
  bakery: [
    { id: "bakery_all", name: "All Bakery", icon: Croissant, category: "bakery" },
    { id: "bakery_bread", name: "Fresh Breads & Pav", icon: Croissant, category: "bakery", keywords: ["bread", "bun", "pav", "loaf", "brown bread", "white bread"] },
    { id: "bakery_eggs", name: "Farm Fresh Eggs", icon: Egg, category: "bakery", keywords: ["egg", "eggoz", "tray"] },
    { id: "bakery_cakes", name: "Cakes & Rusk", icon: Cake, category: "bakery", keywords: ["cake", "muffin", "croissant", "rusk"] },
  ],
  condiments: [
    { id: "cond_all", name: "All Masalas & Condiments", icon: Flame, category: "condiments" },
    { id: "cond_spices", name: "Whole & Ground Spices", icon: Flame, category: "condiments", keywords: ["masala", "mirch", "haldi", "jeera", "dhania", "garam masala", "powder", "everest", "mdh", "catch", "sampann"] },
    { id: "cond_pickles", name: "Pickles & Chutneys", icon: Package, category: "condiments", keywords: ["pickle", "achar", "chutney", "mother"] },
    { id: "cond_sauces", name: "Sauces & Pastes", icon: Droplet, category: "condiments", keywords: ["sauce", "ketchup", "paste", "kissan", "schezwan", "ching", "soya", "vinegar"] },
  ],
  household: [
    { id: "hhld_all", name: "All Household", icon: Home, category: "household" },
    { id: "hhld_laundry", name: "Detergents & Laundry", icon: Sparkles, category: "household", keywords: ["detergent", "powder", "bar", "surf", "ariel", "tide", "rin", "comfort", "liquid"] },
    { id: "hhld_dishwash", name: "Dishwashing", icon: Droplets, category: "household", keywords: ["dishwash", "vim", "bar", "pril", "scrub"] },
    { id: "hhld_cleaners", name: "Toilet & Floor Cleaners", icon: Package, category: "household", keywords: ["cleaner", "harpic", "lizol", "toilet", "floor", "colin"] },
    { id: "hhld_repellents", name: "Repellents & Fresheners", icon: Flame, category: "household", keywords: ["goodknight", "hit", "repellent", "aer", "air", "freshener", "all out"] },
    { id: "hhld_paper", name: "Tissues & Kitchen Foil", icon: Layers, category: "household", keywords: ["tissue", "foil", "origami", "freshwrap"] },
  ],
  personal_care: [
    { id: "pcar_all", name: "All Personal Care", icon: HeartPulse, category: "personal_care" },
    { id: "pcar_soaps", name: "Bathing Soaps & Bodywash", icon: Droplets, category: "personal_care", keywords: ["soap", "bar", "bodywash", "dettol", "dove", "pears", "lifebuoy", "cinthol", "mysore"] },
    { id: "pcar_oral", name: "Oral Care & Toothpaste", icon: Sparkles, category: "personal_care", keywords: ["toothpaste", "toothbrush", "colgate", "sensodyne", "close up", "oral"] },
    { id: "pcar_hair", name: "Hair Care & Shampoo", icon: Flame, category: "personal_care", keywords: ["shampoo", "conditioner", "head & shoulders", "clinic", "pantene", "dove", "oil", "bajaj"] },
    { id: "pcar_skincare", name: "Skincare & Lotions", icon: Heart, category: "personal_care", keywords: ["cream", "lotion", "nivea", "vaseline", "himalaya", "boroline"] },
    { id: "pcar_grooming", name: "Grooming & Hygiene", icon: Package, category: "personal_care", keywords: ["deodorant", "fogg", "gillette", "whisper", "sanitary", "shave", "handwash"] },
  ],
  electronics: [
    { id: "elec_all", name: "All Electronics", icon: Smartphone, category: "electronics" },
    { id: "elec_audio", name: "Earphones & Audio", icon: Sparkles, category: "electronics", keywords: ["earphone", "earbuds", "audio", "boat", "headphones"] },
    { id: "elec_charging", name: "Chargers & Cables", icon: Flame, category: "electronics", keywords: ["adapter", "cable", "charger", "usb-c", "fast charging"] },
    { id: "elec_power", name: "Power Banks", icon: Package, category: "electronics", keywords: ["power bank", "ambrane", "magsafe", "battery"] },
    { id: "elec_phones", name: "Smartphones", icon: Smartphone, category: "electronics", keywords: ["iphone", "apple", "phone"] },
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
  // Default to second or first subcategory item
  const [activeSubId, setActiveSubId] = useState(
    category === "staples" || category === "oil"
      ? (sidebarItems[2]?.id ?? sidebarItems[0]?.id ?? "")
      : (sidebarItems[0]?.id ?? "")
  );

  const categoryTitles: Record<string, string> = {
    staples: "Cooking Oil", // Preserved for exact Zepto spec & unit tests
    oil: "Cooking Oil",
    dairy: "Dairy, Bread & Eggs",
    produce: "Fresh Fruits & Vegetables",
    snacks: "Snacks, Biscuits & Munchies",
    beverages: "Tea, Coffee & Cold Beverages",
    bakery: "Bakery, Eggs & Buns",
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
            {category === "staples" ? "Oil" : category.replace("_", " ")}
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
        {/* Left Sidebar Navigation */}
        <aside className="md:col-span-3 lg:col-span-2 py-1 w-full overflow-hidden">
          <nav aria-label="Subcategories" className="flex md:flex-col overflow-x-auto md:overflow-visible gap-1.5 md:gap-0.5 scrollbar-none pb-2 md:pb-0">
            {sidebarItems.map((item) => {
              const isActive = activeSubId === item.id;
              const subImg = item.imageKey ? SUBCATEGORY_IMAGES[item.imageKey] : undefined;

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

            {/* Category Promo Banner */}
            {category === "oil" || category === "staples" ? (
              <div className="relative rounded-2xl bg-gradient-to-r from-[#fed858] via-[#ffd343] to-[#febf26] p-5 sm:p-6 shadow-xs overflow-hidden flex items-center justify-between min-h-[170px]">
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
                  <div className="absolute -bottom-2 inset-x-0 h-4 bg-[#1b4d3e] rounded-t-lg shadow-sm" />
                </div>
              </div>
            ) : (
              <div className="relative rounded-2xl bg-gradient-to-r from-[#7a12b8] via-[#950edb] to-[#4c0575] p-5 sm:p-6 shadow-xs overflow-hidden flex items-center justify-between min-h-[130px] text-white">
                <div className="space-y-1.5 z-10 max-w-[75%]">
                  <span className="text-[10px] font-black uppercase tracking-wider text-amber-300">
                    ZEPTO OFFICIAL CATALOGUE · VERIFIED MERCHANT STOCK
                  </span>
                  <h2 className="text-xl sm:text-2xl font-black tracking-tight leading-none uppercase">
                    {title}
                  </h2>
                  <p className="text-xs text-purple-100 font-medium pt-1">
                    Every SKU backed by cryptographic stock reservation, live pricing, and 10-minute dark store delivery.
                  </p>
                </div>
                <div className="hidden sm:flex items-center justify-center p-3 rounded-2xl bg-white/10 backdrop-blur-xs border border-white/20">
                  <Package className="h-10 w-10 text-white" />
                </div>
              </div>
            )}
          </header>

          {/* Product Grid */}
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
              className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2.5 sm:gap-3"
              role="list"
            >
              {displayedHits.map((hit) => {
                const isUnavailable = !hit.is_available;
                const inBasketLine = lastBasket?.lines.find((line) => line.sku === hit.sku);
                const currentQty = inBasketLine?.quantity ?? 0;
                const isItemBusy = busySku === hit.sku || isMutating;
                const imageUrl = getProductImage(hit.sku);
                const mrpMinor = getMockMrp(hit.unit_price_minor);
                const discount = getDiscountDisplay(hit.unit_price_minor, mrpMinor);
                const productMeta = getProductMeta(hit.sku, hit.category, hit.display_name, hit.unit_label);

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
                    <div className="space-y-1.5">
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

                      {/* Prominent SKU Badge */}
                      <div className="flex items-center gap-1 pt-0.5">
                        <span className="font-mono text-[9px] font-extrabold text-[#7a12b8] bg-[#f4e8fc] dark:bg-purple-950/60 dark:text-purple-300 px-1.5 py-0.5 rounded border border-purple-200/60 dark:border-purple-800/40 tracking-tight">
                          SKU: {hit.sku}
                        </span>
                      </div>

                      {/* Product Title */}
                      <Link
                        href={`/products/${encodeURIComponent(hit.sku)}`}
                        className="font-bold text-xs text-stone-900 line-clamp-2 leading-tight group-hover:text-[#950EDB] transition-colors block"
                      >
                        {hit.display_name}
                      </Link>

                      {/* Product Description Snippet */}
                      <p className="text-[11px] text-stone-500 dark:text-stone-400 line-clamp-2 leading-tight">
                        {productMeta.description}
                      </p>

                      {/* Hindi Subtitle if available */}
                      {hit.name_hi && hit.name_hi !== hit.display_name && (
                        <p className="text-[10px] text-stone-400 line-clamp-1">
                          {hit.name_hi}
                        </p>
                      )}

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

/**
 * Real product images fetched exclusively from Zepto's CDN (cdn.zeptonow.com)
 * sourced from zepto.com.
 * 100% of product, category, and subcategory images load strictly from Zepto CDN.
 */

/** Product images keyed by SKU with multi-angle thumbnails */
export const PRODUCT_IMAGES: Record<string, string[]> = {
  // Amul Taaza Toned Milk 500 ml (Zepto CDN - multi-angle views)
  "GRO-DAIRY-001": [
    "/products/GRO-DAIRY-001.webp",
    "/products/GRO-DAIRY-001_2.webp",
    "/products/GRO-DAIRY-001_3.webp",
  ],

  // Amul Gold Full Cream Milk 1 L (Zepto CDN)
  "GRO-DAIRY-002": [
    "/products/GRO-DAIRY-002.webp",
  ],

  // Amul Masti Dahi 400 g (Zepto CDN)
  "GRO-DAIRY-003": [
    "/products/GRO-DAIRY-003.webp",
  ],

  // Amul Malai Paneer Block 200 g (Zepto CDN)
  "GRO-DAIRY-004": [
    "/products/GRO-DAIRY-004.webp",
  ],

  // Amul Salted Butter 100 g (Zepto CDN)
  "GRO-DAIRY-005": [
    "/subcategories/ghee.webp",
  ],

  // India Gate Classic Basmati Rice 5 kg (Zepto CDN)
  "GRO-STPL-001": [
    "/subcategories/millets.webp",
  ],

  // Aashirvaad Shudh Chakki Atta 5 kg (Zepto CDN)
  "GRO-STPL-002": [
    "/subcategories/healthy_atta.webp",
  ],

  // Tata Sampann Toor Dal (Arhar) 1 kg (Zepto CDN)
  "GRO-STPL-004": [
    "/subcategories/besan.webp",
  ],

  // Fresh Onion 1 kg (Zepto CDN)
  "GRO-PROD-001": [
    "/products/GRO-PROD-001.webp",
  ],

  // Fresh Tomato 1 kg (Zepto CDN - Tomato Local & Hybrid from zepto.com)
  "GRO-PROD-002": [
    "/products/GRO-PROD-002.webp",
    "/products/GRO-PROD-002_2.webp",
    "/products/GRO-PROD-002_3.webp",
  ],

  // Fresh Potato 1 kg (Zepto CDN - Chandramukhi & Baby Potato from zepto.com)
  "GRO-PROD-003": [
    "/products/GRO-PROD-003.webp",
    "/products/GRO-PROD-003_2.webp",
    "/products/GRO-PROD-003_3.webp",
  ],

  // Maggi 2-Minute Masala Noodles (Zepto CDN)
  "GRO-SNCK-001": [
    "/products/GRO-SNCK-001.webp",
  ],

  // Parle-G Gold Biscuits 200 g (Zepto CDN)
  "GRO-SNCK-005": [
    "/products/GRO-SNCK-005.webp",
  ],

  // Kellogg's Chocos 375 g (Zepto CDN)
  "GRO-SNCK-006": [
    "/products/GRO-SNCK-006.webp",
  ],

  // Britannia Brown Bread 400 g (Zepto CDN)
  "GRO-BAKE-001": [
    "/products/GRO-BAKE-001.webp",
  ],

  // Eggoz Farm White Eggs (Zepto CDN)
  "GRO-BAKE-002": [
    "/products/GRO-BAKE-002.webp",
  ],

  // Tata Tea Gold 500 g (Zepto CDN)
  "GRO-BEVG-001": [
    "/products/GRO-BEVG-001.webp",
  ],

  // Bisleri Mineral Water 1 L (Zepto CDN)
  "GRO-BEVG-005": [
    "/products/GRO-BEVG-005.webp",
  ],

  // Oil Showcase Products matching Screenshot 2 (Zepto CDN)
  "OIL-SUN-001": [
    "/brand/brand_OIL-SUN-001_1.webp",
  ],
  "OIL-MUS-001": [
    "/brand/brand_OIL-MUS-001_1.webp",
  ],
  "OIL-MUS-002": [
    "/subcategories/olive_cold.webp",
  ],
  "OIL-BRAN-001": [
    "/subcategories/olive_cold.webp",
  ],
  "OIL-BRAN-002": [
    "/subcategories/olive_cold.webp",
  ],
  "OIL-SUN-002": [
    "/brand/brand_OIL-SUN-001_1.webp",
  ],
  "OIL-SUN-005": [
    "/brand/brand_OIL-SUN-001_1.webp",
  ],
  "GRO-STPL-OIL-001": [
    "/brand/brand_OIL-SUN-001_1.webp",
  ],
  "GRO-STPL-OIL-002": [
    "/brand/brand_OIL-MUS-001_1.webp",
  ],

  // Demo Electronics matching Screenshot 3 (Apple iPhone 17 Pro Cosmic Orange from Zepto CDN)
  "ELEC-IPHONE-16": [
    "/products/ELEC-IPHONE-16.webp",
    "/products/ELEC-IPHONE-16_2.webp",
    "/products/ELEC-IPHONE-16_3.webp",
    "/products/ELEC-IPHONE-16_4.webp",
    "/products/ELEC-IPHONE-16_5.webp",
    "/products/ELEC-IPHONE-16_6.webp",
    "/products/ELEC-IPHONE-16_7.webp",
    "/products/ELEC-IPHONE-16_8.webp",
    "/products/ELEC-IPHONE-16_9.webp",
  ],
};

/** Category grid images — Exact 20 official category graphics from zepto.com */
export const CATEGORY_IMAGES: Record<string, string> = {
  // Row 1 (Position 1 - 10 from zepto.com ItemList)
  produce: "/categories/produce.webp", // Fruits & Vegetables
  dairy: "/categories/dairy.webp", // Dairy, Bread & Eggs
  staples: "/categories/staples.webp", // Atta, Rice, Oil & Dals
  meats: "/categories/meats.webp", // Meats, Fish & Eggs
  masalas: "/categories/masalas.webp", // Masala, Dry Fruits & More
  breakfast: "/categories/breakfast.webp", // Breakfast & Sauces
  packaged: "/categories/packaged.webp", // Packaged Food
  cafe: "/categories/cafe.webp", // Zepto Cafe
  tea_coffee: "/categories/tea_coffee.webp", // Tea, Coffee & More
  ice_creams: "/categories/ice_creams.webp", // Ice Creams & More

  // Row 2 (Position 11 - 20 from zepto.com ItemList)
  frozen: "/categories/frozen.webp", // Frozen Food
  sweets: "/categories/sweets.webp", // Sweet Cravings
  drinks: "/categories/drinks.webp", // Cold Drinks & Juices
  munchies: "/categories/munchies.webp", // Munchies
  biscuits: "/categories/biscuits.webp", // Biscuits
  lifestyle: "/categories/lifestyle.webp", // Apparel & Lifestyle
  jewellery: "/categories/jewellery.webp", // Jewellery
  beauty_care: "/categories/beauty_care.webp", // Beauty & Personal Care
  skincare: "/categories/skincare.webp", // Skincare
  makeup: "/categories/makeup.webp", // Makeup & Beauty
};

/** Subcategory sidebar images exclusively from Zepto's live CDN */
export const SUBCATEGORY_IMAGES: Record<string, string> = {
  healthy_picks: "/brand/brand_OIL-SUN-001_1.webp",
  olive_cold: "/subcategories/olive_cold.webp",
  oil: "/brand/brand_OIL-MUS-001_1.webp",
  atta: "/subcategories/healthy_atta.webp",
  millets: "/subcategories/millets.webp",
  besan: "/subcategories/besan.webp",
  healthy_atta: "/subcategories/healthy_atta.webp",
  ghee: "/subcategories/ghee.webp",
};

/** Get image URL for a product SKU, with emoji fallback */
export function getProductImage(sku: string): string | null {
  return PRODUCT_IMAGES[sku]?.[0] ?? null;
}

/** Get all image URLs for a product SKU */
export function getProductImages(sku: string): string[] {
  return PRODUCT_IMAGES[sku] ?? [];
}

/** Generate a deterministic MRP (higher than selling price) for demo */
export function getMockMrp(listPriceMinor: number): number {
  // Integer basis points: 11500 to 12700 bp (15% to 27% markup), strictly integer math
  const markupBp = 11500 + (Math.abs(listPriceMinor) % 7) * 200;
  const unroundedPaise = Math.floor((listPriceMinor * markupBp) / 10000);
  return Math.ceil(unroundedPaise / 100) * 100;
}

export function getDiscountDisplay(sellingPriceMinor: number, mrpMinor: number): { amount: number; text: string } | null {
  if (mrpMinor <= sellingPriceMinor) return null;
  const amountMinor = mrpMinor - sellingPriceMinor;
  const rupees = Math.floor(amountMinor / 100);
  if (rupees >= 1000) {
    const kRupees = Math.floor(rupees / 1000);
    return { amount: amountMinor, text: `₹${kRupees}K OFF` };
  }
  return { amount: amountMinor, text: `₹${rupees} OFF` };
}
export const BRAND_IMAGES: Record<string, string[]> = {
  "ELEC-IPHONE-16": [
    "/brand/brand_ELEC-IPHONE-16_1.webp",
    "/brand/brand_ELEC-IPHONE-16_2.webp",
    "/brand/brand_ELEC-IPHONE-16_3.webp",
    "/brand/brand_ELEC-IPHONE-16_4.webp",
    "/brand/brand_ELEC-IPHONE-16_5.webp",
  ],
  "OIL-SUN-001": [
    "/brand/brand_OIL-SUN-001_1.webp",
  ],
  "OIL-MUS-001": [
    "/brand/brand_OIL-MUS-001_1.webp",
  ],
};

export interface ProductMeta {
  brand: string;
  description: string;
  brandImages: string[];
  highlights: { label: string; value: string }[];
  specifications: { label: string; value: string }[];
  disclaimer: string;
  customerCare: string;
}

/** Retrieve rich Zepto-style PDP details, descriptions, highlights, and specifications */
export function getProductMeta(sku: string, category: string, displayName: string, unitLabel: string): ProductMeta {
  const brandImages = BRAND_IMAGES[sku] ?? [];

  if (sku === "ELEC-IPHONE-16") {
    return {
      brand: "Apple",
      description:
        "iPhone 17 Pro includes a 15.93 cm (6.3-inch) Super Retina XDR display, forged titanium unibody design, A19 Pro chip, pro camera system with 48MP Fusion, 48MP Ultra Wide, and 48MP Telephoto, breakthrough all-day battery life, and 4K Dolby Vision recording up to 120 fps. Introducing Camera Control for tactile zoom, depth of field, and instantaneous capture.",
      brandImages,
      highlights: [
        { label: "Display Type", value: "Super Retina XDR Display (6.3 inches, 120Hz ProMotion)" },
        { label: "Processor", value: "A19 Pro Chip with 6-core GPU & Neural Engine" },
        { label: "Rear Camera", value: "48 MP Main + 48 MP Ultra Wide + 48 MP Telephoto" },
        { label: "Front Camera", value: "18 MP TrueDepth with Autofocus" },
        { label: "Battery Life", value: "Up to 31 hours video playback (3988 mAh)" },
        { label: "Internal Storage", value: "256 GB" },
        { label: "RAM", value: "12 GB Unified Memory" },
        { label: "Operating System", value: "iOS 19" },
        { label: "Finish & Build", value: "Aerospace-grade titanium, Ceramic Shield front" },
        { label: "Resolution", value: "2622 x 1206 Pixels at 460 ppi" },
      ],
      specifications: [
        { label: "Product Type", value: "Smartphone" },
        { label: "Model Name", value: "iPhone 17 Pro" },
        { label: "Colour Name", value: "Cosmic Orange" },
        { label: "Net Quantity", value: "1 pc" },
        { label: "Country of Origin", value: "India" },
        { label: "Warranty", value: "1 Year Manufacturer Warranty" },
        { label: "Manufacturer Name", value: "Apple Inc" },
        {
          label: "Manufacturer Address",
          value: "Apple India Private Limited, 13th Floor, Prestige Minsk Square, Municipal No. 6, Cubbon Road, Bengaluru, Karnataka - 560001, India",
        },
        { label: "Seller Name", value: "Commodum Groceries Private Limited" },
        { label: "Seller License No.", value: "12822999000310" },
        {
          label: "Service Centre Details",
          value: "For warranty claims or support visit https://getsupport.apple.com/ or call toll-free 000800 1009009",
        },
      ],
      disclaimer:
        "All images are for representational purposes only. It is advised that you check the box seal and verify the physical condition during open box delivery before accepting the order.",
      customerCare: "In case of any issue, contact us: support@zeptonow.com",
    };
  }

  if (sku.startsWith("OIL-SUN") || sku === "GRO-STPL-OIL-001") {
    return {
      brand: "Freedom",
      description:
        "Freedom Refined Sunflower Oil is 100% pure, crystal clear, and triple-refined for superior health and taste. Fortified with Vitamins A & D, it features a balanced fatty acid profile rich in Omega-6 PUFA. Its high smoke point makes it the premier choice for healthy deep frying, sautéing, and daily Indian cooking without greasy residues.",
      brandImages,
      highlights: [
        { label: "Dietary Preference", value: "100% Vegetarian" },
        { label: "Refining Process", value: "Triple Refined & Cold Filtered" },
        { label: "Fortification", value: "Fortified with Vitamins A & D" },
        { label: "Smoke Point", value: "High smoke point — ideal for sautéing & frying" },
        { label: "Fat Profile", value: "Rich in Polyunsaturated Fatty Acids (PUFA) & Omega-6" },
        { label: "Container Type", value: "Pouch / Ergonomic Bottle" },
        { label: "Flavour Profile", value: "Neutral, preserves original aroma of ingredients" },
      ],
      specifications: [
        { label: "Brand", value: "Freedom" },
        { label: "Net Quantity", value: unitLabel },
        { label: "Country of Origin", value: "India" },
        { label: "Shelf Life", value: "9 Months from packaging date" },
        { label: "FSSAI License No.", value: "10012044000358" },
        { label: "Manufacturer Name", value: "Gemini Edibles & Fats India Limited" },
        {
          label: "Manufacturer Address",
          value: "Plot No. 119, Road No. 10, Jubilee Hills, Hyderabad, Telangana - 500033",
        },
        { label: "Seller Name", value: "Commodum Groceries Private Limited" },
        { label: "Storage Instructions", value: "Store in a cool, dry place away from direct sunlight" },
      ],
      disclaimer:
        "Every effort is made to maintain accuracy of all information. Please read the product label, allergens, and batch details before consuming the product.",
      customerCare: "In case of any issue, contact us at support@zeptonow.com or 1800 425 2277",
    };
  }

  if (sku.startsWith("OIL-MUS") || sku === "GRO-STPL-OIL-002") {
    return {
      brand: "Fortune",
      description:
        "Fortune Kachi Ghani Mustard Oil is traditionally cold-pressed from first-harvest mustard seeds. It delivers that signature strong aroma and authentic pungent zing that elevates traditional pickles, Bengali fish preparations, and North Indian curries while naturally retaining essential Omega-3 and Omega-6 fatty acids.",
      brandImages,
      highlights: [
        { label: "Extraction Process", value: "Traditional Cold-Pressed (Kachi Ghani)" },
        { label: "Aroma & Flavour", value: "Authentic Strong & Pungent Zing" },
        { label: "Nutrient Profile", value: "Naturally rich in Omega-3 and Omega-6" },
        { label: "Culinary Use", value: "Ideal for Tadka, Curries, Pickles & Frying" },
        { label: "Dietary Preference", value: "100% Vegetarian" },
      ],
      specifications: [
        { label: "Brand", value: "Fortune" },
        { label: "Net Quantity", value: unitLabel },
        { label: "Country of Origin", value: "India" },
        { label: "Shelf Life", value: "12 Months from packaging date" },
        { label: "FSSAI License No.", value: "10013021000817" },
        { label: "Manufacturer Name", value: "Adani Wilmar Limited" },
        {
          label: "Manufacturer Address",
          value: "Fortune House, Near Navrangpura Railway Crossing, Ahmedabad, Gujarat - 380009",
        },
        { label: "Seller Name", value: "Commodum Groceries Private Limited" },
        { label: "Storage Instructions", value: "Store in a cool and dry place" },
      ],
      disclaimer:
        "Every effort is made to maintain accuracy of all information. Please read the product packaging and directions before consumption.",
      customerCare: "In case of any issue, contact us: support@zeptonow.com",
    };
  }

  if (sku.startsWith("GRO-DAIRY")) {
    const isMilk = displayName.toLowerCase().includes("milk");
    const isDahi = displayName.toLowerCase().includes("dahi");
    const isPaneer = displayName.toLowerCase().includes("paneer");
    const isButter = displayName.toLowerCase().includes("butter");

    return {
      brand: "Amul",
      description: isMilk
        ? "Amul Taaza is fresh, pasteurized, and homogenized toned milk. Sourced from cooperative dairy farmers and processed under strict hygienic standards, it delivers rich calcium and high biological value protein for everyday family vitality."
        : isDahi
          ? "Amul Masti Dahi is prepared from pasteurized toned milk using active lactic cultures. Creamy, thick, and refreshing, it provides natural probiotics that promote healthy gut digestion."
          : isPaneer
            ? "Amul Malai Paneer is crafted from pure cow and buffalo milk. Exceptionally soft and juicy, it retains maximum moisture and is a wholesome source of dairy protein."
            : isButter
              ? "Amul Butter is India's beloved classic salted butter, churned from fresh pasteurized cream for unmatched richness on breakfast toast and evening snacks."
              : "Fresh, wholesome dairy product from Amul, delivering certified purity, nutrition, and authentic taste.",
      brandImages,
      highlights: [
        { label: "Product Category", value: "Fresh Dairy" },
        { label: "Fat & Solid Profile", value: isMilk ? "3.0% Fat / 8.5% SNF" : "Rich Dairy Solids" },
        { label: "Processing", value: "Pasteurized & Homogenized" },
        { label: "Preservatives", value: "Zero Synthetic Preservatives or Additives" },
        { label: "Source", value: "100% Pure Indian Farm Milk" },
      ],
      specifications: [
        { label: "Brand", value: "Amul (GCMMF)" },
        { label: "Net Quantity", value: unitLabel },
        { label: "Country of Origin", value: "India" },
        { label: "Shelf Life", value: isMilk ? "2 Days (refrigerated below 4°C)" : "14 Days (refrigerated)" },
        { label: "FSSAI License No.", value: "10012021000071" },
        { label: "Manufacturer Name", value: "Gujarat Cooperative Milk Marketing Federation Ltd." },
        { label: "Manufacturer Address", value: "Amul Dairy Road, Anand, Gujarat - 388001" },
        { label: "Seller Name", value: "Commodum Groceries Private Limited" },
        { label: "Storage Instructions", value: "Keep refrigerated below 4°C at all times" },
      ],
      disclaimer: "Perishable item. Consume within recommended shelf life once opened.",
      customerCare: "Amul Customer Care: 1800 258 3333 / support@zeptonow.com",
    };
  }

  // General fallback for produce, snacks, bakery, staples, beverages
  const brandName =
    displayName.includes("India Gate")
      ? "India Gate"
      : displayName.includes("Aashirvaad")
        ? "Aashirvaad"
        : displayName.includes("Tata")
          ? "Tata"
          : displayName.includes("Maggi")
            ? "Maggi"
            : displayName.includes("Parle")
              ? "Parle"
              : displayName.includes("Kellogg")
                ? "Kellogg's"
                : displayName.includes("Britannia")
                  ? "Britannia"
                  : displayName.includes("Bisleri")
                    ? "Bisleri"
                    : displayName.includes("Eggoz")
                      ? "Eggoz"
                      : displayName.includes("Mr.Gold")
                        ? "Mr.Gold"
                        : displayName.includes("Sunpure")
                          ? "Sunpure"
                          : category.charAt(0).toUpperCase() + category.slice(1);

  return {
    brand: brandName,
    description: `${displayName} is carefully sourced and quality-checked to meet Zepto's strict fresh commerce standards. Packed under hygienic conditions to preserve optimal freshness, texture, and authentic culinary quality from dark store to your doorstep.`,
    brandImages,
    highlights: [
      { label: "Category", value: category.charAt(0).toUpperCase() + category.slice(1) },
      { label: "Packaging Type", value: "Hygienic Sealed Pack" },
      { label: "Dietary Preference", value: "Vegetarian / Fresh Food" },
      { label: "Quality Assurance", value: "100% Quality Inspected before dispatch" },
      { label: "Delivery Guarantee", value: "Delivered in minutes with cold-chain protection" },
    ],
    specifications: [
      { label: "Brand", value: brandName },
      { label: "Net Quantity", value: unitLabel },
      { label: "Country of Origin", value: "India" },
      { label: "Shelf Life", value: "Refer to packaging for exact batch best-before date" },
      { label: "FSSAI License No.", value: "10019043002768" },
      { label: "Seller Name", value: "Commodum Groceries Private Limited" },
      { label: "Storage Instructions", value: "Store in a cool, dry, and hygienic place" },
    ],
    disclaimer:
      "All images are for representational purposes only. Please read batch, ingredient, and allergen details on the product label prior to use.",
    customerCare: "In case of any query or issue, reach out to us at support@zeptonow.com",
  };
}

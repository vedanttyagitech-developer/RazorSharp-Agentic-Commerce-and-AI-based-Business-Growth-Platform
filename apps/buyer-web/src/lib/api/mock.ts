/**
 * Deterministic mock of the CommerceClient interface.
 *
 * Walks the eleven-step demonstration (spec 2.5) without a backend: grounded search over
 * a slice of the Demo Grocery Store fixture, the fee engine's exact arithmetic, versioned
 * checkouts with real SHA-256 content hashes, a scripted merchant-state change underneath
 * the first approval (denied REAPPROVAL_REQUIRED with an exact delta and version N+1),
 * a simulated Razorpay handoff, browser-callback-then-evidence capture, webhook replay,
 * unknown-outcome reconciliation, late (stale) capture with one automatic refund, and
 * buyer-confirmed refunds. State is persisted to sessionStorage so navigation and reloads
 * keep the journey; the scripted worker steps re-arm on hydrate.
 *
 * Selected with NEXT_PUBLIC_API_MODE=mock. Nothing here talks to the network.
 */
import { canonicalHash, sha256Hex, type Canonical } from "../hash";
import type { CommerceClient, EventSubscriptionOptions, SearchParams } from "./client";
import { ApiError } from "./problem";
import type {
  ApprovalCard,
  ApprovalEcho,
  ApprovalRecord,
  ApproveResponse,
  AttemptSummary,
  Basket,
  CancelResponse,
  Checkout,
  CheckoutEvent,
  CheckoutState,
  Delta,
  KernelDecision,
  Order,
  PaymentHandoff,
  Product,
  ProofChain,
  ProofLink,
  Quote,
  QuoteLine,
  RecoveryCode,
  Refund,
  RefundRequest,
  RefundResponse,
  Reservation,
  RuntimeConfig,
  SearchHit,
  SearchResponse,
  SubmitResponse,
  TimelineResponse,
  TimelineRow,
  Unavailability,
  VerifyRequest,
  VerifyResponse,
  VersionSummary,
} from "./types";

// ------------------------------------------------------------------------ fixture

const SOURCE = "merchant-sim:demo-grocery/v1";
const CURRENCY = "INR";
const RESERVATION_TTL_MS = 900_000;
const APPROVAL_TTL_MS = 600_000;
const STEP_DELAY_MS = 1100;
const STORAGE_KEY = "buyer-web:mock-state:v3";
export const MOCK_RAZORPAY_KEY_ID = "rzp_test_MOCK00000000";

interface FixtureProduct {
  sku: string;
  name_en: string;
  name_hi: string;
  category: string;
  unit_label: string;
  list_price_minor: number;
  baseline_stock: number;
  tax_bp: number;
  synonyms: string[];
  listed?: boolean;
}

const FIXTURE: FixtureProduct[] = [
  { sku: "GRO-DAIRY-001", name_en: "Amul Taaza Toned Milk 500 ml", name_hi: "अमूल ताज़ा टोंड दूध 500 मिली", category: "dairy", unit_label: "500 ml", list_price_minor: 2800, baseline_stock: 48, tax_bp: 0, synonyms: ["दूध", "ताज़ा दूध", "doodh", "dudh", "milk", "taaza", "toned milk"] },
  { sku: "GRO-DAIRY-002", name_en: "Amul Gold Full Cream Milk 1 L", name_hi: "अमूल गोल्ड फुल क्रीम दूध 1 लीटर", category: "dairy", unit_label: "1 L", list_price_minor: 7300, baseline_stock: 30, tax_bp: 0, synonyms: ["दूध", "फुल क्रीम दूध", "doodh", "dudh", "milk", "full cream", "gold"] },
  { sku: "GRO-DAIRY-003", name_en: "Amul Masti Dahi 400 g", name_hi: "अमूल मस्ती दही 400 ग्राम", category: "dairy", unit_label: "400 g", list_price_minor: 4500, baseline_stock: 24, tax_bp: 500, synonyms: ["दही", "dahi", "curd", "yoghurt", "yogurt"] },
  { sku: "GRO-DAIRY-004", name_en: "Amul Malai Paneer Block 200 g", name_hi: "अमूल मलाई पनीर 200 ग्राम", category: "dairy", unit_label: "200 g", list_price_minor: 9500, baseline_stock: 0, tax_bp: 500, synonyms: ["पनीर", "paneer", "panir", "cottage cheese", "malai"] },
  { sku: "GRO-DAIRY-005", name_en: "Amul Butter (Salted) 100 g", name_hi: "अमूल मक्खन (नमकीन) 100 ग्राम", category: "dairy", unit_label: "100 g", list_price_minor: 5800, baseline_stock: 20, tax_bp: 1200, synonyms: ["मक्खन", "makkhan", "makhan", "butter"] },
  { sku: "GRO-DAIRY-006", name_en: "Nestlé A+ Slim Toned Milk 1 L", name_hi: "नेस्ले ए+ स्लिम टोंड दूध 1 लीटर", category: "dairy", unit_label: "1 L", list_price_minor: 7800, baseline_stock: 15, tax_bp: 0, synonyms: ["दूध", "स्लिम दूध", "nestle", "slim milk", "doodh", "dudh", "milk"] },
  { sku: "GRO-STPL-001", name_en: "India Gate Classic Basmati Rice 5 kg", name_hi: "इंडिया गेट क्लासिक बासमती चावल 5 किलो", category: "staples", unit_label: "5 kg", list_price_minor: 49900, baseline_stock: 10, tax_bp: 500, synonyms: ["चावल", "बासमती चावल", "chawal", "chaval", "rice", "basmati"] },
  { sku: "GRO-STPL-002", name_en: "Aashirvaad Shudh Chakki Atta 5 kg", name_hi: "आशीर्वाद शुद्ध चक्की आटा 5 किलो", category: "staples", unit_label: "5 kg", list_price_minor: 25500, baseline_stock: 18, tax_bp: 500, synonyms: ["आटा", "गेहूं का आटा", "atta", "aata", "flour", "gehun", "chakki atta"] },
  { sku: "GRO-STPL-003", name_en: "Tata Salt — Iodised, 1 kg", name_hi: "टाटा नमक — आयोडीन युक्त, 1 किलो", category: "staples", unit_label: "1 kg", list_price_minor: 2800, baseline_stock: 40, tax_bp: 500, synonyms: ["नमक", "namak", "salt", "iodised", "iodized"] },
  { sku: "GRO-STPL-004", name_en: "Toor Dal (Arhar) 1 kg", name_hi: "तूर दाल (अरहर) 1 किलो", category: "staples", unit_label: "1 kg", list_price_minor: 18500, baseline_stock: 22, tax_bp: 500, synonyms: ["दाल", "तूर दाल", "अरहर दाल", "dal", "daal", "toor dal", "tur dal", "arhar", "lentil", "pulses"] },
  { sku: "GRO-STPL-005", name_en: "Chana Dal 500 g", name_hi: "चना दाल 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 6200, baseline_stock: 25, tax_bp: 500, synonyms: ["चना दाल", "चना", "chana", "chana dal", "gram", "bengal gram"] },
  { sku: "GRO-STPL-006", name_en: "Madhur Pure & Hygienic Sugar 1 kg", name_hi: "मधुर शुद्ध चीनी 1 किलो", category: "staples", unit_label: "1 kg", list_price_minor: 5200, baseline_stock: 30, tax_bp: 500, synonyms: ["चीनी", "शक्कर", "cheeni", "chini", "sugar", "shakkar", "shakar"] },
  { sku: "GRO-STPL-007", name_en: "Cold-Pressed Groundnut Oil 500 ml", name_hi: "कोल्ड प्रेस्ड मूंगफली तेल 500 मिली", category: "staples", unit_label: "500 ml", list_price_minor: 16633, baseline_stock: 14, tax_bp: 500, synonyms: ["तेल", "मूंगफली तेल", "tel", "oil", "moongfali", "mungfali", "groundnut", "peanut oil"] },
  { sku: "GRO-STPL-008", name_en: "Fortune Sunlite Refined Sunflower Oil 1 L", name_hi: "फॉर्च्यून सनलाइट रिफाइंड सूरजमुखी तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 14900, baseline_stock: 20, tax_bp: 500, synonyms: ["तेल", "सूरजमुखी तेल", "tel", "oil", "sunflower", "refined oil", "fortune"] },
  { sku: "GRO-STPL-009", name_en: "Rajma Chitra 500 g", name_hi: "राजमा चित्रा 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 9800, baseline_stock: 16, tax_bp: 500, synonyms: ["राजमा", "rajma", "kidney beans", "beans"] },
  { sku: "GRO-STPL-010", name_en: "Poha (Flattened Rice) 500 g", name_hi: "पोहा 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 4400, baseline_stock: 20, tax_bp: 500, synonyms: ["पोहा", "चिवड़ा", "poha", "chivda", "flattened rice"] },
  { sku: "GRO-PROD-001", name_en: "Onion (Pyaz) 1 kg", name_hi: "प्याज 1 किलो", category: "produce", unit_label: "1 kg", list_price_minor: 4200, baseline_stock: 35, tax_bp: 0, synonyms: ["प्याज", "pyaz", "pyaaz", "pyaj", "onion", "kanda"] },
  { sku: "GRO-PROD-002", name_en: "Tomato (Tamatar) 1 kg", name_hi: "टमाटर 1 किलो", category: "produce", unit_label: "1 kg", list_price_minor: 3800, baseline_stock: 28, tax_bp: 0, synonyms: ["टमाटर", "tamatar", "tomato", "tamater"] },
  { sku: "GRO-PROD-003", name_en: "Potato (Aloo) 1 kg", name_hi: "आलू 1 किलो", category: "produce", unit_label: "1 kg", list_price_minor: 3200, baseline_stock: 45, tax_bp: 0, synonyms: ["आलू", "aloo", "alu", "potato", "batata"] },
  { sku: "GRO-PROD-004", name_en: "Loose Ginger (Adrak) 100 g", name_hi: "अदरक 100 ग्राम", category: "produce", unit_label: "100 g", list_price_minor: 2400, baseline_stock: 18, tax_bp: 0, synonyms: ["अदरक", "adrak", "ginger"] },
  { sku: "GRO-PROD-005", name_en: "Garlic (Lehsun) 200 g", name_hi: "लहसुन 200 ग्राम", category: "produce", unit_label: "200 g", list_price_minor: 3600, baseline_stock: 16, tax_bp: 0, synonyms: ["लहसुन", "lehsun", "lahsun", "garlic"] },
  { sku: "GRO-PROD-006", name_en: "Banana (Kela) — 6 pcs", name_hi: "केला — 6 नग", category: "produce", unit_label: "6 pcs", list_price_minor: 5400, baseline_stock: 20, tax_bp: 0, synonyms: ["केला", "kela", "banana", "fruit"] },
  { sku: "GRO-PROD-007", name_en: "Coriander Leaves (Hara Dhania) 100 g", name_hi: "हरा धनिया 100 ग्राम", category: "produce", unit_label: "100 g", list_price_minor: 1500, baseline_stock: 12, tax_bp: 0, synonyms: ["धनिया", "हरा धनिया", "dhania", "dhaniya", "hara dhania", "coriander", "cilantro"] },
  { sku: "GRO-PROD-008", name_en: "Green Chilli (Hari Mirch) 100 g", name_hi: "हरी मिर्च 100 ग्राम", category: "produce", unit_label: "100 g", list_price_minor: 1800, baseline_stock: 14, tax_bp: 0, synonyms: ["मिर्च", "हरी मिर्च", "mirch", "mirchi", "hari mirch", "chilli", "chili", "green chili"] },
  { sku: "GRO-PROD-009", name_en: "Lemon (Nimbu) — 4 pcs", name_hi: "नींबू — 4 नग", category: "produce", unit_label: "4 pcs", list_price_minor: 2000, baseline_stock: 26, tax_bp: 0, synonyms: ["नींबू", "nimbu", "neembu", "lemon", "lime"] },
  { sku: "GRO-SNCK-001", name_en: "Maggi 2-Minute Masala Noodles (Pack of 4)", name_hi: "मैगी 2-मिनट मसाला नूडल्स (4 का पैक)", category: "snacks", unit_label: "4 x 70 g", list_price_minor: 9600, baseline_stock: 26, tax_bp: 1200, synonyms: ["मैगी", "नूडल्स", "maggi", "magi", "noodles", "instant noodles"] },
  { sku: "GRO-SNCK-002", name_en: "Britannia 50-50 Maska Chaska 120 g", name_hi: "ब्रिटानिया 50-50 मस्का चस्का 120 ग्राम", category: "snacks", unit_label: "120 g", list_price_minor: 3500, baseline_stock: 30, tax_bp: 1800, synonyms: ["बिस्किट", "मस्का चस्का", "biscuit", "biskut", "maska chaska", "britannia", "cookies"] },
  { sku: "GRO-SNCK-003", name_en: "Haldiram's Aloo Bhujia 200 g", name_hi: "हल्दीराम आलू भुजिया 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 5800, baseline_stock: 22, tax_bp: 1200, synonyms: ["भुजिया", "नमकीन", "आलू भुजिया", "bhujia", "namkeen", "aloo bhujia", "haldiram"] },
  { sku: "GRO-SNCK-004", name_en: "Lay's India's Magic Masala 52 g", name_hi: "लेज़ इंडियाज़ मैजिक मसाला 52 ग्राम", category: "snacks", unit_label: "52 g", list_price_minor: 2000, baseline_stock: 40, tax_bp: 1800, synonyms: ["चिप्स", "वेफर्स", "chips", "lays", "wafers", "magic masala"] },
  { sku: "GRO-SNCK-005", name_en: "Parle-G Gold Biscuits 200 g", name_hi: "पारले-जी गोल्ड बिस्किट 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 3000, baseline_stock: 35, tax_bp: 1800, synonyms: ["बिस्किट", "पारले जी", "parle g", "parleg", "biscuit", "biskut", "glucose biscuit"] },
  { sku: "GRO-SNCK-006", name_en: "Kellogg's Chocos 375 g", name_hi: "केलॉग्स चॉकोस 375 ग्राम", category: "snacks", unit_label: "375 g", list_price_minor: 25500, baseline_stock: 10, tax_bp: 1800, synonyms: ["चॉकोस", "सीरियल", "chocos", "cereal", "kellogs", "cornflakes"], listed: false },
  { sku: "GRO-BEVG-001", name_en: "Tata Tea Gold 500 g", name_hi: "टाटा टी गोल्ड 500 ग्राम", category: "beverages", unit_label: "500 g", list_price_minor: 28500, baseline_stock: 16, tax_bp: 500, synonyms: ["चाय", "चाय पत्ती", "chai", "chay", "tea", "chaipatti", "tea leaves"] },
  { sku: "GRO-BEVG-002", name_en: "Nescafé Classic Instant Coffee 50 g", name_hi: "नेस्कैफे क्लासिक इंस्टेंट कॉफ़ी 50 ग्राम", category: "beverages", unit_label: "50 g", list_price_minor: 18500, baseline_stock: 12, tax_bp: 1800, synonyms: ["कॉफ़ी", "coffee", "cofee", "kaufi", "nescafe", "instant coffee"] },
  { sku: "GRO-BEVG-003", name_en: "Coca-Cola 750 ml", name_hi: "कोका-कोला 750 मिली", category: "beverages", unit_label: "750 ml", list_price_minor: 4000, baseline_stock: 30, tax_bp: 2800, synonyms: ["कोक", "ठंडा", "कोल्ड ड्रिंक", "coke", "cola", "thanda", "cold drink", "soft drink"] },
  { sku: "GRO-BEVG-004", name_en: "Real Fruit Power — Mixed Fruit Juice 1 L", name_hi: "रियल फ्रूट पावर — मिक्स्ड फ्रूट जूस 1 लीटर", category: "beverages", unit_label: "1 L", list_price_minor: 11000, baseline_stock: 18, tax_bp: 1200, synonyms: ["जूस", "फलों का रस", "juice", "jus", "fruit juice", "phal", "real"] },
  { sku: "GRO-BEVG-005", name_en: "Bisleri Mineral Water 1 L", name_hi: "बिसलेरी मिनरल पानी 1 लीटर", category: "beverages", unit_label: "1 L", list_price_minor: 2000, baseline_stock: 50, tax_bp: 1800, synonyms: ["पानी", "pani", "paani", "water", "bisleri", "mineral water"] },
  { sku: "GRO-BAKE-001", name_en: "Britannia Brown Bread 400 g", name_hi: "ब्रिटानिया ब्राउन ब्रेड 400 ग्राम", category: "bakery", unit_label: "400 g", list_price_minor: 5000, baseline_stock: 20, tax_bp: 500, synonyms: ["ब्रेड", "डबल रोटी", "पाव", "bread", "bred", "pav", "double roti", "brown bread"] },
  { sku: "GRO-BAKE-002", name_en: "Farm Eggs — Tray of 6", name_hi: "फार्म अंडे — 6 की ट्रे", category: "bakery", unit_label: "6 pcs", list_price_minor: 6600, baseline_stock: 24, tax_bp: 0, synonyms: ["अंडा", "अंडे", "anda", "ande", "andey", "egg", "eggs"] },
  { sku: "GRO-HHLD-001", name_en: "Surf Excel Easy Wash Detergent Powder 1 kg", name_hi: "सर्फ़ एक्सेल ईज़ी वॉश डिटर्जेंट पाउडर 1 किलो", category: "household", unit_label: "1 kg", list_price_minor: 13500, baseline_stock: 15, tax_bp: 1800, synonyms: ["सर्फ़", "डिटर्जेंट", "कपड़े धोने का पाउडर", "surf", "detergent", "washing powder", "surf excel"] },
  { sku: "GRO-HHLD-002", name_en: "Vim Dishwash Bar 300 g", name_hi: "विम बर्तन साबुन 300 ग्राम", category: "household", unit_label: "300 g", list_price_minor: 3000, baseline_stock: 25, tax_bp: 1800, synonyms: ["बर्तन साबुन", "विम", "vim", "dishwash", "bartan sabun", "dish bar"] },
  { sku: "GRO-HHLD-003", name_en: "Harpic Power Plus Toilet Cleaner 500 ml", name_hi: "हार्पिक पावर प्लस टॉयलेट क्लीनर 500 मिली", category: "household", unit_label: "500 ml", list_price_minor: 9900, baseline_stock: 14, tax_bp: 1800, synonyms: ["हार्पिक", "टॉयलेट क्लीनर", "harpic", "toilet cleaner", "bathroom cleaner"] },
  { sku: "GRO-HHLD-004", name_en: "Nirma Washing Powder­ 1 kg", name_hi: "निरमा वॉशिंग पाउडर­ 1 किलो", category: "household", unit_label: "1 kg", list_price_minor: 7800, baseline_stock: 20, tax_bp: 1800, synonyms: ["निरमा", "वॉशिंग पाउडर", "nirma", "washing powder", "detergent"] },
  { sku: "GRO-PERS-001", name_en: "Colgate Strong Teeth Toothpaste 200 g", name_hi: "कोलगेट स्ट्रॉन्ग टीथ टूथपेस्ट 200 ग्राम", category: "personal_care", unit_label: "200 g", list_price_minor: 11500, baseline_stock: 20, tax_bp: 1800, synonyms: ["टूथपेस्ट", "मंजन", "पेस्ट", "toothpaste", "colgate", "manjan", "paste", "tooth paste"] },
  { sku: "GRO-PERS-002", name_en: "Dettol Original Soap — 4 × 125 g", name_hi: "डेटॉल ओरिजिनल साबुन — 4 × 125 ग्राम", category: "personal_care", unit_label: "4 x 125 g", list_price_minor: 17600, baseline_stock: 18, tax_bp: 1800, synonyms: ["साबुन", "डेटॉल", "sabun", "saabun", "soap", "dettol", "bathing bar"] },
  { sku: "GRO-PERS-003", name_en: "Head & Shoulders Anti-Dandruff Shampoo 340 ml", name_hi: "हेड एंड शोल्डर्स एंटी-डैंड्रफ शैम्पू 340 मिली", category: "personal_care", unit_label: "340 ml", list_price_minor: 39900, baseline_stock: 8, tax_bp: 1800, synonyms: ["शैम्पू", "shampoo", "shampu", "anti dandruff", "head and shoulders"] },
  { sku: "GRO-COND-001", name_en: "Mother's Recipe Mango Pickle 400 g", name_hi: "मदर्स रेसिपी आम का अचार 400 ग्राम", category: "condiments", unit_label: "400 g", list_price_minor: 9900, baseline_stock: 16, tax_bp: 1200, synonyms: ["अचार", "आम का अचार", "achar", "aachar", "pickle", "aam ka achar", "mango pickle"] },
  { sku: "GRO-COND-002", name_en: "MDH Garam Masala 100 g", name_hi: "एमडीएच गरम मसाला 100 ग्राम", category: "condiments", unit_label: "100 g", list_price_minor: 8500, baseline_stock: 22, tax_bp: 500, synonyms: ["गरम मसाला", "मसाला", "garam masala", "masala", "mdh", "spice mix"] },
  { sku: "GRO-COND-003", name_en: "Kissan Fresh Tomato Ketchup 950 g", name_hi: "किसान फ्रेश टमाटर केचप 950 ग्राम", category: "condiments", unit_label: "950 g", list_price_minor: 14500, baseline_stock: 12, tax_bp: 1200, synonyms: ["केचप", "टमाटर सॉस", "ketchup", "sauce", "tomato sauce", "kissan"] },
  { sku: "GRO-COND-004", name_en: "Everest Haldi (Turmeric) Powder 200 g", name_hi: "एवरेस्ट हल्दी पाउडर 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 7200, baseline_stock: 20, tax_bp: 500, synonyms: ["हल्दी", "हल्दी पाउडर", "haldi", "turmeric", "haldi powder"] },
  { sku: "ELEC-IPHONE-16", name_en: "Apple iPhone 17 Pro | 256 GB | Cosmic Orange", name_hi: "एप्पल आईफोन 17 प्रो | 256 जीबी | कॉस्मिक ऑरेंज", category: "electronics", unit_label: "1 pc", list_price_minor: 12689900, baseline_stock: 12, tax_bp: 1800, synonyms: ["आईफोन", "फोन", "मोबाइल", "एप्पल", "iphone", "apple", "phone", "mobile", "17 pro", "cosmic orange", "electronics"] },
  { sku: "OIL-SUN-001", name_en: "Freedom Refined Sunflower Oil", name_hi: "फ्रीडम रिफाइंड सूरजमुखी तेल", category: "staples", unit_label: "1 L", list_price_minor: 17900, baseline_stock: 40, tax_bp: 500, synonyms: ["तेल", "सूरजमुखी", "फ्रीडम", "oil", "cooking oil", "sunflower", "freedom", "tel"] },
  { sku: "OIL-MUS-001", name_en: "Fortune Kachi Ghani Mustard Oil | Bottle", name_hi: "फॉर्च्यून कच्ची घानी सरसों का तेल", category: "staples", unit_label: "1 L", list_price_minor: 20700, baseline_stock: 35, tax_bp: 500, synonyms: ["तेल", "सरसों", "फॉर्च्यून", "कच्ची घानी", "oil", "mustard", "fortune", "kachi ghani", "sarson", "tel"] },
  { sku: "OIL-SUN-002", name_en: "Fortune Sunlite Refined Sunflower Oil 1 L", name_hi: "फॉर्च्यून सनलाइट रिफाइंड सूरजमुखी तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 16500, baseline_stock: 30, tax_bp: 500, synonyms: ["तेल", "सूरजमुखी", "फॉर्च्यून", "oil", "sunflower", "fortune", "tel"] },
  { sku: "OIL-SUN-005", name_en: "Gemini Pure Refined Sunflower Oil 1 L", name_hi: "जेमिनी प्योर रिफाइंड सूरजमुखी तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 15500, baseline_stock: 25, tax_bp: 500, synonyms: ["तेल", "सूरजमुखी", "जेमिनी", "oil", "sunflower", "gemini", "tel"] },
  { sku: "OIL-BRAN-001", name_en: "Fortune Rice Bran Health Oil 1 L", name_hi: "फॉर्च्यून राइस ब्रान हेल्थ ऑयल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 17500, baseline_stock: 20, tax_bp: 500, synonyms: ["तेल", "राइस ब्रान", "फॉर्च्यून", "oil", "rice bran", "fortune", "tel"] },
  { sku: "OIL-BRAN-002", name_en: "Emami Healthy & Tasty Rice Bran Oil 1 L", name_hi: "इमामी हेल्दी एंड टेस्टी राइस ब्रान तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 17000, baseline_stock: 20, tax_bp: 500, synonyms: ["तेल", "राइस ब्रान", "इमामी", "oil", "rice bran", "emami", "tel"] },
  { sku: "GRO-STPL-OIL-001", name_en: "Freedom Refined Sunflower Oil 1 L", name_hi: "फ्रीडम रिफाइंड सूरजमुखी तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 17900, baseline_stock: 30, tax_bp: 500, synonyms: ["तेल", "सूरजमुखी", "फ्रीडम", "oil", "sunflower", "freedom", "tel"] },
  { sku: "GRO-STPL-OIL-002", name_en: "Fortune Kachi Ghani Mustard Oil 1 L", name_hi: "फॉर्च्यून कच्ची घानी सरसों का तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 20700, baseline_stock: 25, tax_bp: 500, synonyms: ["तेल", "सरसों", "फॉर्च्यून", "oil", "mustard", "fortune", "tel"] },
  { sku: "GRO-DAIRY-007", name_en: "Amul Masti Spiced Buttermilk 200 ml", name_hi: "अमूल मस्ती मसाला छाछ 200 मिली", category: "dairy", unit_label: "200 ml", list_price_minor: 1500, baseline_stock: 35, tax_bp: 0, synonyms: ["छाछ", "मट्ठा", "मसाला छाछ", "chaas", "chhaas", "buttermilk", "mattha", "masti", "spiced buttermilk"] },
  { sku: "GRO-DAIRY-008", name_en: "Amul Shrikhand Kesar 500 g", name_hi: "अमूल श्रीखंड केसर 500 ग्राम", category: "dairy", unit_label: "500 g", list_price_minor: 13000, baseline_stock: 30, tax_bp: 1200, synonyms: ["श्रीखंड", "केसर श्रीखंड", "shrikhand", "kesar shrikhand", "sweet curd", "amul shrikhand"] },
  { sku: "GRO-DAIRY-009", name_en: "Amul Masti Dahi (Curd) 400 g", name_hi: "अमूल मस्ती दही 400 ग्राम", category: "dairy", unit_label: "400 g", list_price_minor: 3500, baseline_stock: 28, tax_bp: 0, synonyms: ["दही", "मस्ती दही", "dahi", "curd", "yogurt", "amul dahi", "masti dahi"] },
  { sku: "GRO-DAIRY-010", name_en: "Amul Pure Cow Ghee 500 ml", name_hi: "अमूल शुद्ध गाय का घी 500 मिली", category: "dairy", unit_label: "500 ml", list_price_minor: 32500, baseline_stock: 22, tax_bp: 1200, synonyms: ["घी", "गाय का घी", "शुद्ध घी", "ghee", "ghi", "cow ghee", "amul ghee", "desi ghee"] },
  { sku: "GRO-DAIRY-011", name_en: "Mother Dairy Full Cream Milk 1 L", name_hi: "मदर डेयरी फुल क्रीम दूध 1 लीटर", category: "dairy", unit_label: "1 L", list_price_minor: 6800, baseline_stock: 30, tax_bp: 0, synonyms: ["दूध", "फुल क्रीम दूध", "doodh", "milk", "mother dairy", "full cream milk"] },
  { sku: "GRO-DAIRY-012", name_en: "Mother Dairy Classic Dahi 400 g", name_hi: "मदर डेयरी क्लासिक दही 400 ग्राम", category: "dairy", unit_label: "400 g", list_price_minor: 3500, baseline_stock: 24, tax_bp: 0, synonyms: ["दही", "मदर डेयरी दही", "dahi", "curd", "mother dairy dahi"] },
  { sku: "GRO-DAIRY-013", name_en: "Amul Cheese Slices (Pack of 10) 200 g", name_hi: "अमूल चीज़ स्लाइस (10 का पैक) 200 ग्राम", category: "dairy", unit_label: "200 g", list_price_minor: 14000, baseline_stock: 25, tax_bp: 1200, synonyms: ["चीज़", "चीज़ स्लाइस", "cheese", "chij", "cheese slices", "amul cheese"] },
  { sku: "GRO-DAIRY-014", name_en: "Amul Processed Cheese Block 200 g", name_hi: "अमूल प्रोसेस्ड चीज़ ब्लॉक 200 ग्राम", category: "dairy", unit_label: "200 g", list_price_minor: 12500, baseline_stock: 20, tax_bp: 1200, synonyms: ["चीज़", "चीज़ ब्लॉक", "cheese", "cheese block", "cube cheese"] },
  { sku: "GRO-DAIRY-015", name_en: "Amul Fresh Cream 250 ml", name_hi: "अमूल फ्रेश क्रीम 250 मिली", category: "dairy", unit_label: "250 ml", list_price_minor: 6700, baseline_stock: 18, tax_bp: 1200, synonyms: ["मलाई", "क्रीम", "ताज़ा क्रीम", "cream", "fresh cream", "malai", "whipping cream"] },
  { sku: "GRO-DAIRY-016", name_en: "Amul Kool Kesar Flavoured Milk 180 ml", name_hi: "अमूल कूल केसर फ्लेवर्ड दूध 180 मिली", category: "dairy", unit_label: "180 ml", list_price_minor: 2500, baseline_stock: 32, tax_bp: 1200, synonyms: ["केसर दूध", "फ्लेवर्ड दूध", "doodh", "kesar milk", "flavoured milk", "amul kool", "kesar"] },
  { sku: "GRO-DAIRY-017", name_en: "Mother Dairy Rabri 200 g", name_hi: "मदर डेयरी रबड़ी 200 ग्राम", category: "dairy", unit_label: "200 g", list_price_minor: 8000, baseline_stock: 22, tax_bp: 1200, synonyms: ["रबड़ी", "खीर", "rabri", "kheer", "sweet rabri", "mother dairy rabri"] },
  { sku: "GRO-DAIRY-018", name_en: "Milky Mist Greek Yogurt Natural 100 g", name_hi: "मिल्की मिस्ट ग्रीक योगर्ट 100 ग्राम", category: "dairy", unit_label: "100 g", list_price_minor: 4500, baseline_stock: 15, tax_bp: 0, synonyms: ["योगर्ट", "ग्रीक योगर्ट", "greek yogurt", "yogurt", "curd", "milky mist"] },
  { sku: "GRO-DAIRY-019", name_en: "Epigamia Greek Yogurt Blueberry 90 g", name_hi: "एपिगामिया ग्रीक योगर्ट ब्लूबेरी 90 ग्राम", category: "dairy", unit_label: "90 g", list_price_minor: 5000, baseline_stock: 16, tax_bp: 1200, synonyms: ["योगर्ट", "ब्लूबेरी योगर्ट", "epigamia", "flavored yogurt", "blueberry yogurt"] },
  { sku: "GRO-DAIRY-020", name_en: "Nestle A+ Slim Skimmed Milk 1 L", name_hi: "नेस्ले ए+ स्लिम टोंड मिल्क 1 लीटर", category: "dairy", unit_label: "1 L", list_price_minor: 8500, baseline_stock: 18, tax_bp: 0, synonyms: ["दूध", "स्लिम मिल्क", "doodh", "nestle milk", "slim milk", "toned milk", "tetra pack milk"] },
  { sku: "GRO-DAIRY-021", name_en: "Amul Lassi (Tetra Pack) 250 ml", name_hi: "अमूल लस्सी 250 मिली", category: "dairy", unit_label: "250 ml", list_price_minor: 2000, baseline_stock: 40, tax_bp: 0, synonyms: ["लस्सी", "अमूल लस्सी", "lassi", "amul lassi", "sweet lassi"] },
  { sku: "GRO-DAIRY-022", name_en: "Gowardhan Desi Ghee 1 L", name_hi: "गोवर्धन देसी घी 1 लीटर", category: "dairy", unit_label: "1 L", list_price_minor: 65000, baseline_stock: 14, tax_bp: 1200, synonyms: ["घी", "गोवर्धन घी", "gowardhan ghee", "cow ghee", "desi ghee"] },
  { sku: "GRO-DAIRY-023", name_en: "Amul Mithai Mate (Condensed Milk) 200 g", name_hi: "अमूल मिठाई मेट (कंडेंस्ड मिल्क) 200 ग्राम", category: "dairy", unit_label: "200 g", list_price_minor: 6200, baseline_stock: 18, tax_bp: 1200, synonyms: ["कंडेंस्ड मिल्क", "मिठाई मेट", "condensed milk", "mithai mate", "amul mithai mate"] },
  { sku: "GRO-DAIRY-024", name_en: "Britannia Cheese Block 200 g", name_hi: "ब्रिटानिया चीज़ ब्लॉक 200 ग्राम", category: "dairy", unit_label: "200 g", list_price_minor: 13000, baseline_stock: 0, tax_bp: 1200, synonyms: ["चीज़", "ब्रिटानिया चीज़", "britannia cheese", "cheese block"], listed: false },
  { sku: "GRO-STPL-011", name_en: "Aashirvaad Sharbati Atta 5 kg", name_hi: "आशीर्वाद शरबती आटा 5 किलो", category: "staples", unit_label: "5 kg", list_price_minor: 31000, baseline_stock: 20, tax_bp: 500, synonyms: ["आटा", "शरबती आटा", "गेंहू का आटा", "atta", "aata", "sharbati atta", "wheat flour", "flour"] },
  { sku: "GRO-STPL-012", name_en: "Fortune Besan (Gram Flour) 500 g", name_hi: "फॉर्च्यून बेसन 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 5800, baseline_stock: 25, tax_bp: 500, synonyms: ["बेसन", "चने का आटा", "besan", "gram flour", "chana flour"] },
  { sku: "GRO-STPL-013", name_en: "Tata Sampann Moong Dal (Split) 500 g", name_hi: "टाटा सम्पन्न मूंग दाल (धुली) 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 7800, baseline_stock: 22, tax_bp: 500, synonyms: ["मूंग दाल", "पीली दाल", "moong dal", "mung dal", "yellow dal", "split moong"] },
  { sku: "GRO-STPL-014", name_en: "Tata Sampann Urad Dal (White Split) 500 g", name_hi: "टाटा सम्पन्न उड़द दाल 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 8600, baseline_stock: 18, tax_bp: 500, synonyms: ["उड़द दाल", "धुली उड़द", "urad dal", "udad dal", "white urad"] },
  { sku: "GRO-STPL-015", name_en: "Tata Sampann Masoor Dal 500 g", name_hi: "टाटा सम्पन्न मसूर दाल 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 6500, baseline_stock: 20, tax_bp: 500, synonyms: ["मसूर दाल", "लाल दाल", "masoor dal", "masur dal", "red lentil"] },
  { sku: "GRO-STPL-016", name_en: "Kabuli Chana (Chickpeas) 500 g", name_hi: "काबुली चना (छोले) 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 8200, baseline_stock: 16, tax_bp: 500, synonyms: ["चना", "काबुली चना", "छोले", "chickpeas", "kabuli chana", "chhole", "chole"] },
  { sku: "GRO-STPL-017", name_en: "Kala Chana (Brown Gram) 500 g", name_hi: "काला चना 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 5400, baseline_stock: 22, tax_bp: 500, synonyms: ["काला चना", "चना", "kala chana", "black gram", "brown chana"] },
  { sku: "GRO-STPL-018", name_en: "Daawat Rozana Super Basmati Rice 1 kg", name_hi: "दावत रोज़ाना सुपर बासमती चावल 1 किलो", category: "staples", unit_label: "1 kg", list_price_minor: 9500, baseline_stock: 25, tax_bp: 500, synonyms: ["चावल", "बासमती चावल", "chawal", "rice", "basmati rice", "daawat rice"] },
  { sku: "GRO-STPL-019", name_en: "Sona Masoori Raw Rice 5 kg", name_hi: "सोना मसूरी कच्चा चावल 5 किलो", category: "staples", unit_label: "5 kg", list_price_minor: 34500, baseline_stock: 15, tax_bp: 500, synonyms: ["चावल", "सोना मसूरी", "sona masoori", "raw rice", "rice", "chawal"] },
  { sku: "GRO-STPL-020", name_en: "Tata Rock Salt (Sendha Namak) 1 kg", name_hi: "टाटा सेंधा नमक 1 किलो", category: "staples", unit_label: "1 kg", list_price_minor: 9000, baseline_stock: 30, tax_bp: 500, synonyms: ["सेंधा नमक", "व्रत का नमक", "sendha namak", "rock salt", "vrat namak", "pink salt"] },
  { sku: "GRO-STPL-021", name_en: "Saffola Gold Pro Healthy Lifestyle Edible Oil 1 L", name_hi: "सaffola गोल्ड प्रो एडिबल तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 17500, baseline_stock: 24, tax_bp: 500, synonyms: ["तेल", "कुकिंग ऑयल", "saffola", "saffola gold", "edible oil", "cooking oil", "tel"] },
  { sku: "GRO-STPL-022", name_en: "Dhara Mustard Oil 1 L", name_hi: "धारा कच्ची घानी सरसों तेल 1 लीटर", category: "staples", unit_label: "1 L", list_price_minor: 15500, baseline_stock: 20, tax_bp: 500, synonyms: ["सरसों तेल", "कच्ची घानी", "dhara oil", "mustard oil", "kachi ghani", "sarson tel"] },
  { sku: "GRO-STPL-023", name_en: "Organic Jaggery Powder (Gur) 500 g", name_hi: "ऑर्गेनिक गुड़ पाउडर 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 6500, baseline_stock: 22, tax_bp: 500, synonyms: ["गुड़", "गुड़ पाउडर", "jaggery", "gur", "gud", "jaggery powder", "organic jaggery"] },
  { sku: "GRO-STPL-024", name_en: "Sooji (Semolina / Rava) 500 g", name_hi: "सूजी (रवा) 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 3800, baseline_stock: 30, tax_bp: 500, synonyms: ["सूजी", "रवा", "sooji", "suji", "rava", "semolina"] },
  { sku: "GRO-STPL-025", name_en: "Maida (Refined Wheat Flour) 500 g", name_hi: "मैदा 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 3500, baseline_stock: 28, tax_bp: 500, synonyms: ["मैदा", "रिफाइंड आटा", "maida", "all purpose flour", "refined flour"] },
  { sku: "GRO-STPL-026", name_en: "Sabudana (Tapioca Sago) 500 g", name_hi: "साबूदाना 500 ग्राम", category: "staples", unit_label: "500 g", list_price_minor: 5500, baseline_stock: 25, tax_bp: 500, synonyms: ["स", "ा", "ब", "ू", "द", "ा", "न", "ा", "sabudana", "sago", "tapioca"] },
  { sku: "GRO-STPL-027", name_en: "Bambino Roasted Vermicelli 400 g", name_hi: "बम्बिनो भुनी हुई सेवई 400 ग्राम", category: "staples", unit_label: "400 g", list_price_minor: 4800, baseline_stock: 20, tax_bp: 500, synonyms: ["सेवई", "सेवइयां", "vermicelli", "sewai", "bambino", "roasted vermicelli"] },
  { sku: "GRO-STPL-028", name_en: "Fortune Soya Badi (Chunks) 200 g", name_hi: "फॉर्च्यून सोया बड़ी (चंक्स) 200 ग्राम", category: "staples", unit_label: "200 g", list_price_minor: 5000, baseline_stock: 25, tax_bp: 500, synonyms: ["सोया बड़ी", "सोयाबीन", "soya chunks", "soya badi", "soyabean", "fortune soya"] },
  { sku: "GRO-PROD-010", name_en: "Cauliflower (Phool Gobhi) 1 pc", name_hi: "फूल गोभी 1 नग", category: "produce", unit_label: "1 pc", list_price_minor: 4500, baseline_stock: 20, tax_bp: 0, synonyms: ["गोभी", "फूल गोभी", "cauliflower", "gobhi", "phool gobhi"] },
  { sku: "GRO-PROD-011", name_en: "Cabbage (Patta Gobhi) 1 pc", name_hi: "पत्ता गोभी 1 नग", category: "produce", unit_label: "1 pc", list_price_minor: 3500, baseline_stock: 25, tax_bp: 0, synonyms: ["पत्ता गोभी", "बंद गोभी", "cabbage", "patta gobhi", "bandh gobhi"] },
  { sku: "GRO-PROD-012", name_en: "Lady Finger (Bhindi / Okra) 500 g", name_hi: "भिंडी 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 3600, baseline_stock: 20, tax_bp: 0, synonyms: ["भ", "ि", "ं", "ड", "ी", "bhindi", "okra", "lady finger", "bhendi"] },
  { sku: "GRO-PROD-013", name_en: "Green Capsicum (Shimla Mirch) 500 g", name_hi: "शिमला मिर्च 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 4800, baseline_stock: 18, tax_bp: 0, synonyms: ["श", "ि", "म", "ल", "ा", " ", "म", "ि", "र", "्", "च", "capsicum", "shimla mirch", "green pepper"] },
  { sku: "GRO-PROD-014", name_en: "Carrot (Gajar) 500 g", name_hi: "गाजर 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 3200, baseline_stock: 22, tax_bp: 0, synonyms: ["ग", "ा", "ज", "र", "gajar", "carrot", "red carrot"] },
  { sku: "GRO-PROD-015", name_en: "Cucumber (Kheera) 500 g", name_hi: "खीरा 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 2800, baseline_stock: 24, tax_bp: 0, synonyms: ["खीरा", "ककड़ी", "kheera", "cucumber", "kakdi"] },
  { sku: "GRO-PROD-016", name_en: "Spinach (Palak) 250 g", name_hi: "पालक 250 ग्राम", category: "produce", unit_label: "250 g", list_price_minor: 2200, baseline_stock: 15, tax_bp: 0, synonyms: ["प", "ा", "ल", "क", "palak", "spinach", "green leaves"] },
  { sku: "GRO-PROD-017", name_en: "Green Peas (Matar) 500 g", name_hi: "हरी मटर 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 5500, baseline_stock: 18, tax_bp: 0, synonyms: ["मटर", "हरी मटर", "matar", "peas", "green peas"] },
  { sku: "GRO-PROD-018", name_en: "Bottle Gourd (Lauki / Ghiya) 1 pc", name_hi: "लौकी 1 नग", category: "produce", unit_label: "1 pc", list_price_minor: 3800, baseline_stock: 16, tax_bp: 0, synonyms: ["लौकी", "घिया", "lauki", "ghiya", "bottle gourd"] },
  { sku: "GRO-PROD-019", name_en: "Bitter Gourd (Karela) 500 g", name_hi: "करेला 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 4200, baseline_stock: 14, tax_bp: 0, synonyms: ["क", "र", "े", "ल", "ा", "karela", "bitter gourd"] },
  { sku: "GRO-PROD-020", name_en: "Fresh Mint Leaves (Pudina) 100 g", name_hi: "पुदीना 100 ग्राम", category: "produce", unit_label: "100 g", list_price_minor: 1500, baseline_stock: 15, tax_bp: 0, synonyms: ["प", "ु", "द", "ी", "न", "ा", "pudina", "mint", "mint leaves"] },
  { sku: "GRO-PROD-021", name_en: "Beetroot (Chukandar) 500 g", name_hi: "चुकंदर 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 3000, baseline_stock: 18, tax_bp: 0, synonyms: ["च", "ु", "क", "ं", "द", "र", "beetroot", "chukandar"] },
  { sku: "GRO-PROD-022", name_en: "Fresh Coconut (Nariyal) 1 pc", name_hi: "पानी वाला नारियल 1 नग", category: "produce", unit_label: "1 pc", list_price_minor: 4000, baseline_stock: 25, tax_bp: 0, synonyms: ["न", "ा", "र", "ि", "य", "ल", "nariyal", "coconut", "water coconut"] },
  { sku: "GRO-PROD-023", name_en: "Sweet Corn (2 pcs)", name_hi: "स्वीट कॉर्न (2 नग)", category: "produce", unit_label: "2 pcs", list_price_minor: 4500, baseline_stock: 18, tax_bp: 0, synonyms: ["मक्का", "स्वीट कॉर्न", "भुट्टा", "corn", "sweet corn", "bhutta"] },
  { sku: "GRO-PROD-024", name_en: "Button Mushrooms 200 g", name_hi: "बटन मशरूम 200 ग्राम", category: "produce", unit_label: "200 g", list_price_minor: 5200, baseline_stock: 14, tax_bp: 0, synonyms: ["म", "श", "र", "ू", "म", "mushroom", "button mushroom"] },
  { sku: "GRO-PROD-025", name_en: "Royal Gala Apple (4 pcs)", name_hi: "रॉयल गाला सेब (4 नग)", category: "produce", unit_label: "4 pcs", list_price_minor: 16000, baseline_stock: 20, tax_bp: 0, synonyms: ["स", "े", "ब", "seb", "apple", "gala apple"] },
  { sku: "GRO-PROD-026", name_en: "Pomegranate (Anar) 1 kg", name_hi: "अनार 1 किलो", category: "produce", unit_label: "1 kg", list_price_minor: 21000, baseline_stock: 15, tax_bp: 0, synonyms: ["अ", "न", "ा", "र", "anar", "pomegranate"] },
  { sku: "GRO-PROD-027", name_en: "Nagpur Orange (Santra) 1 kg", name_hi: "नागपुर संतरा 1 किलो", category: "produce", unit_label: "1 kg", list_price_minor: 9500, baseline_stock: 18, tax_bp: 0, synonyms: ["संतरा", "नारंगी", "santra", "orange", "oranges"] },
  { sku: "GRO-PROD-028", name_en: "Papaya Semi-Ripe (1 pc)", name_hi: "पपीता 1 नग", category: "produce", unit_label: "1 pc", list_price_minor: 6500, baseline_stock: 16, tax_bp: 0, synonyms: ["प", "प", "ी", "त", "ा", "papaya", "papita"] },
  { sku: "GRO-PROD-029", name_en: "Sweet Lime (Mosambi) 1 kg", name_hi: "मौसमी 1 किलो", category: "produce", unit_label: "1 kg", list_price_minor: 9000, baseline_stock: 18, tax_bp: 0, synonyms: ["मौसमी", "मुसम्मी", "mosambi", "sweet lime"] },
  { sku: "GRO-PROD-030", name_en: "Seedless Green Grapes (Angoor) 500 g", name_hi: "अंगूर 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 7500, baseline_stock: 14, tax_bp: 0, synonyms: ["अ", "ं", "ग", "ू", "र", "angoor", "grapes", "green grapes"] },
  { sku: "GRO-PROD-031", name_en: "Watermelon (Tarbooz) 1 pc", name_hi: "तरबूज 1 नग", category: "produce", unit_label: "1 pc", list_price_minor: 8500, baseline_stock: 12, tax_bp: 0, synonyms: ["त", "र", "ब", "ू", "ज", "tarbooz", "watermelon"] },
  { sku: "GRO-PROD-032", name_en: "Elaichi Banana (Yellaki) 500 g", name_hi: "इलायची केला 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 6500, baseline_stock: 15, tax_bp: 0, synonyms: ["इ", "ल", "ा", "य", "च", "ी", " ", "क", "े", "ल", "ा", "elaichi banana", "yellaki", "small banana", "kela"] },
  { sku: "GRO-PROD-033", name_en: "Custard Apple (Sitaphal) 500 g", name_hi: "सीताफल 500 ग्राम", category: "produce", unit_label: "500 g", list_price_minor: 11000, baseline_stock: 10, tax_bp: 0, synonyms: ["सीताफल", "शरीफा", "sitaphal", "custard apple", "sharifa"] },
  { sku: "GRO-PROD-034", name_en: "Baby Corn 200 g", name_hi: "बेबी कॉर्न 200 ग्राम", category: "produce", unit_label: "200 g", list_price_minor: 4800, baseline_stock: 15, tax_bp: 0, synonyms: ["ब", "े", "ब", "ी", " ", "क", "ॉ", "र", "्", "न", "baby corn", "corn"] },
  { sku: "GRO-SNCK-007", name_en: "Kurkure Masala Munch 82 g", name_hi: "कुरकुरे मसाला मंच 82 ग्राम", category: "snacks", unit_label: "82 g", list_price_minor: 2000, baseline_stock: 35, tax_bp: 1800, synonyms: ["कुरकुरे", "kurkure", "masala munch", "crisps"] },
  { sku: "GRO-SNCK-008", name_en: "Britannia Good Day Cashew Biscuits 200 g", name_hi: "ब्रिटानिया गुड डे काजू बिस्किट 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 4500, baseline_stock: 30, tax_bp: 1800, synonyms: ["गुड डे", "काजू बिस्किट", "good day", "cashew biscuit", "cookies"] },
  { sku: "GRO-SNCK-009", name_en: "Britannia Marie Gold Biscuits 250 g", name_hi: "ब्रिटानिया मारी गोल्ड बिस्किट 250 ग्राम", category: "snacks", unit_label: "250 g", list_price_minor: 3800, baseline_stock: 32, tax_bp: 1800, synonyms: ["मारी गोल्ड", "चाय बिस्किट", "marie gold", "marie biscuit", "tea biscuit"] },
  { sku: "GRO-SNCK-010", name_en: "Cadbury Dairy Milk Chocolate 50 g", name_hi: "कैडबरी डेयरी मिल्क चॉकलेट 50 ग्राम", category: "snacks", unit_label: "50 g", list_price_minor: 4500, baseline_stock: 40, tax_bp: 1800, synonyms: ["चॉकलेट", "डेयरी मिल्क", "chocolate", "dairy milk", "cadbury", "mithai"] },
  { sku: "GRO-SNCK-011", name_en: "Nestle KitKat 4-Finger 38.5 g", name_hi: "नेस्ले किटकैट 38.5 ग्राम", category: "snacks", unit_label: "38.5 g", list_price_minor: 3000, baseline_stock: 35, tax_bp: 1800, synonyms: ["किटकैट", "चॉकलेट", "kitkat", "chocolate", "wafer chocolate"] },
  { sku: "GRO-SNCK-012", name_en: "Oreo Vanilla Cream Biscuits 120 g", name_hi: "ओरियो वैनिला क्रीम बिस्किट 120 ग्राम", category: "snacks", unit_label: "120 g", list_price_minor: 3500, baseline_stock: 28, tax_bp: 1800, synonyms: ["ओरियो", "क्रीम बिस्किट", "oreo", "cream biscuit", "cookies"] },
  { sku: "GRO-SNCK-013", name_en: "Sunfeast Dark Fantasy Choco Fills 300 g", name_hi: "सनफीस्ट डार्क फैंटेसी चोको फिल्स 300 ग्राम", category: "snacks", unit_label: "300 g", list_price_minor: 12000, baseline_stock: 22, tax_bp: 1800, synonyms: ["डार्क फैंटेसी", "चॉकलेट बिस्किट", "dark fantasy", "choco fills", "sunfeast"] },
  { sku: "GRO-SNCK-014", name_en: "Haldiram's Khatta Meetha Namkeen 200 g", name_hi: "हल्दीराम खट्टा मीठा नमकीन 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 5500, baseline_stock: 25, tax_bp: 1200, synonyms: ["खट्टा मीठा", "नमकीन", "khatta meetha", "namkeen", "haldirams"] },
  { sku: "GRO-SNCK-015", name_en: "Haldiram's Moong Dal 200 g", name_hi: "हल्दीराम नमकीन मूंग दाल 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 6000, baseline_stock: 26, tax_bp: 1200, synonyms: ["मूंग दाल नमकीन", "moong dal namkeen", "salted moong dal", "haldiram"] },
  { sku: "GRO-SNCK-016", name_en: "Yippee! Magic Masala Noodles 240 g", name_hi: "यिप्पी मैजिक मसाला नूडल्स 240 ग्राम", category: "snacks", unit_label: "4 x 60 g", list_price_minor: 5500, baseline_stock: 24, tax_bp: 1200, synonyms: ["यिप्पी", "नूडल्स", "yippee", "noodles", "instant noodles"] },
  { sku: "GRO-SNCK-017", name_en: "Lay's Classic Salted Potato Chips 52 g", name_hi: "लेज़ क्लासिक साल्टेड चिप्स 52 ग्राम", category: "snacks", unit_label: "52 g", list_price_minor: 2000, baseline_stock: 35, tax_bp: 1800, synonyms: ["साल्टेड चिप्स", "lays salted", "potato chips", "classic salted"] },
  { sku: "GRO-SNCK-018", name_en: "Bingo! Tedhe Medhe Masala Tadka 90 g", name_hi: "बिंगो टेढ़े मेढ़े 90 ग्राम", category: "snacks", unit_label: "90 g", list_price_minor: 2000, baseline_stock: 30, tax_bp: 1800, synonyms: ["टेढ़े मेढ़े", "bingo", "tedhe medhe", "snacks"] },
  { sku: "GRO-SNCK-019", name_en: "Act II Butter Popcorn 150 g", name_hi: "एक्ट II बटर पॉपकॉर्न 150 ग्राम", category: "snacks", unit_label: "150 g", list_price_minor: 4000, baseline_stock: 25, tax_bp: 1200, synonyms: ["पॉपकॉर्न", "popcorn", "act 2", "butter popcorn"] },
  { sku: "GRO-SNCK-020", name_en: "Bikaji Bhujia Sev 400 g", name_hi: "बीकाजी भुजिया सेव 400 ग्राम", category: "snacks", unit_label: "400 g", list_price_minor: 11000, baseline_stock: 20, tax_bp: 1200, synonyms: ["भुजिया", "सेव", "bikaji", "bhujia", "sev", "namkeen"] },
  { sku: "GRO-SNCK-021", name_en: "Britannia Bourbon Chocolate Cream Biscuits 150 g", name_hi: "ब्रिटानिया बॉर्बन बिस्किट 150 ग्राम", category: "snacks", unit_label: "150 g", list_price_minor: 3500, baseline_stock: 28, tax_bp: 1800, synonyms: ["बॉर्बन", "चॉकलेट बिस्किट", "bourbon", "biscuit", "cream biscuit"] },
  { sku: "GRO-SNCK-022", name_en: "Parle Hide & Seek Chocolate Chip Cookies 120 g", name_hi: "पारले हाइड एंड सीक 120 ग्राम", category: "snacks", unit_label: "120 g", list_price_minor: 3500, baseline_stock: 30, tax_bp: 1800, synonyms: ["हाइड एंड सीक", "hide and seek", "choco chip", "cookies"] },
  { sku: "GRO-SNCK-023", name_en: "Roasted & Salted California Almonds (Badam) 200 g", name_hi: "रोस्टेड बादाम 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 24000, baseline_stock: 22, tax_bp: 1200, synonyms: ["बादाम", "रोस्टेड बादाम", "almonds", "badam", "roasted badam", "dry fruit"] },
  { sku: "GRO-SNCK-024", name_en: "Whole Cashews (Kaju) 200 g", name_hi: "काजू 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 22500, baseline_stock: 20, tax_bp: 1200, synonyms: ["काजू", "cashews", "kaju", "dry fruit"] },
  { sku: "GRO-SNCK-025", name_en: "Kishmish (Raisins) 200 g", name_hi: "किशमिश 200 ग्राम", category: "snacks", unit_label: "200 g", list_price_minor: 9500, baseline_stock: 25, tax_bp: 1200, synonyms: ["किशमिश", "दाख", "kishmish", "raisins", "dry fruit"] },
  { sku: "GRO-SNCK-026", name_en: "Phool Makhana (Fox Nuts) 100 g", name_hi: "फूल मखाना 100 ग्राम", category: "snacks", unit_label: "100 g", list_price_minor: 12500, baseline_stock: 22, tax_bp: 500, synonyms: ["मखाना", "फूल मखाना", "makhana", "fox nuts", "phool makhana"] },
  { sku: "GRO-SNCK-027", name_en: "Haldiram's Soan Papdi 250 g", name_hi: "हल्दीराम सोहन पापड़ी 250 ग्राम", category: "snacks", unit_label: "250 g", list_price_minor: 8500, baseline_stock: 18, tax_bp: 1800, synonyms: ["सोन पापड़ी", "मिठाई", "soan papdi", "mithai", "haldiram sweets"] },
  { sku: "GRO-SNCK-028", name_en: "Kellogg's Corn Flakes 475 g", name_hi: "केलॉग्स कॉर्न फ्लेक्स 475 ग्राम", category: "snacks", unit_label: "475 g", list_price_minor: 21000, baseline_stock: 15, tax_bp: 1800, synonyms: ["कॉर्न फ्लेक्स", "सीरियल", "corn flakes", "kelloggs", "cereal", "breakfast"] },
  { sku: "GRO-SNCK-029", name_en: "Quaker Rolled Oats 1 kg", name_hi: "क्वेकर रोल्ड ओट्स 1 किलो", category: "snacks", unit_label: "1 kg", list_price_minor: 19500, baseline_stock: 20, tax_bp: 1200, synonyms: ["ओट्स", "oats", "quaker oats", "breakfast cereal"] },
  { sku: "GRO-SNCK-030", name_en: "Cadbury 5 Star Chocolate Bar 40 g", name_hi: "कैडबरी 5 स्टार 40 ग्राम", category: "snacks", unit_label: "40 g", list_price_minor: 2000, baseline_stock: 35, tax_bp: 1800, synonyms: ["5 स्टार", "चॉकलेट", "5 star", "chocolate", "cadbury"] },
  { sku: "GRO-SNCK-031", name_en: "Ching's Secret Veg Hakka Noodles 150 g", name_hi: "चिंग्स सीक्रेट हक्का नूडल्स 150 ग्राम", category: "snacks", unit_label: "150 g", list_price_minor: 4500, baseline_stock: 24, tax_bp: 1200, synonyms: ["हक्का नूडल्स", "chings", "hakka noodles", "noodles"] },
  { sku: "GRO-SNCK-032", name_en: "Maggi Nutri-licious Masala Oats Noodles 292 g", name_hi: "मैगी ओट्स नूडल्स 292 ग्राम", category: "snacks", unit_label: "4 x 73 g", list_price_minor: 11000, baseline_stock: 16, tax_bp: 1200, synonyms: ["मैगी ओट्स नूडल्स", "maggi oats", "oats noodles", "maggi"] },
  { sku: "GRO-BEVG-006", name_en: "Red Label Strong Tea 500 g", name_hi: "ब्रूक बॉन्ड रेड लेबल चाय 500 ग्राम", category: "beverages", unit_label: "500 g", list_price_minor: 27000, baseline_stock: 22, tax_bp: 500, synonyms: ["चाय", "रेड लेबल", "red label", "tea", "chai", "chaipatti"] },
  { sku: "GRO-BEVG-007", name_en: "Taj Mahal Premium Tea 500 g", name_hi: "ताज महल प्रीमियम चाय 500 ग्राम", category: "beverages", unit_label: "500 g", list_price_minor: 38000, baseline_stock: 18, tax_bp: 500, synonyms: ["ताज महल चाय", "चाय", "taj mahal tea", "tea", "chai"] },
  { sku: "GRO-BEVG-008", name_en: "Wagh Bakri Premium Leaf Tea 500 g", name_hi: "वाघ बकरी चाय 500 ग्राम", category: "beverages", unit_label: "500 g", list_price_minor: 29500, baseline_stock: 20, tax_bp: 500, synonyms: ["वाघ बकरी चाय", "wagh bakri", "tea", "chai"] },
  { sku: "GRO-BEVG-009", name_en: "Bru Gold Instant Coffee 100 g Jar", name_hi: "ब्रू गोल्ड इंस्टेंट कॉफी 100 ग्राम", category: "beverages", unit_label: "100 g", list_price_minor: 31000, baseline_stock: 16, tax_bp: 500, synonyms: ["कॉफी", "ब्रू गोल्ड", "bru", "bru gold", "coffee", "instant coffee"] },
  { sku: "GRO-BEVG-010", name_en: "Nescafe Sunrise Instant Coffee-Chicory 100 g", name_hi: "नेस्केफे सनराइज कॉफी 100 ग्राम", category: "beverages", unit_label: "100 g", list_price_minor: 18500, baseline_stock: 20, tax_bp: 500, synonyms: ["सनराइज कॉफी", "कॉफी", "sunrise", "nescafe sunrise", "coffee"] },
  { sku: "GRO-BEVG-011", name_en: "Thums Up Soft Drink Can 300 ml", name_hi: "थम्स अप कैन 300 मिली", category: "beverages", unit_label: "300 ml", list_price_minor: 4000, baseline_stock: 35, tax_bp: 2800, synonyms: ["थम्स अप", "कोल्ड ड्रिंक", "thums up", "cold drink", "soda", "cola"] },
  { sku: "GRO-BEVG-012", name_en: "Coca-Cola Original Taste Can 300 ml", name_hi: "कोका कोला कैन 300 मिली", category: "beverages", unit_label: "300 ml", list_price_minor: 4000, baseline_stock: 35, tax_bp: 2800, synonyms: ["कोका कोला", "कोक", "coca cola", "coke", "soft drink", "cold drink"] },
  { sku: "GRO-BEVG-013", name_en: "Sprite Lime Soft Drink Can 300 ml", name_hi: "स्प्राइट लेमन कैन 300 मिली", category: "beverages", unit_label: "300 ml", list_price_minor: 4000, baseline_stock: 32, tax_bp: 2800, synonyms: ["स्प्राइट", "कोल्ड ड्रिंक", "sprite", "soft drink", "cold drink", "lemon soda"] },
  { sku: "GRO-BEVG-014", name_en: "Frooti Mango Drink 1.2 L", name_hi: "फ्रूटी मैंगो ड्रिंक 1.2 लीटर", category: "beverages", unit_label: "1.2 L", list_price_minor: 7000, baseline_stock: 24, tax_bp: 1200, synonyms: ["फ्रूटी", "मैंगो जूस", "frooti", "mango drink", "mango juice"] },
  { sku: "GRO-BEVG-015", name_en: "Maaza Mango Drink Bottle 1.2 L", name_hi: "माज़ा मैंगो ड्रिंक 1.2 लीटर", category: "beverages", unit_label: "1.2 L", list_price_minor: 7500, baseline_stock: 22, tax_bp: 1200, synonyms: ["माज़ा", "मैंगो जूस", "maaza", "mango juice", "mango drink"] },
  { sku: "GRO-BEVG-016", name_en: "Real Fruit Power Mixed Fruit Juice 1 L", name_hi: "रियल मिक्स्ड फ्रूट जूस 1 लीटर", category: "beverages", unit_label: "1 L", list_price_minor: 12500, baseline_stock: 20, tax_bp: 1200, synonyms: ["रियल जूस", "फ्रूट जूस", "real juice", "mixed fruit juice", "juice"] },
  { sku: "GRO-BEVG-017", name_en: "Paper Boat Aamras Mango Juice 250 ml", name_hi: "पेपर बोट आमरस 250 मिली", category: "beverages", unit_label: "250 ml", list_price_minor: 3500, baseline_stock: 28, tax_bp: 1200, synonyms: ["आमरस", "पेपर बोट", "paper boat", "aamras", "mango juice"] },
  { sku: "GRO-BEVG-018", name_en: "Kinley Packaged Drinking Water 1 L", name_hi: "किन्ले मिनरल वाटर 1 लीटर", category: "beverages", unit_label: "1 L", list_price_minor: 2000, baseline_stock: 50, tax_bp: 1800, synonyms: ["पानी", "मिनरल वाटर", "water", "mineral water", "kinley", "drinking water"] },
  { sku: "GRO-BEVG-019", name_en: "Bisleri Club Soda 750 ml", name_hi: "बिसलेरी क्लब सोडा 750 मिली", category: "beverages", unit_label: "750 ml", list_price_minor: 2000, baseline_stock: 30, tax_bp: 2800, synonyms: ["सोडा", "क्लब सोडा", "soda", "club soda", "bisleri soda"] },
  { sku: "GRO-BEVG-020", name_en: "Red Bull Energy Drink 250 ml", name_hi: "रेड बुल एनर्जी ड्रिंक 250 मिली", category: "beverages", unit_label: "250 ml", list_price_minor: 12500, baseline_stock: 25, tax_bp: 2800, synonyms: ["रेड बुल", "एनर्जी ड्रिंक", "red bull", "energy drink"] },
  { sku: "GRO-BEVG-021", name_en: "Hamdard Rooh Afza Sharbat 750 ml", name_hi: "हमदर्द रूह अफ़ज़ा शरबत 750 मिली", category: "beverages", unit_label: "750 ml", list_price_minor: 17500, baseline_stock: 20, tax_bp: 1200, synonyms: ["रूह अफ़ज़ा", "शरबत", "गुलाब शरबत", "rooh afza", "sharbat", "rose syrup", "hamdard"] },
  { sku: "GRO-BEVG-022", name_en: "Tender Coconut Water (Fresh) 200 ml", name_hi: "ताज़ा नारियल पानी 200 मिली", category: "beverages", unit_label: "200 ml", list_price_minor: 5000, baseline_stock: 22, tax_bp: 0, synonyms: ["नारियल पानी", "coconut water", "nariyal pani", "tender coconut"] },
  { sku: "GRO-BEVG-023", name_en: "Horlicks Health Drink Classic Malt 500 g", name_hi: "हॉर्लिक्स क्लासिक माल्ट 500 ग्राम", category: "beverages", unit_label: "500 g", list_price_minor: 26500, baseline_stock: 16, tax_bp: 1800, synonyms: ["हॉर्लिक्स", "horlicks", "health drink", "malt drink"] },
  { sku: "GRO-BEVG-024", name_en: "Bournvita Cadbury Chocolate Health Drink 500 g", name_hi: "बॉर्नविटा चॉकलेट हेल्थ ड्रिंक 500 ग्राम", category: "beverages", unit_label: "500 g", list_price_minor: 24500, baseline_stock: 18, tax_bp: 1800, synonyms: ["बॉर्नविटा", "bournvita", "chocolate drink", "cadbury bournvita"] },
  { sku: "GRO-BEVG-025", name_en: "Tetley Green Tea Lemon & Honey (Pack of 25 Bags)", name_hi: "टेटली ग्रीन टी लेमन एंड हनी 25 टी बैग", category: "beverages", unit_label: "25 bags", list_price_minor: 21000, baseline_stock: 15, tax_bp: 500, synonyms: ["ग्रीन टी", "टेटली", "green tea", "tetley", "tea bags"] },
  { sku: "GRO-BAKE-003", name_en: "English Oven 100% Whole Wheat Bread 400 g", name_hi: "इंग्लिश ओवन 100% व्होल व्हीट ब्रेड 400 ग्राम", category: "bakery", unit_label: "400 g", list_price_minor: 5000, baseline_stock: 24, tax_bp: 0, synonyms: ["ब्राउन ब्रेड", "गेंहू की ब्रेड", "whole wheat bread", "brown bread", "english oven", "bread"] },
  { sku: "GRO-BAKE-004", name_en: "Britannia Sandwich White Bread 400 g", name_hi: "ब्रिटानिया सैंडविच वाइट ब्रेड 400 ग्राम", category: "bakery", unit_label: "400 g", list_price_minor: 4000, baseline_stock: 26, tax_bp: 0, synonyms: ["वाइट ब्रेड", "सैंडविच ब्रेड", "white bread", "sandwich bread", "britannia bread", "bread"] },
  { sku: "GRO-BAKE-005", name_en: "English Oven Sandwich Bread 400 g", name_hi: "इंग्लिश ओवन सैंडविच ब्रेड 400 ग्राम", category: "bakery", unit_label: "400 g", list_price_minor: 4500, baseline_stock: 20, tax_bp: 0, synonyms: ["सैंडविच ब्रेड", "sandwich bread", "english oven"] },
  { sku: "GRO-BAKE-006", name_en: "Ladi Pav (Fresh Baked, 6 pcs)", name_hi: "लादी पाव (ताज़ा, 6 नग)", category: "bakery", unit_label: "6 pcs", list_price_minor: 3000, baseline_stock: 25, tax_bp: 0, synonyms: ["पाव", "लादी पाव", "pav", "ladi pav", "bun", "pao"] },
  { sku: "GRO-BAKE-007", name_en: "English Oven Burger Buns (Pack of 2)", name_hi: "इंग्लिश ओवन बर्गर बन (2 का पैक)", category: "bakery", unit_label: "2 pcs", list_price_minor: 3500, baseline_stock: 20, tax_bp: 0, synonyms: ["बर्गर बन", "burger buns", "bun", "burger bun"] },
  { sku: "GRO-BAKE-008", name_en: "Britannia Toastea Premium Milk Rusk 200 g", name_hi: "ब्रिटानिया प्रीमियम मिल्क टोस्ट रस्क 200 ग्राम", category: "bakery", unit_label: "200 g", list_price_minor: 4000, baseline_stock: 30, tax_bp: 500, synonyms: ["रस्क", "टोस्ट", "rusk", "toast", "milk rusk", "britannia rusk"] },
  { sku: "GRO-BAKE-009", name_en: "Britannia Fruit Cake 120 g", name_hi: "ब्रिटानिया फ्रूट केक 120 ग्राम", category: "bakery", unit_label: "120 g", list_price_minor: 3000, baseline_stock: 28, tax_bp: 1800, synonyms: ["केक", "फ्रूट केक", "cake", "fruit cake", "tea cake"] },
  { sku: "GRO-BAKE-010", name_en: "Eggoz Farm Fresh White Eggs (Pack of 12)", name_hi: "एगोज़ फार्म फ्रेश सफेद अंडे (12 का पैक)", category: "bakery", unit_label: "12 pcs", list_price_minor: 15500, baseline_stock: 22, tax_bp: 0, synonyms: ["अंडे", "12 अंडे", "eggs", "ande", "white eggs", "12 eggs", "eggoz"] },
  { sku: "GRO-BAKE-011", name_en: "Farm Fresh Brown Eggs (Pack of 6)", name_hi: "फार्म फ्रेश ब्राउन अंडे (6 का पैक)", category: "bakery", unit_label: "6 pcs", list_price_minor: 9500, baseline_stock: 18, tax_bp: 0, synonyms: ["ब्राउन अंडे", "देसी अंडे", "brown eggs", "desi eggs", "ande"] },
  { sku: "GRO-BAKE-012", name_en: "The Health Factory Multi-Protein Bread 250 g", name_hi: "द हेल्थ फैक्ट्री मल्टी-प्रोटीन ब्रेड 250 ग्राम", category: "bakery", unit_label: "250 g", list_price_minor: 6500, baseline_stock: 16, tax_bp: 0, synonyms: ["प्रोटीन ब्रेड", "protein bread", "health factory", "bread"] },
  { sku: "GRO-BAKE-013", name_en: "English Oven Garlic Bread Loaf 200 g", name_hi: "गार्लिक ब्रेड लोफ 200 ग्राम", category: "bakery", unit_label: "200 g", list_price_minor: 5500, baseline_stock: 15, tax_bp: 0, synonyms: ["गार्लिक ब्रेड", "garlic bread", "bread loaf"] },
  { sku: "GRO-BAKE-014", name_en: "English Oven Pizza Base (2 pcs) 200 g", name_hi: "पिज्जा बेस (2 नग) 200 ग्राम", category: "bakery", unit_label: "2 pcs", list_price_minor: 4500, baseline_stock: 18, tax_bp: 0, synonyms: ["पिज्जा बेस", "pizza base", "pizza crust"] },
  { sku: "GRO-BAKE-015", name_en: "Winkies Swiss Roll Chocolate 100 g", name_hi: "विंकीज़ स्विस रोल चॉकलेट 100 ग्राम", category: "bakery", unit_label: "100 g", list_price_minor: 4000, baseline_stock: 20, tax_bp: 1800, synonyms: ["स्विस रोल", "केक", "swiss roll", "cake", "chocolate cake"] },
  { sku: "GRO-BAKE-016", name_en: "Farm Fresh Eggs Crate (Pack of 30)", name_hi: "अंडों की क्रेट (30 का पैक)", category: "bakery", unit_label: "30 pcs", list_price_minor: 24000, baseline_stock: 12, tax_bp: 0, synonyms: ["30 अंडे", "अंडा क्रेट", "egg crate", "30 eggs", "ande"] },
  { sku: "GRO-BAKE-017", name_en: "English Oven Brown Bread 400 g", name_hi: "इंग्लिश ओवन ब्राउन ब्रेड 400 ग्राम", category: "bakery", unit_label: "400 g", list_price_minor: 4800, baseline_stock: 20, tax_bp: 0, synonyms: ["ब्राउन ब्रेड", "brown bread", "bread"] },
  { sku: "GRO-BAKE-018", name_en: "Bakefresh Sweet Coconut Cookies 200 g", name_hi: "कोकोनट कुकीज 200 ग्राम", category: "bakery", unit_label: "200 g", list_price_minor: 6000, baseline_stock: 0, tax_bp: 1800, synonyms: ["कोकोनट बिस्किट", "coconut cookies", "bakery cookies"], listed: false },
  { sku: "GRO-HHLD-005", name_en: "Vim Dishwash Bar 300 g", name_hi: "विम डिशवॉश बार 300 ग्राम", category: "household", unit_label: "300 g", list_price_minor: 2200, baseline_stock: 40, tax_bp: 1800, synonyms: ["विम बार", "बर्तन धोने का साबुन", "vim bar", "dishwash bar", "bartan sabun", "soap"] },
  { sku: "GRO-HHLD-006", name_en: "Vim Dishwash Gel Lemon 500 ml", name_hi: "विम जेल नींबू 500 मिली", category: "household", unit_label: "500 ml", list_price_minor: 12500, baseline_stock: 30, tax_bp: 1800, synonyms: ["विम जेल", "बर्तन जेल", "vim gel", "dishwash gel", "lemon gel"] },
  { sku: "GRO-HHLD-007", name_en: "Surf Excel Easy Wash Detergent Powder 1 kg", name_hi: "सर्फ एक्सेल ईज़ी वॉश डिटर्जेंट 1 किलो", category: "household", unit_label: "1 kg", list_price_minor: 14500, baseline_stock: 25, tax_bp: 1800, synonyms: ["सर्फ", "डिटर्जेंट पाउडर", "surf excel", "detergent", "washing powder", "surf"] },
  { sku: "GRO-HHLD-008", name_en: "Ariel Matic Front Load Detergent 1 kg", name_hi: "एरियल मैटिक फ्रंट लोड डिटर्जेंट 1 किलो", category: "household", unit_label: "1 kg", list_price_minor: 26000, baseline_stock: 20, tax_bp: 1800, synonyms: ["एरियल", "डिटर्जेंट पाउडर", "ariel", "ariel matic", "washing powder"] },
  { sku: "GRO-HHLD-009", name_en: "Tide Plus Double Power Detergent 1 kg", name_hi: "टाइड प्लस डबल पावर 1 किलो", category: "household", unit_label: "1 kg", list_price_minor: 13000, baseline_stock: 28, tax_bp: 1800, synonyms: ["टाइड", "सर्फ", "tide", "tide powder", "detergent"] },
  { sku: "GRO-HHLD-010", name_en: "Rin Detergent Bar (Pack of 4) 250 g each", name_hi: "रिन साबुन (4 का पैक)", category: "household", unit_label: "4 x 250 g", list_price_minor: 8000, baseline_stock: 30, tax_bp: 1800, synonyms: ["रिन साबुन", "कपड़े धोने का साबुन", "rin bar", "rin soap", "washing soap"] },
  { sku: "GRO-HHLD-011", name_en: "Lizol Disinfectant Floor Cleaner Citrus 1 L", name_hi: "लाइज़ोल फ्लोर क्लीनर नींबू 1 लीटर", category: "household", unit_label: "1 L", list_price_minor: 21500, baseline_stock: 22, tax_bp: 1800, synonyms: ["फर्श क्लीनर", "लाइज़ोल", "lizol", "floor cleaner", "disinfectant"] },
  { sku: "GRO-HHLD-012", name_en: "Harpic Power Plus Toilet Cleaner 1 L", name_hi: "हार्पिक टॉयलेट क्लीनर 1 लीटर", category: "household", unit_label: "1 L", list_price_minor: 20500, baseline_stock: 24, tax_bp: 1800, synonyms: ["टॉयलेट क्लीनर", "हार्पिक", "harpic", "toilet cleaner"] },
  { sku: "GRO-HHLD-013", name_en: "Colin Glass & Surface Cleaner 500 ml", name_hi: "कोलिन ग्लास क्लीनर 500 मिली", category: "household", unit_label: "500 ml", list_price_minor: 10500, baseline_stock: 20, tax_bp: 1800, synonyms: ["कोलिन", "कांच क्लीनर", "colin", "glass cleaner", "surface cleaner"] },
  { sku: "GRO-HHLD-014", name_en: "Scotch-Brite Sponge Wipe (Pack of 3)", name_hi: "स्कॉच-ब्राइट स्पंज वाइप (3 का पैक)", category: "household", unit_label: "3 pcs", list_price_minor: 13500, baseline_stock: 25, tax_bp: 1800, synonyms: ["स्पंज वाइप", "रसोई कपड़ा", "sponge wipe", "scotch brite", "kitchen wipe"] },
  { sku: "GRO-HHLD-015", name_en: "Goodknight Gold Flash Mosquito Liquid Refill (Twin Pack)", name_hi: "गुडनाइट मॉस्किटो रिफिल (2 का पैक)", category: "household", unit_label: "2 x 45 ml", list_price_minor: 15500, baseline_stock: 22, tax_bp: 1800, synonyms: ["मच्छर की दवा", "गुडनाइट", "goodknight", "mosquito refill", "all out"] },
  { sku: "GRO-HHLD-016", name_en: "Hit Flying Insect Mosquito Killer Spray 400 ml", name_hi: "काला हिट स्प्रे 400 मिली", category: "household", unit_label: "400 ml", list_price_minor: 23000, baseline_stock: 18, tax_bp: 1800, synonyms: ["काला हिट", "मच्छर स्प्रे", "hit spray", "mosquito spray", "kala hit"] },
  { sku: "GRO-HHLD-017", name_en: "Freshwrap Aluminium Foil 11 m", name_hi: "फ्रेशवैप एल्युमिनियम फॉयल 11 मीटर", category: "household", unit_label: "11 m", list_price_minor: 11500, baseline_stock: 20, tax_bp: 1800, synonyms: ["फॉइल पेपर", "एल्युमिनियम फॉयल", "aluminium foil", "foil paper", "freshwrap"] },
  { sku: "GRO-HHLD-018", name_en: "Origami So Soft 2-Ply Kitchen Towels (Pack of 2)", name_hi: "ओरिगामी किचन टॉवल (2 का पैक)", category: "household", unit_label: "2 rolls", list_price_minor: 12000, baseline_stock: 20, tax_bp: 1200, synonyms: ["किचन रोल", "टिश्यू पेपर", "kitchen towel", "tissue roll", "tissue paper"] },
  { sku: "GRO-HHLD-019", name_en: "Garbage Bags Medium (Black, 30 Bags)", name_hi: "कचरे की थैली मध्यम (30 बैग)", category: "household", unit_label: "30 bags", list_price_minor: 11000, baseline_stock: 25, tax_bp: 1800, synonyms: ["कचरा बैग", "डस्टबिन बैग", "garbage bags", "trash bags", "dustbin bags"] },
  { sku: "GRO-HHLD-020", name_en: "Comfort After Wash Fabric Conditioner Lily 860 ml", name_hi: "कम्फर्ट फैब्रिक कंडीशनर 860 मिली", category: "household", unit_label: "860 ml", list_price_minor: 22000, baseline_stock: 16, tax_bp: 1800, synonyms: ["कम्फर्ट", "फैब्रिक कंडीशनर", "comfort", "fabric conditioner"] },
  { sku: "GRO-HHLD-021", name_en: "Odonil Room Air Freshener Jasmine 50 g", name_hi: "ओडोनिल रूम फ्रेशनर 50 ग्राम", category: "household", unit_label: "50 g", list_price_minor: 5500, baseline_stock: 30, tax_bp: 1800, synonyms: ["रूम फ्रेशनर", "ओडोनिल", "odonil", "air freshener", "room freshener"] },
  { sku: "GRO-HHLD-022", name_en: "Scotch-Brite Heavy Duty Scrub Sponge (Pack of 3)", name_hi: "स्कॉच-ब्राइट स्क्रब स्पंज (3 का पैक)", category: "household", unit_label: "3 pcs", list_price_minor: 9000, baseline_stock: 25, tax_bp: 1800, synonyms: ["बर्तन मांजने का स्क्रबर", "scrub pad", "scrubber", "scotch brite"] },
  { sku: "GRO-HHLD-023", name_en: "Dettol Disinfectant Multi-Use Liquid Lime 1 L", name_hi: "डेटॉल डिसइंफेक्टेंट लिक्विड 1 लीटर", category: "household", unit_label: "1 L", list_price_minor: 36500, baseline_stock: 15, tax_bp: 1800, synonyms: ["डेटॉल लिक्विड", "dettol disinfectant", "dettol liquid", "surface cleaner"] },
  { sku: "GRO-HHLD-024", name_en: "Pril Tamarind Dishwash Liquid 750 ml", name_hi: "प्रिल डिशवॉश लिक्विड 750 मिली", category: "household", unit_label: "750 ml", list_price_minor: 18500, baseline_stock: 18, tax_bp: 1800, synonyms: ["प्रिल लिक्विड", "pril", "dishwash liquid", "bartan liquid"] },
  { sku: "GRO-PCAR-004", name_en: "Dettol Original Bathing Soap Bar 125 g (Pack of 4)", name_hi: "डेटॉल ओरिजिनल साबुन (4 का पैक)", category: "personal_care", unit_label: "4 x 125 g", list_price_minor: 21500, baseline_stock: 25, tax_bp: 1800, synonyms: ["डेटॉल साबुन", "नहाने का साबुन", "dettol soap", "soap", "bathing soap", "dettol"] },
  { sku: "GRO-PCAR-005", name_en: "Lifebuoy Total Germ Protection Soap 125 g (Pack of 4)", name_hi: "लाइफबॉय साबुन (4 का पैक)", category: "personal_care", unit_label: "4 x 125 g", list_price_minor: 14500, baseline_stock: 28, tax_bp: 1800, synonyms: ["लाइफबॉय", "साबुन", "lifebuoy soap", "soap", "lifebuoy"] },
  { sku: "GRO-PCAR-006", name_en: "Dove Cream Beauty Bathing Bar 100 g (Pack of 3)", name_hi: "डव ब्यूटी बार (3 का पैक)", category: "personal_care", unit_label: "3 x 100 g", list_price_minor: 21000, baseline_stock: 22, tax_bp: 1800, synonyms: ["डव साबुन", "dove soap", "dove", "beauty bar"] },
  { sku: "GRO-PCAR-007", name_en: "Pears Pure & Gentle Soap 125 g (Pack of 3)", name_hi: "पेयर्स ग्लिसरीन साबुन (3 का पैक)", category: "personal_care", unit_label: "3 x 125 g", list_price_minor: 23000, baseline_stock: 20, tax_bp: 1800, synonyms: ["पेयर्स साबुन", "ग्लिसरीन साबुन", "pears soap", "glycerin soap", "pears"] },
  { sku: "GRO-PCAR-008", name_en: "Lifebuoy Total Handwash Pump 200 ml", name_hi: "लाइफबॉय हैंडवॉश 200 मिली", category: "personal_care", unit_label: "200 ml", list_price_minor: 9000, baseline_stock: 30, tax_bp: 1800, synonyms: ["हैंडवॉश", "लाइफबॉय हैंडवॉश", "handwash", "lifebuoy handwash", "hand wash"] },
  { sku: "GRO-PCAR-009", name_en: "Dettol Liquid Handwash Refill 675 ml", name_hi: "डेटॉल हैंडवॉश रिफिल 675 मिली", category: "personal_care", unit_label: "675 ml", list_price_minor: 12500, baseline_stock: 24, tax_bp: 1800, synonyms: ["डेटॉल रिफिल", "हैंडवॉश", "dettol handwash", "handwash refill"] },
  { sku: "GRO-PCAR-010", name_en: "Colgate Strong Teeth Toothpaste 300 g (Pack of 2)", name_hi: "कोलगेट स्ट्रांग टीथ टूथपेस्ट (2 का पैक)", category: "personal_care", unit_label: "2 x 150 g", list_price_minor: 18500, baseline_stock: 30, tax_bp: 1800, synonyms: ["कोलगेट", "टूथपेस्ट", "colgate", "toothpaste", "dant manjan"] },
  { sku: "GRO-PCAR-011", name_en: "Colgate MaxFresh Spicy Red Gel Toothpaste 150 g", name_hi: "कोलगेट मैक्सफ्रेश जेल 150 ग्राम", category: "personal_care", unit_label: "150 g", list_price_minor: 11500, baseline_stock: 25, tax_bp: 1800, synonyms: ["मैक्सफ्रेश", "टूथपेस्ट", "colgate maxfresh", "gel toothpaste", "maxfresh"] },
  { sku: "GRO-PCAR-012", name_en: "Sensodyne Fresh Gel Sensitive Toothpaste 150 g", name_hi: "सेंसोडाइन फ्रेश जेल टूथपेस्ट 150 ग्राम", category: "personal_care", unit_label: "150 g", list_price_minor: 22500, baseline_stock: 20, tax_bp: 1800, synonyms: ["सेंसोडाइन", "टूथपेस्ट", "sensodyne", "sensitive toothpaste"] },
  { sku: "GRO-PCAR-013", name_en: "Close Up Everfresh Red Hot Gel 150 g", name_hi: "क्लोज़ अप रेड हॉट जेल 150 ग्राम", category: "personal_care", unit_label: "150 g", list_price_minor: 11000, baseline_stock: 24, tax_bp: 1800, synonyms: ["क्लोज़ अप", "close up", "gel toothpaste"] },
  { sku: "GRO-PCAR-014", name_en: "Oral-B Shiny Clean Soft Toothbrush (Pack of 4)", name_hi: "ओरल-बी सॉफ्ट टूथब्रश (4 का पैक)", category: "personal_care", unit_label: "4 pcs", list_price_minor: 10000, baseline_stock: 30, tax_bp: 1800, synonyms: ["टूथब्रश", "ब्रश", "toothbrush", "oral b", "brush"] },
  { sku: "GRO-PCAR-015", name_en: "Head & Shoulders Cool Menthol Anti-Dandruff Shampoo 340 ml", name_hi: "हेड एंड शोल्डर्स शैम्पू 340 मिली", category: "personal_care", unit_label: "340 ml", list_price_minor: 34000, baseline_stock: 20, tax_bp: 1800, synonyms: ["शैम्पू", "हेड एंड शोल्डर्स", "head and shoulders", "shampoo", "anti dandruff"] },
  { sku: "GRO-PCAR-016", name_en: "Clinic Plus Strong & Long Shampoo 340 ml", name_hi: "क्लिनिक प्लस शैम्पू 340 मिली", category: "personal_care", unit_label: "340 ml", list_price_minor: 19500, baseline_stock: 25, tax_bp: 1800, synonyms: ["क्लिनिक प्लस", "शैम्पू", "clinic plus", "shampoo"] },
  { sku: "GRO-PCAR-017", name_en: "Pantene Hairfall Control Shampoo 340 ml", name_hi: "पैंटीन हेयरफॉल कंट्रोल शैम्पू 340 मिली", category: "personal_care", unit_label: "340 ml", list_price_minor: 31000, baseline_stock: 18, tax_bp: 1800, synonyms: ["पैंटीन शैम्पू", "pantene", "shampoo"] },
  { sku: "GRO-PCAR-018", name_en: "Bajaj Almond Drops Non-Sticky Hair Oil 190 ml", name_hi: "बजाज बादाम हेयर ऑयल 190 मिली", category: "personal_care", unit_label: "190 ml", list_price_minor: 14000, baseline_stock: 22, tax_bp: 1800, synonyms: ["बादाम तेल", "हेयर ऑयल", "bajaj almond drops", "hair oil", "almond oil"] },
  { sku: "GRO-PCAR-019", name_en: "Dabur Amla Hair Oil 275 ml", name_hi: "डाबर आंवला हेयर ऑयल 275 मिली", category: "personal_care", unit_label: "275 ml", list_price_minor: 13500, baseline_stock: 24, tax_bp: 1800, synonyms: ["आंवला तेल", "डाबर आंवला", "dabur amla", "amla oil", "hair oil"] },
  { sku: "GRO-PCAR-020", name_en: "Himalaya Purifying Neem Face Wash 150 ml", name_hi: "हिमालय नीम फेस वॉश 150 मिली", category: "personal_care", unit_label: "150 ml", list_price_minor: 18500, baseline_stock: 25, tax_bp: 1800, synonyms: ["फेस वॉश", "नीम फेस वॉश", "face wash", "himalaya neem", "facewash"] },
  { sku: "GRO-PCAR-021", name_en: "Vaseline Intensive Care Deep Moisture Body Lotion 400 ml", name_hi: "वेसलीन बॉडी लोशन 400 मिली", category: "personal_care", unit_label: "400 ml", list_price_minor: 32500, baseline_stock: 18, tax_bp: 1800, synonyms: ["बॉडी लोशन", "वेसलीन", "vaseline", "body lotion", "moisturizer"] },
  { sku: "GRO-PCAR-022", name_en: "Nivea Soft Light Moisturising Cream 100 ml", name_hi: "निविया सॉफ्ट क्रीम 100 मिली", category: "personal_care", unit_label: "100 ml", list_price_minor: 19000, baseline_stock: 20, tax_bp: 1800, synonyms: ["निविया क्रीम", "कोल्ड क्रीम", "nivea", "nivea soft", "face cream", "moisturizer"] },
  { sku: "GRO-PCAR-023", name_en: "Gillette Classic Regular Shaving Foam 200 ml", name_hi: "जिलेट शेविंग फोम 200 मिली", category: "personal_care", unit_label: "200 ml", list_price_minor: 18500, baseline_stock: 18, tax_bp: 1800, synonyms: ["शेविंग फोम", "जिलेट", "shaving foam", "gillette", "foam"] },
  { sku: "GRO-PCAR-024", name_en: "Whisper Ultra Clean Sanitary Pads (XL, 30 Pads)", name_hi: "व्हिस्पर अल्ट्रा सेनेटरी पैड्स (30 पैड)", category: "personal_care", unit_label: "30 pads", list_price_minor: 36500, baseline_stock: 25, tax_bp: 1200, synonyms: ["सेनेटरी पैड", "व्हिस्पर", "whisper", "sanitary pads", "pads"] },
  { sku: "GRO-PCAR-025", name_en: "Fogg 1000 Sprays Master Intense Deodorant 150 ml", name_hi: "फॉग डिओडोरेंट 150 मिली", category: "personal_care", unit_label: "150 ml", list_price_minor: 22500, baseline_stock: 20, tax_bp: 1800, synonyms: ["डिओडोरेंट", "परफ्यूम", "fogg", "deodorant", "deo", "perfume", "body spray"] },
  { sku: "GRO-COND-005", name_en: "Tata Sampann Turmeric Powder (Haldi) 200 g", name_hi: "टाटा सम्पन्न हल्दी पाउडर 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 5500, baseline_stock: 35, tax_bp: 500, synonyms: ["हल्दी", "हल्दी पाउडर", "haldi", "turmeric", "haldi powder"] },
  { sku: "GRO-COND-006", name_en: "Tata Sampann Red Chilli Powder (Lal Mirch) 200 g", name_hi: "टाटा सम्पन्न लाल मिर्च पाउडर 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 8500, baseline_stock: 30, tax_bp: 500, synonyms: ["लाल मिर्च", "मिर्च पाउडर", "lal mirch", "red chilli powder", "mirch powder"] },
  { sku: "GRO-COND-007", name_en: "Tata Sampann Coriander Powder (Dhania) 200 g", name_hi: "टाटा सम्पन्न धनिया पाउडर 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 6000, baseline_stock: 28, tax_bp: 500, synonyms: ["धनिया पाउडर", "dhania powder", "coriander powder"] },
  { sku: "GRO-COND-008", name_en: "Tata Sampann Garam Masala 100 g", name_hi: "टाटा सम्पन्न गरम मसाला 100 ग्राम", category: "condiments", unit_label: "100 g", list_price_minor: 9500, baseline_stock: 25, tax_bp: 500, synonyms: ["गरम मसाला", "garam masala", "masala"] },
  { sku: "GRO-COND-009", name_en: "Whole Cumin Seeds (Jeera) 200 g", name_hi: "साबुत जीरा 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 12500, baseline_stock: 30, tax_bp: 500, synonyms: ["जीरा", "साबुत जीरा", "jeera", "cumin", "cumin seeds", "zeera"] },
  { sku: "GRO-COND-010", name_en: "Mustard Seeds (Rai / Sarson) 200 g", name_hi: "राई (सरसों दाना) 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 4500, baseline_stock: 30, tax_bp: 500, synonyms: ["राई", "सरसों दाना", "rai", "mustard seeds", "sarson"] },
  { sku: "GRO-COND-011", name_en: "Catch Compounded Asafoetida (Hing) 50 g", name_hi: "कैच हींग 50 ग्राम", category: "condiments", unit_label: "50 g", list_price_minor: 7500, baseline_stock: 25, tax_bp: 500, synonyms: ["हींग", "hing", "asafoetida", "catch hing"] },
  { sku: "GRO-COND-012", name_en: "Ajwain (Carom Seeds) 100 g", name_hi: "अजवाइन 100 ग्राम", category: "condiments", unit_label: "100 g", list_price_minor: 4500, baseline_stock: 22, tax_bp: 500, synonyms: ["अजवाइन", "ajwain", "carom seeds"] },
  { sku: "GRO-COND-013", name_en: "Whole Black Pepper (Sabut Kali Mirch) 100 g", name_hi: "काली मिर्च साबुत 100 ग्राम", category: "condiments", unit_label: "100 g", list_price_minor: 12000, baseline_stock: 20, tax_bp: 500, synonyms: ["काली मिर्च", "kali mirch", "black pepper"] },
  { sku: "GRO-COND-014", name_en: "Green Cardamom (Chhoti Elaichi) 50 g", name_hi: "छोटी इलायची 50 ग्राम", category: "condiments", unit_label: "50 g", list_price_minor: 21500, baseline_stock: 18, tax_bp: 500, synonyms: ["इलायची", "हरी इलायची", "elaichi", "cardamom", "green cardamom"] },
  { sku: "GRO-COND-015", name_en: "Cloves (Laung) 50 g", name_hi: "लौंग 50 ग्राम", category: "condiments", unit_label: "50 g", list_price_minor: 9500, baseline_stock: 20, tax_bp: 500, synonyms: ["लौंग", "laung", "cloves"] },
  { sku: "GRO-COND-016", name_en: "Kasuri Methi (Dried Fenugreek Leaves) 100 g", name_hi: "कसूरी मेथी 100 ग्राम", category: "condiments", unit_label: "100 g", list_price_minor: 5500, baseline_stock: 22, tax_bp: 500, synonyms: ["कसूरी मेथी", "मेथी", "kasuri methi", "fenugreek leaves", "methi"] },
  { sku: "GRO-COND-017", name_en: "Kissan Fresh Tomato Ketchup Bottle 950 g", name_hi: "किसान फ्रेश टोमैटो केचप 950 ग्राम", category: "condiments", unit_label: "950 g", list_price_minor: 13500, baseline_stock: 25, tax_bp: 1200, synonyms: ["टोमैटो सॉस", "केचप", "kissan ketchup", "tomato sauce", "ketchup"] },
  { sku: "GRO-COND-018", name_en: "Maggi Hot & Sweet Chilli Sauce 500 g", name_hi: "मैगी हॉट एंड स्वीट चिली सॉस 500 ग्राम", category: "condiments", unit_label: "500 g", list_price_minor: 11500, baseline_stock: 24, tax_bp: 1200, synonyms: ["चिली सॉस", "मैगी सॉस", "maggi sauce", "hot and sweet", "chilli sauce"] },
  { sku: "GRO-COND-019", name_en: "Ching's Secret Schezwan Chutney 250 g", name_hi: "चिंग्स सीक्रेट शेज़वान चटनी 250 ग्राम", category: "condiments", unit_label: "250 g", list_price_minor: 8500, baseline_stock: 28, tax_bp: 1200, synonyms: ["शेज़वान चटनी", "schezwan chutney", "chings", "schezwan sauce"] },
  { sku: "GRO-COND-020", name_en: "Dabur Hommade Ginger Garlic Paste 200 g", name_hi: "डाबर होममेड अदरक लहसुन पेस्ट 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 5500, baseline_stock: 30, tax_bp: 1200, synonyms: ["अदरक लहसुन पेस्ट", "ginger garlic paste", "adrak lehsun paste", "dabur"] },
  { sku: "GRO-COND-021", name_en: "Mother's Recipe Mango Pickle (Aam Ka Achar) 400 g", name_hi: "मदर्स रेसिपी आम का अचार 400 ग्राम", category: "condiments", unit_label: "400 g", list_price_minor: 12500, baseline_stock: 22, tax_bp: 1200, synonyms: ["आम का अचार", "अचार", "mango pickle", "aam ka achar", "achar", "pickle"] },
  { sku: "GRO-COND-022", name_en: "Tops Mixed Pickle 450 g", name_hi: "टॉप्स मिक्स्ड अचार 450 ग्राम", category: "condiments", unit_label: "450 g", list_price_minor: 11500, baseline_stock: 20, tax_bp: 1200, synonyms: ["मिक्स अचार", "अचार", "mixed pickle", "tops pickle", "achar"] },
  { sku: "GRO-COND-023", name_en: "Ching's Dark Soy Sauce 200 g", name_hi: "चिंग्स डार्क सोया सॉस 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 5500, baseline_stock: 25, tax_bp: 1200, synonyms: ["सोया सॉस", "soy sauce", "soya sauce", "dark soy sauce"] },
  { sku: "GRO-COND-024", name_en: "Ching's Secret Green Chilli Sauce 200 g", name_hi: "चिंग्स ग्रीन चिली सॉस 200 ग्राम", category: "condiments", unit_label: "200 g", list_price_minor: 5500, baseline_stock: 25, tax_bp: 1200, synonyms: ["ग्रीन चिली सॉस", "green chilli sauce", "chilli sauce"] },
  { sku: "ELEC-ACC-001", name_en: "Apple 20W USB-C Power Adapter", name_hi: "एप्पल 20W यूएसबी-सी चार्जर", category: "electronics", unit_label: "1 pc", list_price_minor: 190000, baseline_stock: 15, tax_bp: 1800, synonyms: ["एप्पल चार्जर", "आईफोन चार्जर", "apple charger", "iphone charger", "20w adapter", "charger", "fast charger"] },
  { sku: "ELEC-ACC-002", name_en: "Apple 60W USB-C Woven Charge Cable 1m", name_hi: "एप्पल 60W यूएसबी-सी केबल 1 मीटर", category: "electronics", unit_label: "1 pc", list_price_minor: 190000, baseline_stock: 18, tax_bp: 1800, synonyms: ["एप्पल केबल", "सी टाइप केबल", "apple cable", "type c cable", "usb c cable", "charging cable"] },
  { sku: "ELEC-ACC-003", name_en: "boAt Bassheads 100 Wired in-Ear Earphones with Mic", name_hi: "बोट बासहेड्स 100 वायर्ड इयरफ़ोन", category: "electronics", unit_label: "1 pc", list_price_minor: 39900, baseline_stock: 25, tax_bp: 1800, synonyms: ["ईयरफोन", "बोट ईयरफोन", "boat earphones", "earphones", "bassheads", "headphones"] },
  { sku: "ELEC-ACC-004", name_en: "Mi 10000mAh 22.5W Fast Charging Power Bank 3i", name_hi: "एमआई 10000mAh पावर बैंक", category: "electronics", unit_label: "1 pc", list_price_minor: 129900, baseline_stock: 12, tax_bp: 1800, synonyms: ["पावर बैंक", "एमआई पावर बैंक", "power bank", "mi power bank", "portable charger"] },
];

const FIXTURE_BY_SKU = new Map(FIXTURE.map((product) => [product.sku, product]));

// -------------------------------------------------------------------- mock state

interface SkuState {
  unit_price_minor: number;
  stock_units: number;
  is_listed: boolean;
}

interface MerchantState {
  revision: number;
  skus: Record<string, SkuState>;
  base_delivery_fee_minor: number;
  free_delivery_threshold_minor: number;
  delivery_tax_bp: number;
}

interface MockBasket {
  basket_id: string;
  lines: { sku: string; quantity: number }[];
  quoted_revision: number;
}

interface MockVersion {
  version: number;
  state: CheckoutState;
  content: Canonical;
  quote: Quote;
  content_hash: string;
  policy_receipt_id: string;
  policy_receipt_hash: string;
  amount_minor: number;
  currency: string;
  created_at: string;
  approval_expires_at: string;
  approval: ApprovalRecord | null;
  reservation: Reservation | null;
  previous_version: number | null;
  deltas: Delta[];
}

type PendingKind = "CAPTURE" | "UNKNOWN" | "LATE_CAPTURE" | "REFUND";

interface Pending {
  kind: PendingKind;
  step: number;
  refund_id?: string;
}

interface MockCheckout {
  checkout_id: string;
  basket_id: string;
  state: CheckoutState;
  current_version: number;
  versions: MockVersion[];
  attempt: AttemptSummary | null;
  order_id: string | null;
  injected: boolean;
  scenario: "PAYMENT_UNKNOWN" | null;
  correlation_id: string;
  updated_at: string;
  pending: Pending | null;
}

interface MockState {
  merchant: MerchantState;
  baskets: Record<string, MockBasket>;
  checkouts: Record<string, MockCheckout>;
  orders: Record<string, Order>;
  events: Record<string, CheckoutEvent[]>;
  seq: Record<string, number>;
}

function freshMerchant(): MerchantState {
  const skus: Record<string, SkuState> = {};
  for (const product of FIXTURE) {
    skus[product.sku] = {
      unit_price_minor: product.list_price_minor,
      stock_units: product.baseline_stock,
      is_listed: product.listed ?? true,
    };
  }
  return {
    revision: 3,
    skus,
    base_delivery_fee_minor: 2500,
    free_delivery_threshold_minor: 49900,
    delivery_tax_bp: 1800,
  };
}

function freshState(): MockState {
  return { merchant: freshMerchant(), baskets: {}, checkouts: {}, orders: {}, events: {}, seq: {} };
}

function loadState(): MockState {
  if (typeof window === "undefined") return freshState();
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return freshState();
    const parsed = JSON.parse(raw) as MockState;
    if (!parsed || typeof parsed !== "object" || !parsed.merchant || !parsed.merchant.skus) return freshState();
    // Reconcile any missing FIXTURE SKUs into parsed state so additions never cause 404s
    const fresh = freshState();
    for (const [sku, skuState] of Object.entries(fresh.merchant.skus)) {
      if (!parsed.merchant.skus[sku]) {
        parsed.merchant.skus[sku] = skuState;
      }
    }
    return parsed;
  } catch {
    return freshState();
  }
}

// ------------------------------------------------------------------------ helpers

function nowIso(): string {
  return new Date().toISOString();
}

function isoIn(ms: number): string {
  return new Date(Date.now() + ms).toISOString();
}

function problem(status: number, title: string, detail: string, code?: RecoveryCode): ApiError {
  return new ApiError({ type: `urn:mock:${status}`, title, status, detail, code, extensions: {} });
}

function taxOn(baseMinor: number, rateBp: number): number {
  return Math.floor((baseMinor * rateBp + 5000) / 10000);
}

const REASON_BY_FIELD: Record<string, string> = {
  unit_price_minor: "PRICE_CHANGED",
  delivery_fee_minor: "DELIVERY_FEE_CHANGED",
  total_minor: "TOTAL_CHANGED",
  catalogue_revision: "MERCHANT_STATE_REVISED",
  quantity: "QUANTITY_CHANGED",
  subtotal_minor: "DERIVED_FROM_PRICE_CHANGE",
  tax_minor: "DERIVED_FROM_PRICE_CHANGE",
  items_subtotal_minor: "DERIVED_FROM_PRICE_CHANGE",
  items_tax_minor: "DERIVED_FROM_PRICE_CHANGE",
  delivery_tax_minor: "DERIVED_FROM_FEE_CHANGE",
  free_delivery_applied: "DERIVED_FROM_FEE_CHANGE",
};

/** Exact structural diff of two canonical contents, one Delta per changed leaf. */
export function diffContent(approved: Canonical, current: Canonical, path = ""): Delta[] {
  if (Array.isArray(approved) && Array.isArray(current)) {
    const deltas: Delta[] = [];
    const length = Math.max(approved.length, current.length);
    for (let i = 0; i < length; i += 1) {
      deltas.push(...diffContent(approved[i] ?? null, current[i] ?? null, `${path}[${i}]`));
    }
    return deltas;
  }
  if (isObject(approved) && isObject(current)) {
    const keys = Array.from(new Set([...Object.keys(approved), ...Object.keys(current)])).sort();
    return keys.flatMap((key) =>
      diffContent(approved[key] ?? null, current[key] ?? null, path ? `${path}.${key}` : key),
    );
  }
  if (approved === current) return [];
  const leaf = path.split(".").pop()?.replace(/\[\d+\]$/, "") ?? path;
  return [{ field_path: path, approved, current, reason: REASON_BY_FIELD[leaf] ?? "MATERIAL_CHANGE" }];
}

function isObject(value: Canonical): value is { [key: string]: Canonical } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalize(text: string): string {
  return text
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[­​]/g, "")
    .replace(/[^\p{L}\p{M}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenize(text: string): string[] {
  return normalize(text).split(" ").filter(Boolean);
}

/** A tiny stand-in for the provider HMAC: sha256(order_id|payment_id). Mock only. */
export function mockSignature(orderId: string, paymentId: string): string {
  return sha256Hex(`${orderId}|${paymentId}`);
}

// ------------------------------------------------------------------------- client

export interface MockScenarioControls {
  /** Arm the unknown-outcome path for the next payment callback on this checkout. */
  armPaymentUnknown(checkoutId: string): Promise<Checkout>;
  /** Invalidate an open checkout and deliver a late capture: stale capture, one automatic refund. */
  lateCapture(checkoutId: string): Promise<Checkout>;
  /** Replay the last webhook: inbox marks it duplicate, state does not change. */
  replayWebhook(checkoutId: string): Promise<Checkout>;
  /** Wipe the fixture and start again. */
  reset(): Promise<void>;
}

export interface MockClient extends CommerceClient {
  readonly mode: "mock";
  readonly scenario: MockScenarioControls;
}

export function isMockClient(client: CommerceClient): client is MockClient {
  return client.mode === "mock" && "scenario" in client;
}

export function createMockClient(): MockClient {
  let state = loadState();
  const listeners = new Map<string, Set<(event: CheckoutEvent) => void>>();
  const timers = new Map<string, ReturnType<typeof setTimeout>>();

  function persist(): void {
    if (typeof window === "undefined") return;
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Storage is a convenience; the in-memory state is still authoritative for this tab.
    }
  }

  function nextId(prefix: string, width = 4): string {
    const n = (state.seq[prefix] ?? 0) + 1;
    state.seq[prefix] = n;
    return `${prefix}_${String(n).padStart(width, "0")}`;
  }

  function freshness() {
    return { source: SOURCE, catalogue_revision: state.merchant.revision, observed_at: nowIso() };
  }

  function productView(sku: string): Product {
    const fixture = FIXTURE_BY_SKU.get(sku);
    if (!fixture) throw problem(404, "Unknown SKU", `${sku} is not in the catalogue`);
    let live = state.merchant.skus[sku];
    if (!live) {
      live = {
        unit_price_minor: fixture.list_price_minor,
        stock_units: fixture.baseline_stock,
        is_listed: fixture.listed ?? true,
      };
      state.merchant.skus[sku] = live;
      persist();
    }
    return {
      sku,
      display_name: fixture.name_en,
      name_en: fixture.name_en,
      name_hi: fixture.name_hi,
      category: fixture.category,
      unit_label: fixture.unit_label,
      unit_price_minor: live.unit_price_minor,
      currency: CURRENCY,
      tax_bp: fixture.tax_bp,
      stock_units: live.stock_units,
      is_listed: live.is_listed,
      is_available: live.is_listed && live.stock_units > 0,
      freshness: freshness(),
    };
  }

  // ---- fee engine port -------------------------------------------------------

  function quoteLines(lines: { sku: string; quantity: number }[]): {
    code: RecoveryCode;
    quote: Quote | null;
    unavailable: Unavailability[];
  } {
    if (lines.length === 0) return { code: "OK", quote: null, unavailable: [] };
    const unavailable: Unavailability[] = [];
    const priced: QuoteLine[] = [];
    for (const line of lines) {
      const view = productView(line.sku);
      if (!view.is_available || view.stock_units < line.quantity) {
        unavailable.push({ sku: line.sku, requested: line.quantity, available_units: view.stock_units, listed: view.is_listed });
        continue;
      }
      const subtotal = view.unit_price_minor * line.quantity;
      priced.push({
        sku: line.sku,
        name: view.name_en,
        quantity: line.quantity,
        unit_price_minor: view.unit_price_minor,
        subtotal_minor: subtotal,
        tax_bp: view.tax_bp,
        tax_minor: taxOn(subtotal, view.tax_bp),
      });
    }
    if (unavailable.length > 0) return { code: "STALE_CHECKOUT", quote: null, unavailable };

    const merchant = state.merchant;
    const itemsSubtotal = priced.reduce((sum, line) => sum + line.subtotal_minor, 0);
    const itemsTax = priced.reduce((sum, line) => sum + line.tax_minor, 0);
    const free = itemsSubtotal >= merchant.free_delivery_threshold_minor;
    const deliveryFee = free ? 0 : merchant.base_delivery_fee_minor;
    const deliveryTax = taxOn(deliveryFee, merchant.delivery_tax_bp);
    const total = itemsSubtotal + itemsTax + deliveryFee + deliveryTax;
    const content = quoteContent({
      currency: CURRENCY,
      lines: priced,
      items_subtotal_minor: itemsSubtotal,
      items_tax_minor: itemsTax,
      delivery_fee_minor: deliveryFee,
      delivery_tax_minor: deliveryTax,
      total_minor: total,
      free_delivery_applied: free,
      catalogue_revision: merchant.revision,
    });
    const quote: Quote = {
      currency: CURRENCY,
      lines: priced,
      items_subtotal_minor: itemsSubtotal,
      items_tax_minor: itemsTax,
      delivery_fee_minor: deliveryFee,
      delivery_tax_minor: deliveryTax,
      total_minor: total,
      free_delivery_applied: free,
      gap_to_free_delivery_minor: free ? 0 : merchant.free_delivery_threshold_minor - itemsSubtotal,
      source: SOURCE,
      catalogue_revision: merchant.revision,
      content_hash: canonicalHash(content),
    };
    return { code: "OK", quote, unavailable: [] };
  }

  /** Mirrors merchant_sim.fees.Quote.to_checkout_content: integers and strings only. */
  function quoteContent(input: {
    currency: string;
    lines: QuoteLine[];
    items_subtotal_minor: number;
    items_tax_minor: number;
    delivery_fee_minor: number;
    delivery_tax_minor: number;
    total_minor: number;
    free_delivery_applied: boolean;
    catalogue_revision: number;
  }): Canonical {
    return {
      currency: input.currency,
      lines: input.lines.map((line) => ({
        sku: line.sku,
        name: line.name,
        quantity: line.quantity,
        unit_price_minor: line.unit_price_minor,
        subtotal_minor: line.subtotal_minor,
        tax_bp: line.tax_bp,
        tax_minor: line.tax_minor,
      })),
      items_subtotal_minor: input.items_subtotal_minor,
      items_tax_minor: input.items_tax_minor,
      delivery_fee_minor: input.delivery_fee_minor,
      delivery_tax_minor: input.delivery_tax_minor,
      total_minor: input.total_minor,
      free_delivery_applied: input.free_delivery_applied,
      source: SOURCE,
      catalogue_revision: input.catalogue_revision,
    };
  }

  // ---- events ----------------------------------------------------------------

  type RowInput = Partial<Omit<TimelineRow, "event_id" | "occurred_at" | "correlation_id">> & {
    actor: string;
    action: string;
  };

  function emit(checkout: MockCheckout, input: RowInput): CheckoutEvent {
    const eventId = nextId("evt", 6);
    const version = checkout.versions.find((v) => v.version === (input.version ?? checkout.current_version));
    const row: TimelineRow = {
      event_id: eventId,
      occurred_at: nowIso(),
      actor: input.actor,
      action: input.action,
      source: input.source ?? null,
      version: input.version ?? checkout.current_version,
      hash_short: input.hash_short ?? (version ? version.content_hash.slice(0, 12) : null),
      policy_evaluated: input.policy_evaluated ?? null,
      policy_receipt_hash: input.policy_receipt_hash ?? version?.policy_receipt_hash ?? null,
      authority_ref: input.authority_ref ?? null,
      decision: input.decision ?? null,
      grant: input.grant ?? null,
      provider_result: input.provider_result ?? null,
      reconciliation_result: input.reconciliation_result ?? null,
      correlation_id: checkout.correlation_id,
      scenario_injection: input.scenario_injection ?? false,
    };
    const event: CheckoutEvent = {
      event_id: eventId,
      checkout_id: checkout.checkout_id,
      checkout_state: checkout.state,
      payment_state: checkout.attempt?.state ?? null,
      order_id: checkout.order_id,
      row,
    };
    (state.events[checkout.checkout_id] ??= []).push(event);
    checkout.updated_at = row.occurred_at;
    persist();
    const subscribers = listeners.get(checkout.checkout_id);
    if (subscribers) {
      for (const listener of subscribers) setTimeout(() => listener(event), 0);
    }
    return event;
  }

  // ---- checkout projection --------------------------------------------------

  function approvalCard(checkout: MockCheckout, version: MockVersion): ApprovalCard {
    return {
      checkout_id: checkout.checkout_id,
      version: version.version,
      content_hash: version.content_hash,
      policy_receipt_id: version.policy_receipt_id,
      policy_receipt_hash: version.policy_receipt_hash,
      amount_minor: version.amount_minor,
      currency: version.currency,
      expires_at: version.approval_expires_at,
      reservation: version.reservation,
      quote: version.quote,
      previous_version: version.previous_version,
      deltas: version.deltas,
    };
  }

  function versionSummary(version: MockVersion): VersionSummary {
    return {
      version: version.version,
      state: version.state,
      content_hash: version.content_hash,
      policy_receipt_hash: version.policy_receipt_hash,
      amount_minor: version.amount_minor,
      currency: version.currency,
      created_at: version.created_at,
      approval: version.approval,
    };
  }

  const CANCELLABLE: ReadonlySet<CheckoutState> = new Set([
    "QUOTED",
    "RESERVED",
    "APPROVAL_REQUIRED",
    "APPROVED",
    "EXECUTION_PENDING",
    "PAYMENT_FAILED",
  ]);

  function project(checkout: MockCheckout): Checkout {
    const current = currentVersion(checkout);
    return {
      checkout_id: checkout.checkout_id,
      basket_id: checkout.basket_id,
      state: checkout.state,
      current_version: checkout.current_version,
      versions: checkout.versions.map(versionSummary),
      approval_card: current.state === "APPROVAL_REQUIRED" ? approvalCard(checkout, current) : null,
      attempt: checkout.attempt,
      order_id: checkout.order_id,
      deltas: current.state === "APPROVAL_REQUIRED" ? current.deltas : [],
      cancellable: CANCELLABLE.has(checkout.state),
      updated_at: checkout.updated_at,
    };
  }

  function currentVersion(checkout: MockCheckout): MockVersion {
    const version = checkout.versions.find((v) => v.version === checkout.current_version);
    if (!version) throw problem(500, "Corrupt mock state", "current version missing");
    return version;
  }

  function requireCheckout(checkoutId: string): MockCheckout {
    const checkout = state.checkouts[checkoutId];
    if (!checkout) throw problem(404, "Checkout not found", `No checkout ${checkoutId} for this session`);
    return checkout;
  }

  function requireVersion(checkout: MockCheckout, version: number): MockVersion {
    const found = checkout.versions.find((v) => v.version === version);
    if (!found) throw problem(404, "Version not found", `Checkout ${checkout.checkout_id} has no version ${version}`);
    return found;
  }

  function decision(input: Partial<KernelDecision> & { allowed: boolean; code: RecoveryCode; explanation: string }): KernelDecision {
    return {
      decision_id: nextId("dec"),
      checkout: null,
      deltas: [],
      grant_id: null,
      payment_attempt_id: null,
      next_version: null,
      correlation_id: null,
      ...input,
    };
  }

  function receiptFor(checkout: MockCheckout, version: number, contentHash: string): { id: string; hash: string } {
    const id = nextId("psr");
    const hash = canonicalHash({
      policy_receipt_id: id,
      checkout_id: checkout.checkout_id,
      version,
      content_hash: contentHash,
      policies: ["cancellation@v3", "refund@v2", "substitution@v1", "delivery@v4", "tax_rounding@v1"],
      terms: { cancel_before_dispatch: true, refund_to_source: true, substitution: "buyer_confirmed" },
    });
    return { id, hash };
  }

  function createVersion(checkout: MockCheckout, quote: Quote, content: Canonical, previous: MockVersion | null, deltas: Delta[]): MockVersion {
    const number = checkout.versions.length + 1;
    const receipt = receiptFor(checkout, number, quote.content_hash);
    const version: MockVersion = {
      version: number,
      state: "QUOTED",
      content,
      quote,
      content_hash: quote.content_hash,
      policy_receipt_id: receipt.id,
      policy_receipt_hash: receipt.hash,
      amount_minor: quote.total_minor,
      currency: quote.currency,
      created_at: nowIso(),
      approval_expires_at: isoIn(APPROVAL_TTL_MS),
      approval: null,
      reservation: null,
      previous_version: previous?.version ?? null,
      deltas,
    };
    checkout.versions.push(version);
    checkout.current_version = number;
    checkout.state = "QUOTED";
    emit(checkout, { actor: "kernel", action: "CHECKOUT_VERSION_CREATED", source: SOURCE, version: number, policy_evaluated: "checkout_content@v1" });

    version.reservation = { reservation_id: nextId("rsv"), state: "ACTIVE", expires_at: isoIn(RESERVATION_TTL_MS) };
    version.state = "RESERVED";
    checkout.state = "RESERVED";
    emit(checkout, { actor: "kernel", action: "RESERVATION_CREATED", version: number, authority_ref: version.reservation.reservation_id, policy_evaluated: "reservation_ttl=900s" });

    emit(checkout, { actor: "kernel", action: "POLICY_AT_SALE_RECEIPT_CREATED", version: number, policy_evaluated: "cancellation@v3, refund@v2, substitution@v1, delivery@v4, tax_rounding@v1", policy_receipt_hash: version.policy_receipt_hash });

    version.state = "APPROVAL_REQUIRED";
    checkout.state = "APPROVAL_REQUIRED";
    emit(checkout, { actor: "kernel", action: "APPROVAL_REQUESTED", version: number, hash_short: version.content_hash.slice(0, 12) });
    return version;
  }

  // ---- scripted worker steps -------------------------------------------------

  function schedule(checkout: MockCheckout, pending: Pending): void {
    checkout.pending = pending;
    persist();
    const existing = timers.get(checkout.checkout_id);
    if (existing) clearTimeout(existing);
    timers.set(
      checkout.checkout_id,
      setTimeout(() => runStep(checkout.checkout_id), STEP_DELAY_MS),
    );
  }

  function runStep(checkoutId: string): void {
    const checkout = state.checkouts[checkoutId];
    timers.delete(checkoutId);
    if (!checkout || !checkout.pending) return;
    const script = SCRIPTS[checkout.pending.kind];
    const step = script[checkout.pending.step];
    if (!step) {
      checkout.pending = null;
      persist();
      return;
    }
    step(checkout, checkout.pending);
    checkout.pending.step += 1;
    if (checkout.pending.step >= script.length) {
      checkout.pending = null;
      persist();
    } else {
      schedule(checkout, checkout.pending);
    }
  }

  function markCaptured(checkout: MockCheckout, kind: "WEBHOOK" | "PROVIDER_FETCH", reference: string): void {
    const attempt = requireAttempt(checkout);
    attempt.state = "CAPTURED";
    attempt.razorpay_payment_id ??= nextId("pay_MOCK", 6);
    attempt.capture_evidence = { kind, reference, verified_at: nowIso() };
    checkout.state = "PAID";
    const version = currentVersion(checkout);
    version.state = "PAID";
    if (version.reservation) version.reservation.state = "CONSUMED";
    const order = createOrder(checkout, "CONFIRMED");
    checkout.order_id = order.order_id;
    emit(checkout, {
      actor: kind === "WEBHOOK" ? "razorpay" : "worker",
      action: kind === "WEBHOOK" ? "WEBHOOK_PAYMENT_CAPTURED_APPLIED" : "PROVIDER_FETCH_CAPTURE_APPLIED",
      provider_result: `captured ${attempt.razorpay_payment_id} (${reference})`,
      reconciliation_result: "monotonic_apply: SUBMITTED/AUTHORIZED -> CAPTURED",
      grant: attempt.grant_id ? { grant_id: attempt.grant_id, status: "CONSUMED" } : null,
    });
    emit(checkout, { actor: "kernel", action: "ORDER_CONFIRMED", authority_ref: order.order_id, provider_result: `order ${order.order_id} paid from verified capture` });
  }

  function requireAttempt(checkout: MockCheckout): AttemptSummary {
    if (!checkout.attempt) throw problem(409, "No payment attempt", "This checkout has not been admitted for payment");
    return checkout.attempt;
  }

  function createOrder(checkout: MockCheckout, orderState: Order["state"]): Order {
    const version = currentVersion(checkout);
    const attempt = requireAttempt(checkout);
    const order: Order = {
      order_id: nextId("ord"),
      checkout_id: checkout.checkout_id,
      version: version.version,
      content_hash: version.content_hash,
      policy_receipt_hash: version.policy_receipt_hash,
      state: orderState,
      amount_minor: version.amount_minor,
      currency: version.currency,
      quote: version.quote,
      payment: attempt,
      refunds: [],
      created_at: nowIso(),
    };
    state.orders[order.order_id] = order;
    return order;
  }

  function syncOrderPayment(checkout: MockCheckout): void {
    if (!checkout.order_id || !checkout.attempt) return;
    const order = state.orders[checkout.order_id];
    if (order) order.payment = { ...checkout.attempt };
  }

  const SCRIPTS: Record<PendingKind, Array<(checkout: MockCheckout, pending: Pending) => void>> = {
    CAPTURE: [
      (checkout) => {
        const attempt = requireAttempt(checkout);
        attempt.state = "AUTHORIZED";
        attempt.razorpay_payment_id ??= nextId("pay_MOCK", 6);
        emit(checkout, { actor: "worker", action: "PROVIDER_FETCH_PAYMENT", provider_result: `authorized ${attempt.razorpay_payment_id}`, reconciliation_result: "attempt 1/6: authorized, capture pending" });
      },
      (checkout) => markCaptured(checkout, "WEBHOOK", `payment.captured evt_${checkout.checkout_id.slice(-4)}_01`),
      (checkout) => {
        emit(checkout, { actor: "razorpay", action: "WEBHOOK_DUPLICATE_IGNORED", provider_result: "payment.captured (replay) — inbox dedupe on x-razorpay-event-id", reconciliation_result: "no state change" });
      },
    ],
    UNKNOWN: [
      (checkout) => {
        const attempt = requireAttempt(checkout);
        attempt.state = "UNKNOWN";
        checkout.state = "PAYMENT_UNKNOWN";
        emit(checkout, { actor: "worker", action: "PROVIDER_TIMEOUT", provider_result: "fetch payment: transport timeout after 20s", reconciliation_result: "attempt marked UNKNOWN; reservation held; second attempt blocked" });
      },
      (checkout) => {
        const attempt = requireAttempt(checkout);
        attempt.state = "RECONCILING";
        attempt.reconciliation_attempts += 1;
        emit(checkout, { actor: "worker", action: "RECONCILE_PAYMENT", reconciliation_result: `attempt ${attempt.reconciliation_attempts}/6: fetching by razorpay_order_id` });
      },
      (checkout) => markCaptured(checkout, "PROVIDER_FETCH", "GET /v1/orders/{id}/payments"),
    ],
    LATE_CAPTURE: [
      (checkout) => {
        const attempt = requireAttempt(checkout);
        attempt.state = "STALE_CAPTURE";
        attempt.razorpay_payment_id ??= nextId("pay_MOCK", 6);
        attempt.capture_evidence = { kind: "WEBHOOK", reference: "payment.captured (late)", verified_at: nowIso() };
        const order = createOrder(checkout, "FULFILMENT_BLOCKED");
        checkout.order_id = order.order_id;
        emit(checkout, { actor: "razorpay", action: "WEBHOOK_LATE_CAPTURE", provider_result: `captured ${attempt.razorpay_payment_id} for an invalidated version`, reconciliation_result: "STALE_CAPTURE: fulfilment blocked" });
      },
      (checkout, pending) => {
        const attempt = requireAttempt(checkout);
        attempt.state = "AUTO_REFUND_PENDING";
        const grant = nextId("grt");
        const refund: Refund = { refund_id: nextId("rfd"), amount_minor: currentVersion(checkout).amount_minor, currency: CURRENCY, state: "REFUND_PENDING", reason: "STALE_CAPTURE", automatic: true, created_at: nowIso() };
        pending.refund_id = refund.refund_id;
        const order = checkout.order_id ? state.orders[checkout.order_id] : undefined;
        if (order) order.refunds.push(refund);
        syncOrderPayment(checkout);
        emit(checkout, { actor: "kernel", action: "AUTOMATIC_REFUND_ADMITTED", decision: { allowed: true, code: "OK", reason: "stale_capture_full_refund" }, grant: { grant_id: grant, status: "ISSUED" }, authority_ref: refund.refund_id });
      },
      (checkout, pending) => {
        const attempt = requireAttempt(checkout);
        attempt.state = "REFUNDED";
        checkout.state = "INVALIDATED";
        currentVersion(checkout).state = "INVALIDATED";
        const order = checkout.order_id ? state.orders[checkout.order_id] : undefined;
        if (order) {
          order.state = "REFUNDED";
          const refund = order.refunds.find((r) => r.refund_id === pending.refund_id);
          if (refund) refund.state = "REFUNDED";
        }
        syncOrderPayment(checkout);
        emit(checkout, { actor: "razorpay", action: "REFUND_PROCESSED", provider_result: `refund ${pending.refund_id ?? ""} processed (full)`, reconciliation_result: "verified terminal: REFUNDED; duplicate refund request would be swallowed by idempotency key" });
      },
    ],
    REFUND: [
      (checkout, pending) => {
        const attempt = requireAttempt(checkout);
        const order = checkout.order_id ? state.orders[checkout.order_id] : undefined;
        if (!order) return;
        const refund = order.refunds.find((r) => r.refund_id === pending.refund_id);
        if (!refund) return;
        refund.state = "REFUNDED";
        const refunded = order.refunds.filter((r) => r.state === "REFUNDED").reduce((sum, r) => sum + r.amount_minor, 0);
        const full = refunded >= order.amount_minor;
        attempt.state = full ? "REFUNDED" : "PARTIALLY_REFUNDED";
        order.state = full ? "REFUNDED" : "PARTIALLY_REFUNDED";
        syncOrderPayment(checkout);
        emit(checkout, { actor: "razorpay", action: "REFUND_PROCESSED", provider_result: `refund ${refund.refund_id} processed`, reconciliation_result: full ? "verified terminal: REFUNDED" : "verified: PARTIALLY_REFUNDED" });
      },
    ],
  };

  // Re-arm any scripted step that was mid-flight when the page reloaded.
  for (const checkout of Object.values(state.checkouts)) {
    if (checkout.pending) schedule(checkout, checkout.pending);
  }

  // ---- the interface --------------------------------------------------------

  const client: MockClient = {
    mode: "mock",

    async search({ q, locale = "en-IN", limit = 20 }: SearchParams): Promise<SearchResponse> {
      const tokens = tokenize(q);
      const hits: SearchHit[] = [];
      if (tokens.length > 0) {
        for (const fixture of FIXTURE) {
          const terms = new Set([
            ...tokenize(fixture.name_en),
            ...tokenize(fixture.name_hi),
            ...fixture.synonyms.flatMap(tokenize),
            fixture.category,
            normalize(fixture.sku),
          ]);
          let score = 0;
          const matched: string[] = [];
          for (const token of tokens) {
            if (terms.has(token)) {
              score += 100;
              matched.push(token);
              continue;
            }
            if (token.length >= 3) {
              const prefix = Array.from(terms).sort().find((term) => term.length > token.length && term.startsWith(token));
              if (prefix) {
                score += 40;
                matched.push(prefix);
              }
            }
          }
          if (score > 0) {
            try {
              const view = productView(fixture.sku);
              hits.push({
                ...view,
                display_name: locale === "hi-IN" ? fixture.name_hi : fixture.name_en,
                score,
                matched_terms: matched,
              });
            } catch {
              // Ignore any individual unresolvable SKU
            }
          }
        }
      } else {
        // Empty query returns catalogue products for initial browsing
        for (const fixture of FIXTURE) {
          try {
            const view = productView(fixture.sku);
            hits.push({
              ...view,
              display_name: locale === "hi-IN" ? fixture.name_hi : fixture.name_en,
              score: 1,
              matched_terms: ["catalogue"],
            });
          } catch {
            // Ignore any individual unresolvable SKU
          }
        }
      }
      hits.sort((a, b) => b.score - a.score || Number(!a.is_available) - Number(!b.is_available) || a.sku.localeCompare(b.sku));
      return { query: q, normalized_query: normalize(q), locale, hits: hits.slice(0, limit), freshness: freshness() };
    },

    async getProduct(sku) {
      return productView(sku);
    },

    async createBasket() {
      const basket: MockBasket = { basket_id: nextId("bsk"), lines: [], quoted_revision: state.merchant.revision };
      state.baskets[basket.basket_id] = basket;
      persist();
      return projectBasket(basket, false);
    },

    async setBasketLine(basketId, sku, quantity) {
      const basket = requireBasket(basketId);
      productView(sku);
      if (!Number.isInteger(quantity) || quantity < 0) throw problem(422, "Invalid quantity", "quantity must be a non-negative integer");
      basket.lines = basket.lines.filter((line) => line.sku !== sku);
      if (quantity > 0) basket.lines.push({ sku, quantity });
      basket.quoted_revision = state.merchant.revision;
      persist();
      return projectBasket(basket, false);
    },

    async getBasket(basketId) {
      const basket = requireBasket(basketId);
      const stale = basket.quoted_revision !== state.merchant.revision;
      basket.quoted_revision = state.merchant.revision;
      persist();
      return projectBasket(basket, stale);
    },

    async checkoutBasket(basketId) {
      const basket = requireBasket(basketId);
      const result = quoteLines(basket.lines);
      if (!result.quote) {
        throw problem(409, "Basket cannot be checked out", result.unavailable.length ? `Unavailable: ${result.unavailable.map((u) => u.sku).join(", ")}` : "The basket is empty", result.code === "OK" ? undefined : result.code);
      }
      const checkout: MockCheckout = {
        checkout_id: nextId("chk"),
        basket_id: basketId,
        state: "DRAFT",
        current_version: 0,
        versions: [],
        attempt: null,
        order_id: null,
        injected: false,
        scenario: null,
        correlation_id: nextId("corr", 6),
        updated_at: nowIso(),
        pending: null,
      };
      state.checkouts[checkout.checkout_id] = checkout;
      emit(checkout, { actor: "buyer", action: "CHECKOUT_REQUESTED", source: SOURCE, version: 0, hash_short: null, policy_receipt_hash: null });
      const content = quoteContent({ ...result.quote, catalogue_revision: result.quote.catalogue_revision });
      createVersion(checkout, result.quote, content, null, []);
      persist();
      return project(checkout);
    },

    async getCheckout(checkoutId) {
      return project(requireCheckout(checkoutId));
    },

    async approveVersion(checkoutId, versionNumber, echo: ApprovalEcho): Promise<ApproveResponse> {
      const checkout = requireCheckout(checkoutId);
      const version = requireVersion(checkout, versionNumber);
      if (version.state !== "APPROVAL_REQUIRED") {
        throw problem(409, "Version is not approvable", `Version ${versionNumber} is ${version.state}${version.state === "INVALIDATED" ? "; approve version " + checkout.current_version : ""}`, version.state === "INVALIDATED" ? "REAPPROVAL_REQUIRED" : "AUTHORITY_INSUFFICIENT");
      }
      const exact = echo.content_hash === version.content_hash && echo.amount_minor === version.amount_minor && echo.currency === version.currency && Object.keys(echo).length === 3;
      if (!exact) {
        emit(checkout, { actor: "kernel", action: "APPROVAL_REJECTED_ECHO_MISMATCH", version: versionNumber, decision: { allowed: false, code: "AUTHORITY_INSUFFICIENT", reason: "approval_echo_mismatch" } });
        throw problem(422, "Approval does not match the displayed version", "The approved hash, amount or currency differs from the version on record. Reload the card and approve what it shows.", "AUTHORITY_INSUFFICIENT");
      }
      const approval: ApprovalRecord = {
        approval_id: nextId("apr"),
        version: versionNumber,
        content_hash: version.content_hash,
        policy_receipt_hash: version.policy_receipt_hash,
        amount_minor: version.amount_minor,
        currency: version.currency,
        approved_at: nowIso(),
        expires_at: version.approval_expires_at,
        authority_epoch: 1,
      };
      version.approval = approval;
      version.state = "APPROVED";
      checkout.state = "APPROVED";
      emit(checkout, { actor: "buyer", action: "APPROVAL_RECORDED", version: versionNumber, authority_ref: approval.approval_id, policy_evaluated: "trusted_surface_echo=exact" });
      persist();
      return { approval, checkout: project(checkout) };
    },

    async rejectVersion(checkoutId, versionNumber, reason) {
      const checkout = requireCheckout(checkoutId);
      const version = requireVersion(checkout, versionNumber);
      if (version.reservation?.state === "ACTIVE") version.reservation.state = "RELEASED";
      version.state = "CANCELLED";
      checkout.state = "CANCELLED";
      emit(checkout, { actor: "buyer", action: "APPROVAL_REJECTED", version: versionNumber, provider_result: null, reconciliation_result: `reservation released (${reason ?? "buyer_rejected"})` });
      persist();
      return project(checkout);
    },

    async submitVersion(checkoutId, versionNumber): Promise<SubmitResponse> {
      const checkout = requireCheckout(checkoutId);
      const version = requireVersion(checkout, versionNumber);
      const ref = { checkout_id: checkoutId, version: versionNumber, content_hash: version.content_hash };
      emit(checkout, { actor: "buyer", action: "SUBMIT_REQUESTED", version: versionNumber, authority_ref: version.approval?.approval_id ?? null });

      if (checkout.attempt && !["FAILED", "EXPIRED"].includes(checkout.attempt.state)) {
        const dup = decision({ allowed: false, code: "DUPLICATE_OPERATION", explanation: "duplicate_submit_live_attempt", checkout: ref, payment_attempt_id: checkout.attempt.attempt_id, correlation_id: checkout.correlation_id });
        emit(checkout, { actor: "kernel", action: "ADMISSION_DECISION", version: versionNumber, decision: { allowed: false, code: "DUPLICATE_OPERATION", reason: "one live attempt per checkout" } });
        return { decision: dup, outcome: "DUPLICATE_OPERATION", attempt_id: checkout.attempt.attempt_id, checkout: project(checkout) };
      }

      if (version.state !== "APPROVED" || !version.approval) {
        const code: RecoveryCode = version.state === "INVALIDATED" ? "REAPPROVAL_REQUIRED" : "AUTHORITY_INSUFFICIENT";
        const denied = decision({ allowed: false, code, explanation: version.state === "INVALIDATED" ? "version_invalidated_approve_next" : "approval_missing", checkout: ref, next_version: version.state === "INVALIDATED" ? checkout.current_version : null, correlation_id: checkout.correlation_id });
        emit(checkout, { actor: "kernel", action: "ADMISSION_DECISION", version: versionNumber, decision: { allowed: false, code, reason: denied.explanation } });
        return { decision: denied, outcome: code, attempt_id: null, checkout: project(checkout) };
      }

      if (!checkout.injected) {
        // Step 5: merchant state changes underneath the approved checkout.
        checkout.injected = true;
        const firstSku = version.quote.lines[0].sku;
        const before = state.merchant.skus[firstSku].unit_price_minor;
        state.merchant.revision += 1;
        state.merchant.skus[firstSku].unit_price_minor = before + 1000;
        emit(checkout, { actor: "merchant", action: "SCENARIO_INJECTION PRICE_SET", source: SOURCE, version: versionNumber, provider_result: `${firstSku}: unit_price_minor ${before} -> ${before + 1000} (revision ${state.merchant.revision})`, scenario_injection: true });
        if (!version.quote.free_delivery_applied) {
          const feeBefore = state.merchant.base_delivery_fee_minor;
          state.merchant.revision += 1;
          state.merchant.base_delivery_fee_minor = feeBefore + 2000;
          emit(checkout, { actor: "merchant", action: "SCENARIO_INJECTION DELIVERY_FEE_SET", source: SOURCE, version: versionNumber, provider_result: `base_delivery_fee_minor ${feeBefore} -> ${feeBefore + 2000} (revision ${state.merchant.revision})`, scenario_injection: true });
        }

        // Steps 6 and 7: the kernel refuses version N and shows the exact delta.
        emit(checkout, { actor: "kernel", action: "REVALIDATION", version: versionNumber, source: SOURCE, policy_evaluated: "freshness: catalogue_revision", reconciliation_result: `approved revision ${version.quote.catalogue_revision} != current ${state.merchant.revision}` });
        const basket = requireBasket(checkout.basket_id);
        const requote = quoteLines(basket.lines);
        if (!requote.quote) {
          const stale = decision({ allowed: false, code: "STALE_CHECKOUT", explanation: "lines_unavailable", checkout: ref, correlation_id: checkout.correlation_id });
          version.state = "INVALIDATED";
          checkout.state = "INVALIDATED";
          emit(checkout, { actor: "kernel", action: "ADMISSION_DECISION", version: versionNumber, decision: { allowed: false, code: "STALE_CHECKOUT", reason: "lines_unavailable" } });
          return { decision: stale, outcome: "STALE_CHECKOUT", attempt_id: null, checkout: project(checkout) };
        }
        const nextContent = quoteContent({ ...requote.quote, catalogue_revision: requote.quote.catalogue_revision });
        const deltas = diffContent(version.content, nextContent);
        version.state = "INVALIDATED";
        if (version.reservation?.state === "ACTIVE") version.reservation.state = "RELEASED";
        checkout.state = "INVALIDATED";
        const denied = decision({ allowed: false, code: "REAPPROVAL_REQUIRED", explanation: "material_change_since_approval", checkout: ref, deltas, next_version: versionNumber + 1, correlation_id: checkout.correlation_id });
        emit(checkout, { actor: "kernel", action: "ADMISSION_DECISION", version: versionNumber, decision: { allowed: false, code: "REAPPROVAL_REQUIRED", reason: "material_change_since_approval" }, reconciliation_result: `${deltas.length} field(s) differ; version ${versionNumber} invalidated` });
        emit(checkout, { actor: "kernel", action: "RESERVATION_RELEASED", version: versionNumber, reconciliation_result: "cause: SUPERSEDED" });
        createVersion(checkout, requote.quote, nextContent, version, deltas);
        persist();
        return { decision: denied, outcome: "REAPPROVAL_REQUIRED", attempt_id: null, checkout: project(checkout) };
      }

      // Step 9: admitted exactly once; one grant; one Razorpay order.
      emit(checkout, { actor: "kernel", action: "REVALIDATION", version: versionNumber, source: SOURCE, policy_evaluated: "freshness: catalogue_revision", reconciliation_result: `approved revision ${version.quote.catalogue_revision} == current ${state.merchant.revision}` });
      const grantId = nextId("grt");
      const attempt: AttemptSummary = {
        attempt_id: nextId("att"),
        version: versionNumber,
        state: "CREATED",
        razorpay_order_id: null,
        razorpay_payment_id: null,
        grant_id: grantId,
        capture_evidence: null,
        reconciliation_attempts: 0,
      };
      checkout.attempt = attempt;
      version.state = "EXECUTION_PENDING";
      checkout.state = "EXECUTION_PENDING";
      const allowed = decision({ allowed: true, code: "OK", explanation: "admitted", checkout: ref, grant_id: grantId, payment_attempt_id: attempt.attempt_id, correlation_id: checkout.correlation_id });
      emit(checkout, { actor: "kernel", action: "ADMISSION_DECISION", version: versionNumber, decision: { allowed: true, code: "OK", reason: "admitted" }, grant: { grant_id: grantId, status: "ISSUED" }, authority_ref: version.approval.approval_id });
      attempt.razorpay_order_id = `order_MOCK${String(state.seq["att"] ?? 1).padStart(10, "0")}`;
      attempt.state = "SUBMITTED";
      version.state = "AWAITING_PAYMENT";
      checkout.state = "AWAITING_PAYMENT";
      emit(checkout, { actor: "worker", action: "PAYMENT_CREATE_ORDER", grant: { grant_id: grantId, status: "CONSUMED" }, provider_result: `razorpay order ${attempt.razorpay_order_id} amount ${version.amount_minor} ${version.currency}` });
      persist();
      return { decision: allowed, outcome: "OK", attempt_id: attempt.attempt_id, checkout: project(checkout) };
    },

    async cancelCheckout(checkoutId, reason): Promise<CancelResponse> {
      const checkout = requireCheckout(checkoutId);
      const version = currentVersion(checkout);
      if (CANCELLABLE.has(checkout.state)) {
        if (version.reservation?.state === "ACTIVE") version.reservation.state = "RELEASED";
        version.state = "CANCELLED";
        checkout.state = "CANCELLED";
        const allowed = decision({ allowed: true, code: "OK", explanation: "cancelled_within_policy", checkout: { checkout_id: checkoutId, version: version.version, content_hash: version.content_hash }, correlation_id: checkout.correlation_id });
        emit(checkout, { actor: "buyer", action: "CHECKOUT_CANCELLED", policy_evaluated: "cancellation@v3: before_dispatch", decision: { allowed: true, code: "OK", reason: reason ?? "buyer_cancelled" }, reconciliation_result: "reservation released" });
        persist();
        return { decision: allowed, checkout: project(checkout) };
      }
      if (checkout.state === "AWAITING_PAYMENT") {
        checkout.state = "INVALIDATED_AWAITING_PAYMENT_RESULT";
        version.state = "INVALIDATED_AWAITING_PAYMENT_RESULT";
        const allowed = decision({ allowed: true, code: "OK", explanation: "invalidated_awaiting_payment_result", checkout: { checkout_id: checkoutId, version: version.version, content_hash: version.content_hash }, correlation_id: checkout.correlation_id });
        emit(checkout, { actor: "kernel", action: "OPEN_CHECKOUT_INVALIDATED", policy_evaluated: "spec 10.8", reconciliation_result: "payment surface was open: awaiting result; any capture will be refunded" });
        persist();
        return { decision: allowed, checkout: project(checkout) };
      }
      const denied = decision({ allowed: false, code: "POLICY_EXCEPTION", explanation: checkout.state === "PAID" ? "cancel_window_closed_request_refund" : "not_cancellable_in_state", checkout: { checkout_id: checkoutId, version: version.version, content_hash: version.content_hash }, correlation_id: checkout.correlation_id });
      emit(checkout, { actor: "kernel", action: "CANCEL_DENIED", decision: { allowed: false, code: "POLICY_EXCEPTION", reason: denied.explanation } });
      return { decision: denied, checkout: project(checkout) };
    },

    async getPaymentHandoff(checkoutId): Promise<PaymentHandoff> {
      const checkout = requireCheckout(checkoutId);
      const version = currentVersion(checkout);
      return {
        checkout_id: checkoutId,
        version: version.version,
        attempt_id: checkout.attempt?.attempt_id ?? null,
        state: checkout.attempt?.state ?? null,
        provider: "razorpay",
        razorpay_key_id: MOCK_RAZORPAY_KEY_ID,
        razorpay_order_id: checkout.attempt?.razorpay_order_id ?? null,
        amount_minor: version.amount_minor,
        currency: version.currency,
        merchant_name: "Demo Grocery Store",
        description: `Checkout ${checkoutId} v${version.version}`,
        prefill: { name: "Demo Buyer", email: "buyer@example.test", contact: "9999999999" },
      };
    },

    async verifyPayment(body: VerifyRequest): Promise<VerifyResponse> {
      const checkout = requireCheckout(body.checkout_id);
      const attempt = requireAttempt(checkout);
      if (attempt.razorpay_order_id !== body.razorpay_order_id) {
        throw problem(422, "Order mismatch", "razorpay_order_id does not belong to this checkout", "AUTHORITY_INSUFFICIENT");
      }
      if (mockSignature(body.razorpay_order_id, body.razorpay_payment_id) !== body.razorpay_signature) {
        emit(checkout, { actor: "kernel", action: "BROWSER_CALLBACK_REJECTED", provider_result: "signature verification failed" });
        throw problem(422, "Signature verification failed", "The callback signature did not verify server-side", "PAYMENT_UNKNOWN");
      }
      attempt.razorpay_payment_id = body.razorpay_payment_id;
      emit(checkout, { actor: "buyer", action: "BROWSER_CALLBACK_RECORDED", provider_result: `signature verified for ${body.razorpay_payment_id}`, reconciliation_result: "recorded as BROWSER_CALLBACK; not capture evidence (ADR D8)" });
      emit(checkout, { actor: "worker", action: "RECONCILE_PAYMENT_ENQUEUED", reconciliation_result: "durable command queued; capture applies only from PROVIDER_FETCH or WEBHOOK" });
      const kind: PendingKind =
        checkout.state === "INVALIDATED_AWAITING_PAYMENT_RESULT" ? "LATE_CAPTURE" : checkout.scenario === "PAYMENT_UNKNOWN" ? "UNKNOWN" : "CAPTURE";
      checkout.scenario = null;
      schedule(checkout, { kind, step: 0 });
      return {
        accepted: true,
        attempt_id: attempt.attempt_id,
        state: attempt.state,
        evidence_kind: "BROWSER_CALLBACK",
        message: "Callback recorded. Capture is verified server-side from Razorpay evidence; the timeline will report CAPTURED.",
      };
    },

    async getOrder(orderId) {
      const order = state.orders[orderId];
      if (!order) throw problem(404, "Order not found", `No order ${orderId} for this session`);
      return order;
    },

    async requestRefund(orderId, body: RefundRequest): Promise<RefundResponse> {
      const order = state.orders[orderId];
      if (!order) throw problem(404, "Order not found", `No order ${orderId} for this session`);
      const checkout = requireCheckout(order.checkout_id);
      const refunded = order.refunds.filter((r) => r.state === "REFUNDED" || r.state === "REFUND_PENDING").reduce((sum, r) => sum + r.amount_minor, 0);
      const remaining = order.amount_minor - refunded;
      const amount = body.amount_minor ?? remaining;
      const ref = { checkout_id: checkout.checkout_id, version: order.version, content_hash: order.content_hash };
      if (!["CONFIRMED", "PARTIALLY_REFUNDED"].includes(order.state) || amount <= 0 || amount > remaining) {
        const denied = decision({ allowed: false, code: "REFUND_REVIEW_REQUIRED", explanation: amount > remaining ? "amount_exceeds_refundable" : "order_not_refundable_in_state", checkout: ref, correlation_id: checkout.correlation_id });
        emit(checkout, { actor: "kernel", action: "REFUND_DENIED", decision: { allowed: false, code: "REFUND_REVIEW_REQUIRED", reason: denied.explanation } });
        return { decision: denied, refund: null, order };
      }
      const grant = nextId("grt");
      const refund: Refund = { refund_id: nextId("rfd"), amount_minor: amount, currency: order.currency, state: "REFUND_PENDING", reason: body.reason, automatic: false, created_at: nowIso() };
      order.refunds.push(refund);
      const attempt = requireAttempt(checkout);
      attempt.state = "REFUND_PENDING";
      syncOrderPayment(checkout);
      const allowed = decision({ allowed: true, code: "OK", explanation: "refund_admitted", checkout: ref, grant_id: grant, payment_attempt_id: attempt.attempt_id, correlation_id: checkout.correlation_id });
      emit(checkout, { actor: "buyer", action: "REFUND_REQUESTED", authority_ref: refund.refund_id, policy_evaluated: "refund@v2: buyer_confirmed", decision: { allowed: true, code: "OK", reason: "refund_admitted" }, grant: { grant_id: grant, status: "ISSUED" }, provider_result: `refund ${refund.refund_id} ${amount} ${order.currency}` });
      schedule(checkout, { kind: "REFUND", step: 0, refund_id: refund.refund_id });
      return { decision: allowed, refund, order };
    },

    async getTimeline(checkoutId): Promise<TimelineResponse> {
      requireCheckout(checkoutId);
      return { checkout_id: checkoutId, rows: (state.events[checkoutId] ?? []).map((event) => event.row) };
    },

    subscribeEvents(checkoutId, { lastEventId, onEvent, onStatus }: EventSubscriptionOptions) {
      const set = listeners.get(checkoutId) ?? new Set();
      listeners.set(checkoutId, set);
      set.add(onEvent);
      onStatus?.("connecting");
      const backlog = state.events[checkoutId] ?? [];
      const start = lastEventId ? backlog.findIndex((event) => event.event_id === lastEventId) + 1 : 0;
      const replay = backlog.slice(Math.max(start, 0));
      const timer = setTimeout(() => {
        onStatus?.("open");
        for (const event of replay) onEvent(event);
      }, 0);
      return () => {
        clearTimeout(timer);
        set.delete(onEvent);
        onStatus?.("closed");
      };
    },

    async getProof(checkoutId): Promise<ProofChain> {
      const checkout = requireCheckout(checkoutId);
      const version = currentVersion(checkout);
      const events = state.events[checkoutId] ?? [];
      const attempt = checkout.attempt;
      const order = checkout.order_id ? state.orders[checkout.order_id] : null;
      const admission = events.find((e) => e.row.action === "ADMISSION_DECISION" && e.row.decision?.allowed && e.row.version === version.version);
      const terminal = attempt ? ["CAPTURED", "REFUNDED", "PARTIALLY_REFUNDED", "FAILED"].includes(attempt.state) : false;
      const links: ProofLink[] = [
        { step: 1, name: "Buyer intent and trusted-surface principal", present: true, verified: true, reference: `session/${checkout.correlation_id}`, hash: null, detail: "Trusted buyer surface; no LLM tool issued this checkout." },
        { step: 2, name: "Authoritative merchant facts and freshness", present: true, verified: true, reference: `${SOURCE} @ revision ${version.quote.catalogue_revision}`, hash: null, detail: "Prices and stock read at one catalogue revision." },
        { step: 3, name: "Checkout version and JCS content hash", present: true, verified: true, reference: `${checkout.checkout_id} v${version.version}`, hash: version.content_hash, detail: "SHA-256 over RFC 8785 canonical content." },
        { step: 4, name: "Policy-at-Sale Receipt hash", present: true, verified: true, reference: version.policy_receipt_id, hash: version.policy_receipt_hash, detail: "Bound into the approval and copied to the order." },
        { step: 5, name: "Buyer approval and authority epoch", present: version.approval !== null, verified: version.approval?.content_hash === version.content_hash && version.approval?.amount_minor === version.amount_minor, reference: version.approval?.approval_id ?? null, hash: version.approval?.content_hash ?? null, detail: version.approval ? `Approved ${version.approval.amount_minor} ${version.approval.currency}, epoch ${version.approval.authority_epoch}` : "No approval recorded for this version." },
        { step: 6, name: "Kernel admission decision", present: admission !== undefined, verified: admission !== undefined, reference: admission?.event_id ?? null, hash: null, detail: admission ? "Allowed under row locks; single winner." : "Not admitted." },
        { step: 7, name: "Single-use Execution Grant and durable command", present: attempt?.grant_id != null, verified: attempt?.grant_id != null, reference: attempt?.grant_id ?? null, hash: null, detail: attempt?.grant_id ? "Issued at admission, consumed by PAYMENT_CREATE_ORDER." : "No grant." },
        { step: 8, name: "Redacted Razorpay request reference", present: attempt?.razorpay_order_id != null, verified: attempt?.razorpay_order_id != null, reference: attempt?.razorpay_order_id ?? null, hash: null, detail: attempt?.razorpay_order_id ? `Order amount ${version.amount_minor} ${version.currency} equals the approved total.` : "No provider order." },
        { step: 9, name: "Verified provider evidence", present: attempt?.capture_evidence != null, verified: attempt?.capture_evidence != null, reference: attempt?.capture_evidence ? `${attempt.capture_evidence.kind}: ${attempt.capture_evidence.reference}` : null, hash: null, detail: attempt?.capture_evidence ? "Capture applied from webhook or provider fetch, never from the browser callback." : "No capture evidence yet." },
        { step: 10, name: "Final PostgreSQL state", present: terminal || order !== null, verified: terminal, reference: order ? `${order.order_id} ${order.state}` : attempt ? attempt.state : checkout.state, hash: null, detail: terminal ? "Terminal payment state agrees with the order state." : "Not terminal yet." },
      ];
      const ok = links.every((link) => link.present && link.verified);
      return {
        checkout_id: checkoutId,
        version: version.version,
        links,
        verdict: {
          ok,
          summary: ok ? "All ten links present, correlated and consistent." : `${links.filter((l) => !l.present).length} link(s) missing; the action cannot be called complete.`,
          checks: [
            { name: "tenant/merchant correlation", ok: true, detail: "single tenant in mock" },
            { name: "hash chain", ok: true, detail: `${events.length} audit rows, no gaps` },
            { name: "amount and currency", ok: version.approval ? version.approval.amount_minor === version.amount_minor : false, detail: `${version.amount_minor} ${version.currency}` },
            { name: "grant consumption", ok: attempt?.grant_id != null, detail: attempt?.grant_id ? "exactly one grant consumed" : "no grant" },
            { name: "final-state consistency", ok: terminal, detail: attempt?.state ?? "no attempt" },
          ],
        },
        attempt_id: attempt?.attempt_id ?? null,
      };
    },

    async getConfig(): Promise<RuntimeConfig> {
      return {
        profile: "mock",
        razorpay_mode: "test (simulated; no network)",
        safe_mode: false,
        degraded: [{ component: "voice", notice: "Realtime voice is not connected in this build. Text mode is active; transcripts are never payment authority." }],
      };
    },

    inspectorUrl: () => null,
    proofUrl: () => null,
    auditVerifyUrl: () => null,

    scenario: {
      async armPaymentUnknown(checkoutId) {
        const checkout = requireCheckout(checkoutId);
        checkout.scenario = "PAYMENT_UNKNOWN";
        emit(checkout, { actor: "operator", action: "SCENARIO_INJECTION FAULT_ARMED", provider_result: "worker fault: lose provider response on next fetch", scenario_injection: true });
        return project(checkout);
      },
      async lateCapture(checkoutId) {
        const checkout = requireCheckout(checkoutId);
        if (checkout.state === "AWAITING_PAYMENT") {
          checkout.state = "INVALIDATED_AWAITING_PAYMENT_RESULT";
          currentVersion(checkout).state = "INVALIDATED_AWAITING_PAYMENT_RESULT";
          emit(checkout, { actor: "operator", action: "SCENARIO_INJECTION INVALIDATE_OPEN_CHECKOUT", reconciliation_result: "open checkout invalidated while the payment surface is open (spec 10.8)", scenario_injection: true });
        }
        if (checkout.state !== "INVALIDATED_AWAITING_PAYMENT_RESULT") {
          throw problem(409, "Late capture needs an open checkout", `Checkout is ${checkout.state}`);
        }
        const attempt = requireAttempt(checkout);
        attempt.razorpay_payment_id ??= nextId("pay_MOCK", 6);
        schedule(checkout, { kind: "LATE_CAPTURE", step: 0 });
        return project(checkout);
      },
      async replayWebhook(checkoutId) {
        const checkout = requireCheckout(checkoutId);
        emit(checkout, { actor: "operator", action: "SCENARIO_INJECTION WEBHOOK_REPLAY", scenario_injection: true, provider_result: "stored raw webhook replayed" });
        emit(checkout, { actor: "razorpay", action: "WEBHOOK_DUPLICATE_IGNORED", provider_result: "inbox dedupe on x-razorpay-event-id", reconciliation_result: "no state change" });
        return project(checkout);
      },
      async reset() {
        for (const timer of timers.values()) clearTimeout(timer);
        timers.clear();
        state = freshState();
        persist();
      },
    },
  };

  function requireBasket(basketId: string): MockBasket {
    const basket = state.baskets[basketId];
    if (!basket) throw problem(404, "Basket not found", `No basket ${basketId} for this session`);
    return basket;
  }

  function projectBasket(basket: MockBasket, stale: boolean): Basket {
    const result = quoteLines(basket.lines);
    return {
      basket_id: basket.basket_id,
      lines: basket.lines,
      code: result.code,
      quote: result.quote,
      unavailable: result.unavailable,
      freshness: freshness(),
      stale,
    };
  }

  return client;
}

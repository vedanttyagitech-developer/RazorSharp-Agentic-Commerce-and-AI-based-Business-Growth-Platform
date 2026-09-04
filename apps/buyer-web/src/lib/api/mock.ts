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

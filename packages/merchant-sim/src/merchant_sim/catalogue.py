"""The Demo Grocery Store catalogue fixture.

Synthetic products for a Zepto-class quick-commerce journey (specification 8). Nothing
here is an official integration with any real retailer; brand-shaped names exist because a
tokenizer that only ever sees ``Milk 1 L`` is not tested at all.

WHAT A ``Product`` IS AND IS NOT
-------------------------------
A ``Product`` is *immutable catalogue data*: identity, names, pack size, tax rate and
search synonyms. It deliberately does NOT carry stock, price or availability. Those are
volatile merchant state and live in :class:`~merchant_sim.store.MerchantStore`, which can
only be changed through a labelled scenario injection. Keeping them apart is what makes
``reset()`` exact and what stops a demo mutation from editing the fixture in place.

``list_price`` is the *baseline* price the store is seeded with, not the live one. Read a
live price from the store, never from here.

TAX
---
``tax_bp`` is a GST rate in basis points (500 = 5%). Rates follow the real Indian
schedule's shape -- fresh milk, eggs and loose vegetables at 0%, staples at 5%, processed
food at 12%, biscuits and household chemicals at 18%, aerated drinks at 28% -- because a
single flat rate would let a per-line rounding bug pass every test.

AWKWARD NAMES ARE ON PURPOSE
----------------------------
Several entries carry punctuation and Unicode that breaks naive tokenizers: an em dash, a
multiplication sign, possessive apostrophes, an acute accent, digits inside a brand name, a
non-breaking space and a soft hyphen. If the tokenizer regresses, these fail first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from commerce_domain import Money

__all__ = ["CATALOGUE", "CURRENCY", "PRODUCTS_BY_SKU", "Category", "Product"]

#: The demo store trades in one currency. A mixed-currency basket is a different problem
#: (settlement, FX evidence) and is out of scope for the simulator.
CURRENCY: Final[str] = "INR"


class Category(StrEnum):
    """Merchandising category. Also indexed, so "dairy" is a usable query."""

    DAIRY = "dairy"
    STAPLES = "staples"
    PRODUCE = "produce"
    SNACKS = "snacks"
    BEVERAGES = "beverages"
    BAKERY = "bakery"
    HOUSEHOLD = "household"
    PERSONAL_CARE = "personal_care"
    CONDIMENTS = "condiments"


@dataclass(frozen=True, slots=True)
class Product:
    """One immutable catalogue record."""

    sku: str
    name_en: str
    name_hi: str
    category: Category
    unit_label: str
    list_price: Money
    baseline_stock: int
    tax_bp: int
    synonyms_hi: tuple[str, ...]
    synonyms_latin: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.list_price.currency != CURRENCY:
            raise ValueError(f"{self.sku}: demo catalogue is {CURRENCY}-only")
        if self.list_price.minor <= 0:
            raise ValueError(f"{self.sku}: a catalogue price must be positive")
        if self.baseline_stock < 0:
            raise ValueError(f"{self.sku}: baseline stock cannot be negative")
        if not 0 <= self.tax_bp <= 10_000:
            raise ValueError(f"{self.sku}: tax_bp must be a rate in basis points")

    def display_name(self, *, devanagari: bool) -> str:
        """Name to show the buyer. Presentation only; never a search or pricing key."""
        return self.name_hi if devanagari else self.name_en


def _inr(minor: int) -> Money:
    return Money(minor, CURRENCY)


# --------------------------------------------------------------------------- the fixture
#
# Prices are exact paise. Two are chosen so that the free-delivery threshold can be tested
# to the paisa against the default policy of a Rs 499.00 threshold:
#   * GRO-STPL-001 is 49900 paise, so one unit lands exactly ON the threshold.
#   * GRO-STPL-007 is 16633 paise, so three units land exactly one paisa BELOW it.
# Do not "tidy" either price without fixing packages/merchant-sim/tests/test_fees.py.

CATALOGUE: Final[tuple[Product, ...]] = (
    # ---- dairy ----------------------------------------------------------------
    Product(
        sku="GRO-DAIRY-001",
        name_en="Amul Taaza Toned Milk 500 ml",
        name_hi="अमूल ताज़ा टोंड दूध 500 मिली",
        category=Category.DAIRY,
        unit_label="500 ml",
        list_price=_inr(2800),
        baseline_stock=48,
        tax_bp=0,
        synonyms_hi=("दूध", "ताज़ा दूध"),
        synonyms_latin=("doodh", "dudh", "milk", "taaza", "toned milk"),
    ),
    Product(
        sku="GRO-DAIRY-002",
        name_en="Amul Gold Full Cream Milk 1 L",
        name_hi="अमूल गोल्ड फुल क्रीम दूध 1 लीटर",
        category=Category.DAIRY,
        unit_label="1 L",
        list_price=_inr(7300),
        baseline_stock=30,
        tax_bp=0,
        synonyms_hi=("दूध", "फुल क्रीम दूध"),
        synonyms_latin=("doodh", "dudh", "milk", "full cream", "gold"),
    ),
    Product(
        sku="GRO-DAIRY-003",
        name_en="Amul Masti Dahi 400 g",
        name_hi="अमूल मस्ती दही 400 ग्राम",
        category=Category.DAIRY,
        unit_label="400 g",
        list_price=_inr(4500),
        baseline_stock=24,
        tax_bp=500,
        synonyms_hi=("दही",),
        synonyms_latin=("dahi", "curd", "yoghurt", "yogurt"),
    ),
    Product(
        sku="GRO-DAIRY-004",
        name_en="Amul Malai Paneer Block 200 g",
        name_hi="अमूल मलाई पनीर 200 ग्राम",
        category=Category.DAIRY,
        unit_label="200 g",
        list_price=_inr(9500),
        baseline_stock=12,
        tax_bp=500,
        synonyms_hi=("पनीर",),
        synonyms_latin=("paneer", "panir", "cottage cheese", "malai"),
    ),
    Product(
        sku="GRO-DAIRY-005",
        name_en="Amul Butter (Salted) 100 g",
        name_hi="अमूल मक्खन (नमकीन) 100 ग्राम",
        category=Category.DAIRY,
        unit_label="100 g",
        list_price=_inr(5800),
        baseline_stock=20,
        tax_bp=1200,
        synonyms_hi=("मक्खन",),
        synonyms_latin=("makkhan", "makhan", "butter"),
    ),
    # Awkward name: composed acute accent, and a "+" that must not split the brand oddly.
    Product(
        sku="GRO-DAIRY-006",
        name_en="Nestlé A+ Slim Toned Milk 1 L",
        name_hi="नेस्ले ए+ स्लिम टोंड दूध 1 लीटर",
        category=Category.DAIRY,
        unit_label="1 L",
        list_price=_inr(7800),
        baseline_stock=15,
        tax_bp=0,
        synonyms_hi=("दूध", "स्लिम दूध"),
        synonyms_latin=("nestle", "slim milk", "doodh", "dudh", "milk"),
    ),
    # ---- staples --------------------------------------------------------------
    Product(
        sku="GRO-STPL-001",
        name_en="India Gate Classic Basmati Rice 5 kg",
        name_hi="इंडिया गेट क्लासिक बासमती चावल 5 किलो",
        category=Category.STAPLES,
        unit_label="5 kg",
        list_price=_inr(49900),
        baseline_stock=10,
        tax_bp=500,
        synonyms_hi=("चावल", "बासमती चावल"),
        synonyms_latin=("chawal", "chaval", "rice", "basmati"),
    ),
    Product(
        sku="GRO-STPL-002",
        name_en="Aashirvaad Shudh Chakki Atta 5 kg",
        name_hi="आशीर्वाद शुद्ध चक्की आटा 5 किलो",
        category=Category.STAPLES,
        unit_label="5 kg",
        list_price=_inr(25500),
        baseline_stock=18,
        tax_bp=500,
        synonyms_hi=("आटा", "गेहूं का आटा"),
        synonyms_latin=("atta", "aata", "flour", "gehun", "chakki atta"),
    ),
    # Awkward name: em dash and comma.
    Product(
        sku="GRO-STPL-003",
        name_en="Tata Salt — Iodised, 1 kg",
        name_hi="टाटा नमक — आयोडीन युक्त, 1 किलो",
        category=Category.STAPLES,
        unit_label="1 kg",
        list_price=_inr(2800),
        baseline_stock=40,
        tax_bp=500,
        synonyms_hi=("नमक",),
        synonyms_latin=("namak", "salt", "iodised", "iodized"),
    ),
    Product(
        sku="GRO-STPL-004",
        name_en="Toor Dal (Arhar) 1 kg",
        name_hi="तूर दाल (अरहर) 1 किलो",
        category=Category.STAPLES,
        unit_label="1 kg",
        list_price=_inr(18500),
        baseline_stock=22,
        tax_bp=500,
        synonyms_hi=("दाल", "तूर दाल", "अरहर दाल"),
        synonyms_latin=("dal", "daal", "toor dal", "tur dal", "arhar", "lentil", "pulses"),
    ),
    Product(
        sku="GRO-STPL-005",
        name_en="Chana Dal 500 g",
        name_hi="चना दाल 500 ग्राम",
        category=Category.STAPLES,
        unit_label="500 g",
        list_price=_inr(6200),
        baseline_stock=25,
        tax_bp=500,
        synonyms_hi=("चना दाल", "चना"),
        synonyms_latin=("chana", "chana dal", "gram", "bengal gram"),
    ),
    # Awkward name: ampersand between words.
    Product(
        sku="GRO-STPL-006",
        name_en="Madhur Pure & Hygienic Sugar 1 kg",
        name_hi="मधुर शुद्ध चीनी 1 किलो",
        category=Category.STAPLES,
        unit_label="1 kg",
        list_price=_inr(5200),
        baseline_stock=30,
        tax_bp=500,
        synonyms_hi=("चीनी", "शक्कर"),
        synonyms_latin=("cheeni", "chini", "sugar", "shakkar", "shakar"),
    ),
    Product(
        sku="GRO-STPL-007",
        name_en="Cold-Pressed Groundnut Oil 500 ml",
        name_hi="कोल्ड प्रेस्ड मूंगफली तेल 500 मिली",
        category=Category.STAPLES,
        unit_label="500 ml",
        list_price=_inr(16633),
        baseline_stock=14,
        tax_bp=500,
        synonyms_hi=("तेल", "मूंगफली तेल"),
        synonyms_latin=("tel", "oil", "moongfali", "mungfali", "groundnut", "peanut oil"),
    ),
    Product(
        sku="GRO-STPL-008",
        name_en="Fortune Sunlite Refined Sunflower Oil 1 L",
        name_hi="फॉर्च्यून सनलाइट रिफाइंड सूरजमुखी तेल 1 लीटर",
        category=Category.STAPLES,
        unit_label="1 L",
        list_price=_inr(14900),
        baseline_stock=20,
        tax_bp=500,
        synonyms_hi=("तेल", "सूरजमुखी तेल"),
        synonyms_latin=("tel", "oil", "sunflower", "refined oil", "fortune"),
    ),
    Product(
        sku="GRO-STPL-009",
        name_en="Rajma Chitra 500 g",
        name_hi="राजमा चित्रा 500 ग्राम",
        category=Category.STAPLES,
        unit_label="500 g",
        list_price=_inr(9800),
        baseline_stock=16,
        tax_bp=500,
        synonyms_hi=("राजमा",),
        synonyms_latin=("rajma", "kidney beans", "beans"),
    ),
    Product(
        sku="GRO-STPL-010",
        name_en="Poha (Flattened Rice) 500 g",
        name_hi="पोहा 500 ग्राम",
        category=Category.STAPLES,
        unit_label="500 g",
        list_price=_inr(4400),
        baseline_stock=20,
        tax_bp=500,
        synonyms_hi=("पोहा", "चिवड़ा"),
        synonyms_latin=("poha", "chivda", "flattened rice"),
    ),
    # ---- produce --------------------------------------------------------------
    Product(
        sku="GRO-PROD-001",
        name_en="Onion (Pyaz) 1 kg",
        name_hi="प्याज 1 किलो",
        category=Category.PRODUCE,
        unit_label="1 kg",
        list_price=_inr(4200),
        baseline_stock=35,
        tax_bp=0,
        synonyms_hi=("प्याज",),
        synonyms_latin=("pyaz", "pyaaz", "pyaj", "onion", "kanda"),
    ),
    Product(
        sku="GRO-PROD-002",
        name_en="Tomato (Tamatar) 1 kg",
        name_hi="टमाटर 1 किलो",
        category=Category.PRODUCE,
        unit_label="1 kg",
        list_price=_inr(3800),
        baseline_stock=28,
        tax_bp=0,
        synonyms_hi=("टमाटर",),
        synonyms_latin=("tamatar", "tomato", "tamater"),
    ),
    Product(
        sku="GRO-PROD-003",
        name_en="Potato (Aloo) 1 kg",
        name_hi="आलू 1 किलो",
        category=Category.PRODUCE,
        unit_label="1 kg",
        list_price=_inr(3200),
        baseline_stock=45,
        tax_bp=0,
        synonyms_hi=("आलू",),
        synonyms_latin=("aloo", "alu", "potato", "batata"),
    ),
    Product(
        sku="GRO-PROD-004",
        name_en="Loose Ginger (Adrak) 100 g",
        name_hi="अदरक 100 ग्राम",
        category=Category.PRODUCE,
        unit_label="100 g",
        list_price=_inr(2400),
        baseline_stock=18,
        tax_bp=0,
        synonyms_hi=("अदरक",),
        synonyms_latin=("adrak", "ginger"),
    ),
    Product(
        sku="GRO-PROD-005",
        name_en="Garlic (Lehsun) 200 g",
        name_hi="लहसुन 200 ग्राम",
        category=Category.PRODUCE,
        unit_label="200 g",
        list_price=_inr(3600),
        baseline_stock=16,
        tax_bp=0,
        synonyms_hi=("लहसुन",),
        synonyms_latin=("lehsun", "lahsun", "garlic"),
    ),
    Product(
        sku="GRO-PROD-006",
        name_en="Banana (Kela) — 6 pcs",
        name_hi="केला — 6 नग",
        category=Category.PRODUCE,
        unit_label="6 pcs",
        list_price=_inr(5400),
        baseline_stock=20,
        tax_bp=0,
        synonyms_hi=("केला",),
        synonyms_latin=("kela", "banana", "fruit"),
    ),
    Product(
        sku="GRO-PROD-007",
        name_en="Coriander Leaves (Hara Dhania) 100 g",
        name_hi="हरा धनिया 100 ग्राम",
        category=Category.PRODUCE,
        unit_label="100 g",
        list_price=_inr(1500),
        baseline_stock=12,
        tax_bp=0,
        synonyms_hi=("धनिया", "हरा धनिया"),
        synonyms_latin=("dhania", "dhaniya", "hara dhania", "coriander", "cilantro"),
    ),
    Product(
        sku="GRO-PROD-008",
        name_en="Green Chilli (Hari Mirch) 100 g",
        name_hi="हरी मिर्च 100 ग्राम",
        category=Category.PRODUCE,
        unit_label="100 g",
        list_price=_inr(1800),
        baseline_stock=14,
        tax_bp=0,
        synonyms_hi=("मिर्च", "हरी मिर्च"),
        synonyms_latin=("mirch", "mirchi", "hari mirch", "chilli", "chili", "green chili"),
    ),
    Product(
        sku="GRO-PROD-009",
        name_en="Lemon (Nimbu) — 4 pcs",
        name_hi="नींबू — 4 नग",
        category=Category.PRODUCE,
        unit_label="4 pcs",
        list_price=_inr(2000),
        baseline_stock=26,
        tax_bp=0,
        synonyms_hi=("नींबू",),
        synonyms_latin=("nimbu", "neembu", "lemon", "lime"),
    ),
    # ---- snacks ---------------------------------------------------------------
    # Awkward name: digits and a hyphen inside the product name, plus parentheses.
    Product(
        sku="GRO-SNCK-001",
        name_en="Maggi 2-Minute Masala Noodles (Pack of 4)",
        name_hi="मैगी 2-मिनट मसाला नूडल्स (4 का पैक)",
        category=Category.SNACKS,
        unit_label="4 x 70 g",
        list_price=_inr(9600),
        baseline_stock=26,
        tax_bp=1200,
        synonyms_hi=("मैगी", "नूडल्स"),
        synonyms_latin=("maggi", "magi", "noodles", "instant noodles"),
    ),
    # Awkward name: a brand that is literally two numbers joined by a hyphen.
    Product(
        sku="GRO-SNCK-002",
        name_en="Britannia 50-50 Maska Chaska 120 g",
        name_hi="ब्रिटानिया 50-50 मस्का चस्का 120 ग्राम",
        category=Category.SNACKS,
        unit_label="120 g",
        list_price=_inr(3500),
        baseline_stock=30,
        tax_bp=1800,
        synonyms_hi=("बिस्किट", "मस्का चस्का"),
        synonyms_latin=("biscuit", "biskut", "maska chaska", "britannia", "cookies"),
    ),
    # Awkward name: possessive apostrophe.
    Product(
        sku="GRO-SNCK-003",
        name_en="Haldiram's Aloo Bhujia 200 g",
        name_hi="हल्दीराम आलू भुजिया 200 ग्राम",
        category=Category.SNACKS,
        unit_label="200 g",
        list_price=_inr(5800),
        baseline_stock=22,
        tax_bp=1200,
        synonyms_hi=("भुजिया", "नमकीन", "आलू भुजिया"),
        synonyms_latin=("bhujia", "namkeen", "aloo bhujia", "haldiram"),
    ),
    # Awkward name: two apostrophes in one string.
    Product(
        sku="GRO-SNCK-004",
        name_en="Lay's India's Magic Masala 52 g",
        name_hi="लेज़ इंडियाज़ मैजिक मसाला 52 ग्राम",
        category=Category.SNACKS,
        unit_label="52 g",
        list_price=_inr(2000),
        baseline_stock=40,
        tax_bp=1800,
        synonyms_hi=("चिप्स", "वेफर्स"),
        synonyms_latin=("chips", "lays", "wafers", "magic masala"),
    ),
    Product(
        sku="GRO-SNCK-005",
        name_en="Parle-G Gold Biscuits 200 g",
        name_hi="पारले-जी गोल्ड बिस्किट 200 ग्राम",
        category=Category.SNACKS,
        unit_label="200 g",
        list_price=_inr(3000),
        baseline_stock=35,
        tax_bp=1800,
        synonyms_hi=("बिस्किट", "पारले जी"),
        synonyms_latin=("parle g", "parleg", "biscuit", "biskut", "glucose biscuit"),
    ),
    Product(
        sku="GRO-SNCK-006",
        name_en="Kellogg's Chocos 375 g",
        name_hi="केलॉग्स चॉकोस 375 ग्राम",
        category=Category.SNACKS,
        unit_label="375 g",
        list_price=_inr(25500),
        baseline_stock=10,
        tax_bp=1800,
        synonyms_hi=("चॉकोस", "सीरियल"),
        synonyms_latin=("chocos", "cereal", "kellogs", "cornflakes"),
    ),
    # ---- beverages ------------------------------------------------------------
    Product(
        sku="GRO-BEVG-001",
        name_en="Tata Tea Gold 500 g",
        name_hi="टाटा टी गोल्ड 500 ग्राम",
        category=Category.BEVERAGES,
        unit_label="500 g",
        list_price=_inr(28500),
        baseline_stock=16,
        tax_bp=500,
        synonyms_hi=("चाय", "चाय पत्ती"),
        synonyms_latin=("chai", "chay", "tea", "chaipatti", "tea leaves"),
    ),
    # Awkward name: acute accent on the brand.
    Product(
        sku="GRO-BEVG-002",
        name_en="Nescafé Classic Instant Coffee 50 g",
        name_hi="नेस्कैफे क्लासिक इंस्टेंट कॉफ़ी 50 ग्राम",
        category=Category.BEVERAGES,
        unit_label="50 g",
        list_price=_inr(18500),
        baseline_stock=12,
        tax_bp=1800,
        synonyms_hi=("कॉफ़ी",),
        synonyms_latin=("coffee", "cofee", "kaufi", "nescafe", "instant coffee"),
    ),
    Product(
        sku="GRO-BEVG-003",
        name_en="Coca-Cola 750 ml",
        name_hi="कोका-कोला 750 मिली",
        category=Category.BEVERAGES,
        unit_label="750 ml",
        list_price=_inr(4000),
        baseline_stock=30,
        tax_bp=2800,
        synonyms_hi=("कोक", "ठंडा", "कोल्ड ड्रिंक"),
        synonyms_latin=("coke", "cola", "thanda", "cold drink", "soft drink"),
    ),
    # Awkward name: em dash separating brand from descriptor.
    Product(
        sku="GRO-BEVG-004",
        name_en="Real Fruit Power — Mixed Fruit Juice 1 L",
        name_hi="रियल फ्रूट पावर — मिक्स्ड फ्रूट जूस 1 लीटर",
        category=Category.BEVERAGES,
        unit_label="1 L",
        list_price=_inr(11000),
        baseline_stock=18,
        tax_bp=1200,
        synonyms_hi=("जूस", "फलों का रस"),
        synonyms_latin=("juice", "jus", "fruit juice", "phal", "real"),
    ),
    Product(
        sku="GRO-BEVG-005",
        name_en="Bisleri Mineral Water 1 L",
        name_hi="बिसलेरी मिनरल पानी 1 लीटर",
        category=Category.BEVERAGES,
        unit_label="1 L",
        list_price=_inr(2000),
        baseline_stock=50,
        tax_bp=1800,
        synonyms_hi=("पानी",),
        synonyms_latin=("pani", "paani", "water", "bisleri", "mineral water"),
    ),
    # ---- bakery and eggs ------------------------------------------------------
    Product(
        sku="GRO-BAKE-001",
        name_en="Britannia Brown Bread 400 g",
        name_hi="ब्रिटानिया ब्राउन ब्रेड 400 ग्राम",
        category=Category.BAKERY,
        unit_label="400 g",
        list_price=_inr(5000),
        baseline_stock=20,
        tax_bp=500,
        synonyms_hi=("ब्रेड", "डबल रोटी", "पाव"),
        synonyms_latin=("bread", "bred", "pav", "double roti", "brown bread"),
    ),
    Product(
        sku="GRO-BAKE-002",
        name_en="Farm Eggs — Tray of 6",
        name_hi="फार्म अंडे — 6 की ट्रे",
        category=Category.BAKERY,
        unit_label="6 pcs",
        list_price=_inr(6600),
        baseline_stock=24,
        tax_bp=0,
        synonyms_hi=("अंडा", "अंडे"),
        synonyms_latin=("anda", "ande", "andey", "egg", "eggs"),
    ),
    # ---- household ------------------------------------------------------------
    Product(
        sku="GRO-HHLD-001",
        name_en="Surf Excel Easy Wash Detergent Powder 1 kg",
        name_hi="सर्फ़ एक्सेल ईज़ी वॉश डिटर्जेंट पाउडर 1 किलो",
        category=Category.HOUSEHOLD,
        unit_label="1 kg",
        list_price=_inr(13500),
        baseline_stock=15,
        tax_bp=1800,
        synonyms_hi=("सर्फ़", "डिटर्जेंट", "कपड़े धोने का पाउडर"),
        synonyms_latin=("surf", "detergent", "washing powder", "surf excel"),
    ),
    Product(
        sku="GRO-HHLD-002",
        name_en="Vim Dishwash Bar 300 g",
        name_hi="विम बर्तन साबुन 300 ग्राम",
        category=Category.HOUSEHOLD,
        unit_label="300 g",
        list_price=_inr(3000),
        baseline_stock=25,
        tax_bp=1800,
        synonyms_hi=("बर्तन साबुन", "विम"),
        synonyms_latin=("vim", "dishwash", "bartan sabun", "dish bar"),
    ),
    Product(
        sku="GRO-HHLD-003",
        name_en="Harpic Power Plus Toilet Cleaner 500 ml",
        name_hi="हार्पिक पावर प्लस टॉयलेट क्लीनर 500 मिली",
        category=Category.HOUSEHOLD,
        unit_label="500 ml",
        list_price=_inr(9900),
        baseline_stock=14,
        tax_bp=1800,
        synonyms_hi=("हार्पिक", "टॉयलेट क्लीनर"),
        synonyms_latin=("harpic", "toilet cleaner", "bathroom cleaner"),
    ),
    # Awkward name: NO-BREAK SPACE after the brand and a SOFT HYPHEN before the size.
    # Both are invisible. If normalization stops stripping them, this row silently
    # disappears from search results while still looking correct in every log.
    Product(
        sku="GRO-HHLD-004",
        name_en="Nirma Washing Powder­ 1 kg",
        name_hi="निरमा वॉशिंग पाउडर­ 1 किलो",
        category=Category.HOUSEHOLD,
        unit_label="1 kg",
        list_price=_inr(7800),
        baseline_stock=20,
        tax_bp=1800,
        synonyms_hi=("निरमा", "वॉशिंग पाउडर"),
        synonyms_latin=("nirma", "washing powder", "detergent"),
    ),
    # ---- personal care --------------------------------------------------------
    Product(
        sku="GRO-PERS-001",
        name_en="Colgate Strong Teeth Toothpaste 200 g",
        name_hi="कोलगेट स्ट्रॉन्ग टीथ टूथपेस्ट 200 ग्राम",
        category=Category.PERSONAL_CARE,
        unit_label="200 g",
        list_price=_inr(11500),
        baseline_stock=20,
        tax_bp=1800,
        synonyms_hi=("टूथपेस्ट", "मंजन", "पेस्ट"),
        synonyms_latin=("toothpaste", "colgate", "manjan", "paste", "tooth paste"),
    ),
    # Awkward name: MULTIPLICATION SIGN between count and pack size.
    Product(
        sku="GRO-PERS-002",
        name_en="Dettol Original Soap — 4 × 125 g",
        name_hi="डेटॉल ओरिजिनल साबुन — 4 × 125 ग्राम",
        category=Category.PERSONAL_CARE,
        unit_label="4 x 125 g",
        list_price=_inr(17600),
        baseline_stock=18,
        tax_bp=1800,
        synonyms_hi=("साबुन", "डेटॉल"),
        synonyms_latin=("sabun", "saabun", "soap", "dettol", "bathing bar"),
    ),
    Product(
        sku="GRO-PERS-003",
        name_en="Head & Shoulders Anti-Dandruff Shampoo 340 ml",
        name_hi="हेड एंड शोल्डर्स एंटी-डैंड्रफ शैम्पू 340 मिली",
        category=Category.PERSONAL_CARE,
        unit_label="340 ml",
        list_price=_inr(39900),
        baseline_stock=8,
        tax_bp=1800,
        synonyms_hi=("शैम्पू",),
        synonyms_latin=("shampoo", "shampu", "anti dandruff", "head and shoulders"),
    ),
    # ---- condiments -----------------------------------------------------------
    # Awkward name: possessive apostrophe on a common noun.
    Product(
        sku="GRO-COND-001",
        name_en="Mother's Recipe Mango Pickle 400 g",
        name_hi="मदर्स रेसिपी आम का अचार 400 ग्राम",
        category=Category.CONDIMENTS,
        unit_label="400 g",
        list_price=_inr(9900),
        baseline_stock=16,
        tax_bp=1200,
        synonyms_hi=("अचार", "आम का अचार"),
        synonyms_latin=("achar", "aachar", "pickle", "aam ka achar", "mango pickle"),
    ),
    Product(
        sku="GRO-COND-002",
        name_en="MDH Garam Masala 100 g",
        name_hi="एमडीएच गरम मसाला 100 ग्राम",
        category=Category.CONDIMENTS,
        unit_label="100 g",
        list_price=_inr(8500),
        baseline_stock=22,
        tax_bp=500,
        synonyms_hi=("गरम मसाला", "मसाला"),
        synonyms_latin=("garam masala", "masala", "mdh", "spice mix"),
    ),
    Product(
        sku="GRO-COND-003",
        name_en="Kissan Fresh Tomato Ketchup 950 g",
        name_hi="किसान फ्रेश टमाटर केचप 950 ग्राम",
        category=Category.CONDIMENTS,
        unit_label="950 g",
        list_price=_inr(14500),
        baseline_stock=12,
        tax_bp=1200,
        synonyms_hi=("केचप", "टमाटर सॉस"),
        synonyms_latin=("ketchup", "sauce", "tomato sauce", "kissan"),
    ),
    Product(
        sku="GRO-COND-004",
        name_en="Everest Haldi (Turmeric) Powder 200 g",
        name_hi="एवरेस्ट हल्दी पाउडर 200 ग्राम",
        category=Category.CONDIMENTS,
        unit_label="200 g",
        list_price=_inr(7200),
        baseline_stock=20,
        tax_bp=500,
        synonyms_hi=("हल्दी", "हल्दी पाउडर"),
        synonyms_latin=("haldi", "turmeric", "haldi powder"),
    ),
)


def _build_index() -> dict[str, Product]:
    index: dict[str, Product] = {}
    for product in CATALOGUE:
        if product.sku in index:
            # A duplicate SKU would make get_product non-deterministic in which record it
            # returns, and would let a scenario injection edit an invisible twin.
            raise ValueError(f"duplicate SKU in catalogue fixture: {product.sku}")
        index[product.sku] = product
    return index


#: SKU -> immutable catalogue record. Built once; the fixture never changes at runtime.
PRODUCTS_BY_SKU: Final[dict[str, Product]] = _build_index()

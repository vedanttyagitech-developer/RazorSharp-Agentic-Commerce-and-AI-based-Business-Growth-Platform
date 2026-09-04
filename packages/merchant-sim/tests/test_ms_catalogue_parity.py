"""Automated 3-way catalogue parity test across backend, buyer-web, and merchant-console.

Asserts exact 1:1 match across:
  1. packages/merchant-sim/src/merchant_sim/catalogue.py (Authority: CATALOGUE / PRODUCTS_BY_SKU)
  2. apps/buyer-web/src/lib/api/mock.ts (Storefront Fixture: FIXTURE)
  3. apps/merchant-console/src/lib/api/products-data.ts (Merchant Console: CATALOGUE_PRODUCTS)

Checks for all 247 products:
  - Exact SKU presence
  - English Product Name
  - Category Taxonomy
  - Packaging Unit
  - List Price in Integer Minor Units (paise)
  - Baseline Inventory Stock
  - GST Basis Points (tax_bp / tax_basis_points)

Provides precise failure diagnostics identifying SKU and diverging field.
"""

import json
import re
from pathlib import Path
from typing import Any

from merchant_sim.catalogue import PRODUCTS_BY_SKU

REPO_ROOT = Path(__file__).resolve().parents[3]
MOCK_TS_PATH = REPO_ROOT / "apps/buyer-web/src/lib/api/mock.ts"
PRODUCTS_TS_PATH = REPO_ROOT / "apps/merchant-console/src/lib/api/products-data.ts"


def _load_buyer_web_fixture() -> dict[str, dict[str, Any]]:
    assert MOCK_TS_PATH.exists(), f"Storefront mock fixture not found at {MOCK_TS_PATH}"
    text = MOCK_TS_PATH.read_text(encoding="utf-8")
    start = text.find("const FIXTURE: FixtureProduct[] = [")
    assert start != -1, "const FIXTURE: FixtureProduct[] array not found in mock.ts"
    end = text.find("\n];\n", start)
    raw = text[start + len("const FIXTURE: FixtureProduct[] = ") : end + 2]
    # Quote unquoted JS object keys
    json_str = re.sub(r"(\b[a-zA-Z_][a-zA-Z0-9_]*\b)\s*:", r'"\g<1>":', raw)
    # Remove trailing commas
    json_str = re.sub(r",\s*([\]}])", r"\1", json_str)
    items = json.loads(json_str)
    return {item["sku"]: item for item in items}


def _load_merchant_console_products() -> dict[str, dict[str, Any]]:
    msg = f"Merchant console products data not found at {PRODUCTS_TS_PATH}"
    assert PRODUCTS_TS_PATH.exists(), msg
    text = PRODUCTS_TS_PATH.read_text(encoding="utf-8")
    start = text.find("export const CATALOGUE_PRODUCTS: CatalogueProduct[] = [")
    assert start != -1, "CATALOGUE_PRODUCTS array not found in products-data.ts"
    end = text.find("\n];\n", start)
    raw = text[start + len("export const CATALOGUE_PRODUCTS: CatalogueProduct[] = ") : end + 2]
    json_str = re.sub(r",\s*([\]}])", r"\1", raw)
    items = json.loads(json_str)
    return {item["sku"]: item for item in items}


def test_catalogue_3way_parity() -> None:
    buyer_items = _load_buyer_web_fixture()
    console_items = _load_merchant_console_products()

    # 1. Total Count Verification
    authority_count = len(PRODUCTS_BY_SKU)
    assert authority_count == 247, f"Expected 247 authority products, found {authority_count}"
    assert len(buyer_items) == 247, f"Expected 247 buyer-web products, found {len(buyer_items)}"
    assert len(console_items) == 247, f"Expected 247 console products, found {len(console_items)}"

    # 2. SKU Set Parity
    authority_skus = set(PRODUCTS_BY_SKU.keys())
    buyer_skus = set(buyer_items.keys())
    console_skus = set(console_items.keys())

    missing_in_buyer = authority_skus - buyer_skus
    extra_in_buyer = buyer_skus - authority_skus
    assert not missing_in_buyer and not extra_in_buyer, (
        f"SKU mismatch in buyer-web: missing={sorted(missing_in_buyer)}, "
        f"extra={sorted(extra_in_buyer)}"
    )

    missing_in_console = authority_skus - console_skus
    extra_in_console = console_skus - authority_skus
    assert not missing_in_console and not extra_in_console, (
        f"SKU mismatch in console: missing={sorted(missing_in_console)}, "
        f"extra={sorted(extra_in_console)}"
    )

    # 3. Field-by-Field Parity for all 247 SKUs
    for sku, cat_p in PRODUCTS_BY_SKU.items():
        b_p = buyer_items[sku]
        c_p = console_items[sku]

        # Name
        b_name = b_p.get("name_en") or b_p.get("name")
        c_name = c_p.get("name")
        assert cat_p.name_en == b_name == c_name, (
            f"SKU {sku} name mismatch: "
            f"catalogue.py={cat_p.name_en!r} vs mock.ts={b_name!r} vs console={c_name!r}"
        )

        # Category
        b_cat = b_p.get("category")
        c_cat = c_p.get("category")
        assert cat_p.category.value == b_cat == c_cat, (
            f"SKU {sku} category mismatch: "
            f"catalogue.py={cat_p.category.value!r} vs mock.ts={b_cat!r} vs console={c_cat!r}"
        )

        # Unit
        b_unit = b_p.get("unit_label") or b_p.get("unit")
        c_unit = c_p.get("unit")
        assert cat_p.unit_label == b_unit == c_unit, (
            f"SKU {sku} unit mismatch: "
            f"catalogue.py={cat_p.unit_label!r} vs mock.ts={b_unit!r} vs console={c_unit!r}"
        )

        # List Price Minor (Integer Paise)
        b_price = b_p.get("list_price_minor")
        c_price = c_p.get("list_price_minor")
        assert cat_p.list_price.minor == b_price == c_price, (
            f"SKU {sku} list_price_minor mismatch (integer paise): "
            f"catalogue.py={cat_p.list_price.minor} vs mock.ts={b_price} vs console={c_price}"
        )

        # Baseline Stock
        b_stock = b_p.get("baseline_stock") if "baseline_stock" in b_p else b_p.get("stock_units")
        c_stock = c_p.get("stock_units")
        assert cat_p.baseline_stock == b_stock == c_stock, (
            f"SKU {sku} baseline_stock mismatch: "
            f"catalogue.py={cat_p.baseline_stock} vs mock.ts={b_stock} vs console={c_stock}"
        )

        # Tax Basis Points
        b_tax = b_p.get("tax_bp") if "tax_bp" in b_p else b_p.get("tax_basis_points")
        c_tax = c_p.get("tax_basis_points")
        assert cat_p.tax_bp == b_tax == c_tax, (
            f"SKU {sku} tax basis points mismatch: "
            f"catalogue.py={cat_p.tax_bp} vs mock.ts={b_tax} vs console={c_tax}"
        )

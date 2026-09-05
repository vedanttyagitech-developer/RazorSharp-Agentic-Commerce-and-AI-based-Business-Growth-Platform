#!/usr/bin/env python3
"""Regenerate products-data.ts for merchant console from merchant_sim.catalogue authority.

Usage:
    export PATH=\"$HOME/.local/bin:$PATH\"
    uv run --no-sync python apps/merchant-console/src/lib/api/generate_products_data.py
"""

import json
from pathlib import Path

from merchant_sim.catalogue import CATALOGUE

OUTPUT_FILE = Path(__file__).resolve().parent / "products-data.ts"


def generate() -> None:
    products = []
    for p in CATALOGUE:
        # Extract brand from English title (first word or common brand)
        brand = p.name_en.split()[0] if p.name_en else "Standard"

        product_obj = {
            "sku": p.sku,
            "name": p.name_en,
            "category": p.category.value,
            "unit": p.unit_label,
            "list_price_minor": p.list_price.minor,
            "current_price_minor": p.list_price.minor,
            "stock_units": p.baseline_stock,
            "is_listed": True,
            "tax_basis_points": p.tax_bp,
            "brand": brand,
            "category_tiles": [p.category.value],
            "synonyms": list(p.synonyms_hi) + list(p.synonyms_latin),
        }
        products.append(product_obj)

    json_text = json.dumps(products, indent=2, ensure_ascii=False)

    ts_content = f"""import type {{ CatalogueProduct }} from \"./types\";

/**
 * 243-Item Grounded Indian Quick-Commerce Catalogue.
 *
 * Generated automatically from `packages/merchant-sim/src/merchant_sim/catalogue.py`.
 * Zero float calculations: all prices in integer minor units (paise).
 * Do NOT hand-edit. Run `generate_products_data.py` to regenerate.
 */
export const CATALOGUE_PRODUCTS: CatalogueProduct[] = {json_text};
"""

    OUTPUT_FILE.write_text(ts_content, encoding="utf-8")
    print(f"Successfully generated {len(products)} products in {OUTPUT_FILE}")


if __name__ == "__main__":
    generate()

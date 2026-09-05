"""The storefront's offline fixture must agree with the catalogue it stands in for.

``merchant_sim.catalogue`` is the authority. ``apps/buyer-web/src/lib/api/mock.ts``
carries a copy so the storefront runs with no backend, and a copy is a thing that
drifts. When it does, the mock shows a price the kernel would refuse, which is the one
divergence this project cannot afford to demonstrate on camera.

Two sources, not three. The merchant console reads ``GET /v1/catalogue/products`` from
the live API and holds no product data of its own, so there is nothing there to drift.
An earlier version of this test compared a third, generated TypeScript copy; deleting
that copy is a better fix than testing it.

The count is derived from the authority rather than written down. A test asserting "247"
fails for the wrong reason the day a product is added, and tells whoever reads the
failure that the fixture is stale when in fact the catalogue grew.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from merchant_sim.catalogue import PRODUCTS_BY_SKU, Category

REPO_ROOT = Path(__file__).resolve().parents[3]
MOCK_TS_PATH = REPO_ROOT / "apps/buyer-web/src/lib/api/mock.ts"

#: ``mock.ts`` field name -> the attribute on ``merchant_sim.catalogue.Product`` it copies.
#: Written out so a failure names the field a reader can go and look at, and so adding a
#: field to the fixture without adding it here is visible as an omission rather than
#: silently unchecked.
_FIELDS: dict[str, str] = {
    "name_en": "name_en",
    "category": "category",
    "unit_label": "unit_label",
    "list_price_minor": "list_price_minor",
    "baseline_stock": "baseline_stock",
    "tax_bp": "tax_bp",
}

#: Fields that are money or counts. A float here is a bug wherever it appears: JSON has
#: one number type, so ``2800.0`` parses equal to ``2800`` and an ``==`` assertion alone
#: would let a fixture written in rupees pass as one written in paise.
_INTEGER_FIELDS = frozenset({"list_price_minor", "baseline_stock", "tax_bp"})


def _authority_value(sku: str, field: str) -> Any:
    product = PRODUCTS_BY_SKU[sku]
    if field == "category":
        return product.category.value
    if field == "list_price_minor":
        return product.list_price.minor
    return getattr(product, _FIELDS[field])


def _fixture() -> dict[str, dict[str, Any]]:
    """Parse the ``FIXTURE`` array out of ``mock.ts``.

    A regex over TypeScript rather than a build step, because the alternative is making
    the Python test suite depend on a Node toolchain. It is deliberately strict: an
    unparseable fixture fails loudly here rather than being silently skipped, which is
    how a parity test quietly stops testing anything.
    """
    if not MOCK_TS_PATH.exists():
        pytest.fail(f"The storefront fixture is missing: {MOCK_TS_PATH}")
    text = MOCK_TS_PATH.read_text(encoding="utf-8")
    opening = "const FIXTURE: FixtureProduct[] = ["
    start = text.find(opening)
    if start == -1:
        pytest.fail(f"{MOCK_TS_PATH.name} no longer declares `{opening}`")
    end = text.find("\n];\n", start)
    if end == -1:
        pytest.fail(f"{MOCK_TS_PATH.name}: the FIXTURE array is not terminated by `];`")
    raw = text[start + len(opening) - 1 : end + 2]
    # Quote bare object keys, but only where a key can appear: at the start of a line or
    # just after `{` or `,`. Rewriting every `identifier:` would also rewrite a colon
    # inside a string literal, and a product named "Amul: Gold" would then fail to parse.
    quoted = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', raw)
    quoted = re.sub(r",\s*([\]}])", r"\1", quoted)
    try:
        items = json.loads(quoted)
    except json.JSONDecodeError as exc:  # pragma: no cover - a parse failure is the report
        pytest.fail(f"Could not parse the FIXTURE array out of {MOCK_TS_PATH.name}: {exc}")
    return {item["sku"]: item for item in items}


def test_the_storefront_fixture_holds_exactly_the_catalogue_skus() -> None:
    """Set equality, reported as what to add and what to remove."""
    fixture = _fixture()
    authority = set(PRODUCTS_BY_SKU)
    mocked = set(fixture)
    missing = sorted(authority - mocked)
    extra = sorted(mocked - authority)
    assert not missing and not extra, (
        f"mock.ts is out of step with catalogue.py ({len(authority)} products).\n"
        f"  missing from mock.ts ({len(missing)}): {missing[:12]}\n"
        f"  not in catalogue.py ({len(extra)}): {extra[:12]}"
    )


def test_every_copied_field_agrees_product_by_product() -> None:
    """Field-level parity, reported one divergence per line rather than one per run."""
    fixture = _fixture()
    divergences: list[str] = []
    for sku in sorted(set(PRODUCTS_BY_SKU) & set(fixture)):
        item = fixture[sku]
        for field in _FIELDS:
            if field not in item:
                divergences.append(f"{sku}: mock.ts has no {field!r}")
                continue
            expected, actual = _authority_value(sku, field), item[field]
            if expected != actual:
                divergences.append(f"{sku}.{field}: catalogue.py={expected!r} mock.ts={actual!r}")
    assert not divergences, "The storefront fixture has drifted:\n  " + "\n  ".join(
        divergences[:40]
    )


def test_no_price_or_count_is_a_float_on_either_side() -> None:
    """Money is integer paise in both files. ``2800.0`` equals ``2800`` and is still wrong."""
    fixture = _fixture()
    floats: list[str] = []
    for sku in sorted(set(PRODUCTS_BY_SKU) & set(fixture)):
        for field in _INTEGER_FIELDS:
            for source, value in (
                ("catalogue.py", _authority_value(sku, field)),
                ("mock.ts", fixture[sku].get(field)),
            ):
                if not isinstance(value, int) or isinstance(value, bool):
                    floats.append(f"{sku}.{field} in {source} is {type(value).__name__}: {value!r}")
    assert not floats, "Money and counts must be integers:\n  " + "\n  ".join(floats[:20])


def test_every_category_in_the_enum_resolves_to_products() -> None:
    """A category tile that returns nothing is a tile that lies. Nine of ten must be real.

    ``electronics`` is exempt: it exists for the airline and marketplace adaptation the
    roster describes, and the grocery demo tenant deliberately stocks none.
    """
    populated = {product.category for product in PRODUCTS_BY_SKU.values()}
    empty = sorted(
        c.value for c in Category if c not in populated and c is not Category.ELECTRONICS
    )
    assert not empty, f"These categories have no products behind them: {empty}"

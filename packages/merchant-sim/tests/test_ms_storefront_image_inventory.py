"""The storefront's image table is a hand-written inventory of a directory, so pin it here.

``apps/buyer-web/src/lib/product-images.ts`` holds ``VARIANT_COUNTS``: a SKU to the number
of ``.webp`` files downloaded for it. Nothing generates it and nothing checks it, and both
ways it can be wrong are invisible until a buyer finds them.

A catalogue SKU with no row renders ``PLACEHOLDER_IMAGE`` -- an empty frame where a
photograph should be, on a product the merchant is really selling. A row that counts one
file more than exists sends ``imagesFor`` to ``/products/<SKU>_2.webp``, which 404s inside
a gallery the buyer is already scrolling. Neither shows up in a build, a type check or a
lint pass, because at the type level the table is just a record of numbers.

This lives on the Python side rather than in the storefront's vitest suite because only
this side can see all three legs at once. The catalogue authority is
``merchant_sim.catalogue``; the storefront deliberately keeps no copy of it (it reads
``GET /v1/catalogue/products`` and has no offline fixture, so it cannot show a price the
kernel never agreed to), which means a vitest test could compare the table against the
directory and nothing else -- two legs of three, and the missing leg is the one that
catches a newly listed product nobody photographed. The backend job also runs ``pytest
packages/`` on every commit with no ``--passWithNoTests`` escape hatch.

The same failure lives one directory over. ``CATEGORY_IMAGES`` and the promo banners name
files under ``public/categories`` by hand, and those tiles are the home page -- the first
screen anyone opening this demonstration sees. They are checked here rather than in a second
file because the question is identical: does a path the storefront hard-codes point at a
photograph that is really on disk?

Reading the TypeScript as text follows ``test_voice_wire_contract.py``: a test that needed
Node to evaluate the module would be skipped wherever Node is absent, and a contract test
that is usually skipped is not a contract test.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Final

import pytest
from merchant_sim.catalogue import PRODUCTS_BY_SKU

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
PRODUCT_IMAGES_TS: Final[Path] = REPO_ROOT / "apps/buyer-web/src/lib/product-images.ts"
PRODUCTS_DIR: Final[Path] = REPO_ROOT / "apps/buyer-web/public/products"
PUBLIC_DIR: Final[Path] = REPO_ROOT / "apps/buyer-web/public"
BUYER_WEB_SRC: Final[Path] = REPO_ROOT / "apps/buyer-web/src"
PROMO_BANNERS_TSX: Final[Path] = BUYER_WEB_SRC / "features/storefront/promo-banners.tsx"

pytestmark = pytest.mark.skipif(
    not PRODUCT_IMAGES_TS.exists(), reason="the storefront is not present in this checkout"
)

#: ``"<SKU>": <count>`` inside the table body. The keys are quoted SKUs and the values are
#: bare integers, which is the whole grammar the generated table uses.
ROW = re.compile(r'"([A-Z0-9-]+)"\s*:\s*(\d+)')

#: ``<SKU>.webp`` for the first shot, ``<SKU>_<n>.webp`` for the rest. SKUs carry no
#: underscore, so the last underscore is unambiguously the variant separator.
FILENAME = re.compile(r"^(?P<sku>[A-Z0-9-]+)(?:_(?P<variant>\d+))?\.webp$")


def _declared_counts() -> dict[str, int]:
    """``VARIANT_COUNTS`` as Python, parsed out of the TypeScript module.

    A duplicated key is an assertion failure rather than a silently-last-wins entry,
    because that is what the JavaScript object literal itself would do with it: the table
    would claim a count no reader of the file can predict.
    """
    source = PRODUCT_IMAGES_TS.read_text()
    match = re.search(r"const VARIANT_COUNTS[^=]*=\s*\{(.*?)\n\};", source, re.DOTALL)
    if match is None:
        raise AssertionError(
            "VARIANT_COUNTS is no longer a `const ... = { ... };` object literal in "
            f"{PRODUCT_IMAGES_TS.relative_to(REPO_ROOT)}; this test cannot read it and is "
            "passing vacuously until its parser is updated"
        )
    rows = [(sku, int(count)) for sku, count in ROW.findall(match.group(1))]
    assert rows, "VARIANT_COUNTS parsed as empty; the table's shape changed"
    duplicated = sorted(sku for sku, n in Counter(sku for sku, _ in rows).items() if n > 1)
    assert not duplicated, f"SKUs listed twice in VARIANT_COUNTS: {duplicated}"
    return dict(rows)


def _files_on_disk() -> dict[str, set[int]]:
    """SKU to the variant numbers present for it, the bare ``<SKU>.webp`` counting as 1."""
    found: dict[str, set[int]] = {}
    for path in PRODUCTS_DIR.glob("*.webp"):
        match = FILENAME.match(path.name)
        assert match is not None, (
            f"{path.name} does not follow <SKU>.webp or <SKU>_<n>.webp, so no reader of "
            "this directory -- this test or the storefront -- can tell which product it is of"
        )
        variant = match.group("variant")
        found.setdefault(match.group("sku"), set()).add(int(variant) if variant else 1)
    return found


def test_every_catalogue_sku_has_a_row_in_the_image_table() -> None:
    """A listed product with no row shows an empty frame where its photograph should be."""
    missing = sorted(set(PRODUCTS_BY_SKU) - set(_declared_counts()))
    assert not missing, (
        f"{len(missing)} catalogue SKUs have no VARIANT_COUNTS row and render the "
        f"placeholder: {missing}"
    )


def test_no_row_names_a_product_the_catalogue_does_not_sell() -> None:
    """An orphan row is dead weight that hides the next real mismatch behind it."""
    orphans = sorted(set(_declared_counts()) - set(PRODUCTS_BY_SKU))
    assert not orphans, f"VARIANT_COUNTS rows for SKUs no longer in the catalogue: {orphans}"


def test_every_row_counts_the_files_that_are_really_on_disk() -> None:
    """A count above the truth is a 404 in a gallery; below it, a shot nobody ever sees."""
    on_disk = _files_on_disk()
    wrong = [
        f"{sku}: table says {count}, disk has {len(on_disk.get(sku, ()))}"
        for sku, count in sorted(_declared_counts().items())
        if count != len(on_disk.get(sku, ()))
    ]
    assert not wrong, "VARIANT_COUNTS disagrees with public/products:\n  " + "\n  ".join(wrong)


def test_every_photograph_on_disk_belongs_to_a_row() -> None:
    """A file no row counts is a download that never reached a buyer, and nobody notices."""
    unreachable = sorted(set(_files_on_disk()) - set(_declared_counts()))
    assert not unreachable, (
        f"files in public/products that no VARIANT_COUNTS row reaches: {unreachable}"
    )


def test_variant_numbering_runs_from_the_bare_file_upward_with_no_gaps() -> None:
    """``imagesFor`` builds ``_2``, ``_3``, ... by counting, so a gap is a guaranteed 404.

    The count alone cannot catch this: a SKU with ``<SKU>.webp`` and ``<SKU>_3.webp`` has
    two files, so a row saying 2 balances -- and the gallery asks for ``_2``, which is not
    there.
    """
    broken = [
        f"{sku}: has {sorted(variants)}, expected {sorted(range(1, len(variants) + 1))}"
        for sku, variants in sorted(_files_on_disk().items())
        if variants != set(range(1, len(variants) + 1))
    ]
    assert not broken, "variant numbering has gaps:\n  " + "\n  ".join(broken)


# --------------------------------------------------------------- aisle photographs

#: ``produce: "/categories/blinkit_produce.webp"`` inside ``CATEGORY_IMAGES``.
CATEGORY_ROW = re.compile(r'(\w+)\s*:\s*"([^"]+)"')

#: Any absolute local image path written as a string literal, in either module.
LOCAL_IMAGE = re.compile(r'"(/[^"]*\.(?:webp|png|jpe?g|avif|gif|svg))"')

#: An image fetched from somebody else's origin. The CSP already refuses to render one;
#: this catches it at the point it is written, where the error message can say why.
HOTLINK = re.compile(r'https?://[^"\'\s]*\.(?:webp|png|jpe?g|avif|gif)')


def _category_images() -> dict[str, str]:
    """``CATEGORY_IMAGES`` as Python, parsed out of the same TypeScript module."""
    source = PRODUCT_IMAGES_TS.read_text()
    match = re.search(r"const CATEGORY_IMAGES[^=]*=\s*\{(.*?)\n\};", source, re.DOTALL)
    if match is None:
        raise AssertionError(
            "CATEGORY_IMAGES is no longer a `const ... = { ... };` object literal in "
            f"{PRODUCT_IMAGES_TS.relative_to(REPO_ROOT)}; this test cannot read it and is "
            "passing vacuously until its parser is updated"
        )
    rows = dict(CATEGORY_ROW.findall(match.group(1)))
    assert rows, "CATEGORY_IMAGES parsed as empty; the table's shape changed"
    return rows


def _declared_categories() -> list[str]:
    """The ``CATEGORIES`` tuple: the aisles the storefront actually renders tiles for."""
    source = PRODUCT_IMAGES_TS.read_text()
    match = re.search(r"export const CATEGORIES = \[(.*?)\] as const;", source, re.DOTALL)
    assert match is not None, "CATEGORIES is no longer an `as const` array literal"
    slugs = re.findall(r'"([a-z_]+)"', match.group(1))
    assert slugs, "CATEGORIES parsed as empty; the list's shape changed"
    return slugs


def test_every_aisle_the_storefront_lists_has_a_category_photograph() -> None:
    """``categoryImage`` falls back to the placeholder, so a missing row is a grey tile.

    ``CATEGORIES`` drives the home page's row of aisles and the ``/c/<slug>`` routes. A slug
    added there but not to ``CATEGORY_IMAGES`` renders an empty frame on the first screen of
    the demonstration, and nothing else in the build notices.
    """
    unillustrated = sorted(set(_declared_categories()) - set(_category_images()))
    assert not unillustrated, (
        f"{len(unillustrated)} aisles in CATEGORIES have no CATEGORY_IMAGES row and render "
        f"the placeholder tile: {unillustrated}"
    )


def test_every_local_image_the_storefront_names_is_really_on_disk() -> None:
    """A hard-coded path to a file nobody downloaded is a broken tile, not a caught error.

    Unlike ``imagesFor``, these paths are written out literally -- in ``CATEGORY_IMAGES`` and
    in the promo banners' own table -- so there is no count to disagree with and no fallback
    to hide behind. The banner artwork in particular is named nowhere else, which means this
    assertion is the only thing standing between a deleted file and a hole on the home page.
    """
    missing: list[str] = []
    for module in (PRODUCT_IMAGES_TS, PROMO_BANNERS_TSX):
        if not module.exists():
            continue
        for path in sorted(set(LOCAL_IMAGE.findall(module.read_text()))):
            if not (PUBLIC_DIR / path.lstrip("/")).is_file():
                missing.append(
                    f"{module.relative_to(REPO_ROOT)} names {path}, which is not in public/"
                )
    assert not missing, "the storefront points at images that are not on disk:\n  " + "\n  ".join(
        missing
    )


def test_no_storefront_image_is_fetched_from_someone_elses_origin() -> None:
    """Every photograph is local, and the CSP is written on the assumption that it stays so.

    ``img-src 'self' data: blob:`` means a hotlinked URL renders as nothing at all, so this
    would surface as a blank card rather than an error. Catching it here names the file and
    the URL, which is the difference between a five-minute fix and an afternoon in devtools.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {url}"
        for path in sorted(BUYER_WEB_SRC.rglob("*"))
        if path.suffix in {".ts", ".tsx", ".css"} and ".test." not in path.name
        for url in HOTLINK.findall(path.read_text())
    ]
    assert not offenders, (
        "product imagery must be local under public/; these are hotlinked and the CSP will "
        "refuse to render them:\n  " + "\n  ".join(offenders)
    )

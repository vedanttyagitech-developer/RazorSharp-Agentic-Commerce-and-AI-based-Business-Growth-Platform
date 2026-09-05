"""Grounded discovery: search and product detail.

Every hit carries provenance and a catalogue revision, so a price a buyer saw can
be compared against the price admission revalidates. Discovery is where grounding
starts: an agent may only talk about products this endpoint returned.

Owned by build unit B. Both routes are pure reads of the merchant simulator held in this
process (ADR 0003 D14); neither opens a database transaction beyond the short one
:func:`commerce_api.deps.require_session` uses to resolve the bearer token.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from merchant_sim import Locale
from pydantic import BaseModel, ConfigDict

from ..deps import SessionContext, merchant_registry
from ..merchants import MerchantRegistry
from ..schemas import CataloguePageOut, FreshnessOut, ProductOut, SearchHitOut
from ..services import catalogue_service

router = APIRouter(prefix="/v1/catalogue", tags=["catalogue"])

Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


class SearchResponse(BaseModel):
    """Search results and the provenance a grounded answer is required to carry.

    ``skus`` is the closed list of product identifiers a model may reference after this
    call (specification 20.1). It is returned explicitly rather than left to be derived
    from ``hits``, so the grounding rule is a field in the contract rather than a
    convention every caller has to remember.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    normalized_query: str
    locale: Locale
    hits: list[SearchHitOut]
    skus: list[str]
    freshness: FreshnessOut


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search the merchant's live catalogue",
)
def search_catalogue(
    ctx: SessionContext,
    registry: Registry,
    q: Annotated[str, Query(max_length=200, description="Free text: English, Hindi or Hinglish")],
    locale: Annotated[str | None, Query(description="en-IN, hi-IN or hi-Latn-IN")] = None,
    limit: Annotated[int, Query(ge=1, le=catalogue_service.MAX_SEARCH_LIMIT)] = 10,
) -> SearchResponse:
    """Find products, with live price, live stock and the revision they were read at.

    Out-of-stock matches are returned, ranked below equally relevant available ones.
    Hiding them would leave the assistant unable to say "we stock that but it is out",
    which is the sentence that leads to a substitution instead of a lost sale.
    """
    ctx.require("catalogue.read")
    parsed = catalogue_service.locale_from(locale)
    results = catalogue_service.search_catalogue(
        registry,
        merchant_id=ctx.merchant_id,
        query=q,
        locale=parsed,
        limit=limit,
    )
    devanagari = parsed.uses_devanagari
    return SearchResponse(
        query=results.query,
        normalized_query=results.normalized_query,
        locale=results.locale,
        hits=[SearchHitOut.of_hit(hit, devanagari=devanagari) for hit in results.hits],
        skus=list(results.skus()),
        freshness=FreshnessOut.of(results.freshness),
    )


@router.get(
    "/products",
    response_model=CataloguePageOut,
    summary="Page through this merchant's catalogue",
)
def list_products(
    ctx: SessionContext,
    registry: Registry,
    category: Annotated[str | None, Query(description="One category slug, e.g. dairy")] = None,
    listed: Annotated[bool | None, Query(description="Only listed, or only delisted")] = None,
    available: Annotated[bool | None, Query(description="Only sellable, or only not")] = None,
    locale: Annotated[str | None, Query(description="en-IN, hi-IN or hi-Latn-IN")] = None,
    limit: Annotated[int, Query(ge=1, le=catalogue_service.MAX_LIST_LIMIT)] = 50,
    cursor: Annotated[
        str | None, Query(max_length=64, description="Last SKU of the previous page")
    ] = None,
) -> CataloguePageOut:
    """The merchant's own view of the catalogue: every SKU, listed or not, in SKU order.

    ``search`` answers "what is a buyer looking for"; this answers "what do I sell". It
    therefore returns delisted products too, which search ranks away, because a merchant
    managing a catalogue needs to see the rows a buyer never will.
    """
    ctx.require("catalogue.read")
    parsed = catalogue_service.locale_from(locale)
    views, next_cursor, total, counts = catalogue_service.list_products(
        registry,
        merchant_id=ctx.merchant_id,
        category=category,
        listed=listed,
        available=available,
        limit=limit,
        cursor=cursor,
    )
    devanagari = parsed.uses_devanagari
    return CataloguePageOut(
        products=[ProductOut.of(view, devanagari=devanagari) for view in views],
        next_cursor=next_cursor,
        limit=limit,
        matched=total,
        counts_by_category=counts,
        revision=registry.store(ctx.merchant_id).revision,
    )


@router.get(
    "/products/{sku}",
    response_model=ProductOut,
    summary="One product with live availability",
)
def read_product(
    sku: str,
    ctx: SessionContext,
    registry: Registry,
    locale: Annotated[str | None, Query(description="en-IN, hi-IN or hi-Latn-IN")] = None,
) -> ProductOut:
    """Live detail for one SKU.

    ``is_listed`` and ``is_available`` are separate fields: sold out and delisted lead to
    different conversations. A SKU this merchant never issued is a 404 rather than an
    empty result, so a hallucinated identifier cannot pass as merely unavailable.
    """
    ctx.require("catalogue.read")
    parsed = catalogue_service.locale_from(locale)
    view = catalogue_service.product_view(registry, merchant_id=ctx.merchant_id, sku=sku)
    return ProductOut.of(view, devanagari=parsed.uses_devanagari)

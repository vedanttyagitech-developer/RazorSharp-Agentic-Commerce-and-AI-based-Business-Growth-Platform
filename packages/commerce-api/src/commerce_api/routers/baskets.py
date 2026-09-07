"""Baskets and the deterministic quote over them.

A quote is an offer and authorizes nothing. merchant-sim is the only component that
computes a total; this router hands it lines and returns what it says, including the
staleness signal that tells the buyer surface a re-quote has moved.

Owned by build unit B.

Why the two mutations run as the kernel role
--------------------------------------------
A basket is not financial state, and the app role could write it. The
``Idempotency-Key`` record cannot be written by the app role, though --
``idempotency_records`` is on :data:`platform_db.FINANCIAL_TABLES` and the grant set says
so -- and ADR 0003's endpoint catalogue requires a key on both of these routes. D1 already
says API mutations run one transaction as ``commerce_kernel``, so that is what these do,
and the guard is real rather than decorative: a retried ``PUT`` replays its stored body
instead of re-running against a basket somebody else has since edited.

Both ``GET``s run as the app role, which physically cannot write a financial table.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from platform_db import Basket
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..deps import (
    AppSession,
    IdempotencyKey,
    KernelSession,
    SessionContext,
    merchant_registry,
)
from ..idempotency import idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..schemas import BasketOut, CurrentBasketOut, CurrentCartOut
from ..services import basket_service, checkout_service

router = APIRouter(prefix="/v1/baskets", tags=["baskets"])
carts_router = APIRouter(prefix="/v1/carts", tags=["carts"])

Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


class ExpectedBasketRequest(BaseModel):
    """What a RazorAI proposal was built against, echoed back with the buyer's press.

    Every field is required once the block is sent, and ``basket_content_hash`` is
    nullable rather than defaulted: ``null`` is the claim "the basket held nothing
    priceable", which is a different statement from not binding at all, and a default
    would make the two indistinguishable on the wire.
    """

    model_config = ConfigDict(extra="forbid")

    basket_content_hash: str | None
    unit_price_minor: Annotated[int, Field(ge=0)]
    catalogue_revision: Annotated[int, Field(ge=0)]


class SetLineRequest(BaseModel):
    """The absolute quantity this line should have. ``0`` removes it.

    Absolute, not incremental. An increment is not idempotent: a retried request would
    add the item twice, and the ``Idempotency-Key`` on this route would be the only thing
    standing between a buyer and two kilos of onions they asked for once.

    ``expected`` is optional, and it has to be: the basket page's own +/- buttons are the
    buyer acting directly on what is in front of them, with no proposal behind the tap and
    nothing for it to be stale against. It is sent only when the press is confirming
    something RazorAI proposed, and then the server refuses the write if the basket, the
    price or the catalogue moved since. Because it is part of the body it is inside this
    route's idempotency fingerprint automatically, so the durable record says not only
    that a line was set to three but against which proposed bytes.
    """

    model_config = ConfigDict(extra="forbid")

    quantity: Annotated[int, Field(ge=0, le=basket_service.MAX_LINE_QUANTITY)]
    expected: ExpectedBasketRequest | None = None


@router.post(
    "",
    response_model=BasketOut,
    status_code=201,
    summary="Create an empty basket",
)
@carts_router.post(
    "",
    response_model=BasketOut,
    status_code=201,
    summary="Create an empty cart",
)
def create_basket(
    ctx: SessionContext,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
) -> JSONResponse:
    """Open a basket for this session's buyer at this session's merchant.

    Neither identity comes from the request. The bearer token says which tenant, which
    merchant and which buyer this is; a body that could name another merchant would let a
    basket be priced against a store the buyer never chose.
    """
    ctx.require("basket.write")
    payload = request_fingerprint(body={"buyer_ref": ctx.buyer_ref})
    with idempotent_mutation(session, ctx, key, "BASKET_CREATE", payload) as slot:
        body = basket_service.create_basket(session, ctx, registry)
        slot.store(body)
    return JSONResponse(content=body, status_code=201)


@router.put(
    "/{basket_id}/lines/{sku}",
    response_model=BasketOut,
    summary="Set one line's quantity and re-quote",
)
@carts_router.put(
    "/{basket_id}/lines/{sku}",
    response_model=BasketOut,
    summary="Set one line's quantity and re-quote",
)
def set_line(
    basket_id: uuid.UUID,
    sku: str,
    body: SetLineRequest,
    ctx: SessionContext,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
) -> JSONResponse:
    """Set a line to an absolute quantity and return the deterministic quote.

    The basket row is taken ``FOR UPDATE`` before its lines are read, so two surfaces
    editing one basket serialize rather than one overwriting the other. The quote that
    comes back carries per-line tax, the delivery fee and the gap to free delivery, all
    computed by the fee engine -- this route adds nothing up.

    A body carrying ``expected`` is a buyer confirming a RazorAI proposal, and the write
    is refused with ``reason: "proposal_superseded"`` if the basket, the price or the
    catalogue moved since that proposal was built. A body without it is the basket page's
    own +/- button, which has nothing to be stale against, and behaves as it always has.
    """
    ctx.require("basket.write")
    payload = request_fingerprint(
        path_params={"basket_id": basket_id, "sku": sku},
        body=body.model_dump(),
    )
    expected = (
        None
        if body.expected is None
        else basket_service.ExpectedBasket(
            basket_content_hash=body.expected.basket_content_hash,
            unit_price_minor=body.expected.unit_price_minor,
            catalogue_revision=body.expected.catalogue_revision,
        )
    )
    with idempotent_mutation(session, ctx, key, "BASKET_SET_LINE", payload) as slot:
        result = basket_service.set_line(
            session,
            ctx,
            registry,
            basket_id=basket_id,
            sku=sku,
            quantity=body.quantity,
            expected=expected,
        )
        slot.store(result)
    return JSONResponse(content=result)


# Declared before ``/{basket_id}``, and it has to be. FastAPI matches routes in the order
# they were added, so with the parameterised one first every request for this path would
# be handed to it, fail to parse "current" as a UUID, and answer 422 -- a validation error
# for a URL that is not wrong, on the endpoint whose whole job is to be findable.
@router.get(
    "/current",
    response_model=CurrentBasketOut,
    summary="The cart this buyer is working in, if they have one",
)
def read_current_basket(
    ctx: SessionContext,
    session: AppSession,
    registry: Registry,
) -> dict[str, Any]:
    """This buyer's open cart, re-quoted now, or ``{"basket": null}`` when they have none.

    This route exists because until now the cart id lived in one browser tab and nowhere
    else. Nothing on the server could answer "which cart is this buyer in", so a reload
    lost the cart outright and the copilot and the cart page could each be holding a
    different one. "RazorAI said it added something and my cart is empty" was almost
    always that, and almost never a write that failed.

    **What "current" means.** The newest OPEN cart this buyer has, by ``created_at``, with
    the basket id breaking a tie so two carts stamped in the same instant still order the
    same way on every read. A buyer can accumulate several: each ``POST /v1/baskets``
    makes one, and a surface that has lost its id makes another. Of those, the one they
    were given last is the only one they can have been looking at, which is what makes
    newest the defensible answer rather than merely the convenient one. The older ones are
    left exactly as they are -- closing them would be a write on a read path, and one of
    them may be what another open tab is still holding.

    **A cart that has become a checkout is not returned.** ``CHECKED_OUT`` means an
    approval card is in front of the buyer, and the surface for that is the checkout, not
    the cart. Answering with it here would invite a storefront to draw +/- buttons over
    lines somebody has already been asked to consent to. A caller that gets ``null`` while
    a checkout is open must go and read the checkout; what it must not do is take the null
    as licence to start a fresh cart, which would strand the one being paid for.

    **Absence is an answer only because the tenant is bound.** ``app_session`` binds it as
    the transaction's first statement; on a connection where nobody had, row-level
    security would return no rows at all and this would report "no cart" for a buyer who
    has one. The tenant predicate below is written out anyway, the way
    :func:`commerce_api.services.basket_service.load_basket` does it, so the scope is
    readable at the call site as well as enforced under it. ``buyer_ref`` is the half
    row-level security does not cover: the tenant is a boundary, the buyer is not.
    """
    ctx.require("catalogue.read")
    basket = session.execute(
        select(Basket)
        .where(
            Basket.tenant_id == ctx.tenant_id,
            Basket.buyer_ref == ctx.buyer_ref,
            Basket.status == "OPEN",
        )
        .order_by(Basket.created_at.desc(), Basket.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    # Rendered through the same function ``GET /{basket_id}`` uses, so a cart reaches the
    # wire one way however it was found, and a re-quote here cannot drift from one there.
    return {
        "basket": None if basket is None else basket_service.basket_body(basket, registry=registry)
    }


@carts_router.get(
    "/current",
    response_model=CurrentCartOut,
    summary="The cart this buyer is working in, if they have one",
)
def read_current_cart(
    ctx: SessionContext,
    session: AppSession,
    registry: Registry,
) -> dict[str, Any]:
    """Canonical /v1/carts/current endpoint returning {"cart": ...}."""
    ctx.require("catalogue.read")
    cart = session.execute(
        select(Basket)
        .where(
            Basket.tenant_id == ctx.tenant_id,
            Basket.buyer_ref == ctx.buyer_ref,
            Basket.status == "OPEN",
        )
        .order_by(Basket.created_at.desc(), Basket.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {"cart": None if cart is None else basket_service.basket_body(cart, registry=registry)}


@router.get(
    "/{basket_id}",
    response_model=BasketOut,
    summary="Re-quote a basket at current merchant state",
)
@carts_router.get(
    "/{basket_id}",
    response_model=BasketOut,
    summary="Re-quote a cart at current merchant state",
)
def read_basket(
    basket_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    registry: Registry,
) -> dict[str, Any]:
    """The basket, re-priced now, with ``stale`` true when the catalogue has moved.

    The stored quote is never returned. This is what the merchant says at this revision,
    which is the only figure a checkout may be built from.
    """
    ctx.require("catalogue.read")
    return basket_service.read_basket(session, ctx, registry, basket_id)


@router.post(
    "/{basket_id}/checkout",
    status_code=201,
    summary="Open a checkout: version 1, its receipt and its reservation",
)
@carts_router.post(
    "/{basket_id}/checkout",
    status_code=201,
    summary="Open a checkout: version 1, its receipt and its reservation",
)
def create_checkout(
    basket_id: uuid.UUID,
    ctx: SessionContext,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
) -> JSONResponse:
    """Turn a priced basket into checkout version 1 and return its approval card.

    One transaction: the version is written with the exact bytes just quoted, the basket
    is closed so no second checkout can be built from it, the stock is held, and the
    Policy-at-Sale Receipt freezes every merchant policy kind as it stands right now. A
    receipt without its version, or a version without its hold, would be worse than no
    checkout at all, so they commit together or not at all.
    """
    ctx.require("checkout.create")
    payload = request_fingerprint(path_params={"basket_id": basket_id})
    with idempotent_mutation(session, ctx, key, "CHECKOUT_CREATE", payload) as slot:
        card = checkout_service.open_checkout(session, ctx, registry, basket_id=basket_id)
        slot.store(card)
    return JSONResponse(content=card, status_code=201)

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

``GET`` runs as the app role, which physically cannot write a financial table.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..deps import (
    AppSession,
    IdempotencyKey,
    KernelSession,
    SessionContext,
    merchant_registry,
)
from ..idempotency import idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..schemas import BasketOut
from ..services import basket_service, checkout_service

router = APIRouter(prefix="/v1/baskets", tags=["baskets"])

Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


class SetLineRequest(BaseModel):
    """The absolute quantity this line should have. ``0`` removes it.

    Absolute, not incremental. An increment is not idempotent: a retried request would
    add the item twice, and the ``Idempotency-Key`` on this route would be the only thing
    standing between a buyer and two kilos of onions they asked for once.
    """

    model_config = ConfigDict(extra="forbid")

    quantity: Annotated[int, Field(ge=0, le=basket_service.MAX_LINE_QUANTITY)]


@router.post(
    "",
    response_model=BasketOut,
    status_code=201,
    summary="Create an empty basket",
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
    """
    ctx.require("basket.write")
    payload = request_fingerprint(
        path_params={"basket_id": basket_id, "sku": sku},
        body=body.model_dump(),
    )
    with idempotent_mutation(session, ctx, key, "BASKET_SET_LINE", payload) as slot:
        result = basket_service.set_line(
            session, ctx, registry, basket_id=basket_id, sku=sku, quantity=body.quantity
        )
        slot.store(result)
    return JSONResponse(content=result)


@router.get(
    "/{basket_id}",
    response_model=BasketOut,
    summary="Re-quote a basket at current merchant state",
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

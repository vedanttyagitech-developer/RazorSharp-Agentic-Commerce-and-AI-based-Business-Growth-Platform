"""Checkout construction and the checkout read model.

Creating a checkout writes version 1, its Policy-at-Sale Receipt and its reservation
through the kernel in one transaction; that route lives on
:mod:`commerce_api.routers.carts`, because it is addressed by the cart it is built
from. What lives here is the read model, and it is the one endpoint every UI state in
specification 8.2 renders from: the head, all versions with their hashes and invalidation
stamps, the current approval card, the current payment attempt, and the deltas between a
version that was retired and the one that replaced it.

Owned by build unit B. Shares the ``/v1/checkouts`` prefix with
:mod:`commerce_api.routers.approvals`, which owns approve, reject, submit and cancel.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from ..deps import AppSession, SessionContext
from ..schemas import CheckoutOut
from ..services import checkout_service

router = APIRouter(prefix="/v1/checkouts", tags=["checkouts"])


@router.get(
    "/{checkout_id}",
    response_model=CheckoutOut,
    summary="Head, versions, approval card and attempt",
)
def read_checkout(
    checkout_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
) -> dict[str, Any]:
    """Everything the trusted surface needs to render this checkout's current state.

    Invalidated versions are returned too. A superseded version is not noise: it is the
    evidence that an old approval was refused, and hiding it would leave the buyer
    surface unable to show what changed and why a fresh decision is being asked for.

    A checkout belonging to another buyer is a 404, never a 403: a 403 would confirm the
    identifier exists and turn this endpoint into an enumeration oracle.
    """
    ctx.require("order.read")
    return checkout_service.read_checkout(session, ctx, checkout_id)

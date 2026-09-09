"""Checkout construction and the checkout read model.

Creating a checkout writes version 1, its Policy-at-Sale Receipt and its reservation
through the kernel in one transaction; that route lives on
:mod:`commerce_api.routers.carts`, because it is addressed by the cart it is built
from. What lives here is the read model: the single-checkout read every UI state in
specification 8.2 renders from -- the head, all versions with their hashes and
invalidation stamps, the current approval card, the current payment attempt, and the
deltas between a version that was retired and the one that replaced it -- and the
collection read that finds a checkout again when the caller has lost its identifier.

The collection read is deliberately not a ``/current`` singleton. A cart has one obvious
current row, because ``OPEN`` admits exactly one; a checkout has fourteen states and a
buyer may hold several unfinished ones at once, so "the current checkout" would be a
policy choice baked into the API surface, and a wrong one would resurrect on reload the
very checkout the buyer had walked away from. The list says what exists and lets the
surface choose, which is the division the agent turn already states: the client says what
the buyer is looking at, and the server says who is allowed to see it.

Owned by build unit B. Shares the ``/v1/checkouts`` prefix with
:mod:`commerce_api.routers.approvals`, which owns approve, reject, submit and cancel.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from transaction_kernel import CheckoutState

from ..deps import AppSession, SessionContext
from ..schemas import CheckoutOut, CheckoutsPageOut
from ..services import checkout_service, listing
from .evidence import Operator

router = APIRouter(prefix="/v1/checkouts", tags=["checkouts"])


@router.get(
    "",
    response_model=CheckoutsPageOut,
    summary="Checkouts in this scope, newest first, with counts by state",
)
def list_checkouts(
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
    state: Annotated[
        CheckoutState | None,
        Query(description="One checkout state: APPROVAL_REQUIRED, AWAITING_PAYMENT, ..."),
    ] = None,
    live: Annotated[
        bool,
        Query(description="Only checkouts that can still go somewhere."),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=listing.MAX_PAGE_SIZE)] = listing.DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> CheckoutsPageOut:
    """A buyer sees their own checkouts; a scenario-key operator sees the tenant's.

    ``live=true`` is how a surface recovers its place: it returns the checkouts this
    buyer has not finished, which is what a reload, a second device or a voice session --
    none of which kept an identifier -- needs in order to reach the card that is waiting.
    Which states count as unfinished is the kernel's ``NON_TERMINAL_CHECKOUT_STATES``,
    resolved here so that no client holds a second copy of that set.

    Gated on ``order.read``, the capability the single read already requires. A collection
    easier to reach than the rows inside it would be an enumeration of the very thing the
    single read protects.

    Keyset-paginated on ``(created_at, id)``; ``scope`` says which of the two views the
    caller received.
    """
    ctx.require("order.read")
    return listing.list_checkouts(
        session, ctx, operator=operator, state=state, live=live, limit=limit, cursor=cursor
    )


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

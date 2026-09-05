"""The refund collection: what is in flight, what settled, and what nobody knows yet.

**Owned by build unit D.** A read-only router: a refund is *requested* on its order
(``POST /v1/orders/{order_id}/refunds``) and executed by the worker under a single-use
grant; this router only lists what those two wrote.

The states that matter and are easy to conflate are ``REFUND_PENDING``,
``REFUND_UNKNOWN`` and ``REFUND_FAILED``. Pending means the provider was asked and has
not answered; unknown means the answer was lost and reconciliation owns the row; failed
means the provider said no. A console that folded the first two together would show an
operator a refund to retry that is in fact still in flight, and a buyer refunded twice
is how that ends. The list therefore reports the wire state per row and counts every
state across the scope, zeros included.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from transaction_kernel import PaymentState

from ..deps import AppSession, SessionContext
from ..schemas import RefundsPageOut
from ..services import listing
from .evidence import Operator

router = APIRouter(prefix="/v1/refunds", tags=["refunds"])


@router.get(
    "",
    response_model=RefundsPageOut,
    summary="Refunds in this scope, newest first, with counts by state",
)
def list_refunds(
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
    state: Annotated[
        PaymentState | None,
        Query(description="A refund wire state: REFUND_PENDING, REFUND_UNKNOWN, ..."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=listing.MAX_PAGE_SIZE)] = listing.DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> RefundsPageOut:
    """A buyer sees the refunds on their own orders; a scenario-key operator sees the tenant's.

    ``scope`` in the response says which of those the caller received, so a console can
    label a page honestly rather than assume it. Cursors come from ``next_cursor`` and
    are handed back verbatim.
    """
    return listing.list_refunds(
        session, ctx, operator=operator, state=state, limit=limit, cursor=cursor
    )

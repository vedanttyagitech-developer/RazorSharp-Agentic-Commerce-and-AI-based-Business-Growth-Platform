"""What a shop actually sold, counted once.

This lived inside ``routers/merchant_insights.py`` and was read by one caller. It has two
now -- the route, and the Operations specialist's tool -- and two copies of a sum over
``orders`` is the shape that drifts: one of them gains a filter, the panel and the copilot
disagree about the same week, and neither is obviously wrong to whoever is looking.

The definition sentence is here for the same reason. It is what the platform says the
figure is *not* -- not net revenue, not profit, not campaign-attributed growth -- and the
specialist is required to carry it when it quotes the number. A second copy of that
sentence, drifting from this one, would leave the caveat attached to the panel and absent
from the reply, which is the version of this problem that actually happened before the
copilot's figures were grounded at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from platform_db.schema_service import Order
from sqlalchemy import func, select
from sqlalchemy.orm import Session

__all__ = ["DEFINITION", "SalesBucket", "SalesSnapshot", "confirmed_sales"]

#: The platform's own words about the figure below. Quoted, never paraphrased.
DEFINITION: Final[str] = (
    "Confirmed order value before refunds; not net revenue, profit, "
    "or campaign-attributed growth. Currencies are never combined."
)


@dataclass(frozen=True, slots=True)
class SalesBucket:
    """One currency's total. Currencies are never added together."""

    currency: str
    orders: int
    sales_minor: int


@dataclass(frozen=True, slots=True)
class SalesSnapshot:
    days: int
    observed_at: datetime
    since: datetime
    totals: tuple[SalesBucket, ...]
    definition: str = DEFINITION


def confirmed_sales(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    days: int,
) -> SalesSnapshot:
    """Confirmed order value for one merchant over a window, grouped by currency.

    Scoped by the caller's own tenant and merchant, never by anything a model chose: the
    ids come from the authenticated session, so there is no argument here a specialist
    could set to read another shop.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    rows = session.execute(
        select(
            Order.currency,
            func.count().label("orders"),
            func.sum(Order.total_minor).label("sales_minor"),
        )
        .where(
            Order.tenant_id == tenant_id,
            Order.merchant_id == merchant_id,
            Order.created_at >= since,
        )
        .group_by(Order.currency)
        .order_by(Order.currency)
    )
    return SalesSnapshot(
        days=days,
        observed_at=now,
        since=since,
        totals=tuple(
            SalesBucket(
                currency=row.currency,
                orders=int(row.orders),
                sales_minor=int(row.sales_minor),
            )
            for row in rows
        ),
    )

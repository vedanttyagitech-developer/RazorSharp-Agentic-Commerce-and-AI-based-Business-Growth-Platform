"""Merchant-scoped sales facts. Read-only; no model-generated metrics or mixed currencies."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from platform_db.schema_service import Order
from pydantic import BaseModel
from sqlalchemy import func, select

from ..deps import AppSession, SessionContext, require_scenario_key

router = APIRouter(
    prefix="/v1/merchant/insights", tags=["merchant"], dependencies=[Depends(require_scenario_key)]
)


class SalesBucket(BaseModel):
    currency: str
    orders: int
    sales_minor: int


class MerchantInsights(BaseModel):
    days: int
    observed_at: datetime
    since: datetime
    totals: list[SalesBucket]
    definition: str


@router.get("", response_model=MerchantInsights)
def read_insights(
    ctx: SessionContext, session: AppSession, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> MerchantInsights:
    ctx.require("order.read")
    ctx.require("merchant.action.propose")
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    rows = session.execute(
        select(
            Order.currency,
            func.count().label("orders"),
            func.sum(Order.total_minor).label("sales_minor"),
        )
        .where(
            Order.tenant_id == ctx.tenant_id,
            Order.merchant_id == ctx.merchant_id,
            Order.created_at >= since,
        )
        .group_by(Order.currency)
        .order_by(Order.currency)
    )
    return MerchantInsights(
        days=days,
        observed_at=now,
        since=since,
        totals=[
            SalesBucket(
                currency=row.currency, orders=int(row.orders), sales_minor=int(row.sales_minor)
            )
            for row in rows
        ],
        definition=(
            "Confirmed order value before refunds; not net revenue, profit, "
            "or campaign-attributed growth. Currencies are never combined."
        ),
    )

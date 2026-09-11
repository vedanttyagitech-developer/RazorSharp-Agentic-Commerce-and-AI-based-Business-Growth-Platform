"""Merchant-scoped sales facts. Read-only; no model-generated metrics or mixed currencies."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..deps import AppSession, SessionContext, require_scenario_key
from ..services import merchant_insight_service

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
    snapshot = merchant_insight_service.confirmed_sales(
        session, tenant_id=ctx.tenant_id, merchant_id=ctx.merchant_id, days=days
    )
    return MerchantInsights(
        days=snapshot.days,
        observed_at=snapshot.observed_at,
        since=snapshot.since,
        totals=[
            SalesBucket(
                currency=bucket.currency, orders=bucket.orders, sales_minor=bucket.sales_minor
            )
            for bucket in snapshot.totals
        ],
        definition=snapshot.definition,
    )

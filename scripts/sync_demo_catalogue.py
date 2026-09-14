#!/usr/bin/env python3
"""Add fixture SKUs to one existing demo store without resetting stock or prices.

Run with DATABASE_URL_KERNEL set explicitly, then restart the demo services.
Uses the existing inventory ledger and merchant lock. Safe to rerun.
"""

from __future__ import annotations

import argparse
import os

from commerce_api.merchants import MerchantRegistry
from platform_db.schema import Merchant, Tenant
from platform_db.tenancy import set_tenant
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-slug", default="demo")
    parser.add_argument("--merchant-slug", default="demo-grocery")
    args = parser.parse_args()
    engine = create_engine(os.environ["DATABASE_URL_KERNEL"])
    try:
        with Session(engine) as session, session.begin():
            tenant = session.execute(
                select(Tenant).where(Tenant.slug == args.tenant_slug)
            ).scalar_one()
            set_tenant(session, tenant.id)
            merchant = session.execute(
                select(Merchant).where(
                    Merchant.tenant_id == tenant.id, Merchant.slug == args.merchant_slug
                )
            ).scalar_one()
            added = MerchantRegistry().sync_catalogue(session, merchant.id)
        print(
            f"Catalogue synchronized: {added} new products; existing merchant settings preserved."
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

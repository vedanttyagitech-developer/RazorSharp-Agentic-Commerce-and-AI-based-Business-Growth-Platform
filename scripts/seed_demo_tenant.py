#!/usr/bin/env python3
"""Seed the demonstration tenant and merchant into the development database.

Nothing in this repository can be demonstrated without a tenant row: the demo session
endpoint resolves a tenant from a slug, and every table the journey touches is under
row-level security keyed on ``tenant_id``. This script creates that one tenant and its
one merchant, and nothing else. Catalogue, inventory, price and fees are *not* seeded
here on purpose -- they live in the merchant simulator's in-process memory (ADR 0003
D14), so writing them to a table would create a second, disagreeing source of truth.

Three properties, in order of how much they matter:

**It is idempotent.** Both rows are looked up by their natural key before anything is
written, so re-running prints the same identifiers and inserts nothing. A demo that has
to be re-seeded after a crash must not end up with two merchants and a coin toss over
which one the session binds to.

**It writes through the real schema and the real kernel, not raw SQL.** The rows are
``platform_db`` ORM objects, so they inherit the constraints, the naming and the
row-level-security policies the migrations installed; the creation event is appended by
:func:`transaction_kernel.audit.append`, which is the same hash-chained, append-only path
every money action uses. If seeding needed a shortcut around the kernel, the kernel would
be the wrong shape.

**It connects as the kernel role.** ``commerce_kernel`` is the only role permitted to
write ``audit_events``, and the tenant, the merchant and the audit event are one
transaction: an interrupted seed leaves either everything or nothing, never a merchant
whose creation is unrecorded.

It deliberately does not import ``commerce_api``. Seeding must work whether or not the
HTTP layer currently imports, and this script is what a person runs *before* starting it.

Usage::

    scripts/seed_demo_tenant.py
    scripts/seed_demo_tenant.py --tenant-slug acme --merchant-slug acme-grocery
    SEED_DATABASE_URL=postgresql+psycopg://... scripts/seed_demo_tenant.py
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse, urlunparse

# --------------------------------------------------------------------------- defaults

#: The demonstration database, and never the test database. ``DATABASE_URL_KERNEL`` is
#: deliberately NOT consulted: in this repository that variable names ``commerce_test``,
#: and a seed script that silently wrote demo rows into the suite's database would leave
#: tenant rows behind that the isolation fixtures do not clean up.
#: Read from ``DEMO_DATABASE_URL``; the fallback names the role and the database and carries
#: no password, so a password never lives in this file. Set the variable to the kernel
#: role's URL for the development database before running this.
DEFAULT_DATABASE_URL: Final = os.environ.get(
    "DEMO_DATABASE_URL", "postgresql+psycopg://commerce_dev_kernel@localhost:5432/commerce_dev"
)

DEFAULT_TENANT_SLUG: Final = "demo"
DEFAULT_TENANT_NAME: Final = "Demo Commerce"
DEFAULT_MERCHANT_SLUG: Final = "demo-grocery"
#: Matches the simulator's fixture name so the storefront, the audit trail and
#: merchant_sim.CATALOGUE all say the same thing.
DEFAULT_MERCHANT_NAME: Final = "Demo Grocery Store"
DEFAULT_HOME_REGION: Final = "asia-south1"
DEFAULT_CURRENCY: Final = "INR"
DEFAULT_API_BASE: Final = "http://localhost:8000"

#: Audit stream identity. Lower case, matching ``transaction_kernel.checkouts`` and
#: ``transaction_kernel.refunds``.
AGGREGATE_TYPE: Final = "tenant"
EVENT_TYPE: Final = "DEMO_TENANT_SEEDED"


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What the run found or made. ``created`` is false on every re-run."""

    tenant_id: uuid.UUID
    tenant_slug: str
    merchant_id: uuid.UUID
    merchant_slug: str
    created: bool


def redacted(url: str) -> str:
    """A connection URL with its password removed, safe to print or log."""
    parsed = urlparse(url)
    if parsed.hostname is None:
        return "<unparseable-url>"
    user = f"{parsed.username}:***@" if parsed.username else ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse(
        parsed._replace(netloc=f"{user}{parsed.hostname}{port}", query="", fragment="")
    )


def database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/") or "<none>"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed one demo tenant and merchant into the development database.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SEED_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Kernel-role connection URL. Overridable with SEED_DATABASE_URL.",
    )
    parser.add_argument("--tenant-slug", default=DEFAULT_TENANT_SLUG)
    parser.add_argument("--tenant-name", default=DEFAULT_TENANT_NAME)
    parser.add_argument("--merchant-slug", default=DEFAULT_MERCHANT_SLUG)
    parser.add_argument("--merchant-name", default=DEFAULT_MERCHANT_NAME)
    parser.add_argument("--home-region", default=DEFAULT_HOME_REGION)
    parser.add_argument("--currency", default=DEFAULT_CURRENCY)
    parser.add_argument(
        "--api-base",
        default=os.environ.get("API_BASE", DEFAULT_API_BASE),
        help="Only used to print the session curl command.",
    )
    return parser.parse_args(argv)


def seed(
    *,
    tenant_slug: str,
    tenant_name: str,
    merchant_slug: str,
    merchant_name: str,
    home_region: str,
    currency: str,
) -> SeedResult:
    """Create the tenant and merchant if absent, in one kernel-role transaction."""
    # Imported here, after the environment has been set by main(), so that
    # platform_db.get_engine resolves the URL this run was told to use.
    from commerce_domain import uuid7
    from platform_db import Merchant, Tenant, session_scope, set_tenant
    from sqlalchemy import select
    from transaction_kernel import ActorType, audit

    created = False
    with session_scope("KERNEL") as session:
        # `tenants` is the one table outside row-level security -- it is the table the
        # tenant context is resolved *from* -- so this lookup runs before any binding.
        tenant = session.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(id=uuid7(), slug=tenant_slug, name=tenant_name, home_region=home_region)
            session.add(tenant)
            session.flush()
            created = True
        tenant_id: uuid.UUID = tenant.id

        # Everything below is under row-level security. Without this binding the SELECT
        # would return nothing and the INSERT would fail its WITH CHECK -- which is the
        # policy working, not a bug.
        set_tenant(session, tenant_id)

        merchant = session.execute(
            select(Merchant).where(Merchant.tenant_id == tenant_id, Merchant.slug == merchant_slug)
        ).scalar_one_or_none()
        if merchant is None:
            merchant = Merchant(
                id=uuid7(),
                tenant_id=tenant_id,
                slug=merchant_slug,
                name=merchant_name,
                currency=currency,
            )
            session.add(merchant)
            session.flush()
            created = True
        merchant_id: uuid.UUID = merchant.id

        if created:
            # Appended only when something was actually written, so a re-run adds no
            # event. The chain then says exactly once that this tenant came into being.
            audit.append(
                session,
                tenant=tenant_id,
                aggregate_type=AGGREGATE_TYPE,
                aggregate_id=tenant_id,
                event_type=EVENT_TYPE,
                actor_type=ActorType.OPERATOR,
                actor_id="scripts/seed_demo_tenant.py",
                principal_id=None,
                payload={
                    "tenant_slug": tenant_slug,
                    "merchant_id": str(merchant_id),
                    "merchant_slug": merchant_slug,
                    "currency": currency,
                    "home_region": home_region,
                },
                correlation_id=uuid7(),
            )

    return SeedResult(
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        merchant_id=merchant_id,
        merchant_slug=merchant_slug,
        created=created,
    )


def report(result: SeedResult, *, url: str, api_base: str) -> None:
    """Print the identifiers, then the exact command that turns them into a session."""
    verb = "seeded" if result.created else "already present (nothing written)"
    print()
    print(f"Demo tenant {verb} in {database_name(url)}.")
    print()
    print(f"  tenant id       {result.tenant_id}")
    print(f"  tenant slug     {result.tenant_slug}")
    print(f"  merchant id     {result.merchant_id}")
    print(f"  merchant slug   {result.merchant_slug}")
    print()
    print("Mint a buyer session (the API must be running -- `make api` or `make demo`):")
    print()
    print(f"  curl -sS -X POST {api_base}/v1/demo/sessions \\")
    print("    -H 'Content-Type: application/json' \\")
    print(
        f'    -d \'{{"tenant_slug": "{result.tenant_slug}", '
        f'"merchant_slug": "{result.merchant_slug}", "actor_type": "BUYER"}}\''
    )
    print()
    print(
        'For the agent side of the demonstration, change "BUYER" to "AGENT": that session '
        "can build a basket and submit an approved checkout, and cannot approve one."
    )
    print()
    print(
        f"Razorpay webhooks for this tenant go to {api_base}/webhooks/razorpay/{result.tenant_slug}"
    )
    print()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    url: str = args.database_url

    if database_name(url) == "commerce_test":
        print(
            "Refusing to seed commerce_test: it is the suite's database and its fixtures "
            "delete only what they created. Point --database-url at commerce_dev.",
            file=sys.stderr,
        )
        return 2

    # platform_db.database_url("KERNEL") reads DATABASE_URL_KERNEL from the environment.
    # Setting it here, for this process only, is how this launcher chooses its role -- and
    # it is set from the resolved argument so the printed URL and the used URL cannot
    # diverge.
    os.environ["DATABASE_URL_KERNEL"] = url
    print(f"Seeding as the kernel role against {redacted(url)}")

    try:
        result = seed(
            tenant_slug=args.tenant_slug,
            tenant_name=args.tenant_name,
            merchant_slug=args.merchant_slug,
            merchant_name=args.merchant_name,
            home_region=args.home_region,
            currency=args.currency,
        )
    except Exception as exc:  # noqa: BLE001 - a seed failure must explain itself, not traceback
        print(f"\nSeeding failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "\nMost likely causes, in order:\n"
            "  1. PostgreSQL is not running, or commerce_dev does not exist.\n"
            "  2. The migrations or the dev login roles were never applied.\n"
            "Both are fixed by:  make bootstrap",
            file=sys.stderr,
        )
        return 1

    report(result, url=url, api_base=args.api_base.rstrip("/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

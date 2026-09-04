"""Fixtures for tenant-isolation tests.

These tests connect as ``commerce_test_kernel``, a NOSUPERUSER NOBYPASSRLS login role.
That detail is the whole test: PostgreSQL superusers bypass row-level security
unconditionally, so an isolation suite run as a superuser passes while proving nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
APP_URL = os.environ.get(
    "DATABASE_URL_TEST_APP",
    "postgresql+psycopg://commerce_test_app:testpw@localhost:5432/commerce_test",
)
# Fixtures seed and tear down through an owner connection. Application roles
# deliberately have no DELETE on any table, so a fixture cannot clean up as the kernel
# role -- and giving the kernel DELETE just to make tests convenient would erase the
# guarantee the tests exist to prove.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)


def _require_db(url: str) -> Engine:
    engine = create_engine(url, future=True, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for isolation tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "isolation tests must not run as a superuser"
    assert row.rolbypassrls is False, "isolation tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def app_engine() -> Engine:
    return _require_db(APP_URL)


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    """Owner connection, used only to seed and remove fixture data."""
    return _require_db(ADMIN_URL)


@pytest.fixture
def kernel_session(kernel_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def two_tenants(admin_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two tenants, each with one merchant, removed afterwards."""
    a, b = uuid.uuid4(), uuid.uuid4()
    with admin_engine.begin() as conn:
        for tid, slug in ((a, f"t-{a.hex[:8]}"), (b, f"t-{b.hex[:8]}")):
            # Distinct parameter names per column: reusing one bind for a varchar and a
            # text column makes psycopg deduce inconsistent types for the placeholder.
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :slug, :name, 'asia-south1')"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
            # Merchant insert is itself RLS-checked, so bind the tenant first.
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
            conn.execute(
                text(
                    "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                    "VALUES (:id, :t, :slug, :name, 'INR')"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tid,
                    "slug": f"m-{tid.hex[:8]}",
                    "name": f"m-{tid.hex[:8]}",
                },
            )
    yield a, b
    with admin_engine.begin() as conn:
        for tid in (a, b):
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
            conn.execute(text("DELETE FROM merchants WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [a, b]})

"""Shared fixtures for the protocol suites.

Two kinds of test live in this package and they need very different things.

The **cryptographic** tests need keys and nothing else. They are pure, they run everywhere,
and they are where the AP2 conformance suite of specification 15.7 lives. Their fixtures
are the ``cp_*_key`` family below.

The **evidence and replay** tests need a real PostgreSQL, because what they assert is that
a nonce cannot be claimed twice under contention and that an evidence chain is gapless --
neither of which means anything against a fake. They connect as ``commerce_test_kernel``,
a NOSUPERUSER NOBYPASSRLS role, for the reason the kernel's own conftest gives: a superuser
bypasses row-level security and would make every isolation assertion pass vacuously.

The fixture prefix is ``cp_``, following the per-package convention (``adm_`` in
transaction-kernel, ``capi_`` in commerce-api). Test file basenames are ``test_cp_*`` and
unique repository-wide, which ADR 0003 D12 requires because pytest's prepend import mode
puts every tests directory on ``sys.path``.

A note on the AP2 SDK's disk logging. ``ap2.sdk.mandate`` writes a JSON line to a ``.logs``
directory inside the installed package on every ``verify`` and ``present``, and that line
contains the holder public key and the full presentation token. The package neutralises
this in :mod:`commerce_protocols.ap2.signing` for the running service; the autouse fixture
here makes sure a test can never write one either, because a test suite that quietly
scribbles into site-packages is a test suite that behaves differently on a clean machine.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from commerce_domain import uuid7
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

#: Every table these suites write, children before parents, for teardown. The protocol
#: layer adds no tables of its own (ADR 0005), so this is short by construction: evidence
#: lands in ``audit_events`` and replay claims in ``idempotency_records``.
_TENANT_TABLES = ("audit_events", "idempotency_records", "merchants")


def _engine(url: str, pool_size: int = 5) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    return engine


@pytest.fixture(scope="session")
def cp_kernel_engine() -> Engine:
    """The kernel role. Asserted unprivileged, or every isolation claim below is empty."""
    engine = _engine(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert not row.rolsuper, "protocol tests must not run as a superuser"
    assert not row.rolbypassrls, "protocol tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def cp_admin_engine() -> Engine:
    """Owner connection. Application roles have no DELETE, so teardown needs this."""
    return _engine(ADMIN_URL, pool_size=2)


@pytest.fixture
def cp_tenant(cp_admin_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """One tenant and one merchant, torn down afterwards.

    The slug uses ``uuid4`` rather than ``uuid7``: a UUIDv7's leading hex digits are its
    millisecond clock, so every tenant seeded inside the same 256 ms would collide on
    ``uq_tenants_slug``. The commerce-api conftest learned this the same way.
    """
    tenant_id, merchant_id = uuid.uuid4(), uuid7()
    slug = uuid.uuid4().hex[:12]
    with cp_admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :s, :n, 'asia-south1')"
            ),
            {"id": tenant_id, "s": f"cp-{slug}", "n": f"cp-{slug}"},
        )
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :s, :n, 'INR')"
            ),
            {"id": merchant_id, "t": tenant_id, "s": f"cpm-{slug}", "n": f"cpm-{slug}"},
        )

    yield tenant_id, merchant_id

    with cp_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in _TENANT_TABLES:
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608 - fixed list
                {"t": tenant_id},
            )
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


@pytest.fixture
def cp_session(
    cp_kernel_engine: Engine, cp_tenant: tuple[uuid.UUID, uuid.UUID]
) -> Iterator[Session]:
    """A kernel-role session with the tenant bound, inside one open transaction.

    Bound as the transaction's first statement, transaction-local, exactly as
    ``commerce_api.deps`` does it: no statement in the transaction can run unscoped, and
    the setting cannot leak onto the next request that borrows this pooled connection.
    """
    tenant_id, _ = cp_tenant
    session = Session(cp_kernel_engine, expire_on_commit=False)
    session.begin()
    session.execute(SET_TENANT, {"t": str(tenant_id)})
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ------------------------------------------------------------------------ ES256 keys


def _es256_key(kid: str) -> JWK:
    """One P-256 signing key carrying a stable ``kid``.

    Round-tripping through JSON to set the ``kid`` is the SDK's own idiom -- jwcrypto has
    no setter -- and the ``kid`` has to be on the key rather than added at signing time
    because ``ap2.sdk.sdjwt.common.header_parameters`` reads it off the key when it builds
    an SD-JWT header.
    """
    jwk = JWK.from_pyca(ec.generate_private_key(ec.SECP256R1()))
    material = json.loads(jwk.export())
    material["kid"] = kid
    return JWK.from_json(json.dumps(material))


@pytest.fixture(scope="session")
def cp_merchant_key() -> JWK:
    """The merchant's signing key: authorises checkouts (specification 15.2)."""
    return _es256_key("merchant-key-1")


@pytest.fixture(scope="session")
def cp_platform_key() -> JWK:
    """The platform's signing key: issues verification and payment receipts."""
    return _es256_key("platform-key-1")


@pytest.fixture(scope="session")
def cp_buyer_key() -> JWK:
    """The buyer's key: issues checkout and payment mandates in the human-present flow."""
    return _es256_key("buyer-key-1")


@pytest.fixture(scope="session")
def cp_foreign_key() -> JWK:
    """A key nobody in this platform trusts. Every negative signature test needs one."""
    return _es256_key("foreign-key-1")


@pytest.fixture(autouse=True)
def cp_no_sdk_disk_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the AP2 SDK writing mandate logs into site-packages during tests.

    ``ap2.sdk.mandate._log_event`` appends to ``<package>/.logs/mandate_operations.log``
    and the line it writes includes the holder public key and the whole presentation
    token. Autouse rather than opt-in because the leak is silent: a suite that forgot the
    fixture would still pass, having written a key to disk.
    """
    import ap2.sdk.mandate as ap2_mandate

    def _discard(*_args: object, **_kwargs: object) -> None:
        """Accept the SDK's log call and drop it on the floor."""

    monkeypatch.setattr(ap2_mandate, "_log_event", _discard)

"""Fixtures every commerce-api suite builds on.

Written once here so that the catalogue, checkout, payment, evidence and scenario suites
all seed a tenant the same way, authenticate the same way and tear down in the same
order. The names below are a contract: other build units import them by name and should
not re-declare them.

======================  ==========  =========================================================
Fixture                 Scope       Yields
======================  ==========  =========================================================
``capi_admin_engine``   session     ``Engine`` as the database owner. Teardown only -- the
                                    application roles have no DELETE anywhere.
``capi_app_engine``     session     ``Engine`` as ``commerce_test_app``. Asserted
                                    NOSUPERUSER and NOBYPASSRLS.
``capi_kernel_engine``  session     ``Engine`` as ``commerce_test_kernel``. Same assertion.
``settings_for_tests``  session     ``Settings`` built from a dict: test role URLs, the
                                    development profile, fake ``rzp_test_`` credentials
                                    and a known ``SCENARIO_KEY``.
``api_app``             function    a fresh ``FastAPI`` from ``create_app``. Fresh per test
                                    so one test's merchant-state injection cannot leak
                                    into another's assertions.
``client``              function    ``TestClient`` on that app, unauthenticated.
``seeded_tenant``       function    ``SeededTenant(tenant_id, tenant_slug, merchant_id,
                                    merchant_slug)``. Torn down children first.
``buyer``               function    ``(TestClient, MintedSession)`` for one BUYER session.
                                    ``auth_client`` and ``demo_session`` are its halves, so
                                    a test using both talks about the same buyer.
``demo_session``        function    ``MintedSession(token, session_id, tenant_id,
                                    merchant_id, buyer_ref, actor_type)`` for that BUYER.
``auth_client``         function    ``TestClient`` carrying that session's bearer token.
``mint_client``         function    ``(actor_type, buyer_ref) -> (TestClient,
                                    MintedSession)`` for tests that need a second identity
                                    or an AGENT.
``scenario_headers``    session     ``{"X-Scenario-Key": ...}`` matching the settings.
======================  ==========  =========================================================

Row-level security is real in these tests. The roles are ``NOSUPERUSER NOBYPASSRLS``, and
the assertion is made explicitly rather than assumed, because a superuser connection makes
every isolation test pass for the wrong reason.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
from commerce_api.app import create_app
from commerce_api.settings import Settings
from commerce_domain import Money, uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from transaction_kernel import ContentLine, build_checkout_content

APP_URL: Final[str] = os.environ.get(
    "DATABASE_URL_TEST_APP",
    "postgresql+psycopg://commerce_test_app:testpw@localhost:5432/commerce_test",
)
KERNEL_URL: Final[str] = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL: Final[str] = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

#: Credentials with the right shape and no meaning. The Razorpay guard only inspects the
#: key prefix and the secrets' length and distinctness, so a test never needs a real one.
TEST_KEY_ID: Final[str] = "rzp_test_capitestkey"
TEST_KEY_SECRET: Final[str] = "capi-test-api-secret"  # noqa: S105 - fake, prefix-checked only
TEST_WEBHOOK_SECRET: Final[str] = "capi-test-webhook-sec"  # noqa: S105 - fake, see above
TEST_SCENARIO_KEY: Final[str] = "capi-test-scenario-key"

#: Two P-256 signing keys, committed rather than generated per run, and that is the point
#: of them. The property under test is that a signature outlives the process that made it,
#: so the key a test signs with has to be the same key the next test run verifies against
#: -- a fixture that minted a fresh pair each session could not tell a stable key from an
#: ephemeral one, which is exactly the bug these variables exist to prevent.
#:
#: Two distinct kids, because the merchant and the platform must stay distinguishable.
#: They are throwaway keys for a test database and sign nothing outside it.
TEST_MERCHANT_JWK: Final[str] = json.dumps(
    {
        "crv": "P-256",
        "d": "-3ZK3ypHU_1DqZh-Evn7eNeOJUE6G-T2AXQtDHJmPB0",
        "kid": "capi-test-merchant-1",
        "kty": "EC",
        "x": "iHvrk6KI6IRcm2U6WQGrp0k7aUcJN6HgVOXe9ZxAkNg",
        "y": "0je0a7xIzFXsQCLrz9stUpnrh_TqzPMdzhE00vPaX9M",
    }
)
TEST_PLATFORM_JWK: Final[str] = json.dumps(
    {
        "crv": "P-256",
        "d": "S3YXeWqAejg2qcsLuEIHCVyNjh8L6Qm2J6B-okmFjFs",
        "kid": "capi-test-platform-1",
        "kty": "EC",
        "x": "yZa3mOf8jFIafR2OXQBYnlIV3vQqT4_XeZHACAU5drc",
        "y": "zz7Z3BFdBYAHpSpdHNhcOTiJvhqeFzEy0dtLZG-tCTk",
    }
)

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")

#: Teardown order: children before parents, so no FK is ever violated mid-clean.
#: ``orders`` and ``provider_requests`` reference ``payment_attempts`` and
#: ``policy_at_sale_receipts``; ``checkout_versions`` references the receipts too.
_TENANT_TABLES: Final[tuple[str, ...]] = (
    "provider_requests",
    "reconciliation_runs",
    # `execution_grants` precedes `refunds`: a refund grant carries a foreign key onto the
    # refunds row it was issued for (ADR D10), so deleting the refund first strands the
    # grant and teardown fails on fk_execution_grants_refund_id. And `refunds` precedes
    # `orders` for the same kind of reason: a refund names the order it returns money for.
    "execution_grants",
    "refunds",
    # `support_cases` precedes `orders`: a case names the order it is about, and until the
    # helpdesk existed no test had ever opened one, so the omission cost nothing and was
    # invisible. The first test that opened a case failed in teardown rather than in the
    # assertion, which is the confusing way for this to surface.
    "support_cases",
    # `merchant_actions` names its merchant, so it precedes `merchants` -- and it is listed
    # here rather than lower because the merchant row is the last thing deleted before the
    # tenant, and a table that references it must go before every one of them.
    "merchant_actions",
    # `merchant_policy_versions` also names its merchant, so it precedes `merchants` too.
    # This is the third table in one day whose absence here surfaced as a foreign-key error
    # in teardown rather than as an assertion, which is the confusing way for it to appear;
    # `test_capi_teardown_covers_every_table` now fails on the omission instead.
    "merchant_policy_versions",
    "orders",
    "payment_attempts",
    "approvals",
    "reservations",
    "delegated_authorities",
    "checkout_versions",
    "policy_at_sale_receipts",
    "idempotency_records",
    "audit_events",
    "outbox_events",
    "webhook_inbox",
    "scenario_faults",
    "scenario_runs",
    # Outside row-level security -- the platform-wide switch has no tenant -- but a
    # tenant-scoped row still holds a foreign key onto `tenants`, so a test that throws
    # the kill switch would otherwise fail teardown on fk_platform_operating_modes_tenant_id.
    "platform_operating_modes",
    "checkouts",
    "carts",
    "api_sessions",
    "merchants",
)


# ------------------------------------------------------------------------- engines


def _engine(url: str, *, pool_size: int = 5) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    return engine


def _assert_unprivileged(engine: Engine) -> None:
    """A superuser or BYPASSRLS role would make every isolation assertion vacuous."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert not row.rolsuper, "commerce-api tests must not run as a superuser"
    assert not row.rolbypassrls, "commerce-api tests must not run as a BYPASSRLS role"


@pytest.fixture(scope="session")
def capi_admin_engine() -> Engine:
    """Owner connection. Application roles have no DELETE, so teardown needs this."""
    return _engine(ADMIN_URL, pool_size=2)


@pytest.fixture(scope="session")
def capi_app_engine() -> Engine:
    engine = _engine(APP_URL)
    _assert_unprivileged(engine)
    return engine


@pytest.fixture(scope="session")
def capi_kernel_engine() -> Engine:
    engine = _engine(KERNEL_URL)
    _assert_unprivileged(engine)
    return engine


# ------------------------------------------------------------------------ settings


@pytest.fixture(scope="session")
def settings_for_tests() -> Settings:
    """The configuration every test app is built from.

    Constructed from a dictionary, never from ``os.environ``, so a developer's real
    ``.env`` cannot reach a test and a live key cannot reach this process.
    """
    return Settings(
        PROFILE="development",
        DATABASE_URL_APP=APP_URL,
        DATABASE_URL_KERNEL=KERNEL_URL,
        RAZORPAY_KEY_ID=TEST_KEY_ID,
        RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
        RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
        SCENARIO_KEY=TEST_SCENARIO_KEY,
        SESSION_TTL_SECONDS=3600,
        UCP_MERCHANT_SIGNING_JWK=TEST_MERCHANT_JWK,
        UCP_PLATFORM_SIGNING_JWK=TEST_PLATFORM_JWK,
    )


@pytest.fixture(scope="session")
def scenario_headers() -> dict[str, str]:
    """Headers that satisfy ``require_scenario_key`` for ``settings_for_tests``."""
    return {"X-Scenario-Key": TEST_SCENARIO_KEY}


@pytest.fixture
def api_app(settings_for_tests: Settings, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A fresh app per test: a new merchant registry, and so a clean catalogue."""
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    return create_app(settings_for_tests)


@pytest.fixture
def client(api_app: FastAPI) -> Iterator[TestClient]:
    """An unauthenticated client. Use ``auth_client`` for anything behind a session."""
    with TestClient(api_app) as test_client:
        yield test_client


# -------------------------------------------------------------------------- seeding


@dataclass(frozen=True, slots=True)
class SeededTenant:
    """One tenant and one merchant, created for a single test and deleted after it."""

    tenant_id: uuid.UUID
    tenant_slug: str
    merchant_id: uuid.UUID
    merchant_slug: str


@dataclass(frozen=True, slots=True)
class MintedSession:
    """The result of ``POST /v1/demo/sessions``, plus the header it implies."""

    token: str
    session_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    buyer_ref: str
    actor_type: str

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def seeded_tenant(capi_admin_engine: Engine) -> Iterator[SeededTenant]:
    """Create a tenant and a merchant, yield their identity, then delete everything.

    The teardown walks :data:`_TENANT_TABLES` -- children before parents -- and only then
    removes the tenant row. A test that leaves a payment attempt behind therefore cleans
    up rather than failing the next run on a foreign key.
    """
    tenant_id, merchant_id = uuid7(), uuid7()
    # The slug is drawn from uuid4, not from the uuid7 identifiers. A UUIDv7's leading hex
    # digits are its 48-bit millisecond clock, so `uuid7().hex[:10]` is that clock shifted
    # right by eight bits and carries no entropy at all -- five thousand values in a tight
    # loop produce exactly one distinct slug, and every tenant seeded inside the same
    # ~256 ms window collides on uq_tenants_slug at fixture setup. The identifiers stay
    # uuid7 (specification 24.1); only the slug derivation was wrong.
    suffix = uuid.uuid4().hex[:12]
    tenant_slug = f"t-{suffix}"
    merchant_slug = f"m-{suffix}"

    with capi_admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :slug, :name, 'asia-south1')"
            ),
            {"id": tenant_id, "slug": tenant_slug, "name": tenant_slug},
        )
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :tenant, :slug, :name, 'INR')"
            ),
            {
                "id": merchant_id,
                "tenant": tenant_id,
                "slug": merchant_slug,
                "name": "Demo Grocery Store",
            },
        )

    yield SeededTenant(
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        merchant_id=merchant_id,
        merchant_slug=merchant_slug,
    )

    with capi_admin_engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        for table in _TENANT_TABLES:
            # S608: `table` iterates the literal tuple above, never request data, and a
            # SQL identifier cannot be supplied as a bound parameter.
            statement = text(f"DELETE FROM {table} WHERE tenant_id = :tenant")  # noqa: S608
            conn.execute(statement, {"tenant": tenant_id})
        conn.execute(_SET_TENANT, {"tenant_id": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})


# ------------------------------------------------------------------ authentication


@pytest.fixture
def mint_client(
    client: TestClient, seeded_tenant: SeededTenant
) -> Callable[..., tuple[TestClient, MintedSession]]:
    """Factory: mint a session on the seeded tenant and return a client carrying it.

    ``mint_client()`` gives a BUYER; ``mint_client(actor_type="AGENT")`` gives an agent,
    whose capability set deliberately excludes approval. Pass ``buyer_ref`` to get a
    second identity in the same tenant, which is how ownership tests are written.
    """

    def _mint(
        actor_type: str = "BUYER", buyer_ref: str | None = None
    ) -> tuple[TestClient, MintedSession]:
        body: dict[str, str] = {
            "tenant_slug": seeded_tenant.tenant_slug,
            "actor_type": actor_type,
        }
        if buyer_ref is not None:
            body["buyer_ref"] = buyer_ref
        response = client.post("/v1/demo/sessions", json=body)
        assert response.status_code == 201, response.text
        payload = response.json()
        minted = MintedSession(
            token=payload["token"],
            session_id=uuid.UUID(payload["session_id"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            merchant_id=uuid.UUID(payload["merchant_id"]),
            buyer_ref=payload["buyer_ref"],
            actor_type=payload["actor_type"],
        )
        authed = TestClient(client.app, headers=minted.auth_header)
        return authed, minted

    return _mint


@pytest.fixture
def buyer(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> tuple[TestClient, MintedSession]:
    """One BUYER session, minted once. ``auth_client`` and ``demo_session`` are its two
    halves, so a test that uses both is talking about the same buyer -- which is what
    every ownership assertion assumes."""
    return mint_client()


@pytest.fixture
def demo_session(buyer: tuple[TestClient, MintedSession]) -> MintedSession:
    """The BUYER session ``auth_client`` authenticates as."""
    return buyer[1]


@pytest.fixture
def auth_client(buyer: tuple[TestClient, MintedSession]) -> Iterator[TestClient]:
    """A client whose every request carries ``demo_session``'s bearer token."""
    with buyer[0] as authed:
        yield authed


# ----------------------------------------------------------- approved checkout content


def approved_content(checkout_id: uuid.UUID, version: int, total: Money) -> dict[str, Any]:
    """A canonical checkout document a test can store and the kernel will accept.

    Built through :func:`transaction_kernel.build_checkout_content` rather than written
    out as a literal, and that is the whole point of it existing. Two suites previously
    kept their own six-key dictionaries here -- ``checkout_id``, ``version``, ``currency``,
    ``total_minor``, ``line_items``, ``policy_version`` -- which was enough while the
    document was only ever hashed and compared. The moment a reader parsed one through
    the contract it wanted fourteen keys, and both suites failed with a document that the
    real checkout path could never have produced. A fixture that cannot drift from the
    contract is the fix; a fixture with eight more literal keys would only postpone it.

    The figures are derived from ``total`` so a caller keeps naming the one number its
    assertions are about. Tax is apportioned to the lines rather than left at the
    document level, so a rendered breakdown shows tax on items and nothing unexplained on
    delivery.
    """
    tax = total.minor // 21
    subtotal = total.minor - tax
    milk_unit = subtotal // 4
    milk_line = milk_unit * 2
    bread_line = subtotal - milk_line
    milk_tax = tax // 2
    return build_checkout_content(
        checkout_id=checkout_id,
        version=version,
        currency=total.currency,
        lines=(
            ContentLine(
                sku="sku_bread",
                name="Bread",
                quantity=1,
                unit_minor=bread_line,
                line_minor=bread_line,
                tax_minor=tax - milk_tax,
            ),
            ContentLine(
                sku="sku_milk",
                name="Milk",
                quantity=2,
                unit_minor=milk_unit,
                line_minor=milk_line,
                tax_minor=milk_tax,
            ),
        ),
        subtotal_minor=subtotal,
        tax_minor=tax,
        delivery_fee_minor=0,
        discount_minor=0,
        total_minor=total.minor,
        policy_version="pol-v12",
        catalogue_revision=1,
        source_id="sim-test",
    )

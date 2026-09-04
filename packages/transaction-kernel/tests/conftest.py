"""Shared fixtures for admission integration tests.

Builds a checkout that is genuinely admissible — version row, Policy-at-Sale Receipt,
active reservation — so that a denial in a test is caused by the thing under test and not
by missing setup. A fixture that cannot produce an admissible checkout would let every
test pass for the wrong reason.

Connects as ``commerce_test_kernel``, a NOSUPERUSER NOBYPASSRLS role, because a superuser
bypasses row-level security and would make every isolation assertion vacuous.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from transaction_kernel import receipts, reservations
from transaction_kernel.admission import CurrentMerchantState
from transaction_kernel.contracts import ActorType, AgentPrincipal, CheckoutRef
from transaction_kernel.receipts import (
    BuyerVisibleRef,
    MerchantPolicy,
    PolicyKind,
    ReceiptDraft,
)
from transaction_kernel.states import CheckoutState

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

APPROVED_TOTAL = Money(39500, "INR")


def _engine(url: str, pool_size: int = 5) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    return engine


@pytest.fixture(scope="session")
def adm_kernel_engine() -> Engine:
    engine = _engine(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert not row.rolsuper, "admission tests must not run as a superuser"
    assert not row.rolbypassrls, "admission tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def adm_admin_engine() -> Engine:
    """Owner connection. Application roles have no DELETE, so teardown needs this."""
    return _engine(ADMIN_URL, pool_size=2)


@dataclass(frozen=True, slots=True)
class Fixture:
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout: CheckoutRef
    principal: AgentPrincipal
    correlation_id: uuid.UUID


def _content(checkout_id: uuid.UUID, version: int, total: Money) -> dict[str, Any]:
    """The canonical checkout payload. Must match CurrentMerchantState.content_for_hash."""
    return {
        "checkout_id": str(checkout_id),
        "version": version,
        "currency": total.currency,
        "total_minor": total.minor,
        "line_items": {"sku_milk": 2, "sku_bread": 1},
        "policy_version": "pol-v12",
    }


@pytest.fixture
def admissible(adm_admin_engine: Engine, adm_kernel_engine: Engine) -> Iterator[Fixture]:
    tenant_id, merchant_id = uuid.uuid4(), uuid7()
    checkout_id, version = uuid7(), 7
    content = _content(checkout_id, version, APPROVED_TOTAL)
    checkout = CheckoutRef(checkout_id, version, canonical_hash(content))

    with adm_admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :s, :n, 'asia-south1')"
            ),
            {"id": tenant_id, "s": f"t-{tenant_id.hex[:8]}", "n": f"t-{tenant_id.hex[:8]}"},
        )
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :s, :n, 'INR')"
            ),
            {
                "id": merchant_id,
                "t": tenant_id,
                "s": f"m-{merchant_id.hex[:8]}",
                "n": f"m-{merchant_id.hex[:8]}",
            },
        )
        conn.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, 'INR', :total, "
                ":status, true)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "v": version,
                "content": __import__("json").dumps(content, sort_keys=True),
                "h": checkout.content_hash,
                "total": APPROVED_TOTAL.minor,
                "status": CheckoutState.APPROVAL_REQUIRED.value,
            },
        )

    # Receipt and reservation are written through their own modules, so the fixture
    # exercises the same code paths production uses rather than hand-rolling rows.
    session = Session(adm_kernel_engine, expire_on_commit=False)
    with session.begin():
        session.execute(SET_TENANT, {"t": str(tenant_id)})
        issued = receipts.issue_receipt(
            session,
            ReceiptDraft(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout_id=checkout_id,
                checkout_version=version,
                checkout_hash=checkout.content_hash,
                policies=tuple(
                    MerchantPolicy(
                        kind=kind,
                        policy_id=f"pol-{kind.value.lower()}",
                        policy_version=12,
                        terms={"summary": f"{kind.value} terms"},
                    )
                    for kind in PolicyKind
                ),
                tax_policy_version=3,
                rounding_policy_version=1,
                buyer_visible_refs=(
                    BuyerVisibleRef(
                        label="Refund policy",
                        uri="https://demo.invalid/policies/refund",
                        text_hash=canonical_hash({"policy": "refund", "version": 12}),
                    ),
                ),
                correlation_id=uuid7(),
            ),
        )
        session.execute(
            text(
                "UPDATE checkout_versions SET policy_receipt_id = :rid, "
                "policy_receipt_hash = :rh WHERE tenant_id = :t AND checkout_id = :c "
                "AND version = :v"
            ),
            {
                "rid": issued.receipt_id,
                "rh": issued.receipt_hash,
                "t": tenant_id,
                "c": checkout_id,
                "v": version,
            },
        )
        reservations.reserve(
            session, checkout_id=checkout_id, checkout_version=version, ttl_seconds=900
        )
        # Buyer approves: APPROVAL_REQUIRED -> APPROVED, the state admission expects.
        session.execute(
            text(
                "UPDATE checkout_versions SET status = :s WHERE tenant_id = :t "
                "AND checkout_id = :c AND version = :v"
            ),
            {"s": CheckoutState.APPROVED.value, "t": tenant_id, "c": checkout_id, "v": version},
        )
    session.close()

    yield Fixture(
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        checkout=checkout,
        principal=AgentPrincipal(
            principal_id="p-test",
            tenant_id=tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=merchant_id,
            capabilities=frozenset({"checkout.submit_approved"}),
        ),
        correlation_id=uuid7(),
    )

    with adm_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in (
            "execution_grants",
            "payment_attempts",
            "audit_events",
            "reservations",
            "checkout_versions",
            "policy_at_sale_receipts",
            "merchants",
        ):
            # S608: `table` iterates the literal tuple defined directly above, never
            # request data. A SQL identifier cannot be supplied as a bound parameter.
            statement = text(f"DELETE FROM {table} WHERE tenant_id = :t")  # noqa: S608
            conn.execute(statement, {"t": tenant_id})
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})


class StubMerchant:
    """A merchant whose current state the test controls.

    ``unchanged`` reproduces exactly what was approved; the mutators simulate the merchant
    moving underneath an approval, which is the scenario the kernel exists for.
    """

    def __init__(
        self, checkout_id: uuid.UUID, total: Money = APPROVED_TOTAL, available: bool = True
    ) -> None:
        self._checkout_id = checkout_id
        self.total = total
        self.available = available

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        content = _content(checkout_id, version, self.total)
        return CurrentMerchantState(
            total=self.total,
            line_items=content["line_items"],
            all_available=self.available,
            policy_version=content["policy_version"],
        )


@pytest.fixture
def merchant(admissible: Fixture) -> StubMerchant:
    return StubMerchant(admissible.checkout.checkout_id)


@pytest.fixture
def kernel_session_factory(adm_kernel_engine: Engine) -> Iterator[Callable[[], Session]]:
    opened: list[Session] = []

    def factory() -> Session:
        s = Session(adm_kernel_engine, expire_on_commit=False)
        opened.append(s)
        return s

    yield factory
    for s in opened:
        try:
            s.rollback()
        finally:
            s.close()

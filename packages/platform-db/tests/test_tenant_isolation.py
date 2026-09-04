"""Tenant isolation, specification 21.2.

The headline case is ``test_alternating_tenants_on_one_pooled_connection``: connection
pools reuse connections between requests, and a tenant set with session scope survives
into whichever tenant borrows that connection next. That leak is silent and intermittent,
and it presents as corrupted data rather than as an authorization bug.
"""

from __future__ import annotations

import uuid

import pytest
from platform_db import TenantContextError, current_tenant, set_tenant, tenant_scope
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.db

MERCHANT_COUNT = text("SELECT count(*) FROM merchants")


class TestPolicyEnforcement:
    def test_tenant_sees_only_its_own_rows(self, kernel_session: Session, two_tenants):
        a, b = two_tenants
        with kernel_session.begin():
            set_tenant(kernel_session, a)
            assert kernel_session.execute(MERCHANT_COUNT).scalar() == 1
            rows = kernel_session.execute(text("SELECT tenant_id FROM merchants")).scalars().all()
            assert rows == [a]

    def test_other_tenant_rows_are_invisible_not_merely_filtered_by_the_app(
        self, kernel_session: Session, two_tenants
    ):
        a, b = two_tenants
        with kernel_session.begin():
            set_tenant(kernel_session, a)
            # An explicit predicate for the other tenant still returns nothing: the
            # database, not the query, is doing the filtering.
            found = kernel_session.execute(
                text("SELECT count(*) FROM merchants WHERE tenant_id = :b"), {"b": b}
            ).scalar()
            assert found == 0

    def test_unset_tenant_returns_no_rows_fails_closed(self, kernel_session: Session, two_tenants):
        with kernel_session.begin():
            assert current_tenant(kernel_session) is None
            # Forgetting the context yields an empty result, never another tenant's data.
            assert kernel_session.execute(MERCHANT_COUNT).scalar() == 0

    def test_with_check_blocks_writing_into_another_tenant(
        self, kernel_session: Session, two_tenants
    ):
        a, b = two_tenants
        with pytest.raises(ProgrammingError, match="row-level security"):
            with kernel_session.begin():
                set_tenant(kernel_session, a)
                kernel_session.execute(
                    text(
                        "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                        "VALUES (:id, :b, 'smuggled', 'smuggled', 'INR')"
                    ),
                    {"id": uuid.uuid4(), "b": b},
                )


class TestPooledConnectionReuse:
    def test_alternating_tenants_on_one_pooled_connection(self, kernel_engine: Engine, two_tenants):
        """The leak this design exists to prevent.

        Pool size is 1, so every transaction below runs on the same physical connection.
        If the tenant were session-scoped it would survive into the next transaction and
        tenant B would read tenant A's rows.
        """
        a, b = two_tenants
        factory = sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)
        seen: list[tuple[uuid.UUID, int, uuid.UUID | None]] = []

        for tenant in (a, b, a, b, a):
            session = factory()
            try:
                with session.begin():
                    set_tenant(session, tenant)
                    count = session.execute(MERCHANT_COUNT).scalar() or 0
                    owner = session.execute(
                        text("SELECT tenant_id FROM merchants LIMIT 1")
                    ).scalar()
                    seen.append((tenant, count, owner))
            finally:
                session.close()

        for tenant, count, owner in seen:
            assert count == 1, f"tenant {tenant} saw {count} merchants"
            assert owner == tenant, f"tenant {tenant} saw a row owned by {owner}"

    def test_context_does_not_survive_its_transaction(self, kernel_engine: Engine, two_tenants):
        a, _ = two_tenants
        factory = sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)

        session = factory()
        with session.begin():
            set_tenant(session, a)
            assert current_tenant(session) == a
        session.close()

        # A fresh transaction on the same pooled connection starts with no tenant.
        session = factory()
        try:
            with session.begin():
                assert current_tenant(session) is None
                assert session.execute(MERCHANT_COUNT).scalar() == 0
        finally:
            session.close()


class TestTenantContextApi:
    def test_set_tenant_outside_a_transaction_raises(self, kernel_session: Session):
        # A transaction-local setting outside a transaction is discarded immediately,
        # which would present as "all the data vanished" rather than a clear error.
        with pytest.raises(TenantContextError, match="requires an active transaction"):
            set_tenant(kernel_session, uuid.uuid4())

    def test_non_uuid_tenant_is_refused(self, kernel_session: Session):
        with kernel_session.begin():
            with pytest.raises(TenantContextError, match="must be a UUID"):
                set_tenant(kernel_session, "not-a-uuid")  # type: ignore[arg-type]

    def test_scope_restores_previous_tenant(self, kernel_session: Session, two_tenants):
        a, b = two_tenants
        with kernel_session.begin():
            set_tenant(kernel_session, a)
            with tenant_scope(kernel_session, b):
                assert current_tenant(kernel_session) == b
                assert (
                    kernel_session.execute(text("SELECT tenant_id FROM merchants LIMIT 1")).scalar()
                    == b
                )
            assert current_tenant(kernel_session) == a

    def test_scope_restores_unset_state(self, kernel_session: Session, two_tenants):
        a, _ = two_tenants
        with kernel_session.begin():
            assert current_tenant(kernel_session) is None
            with tenant_scope(kernel_session, a):
                assert current_tenant(kernel_session) == a
            assert current_tenant(kernel_session) is None

    def test_scope_restores_even_when_the_block_raises(self, kernel_session: Session, two_tenants):
        a, b = two_tenants
        with kernel_session.begin():
            set_tenant(kernel_session, a)
            with pytest.raises(ValueError, match="boom"), tenant_scope(kernel_session, b):
                raise ValueError("boom")
            assert current_tenant(kernel_session) == a


class TestPrivilegeSeparation:
    def test_app_role_cannot_write_financial_tables(self, app_engine: Engine, two_tenants):
        """Specification 21.3: only the kernel role writes financial state.

        The import-boundary rule is a lint check a determined caller can bypass. A role
        without INSERT on approvals cannot be bypassed by importing another module.
        """
        a, _ = two_tenants
        with pytest.raises(ProgrammingError, match="permission denied"):
            with app_engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(a)})
                conn.execute(
                    text(
                        "INSERT INTO approvals (id, tenant_id, checkout_id, checkout_version, "
                        "content_hash, amount_minor, currency, action, status, expires_at) "
                        "VALUES (:id, :t, :c, 1, 'h', 100, 'INR', 'PAY', 'RECORDED', now())"
                    ),
                    {"id": uuid.uuid4(), "t": a, "c": uuid.uuid4()},
                )

    def test_audit_events_cannot_be_updated_by_any_application_role(self, kernel_engine: Engine):
        """Evidence that can be edited is not evidence."""
        with pytest.raises(ProgrammingError, match="permission denied"):
            with kernel_engine.begin() as conn:
                conn.execute(text("UPDATE audit_events SET event_type = 'tampered'"))

    def test_audit_events_cannot_be_deleted(self, kernel_engine: Engine):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with kernel_engine.begin() as conn:
                conn.execute(text("DELETE FROM audit_events"))

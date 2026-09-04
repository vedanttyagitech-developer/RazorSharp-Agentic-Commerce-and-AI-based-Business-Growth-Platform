"""Service tables (ADR 0003, migration c6ffa021cbb0): isolation, grants and the two
index corrections on existing tables.

Every assertion here is made against PostgreSQL as a NOSUPERUSER NOBYPASSRLS role,
because the guarantees under test -- who can write ``orders``, which tenant can see a
webhook -- live in the database's grant and policy catalogues, not in Python. A test
that inspected ``roles.py`` would pass while a migration that forgot a GRANT left every
new table invisible to the application.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from platform_db import (
    FINANCIAL_TABLES,
    RLS_TABLES,
    SERVICE_RLS_TABLES,
    SERVICE_TABLES,
    WRITE_GRANTS,
    Base,
    roles,
)
from platform_db.rls import (
    TENANT_PREDICATE,
    all_statements,
    grant_statements,
    rls_statements,
    table_statements,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from transaction_kernel.states import CheckoutState

pytestmark = pytest.mark.db

WORKER_URL = os.environ.get(
    "DATABASE_URL_TEST_WORKER",
    "postgresql+psycopg://commerce_test_worker:testpw@localhost:5432/commerce_test",
)

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")
SERVICE_SRC = (
    Path(__file__).resolve().parents[1] / "src" / "platform_db" / "schema_service.py"
).read_text()

# Teardown order: children before parents. orders/provider_requests/reconciliation_runs
# reference payment_attempts, receipts and grants; checkouts reference baskets.
_TEARDOWN_ORDER = (
    "provider_requests",
    "reconciliation_runs",
    "orders",
    "refunds",
    "execution_grants",
    "payment_attempts",
    "approvals",
    "policy_at_sale_receipts",
    "checkouts",
    "baskets",
    "webhook_inbox",
    "scenario_faults",
    "scenario_runs",
    "api_sessions",
    "merchants",
)


@pytest.fixture(scope="session")
def worker_engine() -> Engine:
    """The worker role. Created by scripts/bootstrap_test_roles.sql; skip if absent."""
    engine = create_engine(WORKER_URL, future=True, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            ).one()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"worker role not reachable (run scripts/bootstrap_test_roles.sql): {exc}")
    assert row.rolsuper is False and row.rolbypassrls is False
    return engine


@pytest.fixture
def tenant(admin_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """One tenant with one merchant; every row the test wrote is removed afterwards."""
    tenant_id, merchant_id = uuid.uuid4(), uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :slug, :name, 'asia-south1')"
            ),
            {"id": tenant_id, "slug": f"t-{tenant_id.hex[:8]}", "name": f"t-{tenant_id.hex[:8]}"},
        )
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :slug, :name, 'INR')"
            ),
            {"id": merchant_id, "t": tenant_id, "slug": "m", "name": "m"},
        )
    yield tenant_id, merchant_id
    with admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in _TEARDOWN_ORDER:
            # S608: `table` iterates the literal tuple above, never request data.
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})


def _insert_attempt(
    conn: object,
    tenant_id: uuid.UUID,
    *,
    checkout_id: uuid.UUID | None = None,
    status: str = "SUBMITTED",
    provider_order_id: str | None = None,
) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
            "status, amount_minor, currency, receipt, provider_order_id) VALUES "
            "(:id, :t, :c, 1, :s, 1000, 'INR', :r, :po)"
        ),
        {
            "id": attempt_id,
            "t": tenant_id,
            "c": checkout_id or uuid.uuid4(),
            "s": status,
            "r": f"rcpt-{attempt_id.hex[:12]}",
            "po": provider_order_id,
        },
    )
    return attempt_id


def _insert_receipt(conn: object, tenant_id: uuid.UUID, merchant_id: uuid.UUID) -> uuid.UUID:
    receipt_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO policy_at_sale_receipts (id, tenant_id, merchant_id, checkout_id, "
            "checkout_version, content, receipt_hash) VALUES "
            "(:id, :t, :m, :c, 1, CAST('{}' AS jsonb), 'rh')"
        ),
        {"id": receipt_id, "t": tenant_id, "m": merchant_id, "c": uuid.uuid4()},
    )
    return receipt_id


_ORDER_INSERT = text(
    "INSERT INTO orders (id, tenant_id, merchant_id, checkout_id, checkout_version, "
    "payment_attempt_id, policy_receipt_id, policy_receipt_hash, total_minor, currency, "
    "status, capture_evidence) VALUES (:id, :t, :m, :c, 1, :a, :r, 'rh', 1000, 'INR', "
    "'CONFIRMED', CAST('{\"source\": \"WEBHOOK\"}' AS jsonb))"
)


# ------------------------------------------------------------------- declarations agree


class TestDeclarationsAgree:
    """The Python lists and the database must describe the same tables."""

    def test_service_rls_tables_are_the_tail_of_rls_tables(self) -> None:
        assert RLS_TABLES[-len(SERVICE_RLS_TABLES) :] == SERVICE_RLS_TABLES
        assert "api_sessions" not in RLS_TABLES

    def test_financial_service_tables_are_declared(self) -> None:
        assert {"orders", "provider_requests", "reconciliation_runs"} <= set(FINANCIAL_TABLES)
        # A table cannot be both kernel-only and app-writable.
        assert not set(FINANCIAL_TABLES) & set(WRITE_GRANTS)

    def test_every_service_table_has_a_writer_decision(self) -> None:
        for table in SERVICE_TABLES:
            assert table in FINANCIAL_TABLES or table in WRITE_GRANTS, table

    def test_nobody_is_granted_delete(self) -> None:
        for table, per_role in WRITE_GRANTS.items():
            for role, privileges in per_role.items():
                assert "DELETE" not in privileges, (table, role)

    def test_orm_metadata_matches_the_live_service_tables(self, admin_engine: Engine) -> None:
        """Guards the hand-edited migration against the ORM it was generated from."""

        def only_service(obj: object, name: str | None, type_: str, *_: object) -> bool:
            if type_ == "table":
                return name in SERVICE_TABLES
            table = getattr(obj, "table", None)
            return table is not None and table.name in SERVICE_TABLES

        with admin_engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"include_object": only_service})
            diff = compare_metadata(ctx, Base.metadata)
        assert diff == [], diff

    def test_checkout_head_status_mirrors_checkout_state(self, kernel_engine: Engine) -> None:
        """The head's CHECK is a copy of checkout_versions.status_enum: a state the
        version row can hold but the head rejects would fail at COMMIT."""
        body = SERVICE_SRC[SERVICE_SRC.index("class Checkout(Base):") :]
        match = re.search(r'"status IN \((.*?)\)",\s*\n\s*name="status_enum"', body, re.S)
        assert match
        in_source = set(re.findall(r"'([A-Z_]+)'", match.group(1)))
        assert in_source == {s.value for s in CheckoutState}
        with kernel_engine.connect() as conn:
            live = conn.execute(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                {"n": "ck_checkouts_status_enum"},
            ).scalar()
        assert live
        assert set(re.findall(r"'([A-Z_]+)'", live)) == {s.value for s in CheckoutState}


# ------------------------------------------------------------------ row-level security


class TestRowLevelSecurity:
    @pytest.mark.parametrize("table", SERVICE_RLS_TABLES)
    def test_tenant_owned_table_is_enabled_and_forced(
        self, kernel_engine: Engine, table: str
    ) -> None:
        with kernel_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :n AND relkind = 'r'"
                ),
                {"n": table},
            ).one_or_none()
            policy = conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE tablename = :n AND policyname = 'tenant_isolation'"
                ),
                {"n": table},
            ).one_or_none()
        assert row is not None, f"{table} is missing"
        assert row.relrowsecurity, f"{table} has no row-level security"
        assert row.relforcerowsecurity, f"{table} RLS is not FORCED; the owner would bypass it"
        assert policy is not None, f"{table} has no tenant_isolation policy"
        # PostgreSQL re-renders the predicate; the load-bearing NULLIF must survive.
        assert "NULLIF" in policy.qual and "NULLIF" in policy.with_check

    def test_api_sessions_has_no_row_level_security(self, kernel_engine: Engine) -> None:
        with kernel_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'api_sessions'"
                )
            ).one()
        assert row.relrowsecurity is False
        assert row.relforcerowsecurity is False

    def test_api_session_is_readable_without_a_tenant_bound(
        self, app_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Resolving a bearer token is how a request learns its tenant, so the lookup
        must work before any tenant is set."""
        tenant_id, merchant_id = tenant
        token_hash = uuid.uuid4().hex + uuid.uuid4().hex
        with app_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_sessions (id, tenant_id, merchant_id, token_hash, "
                    "buyer_ref, actor_type, capabilities, expires_at) VALUES "
                    "(:id, :t, :m, :h, 'buyer-1', 'BUYER', CAST('[\"basket.write\"]' AS jsonb), "
                    "now() + interval '1 hour')"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "m": merchant_id, "h": token_hash},
            )
        with app_engine.begin() as conn:
            found = conn.execute(
                text("SELECT tenant_id FROM api_sessions WHERE token_hash = :h"),
                {"h": token_hash},
            ).scalar()
        assert found == tenant_id

    @pytest.mark.parametrize("table", ("baskets", "checkouts", "webhook_inbox"))
    def test_other_tenant_rows_are_invisible(
        self, kernel_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID], table: str
    ) -> None:
        a, b = two_tenants
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(a)})
            _seed_row(conn, table, a)
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(a)})
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 1  # noqa: S608
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(b)})
            # An explicit predicate for the other tenant still returns nothing.
            assert (
                conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :a"),  # noqa: S608
                    {"a": a},
                ).scalar()
                == 0
            )
        with kernel_engine.begin() as conn:
            # Unset context: fails closed.
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0  # noqa: S608
        with pytest.raises(ProgrammingError, match="row-level security"):
            with kernel_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(a)})
                _seed_row(conn, table, b)
        with kernel_engine.begin() as conn:
            # The seeded row must be removed under RLS through an owner connection.
            pass
        _cleanup(kernel_engine, table, a)


def _seed_row(conn: object, table: str, tenant_id: uuid.UUID) -> None:
    merchant_id = conn.execute(  # type: ignore[attr-defined]
        text("SELECT id FROM merchants WHERE tenant_id = :t"), {"t": tenant_id}
    ).scalar()
    if table == "baskets":
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO baskets (id, tenant_id, merchant_id, buyer_ref, lines) "
                "VALUES (:id, :t, :m, 'buyer', CAST('[]' AS jsonb))"
            ),
            {"id": uuid.uuid4(), "t": tenant_id, "m": merchant_id},
        )
    elif table == "checkouts":
        basket_id = uuid.uuid4()
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO baskets (id, tenant_id, merchant_id, buyer_ref, lines, status) "
                "VALUES (:id, :t, :m, 'buyer', CAST('[]' AS jsonb), 'CHECKED_OUT')"
            ),
            {"id": basket_id, "t": tenant_id, "m": merchant_id},
        )
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO checkouts (id, tenant_id, merchant_id, basket_id, buyer_ref, "
                "current_version, status, correlation_id) VALUES "
                "(:id, :t, :m, :b, 'buyer', 1, 'APPROVAL_REQUIRED', :corr)"
            ),
            {
                "id": uuid.uuid4(),
                "t": tenant_id,
                "m": merchant_id,
                "b": basket_id,
                "corr": uuid.uuid4(),
            },
        )
    else:
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, event_type, body_digest, "
                "raw_body, headers_redacted, signature_verified) VALUES "
                "(:id, :t, :k, 'payment.captured', 'd', :body, CAST('{}' AS jsonb), true)"
            ),
            {"id": uuid.uuid4(), "t": tenant_id, "k": f"evt:{uuid.uuid4().hex}", "body": b"{}"},
        )


def _cleanup(engine: Engine, table: str, tenant_id: uuid.UUID) -> None:
    """Application roles have no DELETE; the two_tenants fixture only removes merchants
    and tenants, so the rows this test seeded must go through the owner."""
    admin = create_engine(
        os.environ.get(
            "DATABASE_URL_TEST_ADMIN",
            "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
        ),
        future=True,
        pool_size=1,
        max_overflow=0,
    )
    with admin.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for child in ("checkouts", table, "baskets"):
            conn.execute(text(f"DELETE FROM {child} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
    admin.dispose()
    del engine


# ----------------------------------------------------------------------------- grants


class TestGrants:
    """Specification 21.3: the grant set is the physical boundary of ADR D1."""

    @pytest.mark.parametrize("table", ("orders", "provider_requests", "reconciliation_runs"))
    def test_app_role_cannot_insert_financial_evidence(
        self, app_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID], table: str
    ) -> None:
        tenant_id, _ = tenant
        with pytest.raises(ProgrammingError, match="permission denied"):
            with app_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
                # The statement never reaches the row: the grant check comes first, so
                # a minimal column list is enough to prove the denial.
                conn.execute(
                    text(f"INSERT INTO {table} (id, tenant_id) VALUES (:id, :t)"),  # noqa: S608
                    {"id": uuid.uuid4(), "t": tenant_id},
                )

    @pytest.mark.parametrize("table", ("orders", "provider_requests", "reconciliation_runs"))
    def test_worker_role_cannot_insert_financial_evidence(
        self, worker_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID], table: str
    ) -> None:
        tenant_id, _ = tenant
        with pytest.raises(ProgrammingError, match="permission denied"):
            with worker_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
                conn.execute(
                    text(f"INSERT INTO {table} (id, tenant_id) VALUES (:id, :t)"),  # noqa: S608
                    {"id": uuid.uuid4(), "t": tenant_id},
                )

    def test_kernel_role_writes_all_three(
        self, kernel_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = tenant
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            attempt_id = _insert_attempt(conn, tenant_id, status="CAPTURED")
            receipt_id = _insert_receipt(conn, tenant_id, merchant_id)
            conn.execute(
                _ORDER_INSERT,
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "m": merchant_id,
                    "c": uuid.uuid4(),
                    "a": attempt_id,
                    "r": receipt_id,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO provider_requests (id, tenant_id, payment_attempt_id, operation, "
                    "method, url, body_hash, header_names, http_status, outcome_code) VALUES "
                    "(:id, :t, :a, 'CREATE_ORDER', 'POST', 'https://api.razorpay.com/v1/orders', "
                    "'bh', CAST('[\"authorization\"]' AS jsonb), 200, 'OK')"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "a": attempt_id},
            )
            conn.execute(
                text(
                    "INSERT INTO reconciliation_runs (id, tenant_id, payment_attempt_id, "
                    "attempt_number, reason, identifiers_queried, decision, correlation_id) "
                    "VALUES (:id, :t, :a, 1, 'client_return', CAST('{}' AS jsonb), "
                    "'order_found', :corr)"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "a": attempt_id, "corr": uuid.uuid4()},
            )
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == 1

    def test_one_order_per_payment_attempt(
        self, kernel_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The ON CONFLICT target for idempotent confirmation when a webhook and a
        reconciliation fetch both report the same capture."""
        tenant_id, merchant_id = tenant
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            attempt_id = _insert_attempt(conn, tenant_id, status="CAPTURED")
            receipt_id = _insert_receipt(conn, tenant_id, merchant_id)
            params = {
                "t": tenant_id,
                "m": merchant_id,
                "c": uuid.uuid4(),
                "a": attempt_id,
                "r": receipt_id,
            }
            conn.execute(_ORDER_INSERT, {"id": uuid.uuid4(), **params})
            with pytest.raises(IntegrityError, match="uq_orders_tenant_id_payment_attempt_id"):
                conn.execute(_ORDER_INSERT, {"id": uuid.uuid4(), **params})

    def test_app_arms_faults_and_worker_consumes_them(
        self, app_engine: Engine, worker_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, _ = tenant
        fault_id = uuid.uuid4()
        with app_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO scenario_faults (id, tenant_id, kind, checkout_id) "
                    "VALUES (:id, :t, 'CREATE_ORDER_TIMEOUT', :c)"
                ),
                {"id": fault_id, "t": tenant_id, "c": uuid.uuid4()},
            )
        with worker_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            updated = conn.execute(
                text(
                    "UPDATE scenario_faults SET armed = false, consumed_at = now() "
                    "WHERE id = :id AND armed"
                ),
                {"id": fault_id},
            ).rowcount
        assert updated == 1
        with pytest.raises(ProgrammingError, match="permission denied"):
            with worker_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
                conn.execute(
                    text(
                        "INSERT INTO scenario_faults (id, tenant_id, kind) "
                        "VALUES (:id, :t, 'CREATE_ORDER_TIMEOUT')"
                    ),
                    {"id": uuid.uuid4(), "t": tenant_id},
                )

    def test_kernel_receives_webhooks_and_worker_applies_them(
        self,
        kernel_engine: Engine,
        worker_engine: Engine,
        app_engine: Engine,
        tenant: tuple[uuid.UUID, uuid.UUID],
    ) -> None:
        """ADR D7: the receiver runs as the kernel; the worker stamps the apply outcome;
        the app role can read the inbox for the Inspector but never write it."""
        tenant_id, _ = tenant
        inbox_id = uuid.uuid4()
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, provider_event_id, "
                    "event_type, body_digest, raw_body, headers_redacted, signature_verified, "
                    "order_id) VALUES (:id, :t, :k, 'evt_1', 'payment.captured', 'd', :body, "
                    "CAST('{\"x-razorpay-event-id\": \"evt_1\"}' AS jsonb), true, 'order_1')"
                ),
                {"id": inbox_id, "t": tenant_id, "k": "evt:evt_1", "body": b'{"event":"x"}'},
            )
        with worker_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            updated = conn.execute(
                text(
                    "UPDATE webhook_inbox SET applied_at = now(), apply_status = 'APPLIED', "
                    "state_before = 'SUBMITTED', state_after = 'CAPTURED', changed = true "
                    "WHERE id = :id"
                ),
                {"id": inbox_id},
            ).rowcount
        assert updated == 1
        with app_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            row = conn.execute(
                text("SELECT apply_status, raw_body FROM webhook_inbox WHERE id = :id"),
                {"id": inbox_id},
            ).one()
        assert row.apply_status == "APPLIED"
        assert bytes(row.raw_body) == b'{"event":"x"}'
        with pytest.raises(ProgrammingError, match="permission denied"):
            with app_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
                conn.execute(
                    text("UPDATE webhook_inbox SET duplicate_count = 9 WHERE id = :id"),
                    {"id": inbox_id},
                )

    def test_app_role_writes_baskets_and_checkout_heads(
        self, app_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, _ = tenant
        with app_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            _seed_row(conn, "checkouts", tenant_id)
            conn.execute(text("UPDATE checkouts SET status = 'APPROVED', updated_at = now()"))
            assert conn.execute(text("SELECT status FROM checkouts")).scalar() == "APPROVED"

    @pytest.mark.parametrize("table", SERVICE_TABLES)
    def test_no_application_role_may_delete(self, kernel_engine: Engine, table: str) -> None:
        with kernel_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT r, has_table_privilege(r, :t, 'DELETE') AS can_delete "
                    "FROM unnest(CAST(:roles AS text[])) AS r"
                ),
                {"t": table, "roles": [roles.APP, roles.KERNEL, roles.WORKER]},
            ).all()
        assert all(not row.can_delete for row in rows), rows

    @pytest.mark.parametrize("table", SERVICE_TABLES)
    def test_declared_writers_match_the_catalogue(self, kernel_engine: Engine, table: str) -> None:
        """app and worker hold exactly the privileges roles.py declares; the kernel holds
        at least them (the local bootstrap widens the kernel to every table)."""
        declared = (
            {roles.KERNEL: {"INSERT", "UPDATE"}}
            if table in FINANCIAL_TABLES
            else {role: set(p) for role, p in WRITE_GRANTS[table].items()}
        )
        with kernel_engine.connect() as conn:
            for role in (roles.APP, roles.WORKER, roles.KERNEL):
                held = {
                    p
                    for p in ("INSERT", "UPDATE")
                    if conn.execute(
                        text("SELECT has_table_privilege(:r, :t, :p)"),
                        {"r": role, "t": table, "p": p},
                    ).scalar()
                }
                can_read = conn.execute(
                    text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                    {"r": role, "t": table},
                ).scalar()
                assert can_read, (role, table)
                expected = declared.get(role, set())
                if role == roles.KERNEL:
                    assert expected <= held, (role, table, held)
                else:
                    assert held == expected, (role, table, held)


# ----------------------------------------------------- corrections to existing tables


class TestApprovalUniqueness:
    _INSERT = text(
        "INSERT INTO approvals (id, tenant_id, checkout_id, checkout_version, content_hash, "
        "amount_minor, currency, action, status, expires_at) VALUES "
        "(:id, :t, :c, 1, 'h', 100, 'INR', 'PAY', :s, now() + interval '10 minutes')"
    )

    def test_second_recorded_approval_per_version_is_refused(
        self, kernel_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, _ = tenant
        checkout_id = uuid.uuid4()
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            conn.execute(
                self._INSERT,
                {"id": uuid.uuid4(), "t": tenant_id, "c": checkout_id, "s": "RECORDED"},
            )
            with pytest.raises(IntegrityError, match="uq_approvals_one_recorded_per_version"):
                conn.execute(
                    self._INSERT,
                    {"id": uuid.uuid4(), "t": tenant_id, "c": checkout_id, "s": "RECORDED"},
                )

    def test_reject_then_re_approve_on_one_version_is_allowed(
        self, kernel_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Two CONSUMED (or INVALIDATED) rows per version are fine; the old full unique
        on (…, status) made a buyer's second approval after a rejection collide."""
        tenant_id, _ = tenant
        checkout_id = uuid.uuid4()
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            for status in ("CONSUMED", "CONSUMED", "INVALIDATED", "INVALIDATED", "RECORDED"):
                conn.execute(
                    self._INSERT,
                    {"id": uuid.uuid4(), "t": tenant_id, "c": checkout_id, "s": status},
                )
            assert conn.execute(text("SELECT count(*) FROM approvals")).scalar() == 5
            assert not conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'one_live_approval_per_version'")
            ).scalar()


class TestProviderOrderUniqueness:
    def test_one_razorpay_order_per_attempt_within_a_tenant(
        self, kernel_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, _ = tenant
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            # FAILED is terminal, so only the provider-order index can object here.
            _insert_attempt(conn, tenant_id, status="FAILED", provider_order_id="order_ABC")
            with pytest.raises(IntegrityError, match="uq_payment_attempts_provider_order"):
                _insert_attempt(conn, tenant_id, status="FAILED", provider_order_id="order_ABC")

    def test_null_order_ids_do_not_collide(
        self, kernel_engine: Engine, tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, _ = tenant
        with kernel_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            _insert_attempt(conn, tenant_id, status="FAILED")
            _insert_attempt(conn, tenant_id, status="FAILED")

    def test_uniqueness_is_per_tenant(
        self, kernel_engine: Engine, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Razorpay ids are unique per Razorpay account; two tenants sharing one demo
        account may legitimately see the same id, and the index must not fuse them."""
        a, b = two_tenants
        ids: list[uuid.UUID] = []
        for tenant_id in (a, b):
            with kernel_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
                ids.append(
                    _insert_attempt(
                        conn, tenant_id, status="FAILED", provider_order_id="order_SHARED"
                    )
                )
        admin = create_engine(
            os.environ.get(
                "DATABASE_URL_TEST_ADMIN",
                "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
            ),
            future=True,
            pool_size=1,
            max_overflow=0,
        )
        with admin.begin() as conn:
            for tenant_id in (a, b):
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
                conn.execute(
                    text("DELETE FROM payment_attempts WHERE tenant_id = :t"), {"t": tenant_id}
                )
        admin.dispose()

    def test_lookup_indexes_exist(self, kernel_engine: Engine) -> None:
        with kernel_engine.connect() as conn:
            names = set(
                conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'payment_attempts'")
                ).scalars()
            )
        assert {"uq_payment_attempts_provider_order", "ix_payment_attempts_provider_payment"} <= (
            names
        )


# ------------------------------------------------------------------- statement generator


class TestStatementGenerator:
    """rls.py output is executed by the *first* migration on a fresh database, so it must
    tolerate tables that later revisions create."""

    def test_statements_for_a_missing_table_are_a_no_op(self, admin_engine: Engine) -> None:
        with admin_engine.begin() as conn:
            for statement in table_statements("table_that_does_not_exist"):
                conn.execute(text(statement))

    def test_generated_statements_secure_a_table_end_to_end(self, admin_engine: Engine) -> None:
        """Applied to a throwaway table and rolled back, so the test takes no lock on any
        table another session (or another engineer's suite) may be using. Running the
        generator against the real tables would queue ACCESS EXCLUSIVE requests behind
        every open transaction on them."""
        probe = f"u1_probe_{uuid.uuid4().hex[:12]}"
        with admin_engine.connect() as conn:
            with conn.begin() as tx:
                conn.execute(
                    text(f"CREATE TABLE {probe} (id uuid PRIMARY KEY, tenant_id uuid NOT NULL)")
                )
                for statement in [*rls_statements(probe), *grant_statements(probe)]:
                    conn.execute(text(statement))
                flags = conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :n"
                    ),
                    {"n": probe},
                ).one()
                policy = conn.execute(
                    text("SELECT qual FROM pg_policies WHERE tablename = :n"), {"n": probe}
                ).scalar()
                privileges = {
                    role: {
                        p
                        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
                        if conn.execute(
                            text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": role, "t": probe, "p": p},
                        ).scalar()
                    }
                    for role in (roles.APP, roles.KERNEL, roles.WORKER)
                }
                tx.rollback()
        assert flags.relrowsecurity and flags.relforcerowsecurity
        assert policy and "NULLIF" in policy
        # An undeclared table is read-only for every application role: safe by default.
        assert privileges == {r: {"SELECT"} for r in (roles.APP, roles.KERNEL, roles.WORKER)}

    def test_all_statements_is_the_union_of_roles_rls_and_grants(self) -> None:
        statements = all_statements()
        assert any("CREATE ROLE" in s for s in statements)
        assert sum("CREATE POLICY" in s for s in statements) == len(RLS_TABLES)
        assert all("DELETE" not in s or "REVOKE" in s for s in statements)

    def test_every_rls_table_gets_the_nullif_predicate(self) -> None:
        for table in RLS_TABLES:
            policy = [s for s in table_statements(table) if "CREATE POLICY" in s]
            assert len(policy) == 1, table
            assert TENANT_PREDICATE in policy[0]

    def test_financial_service_tables_are_kernel_only_in_generated_sql(self) -> None:
        for table in ("orders", "provider_requests", "reconciliation_runs"):
            joined = "\n".join(table_statements(table))
            assert f"GRANT INSERT, UPDATE ON {table} TO {roles.KERNEL}" in joined
            assert f"REVOKE INSERT, UPDATE ON {table} FROM {roles.APP}, {roles.WORKER}" in joined

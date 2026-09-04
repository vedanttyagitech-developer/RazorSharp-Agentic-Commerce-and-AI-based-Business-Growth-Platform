"""The privilege boundary, proven by asking PostgreSQL to refuse.

ADR 0003 D1 says the physical boundary between "the API proposes" and "the kernel
authorizes" is the database grant set. Until this module existed that was configuration
and convention: :mod:`platform_db.roles` declared who may write what, :mod:`platform_db.rls`
turned the declaration into GRANT statements, and nothing anywhere attempted a forbidden
write to see whether the server actually said no. A reviewer asking "what stops the API
writing a payment row directly?" deserves a refusal from PostgreSQL, not a paragraph.

So every assertion here is made by executing the statement as the real restricted login
role and reading the server's answer. The three questions asked are:

1. For every table in :data:`platform_db.roles.FINANCIAL_TABLES`, can the app role or the
   worker role INSERT or UPDATE? (They must not. The kernel must.)
2. Is ``audit_events`` genuinely append-only -- INSERT accepted from the kernel, UPDATE and
   DELETE refused for every application role -- and does any application role hold DELETE
   on anything at all? (An evidence chain anyone can edit proves nothing.)
3. Does tenant isolation hold on the service tables added by ADR 0003 (``baskets``,
   ``checkouts``, ``orders``, ``webhook_inbox``, ``provider_requests``,
   ``reconciliation_runs``) as it does on the kernel's own, including when no tenant is
   bound at all?

Two design notes.

*Nothing is committed by the privilege tests.* :func:`_attempt` runs each candidate
statement inside a transaction it always rolls back, so a statement that turns out to be
permitted leaves no row behind. That matters because the application roles have no DELETE
anywhere -- by design -- so a permitted-but-unwanted write could not be cleaned up by the
role that made it.

*A permitted statement need not succeed.* The kernel-may-write cases deliberately submit a
minimal column list, so PostgreSQL accepts the privilege and then rejects the row for a
missing NOT NULL column. That is the proof: the same statement that the app role never
gets to attempt (SQLSTATE 42501, "permission denied for table") reaches constraint
checking under the kernel. The assertion is therefore "not refused for privilege", which
survives future column changes, and is paired with a direct ``has_table_privilege`` read.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Final, NamedTuple

import pytest
from platform_db import roles
from platform_db.roles import APP, FINANCIAL_TABLES, KERNEL, WORKER
from platform_db.schema import Base
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.db

WORKER_URL: Final = os.environ.get(
    "DATABASE_URL_TEST_WORKER",
    "postgresql+psycopg://commerce_test_worker:testpw@localhost:5432/commerce_test",
)

SET_TENANT: Final = text("SELECT set_config('app.tenant_id', :t, true)")

#: SQLSTATE 42501, insufficient_privilege. PostgreSQL uses it for two different refusals:
#: a missing table grant ("permission denied for table x") and a row-level security
#: WITH CHECK violation ("new row violates row-level security policy for table x"). The
#: tests below distinguish them by message, because only the first is a privilege answer.
INSUFFICIENT_PRIVILEGE: Final = "42501"

#: The application roles. ``commerce_migration`` and ``commerce_analytics`` are not tested
#: here: neither has a login role in the test database, and neither is used by a running
#: process. What matters is that the three roles the API, kernel and worker connect as
#: cannot exceed their declared privileges.
APPLICATION_ROLES: Final[tuple[str, ...]] = (APP, KERNEL, WORKER)

#: The service tables whose tenant isolation this module checks, per ADR 0003. The kernel's
#: own tables are covered by test_tenant_isolation.py; these were added later and the same
#: proof has to be repeated for them, because a migration that creates a table and forgets
#: ``FORCE ROW LEVEL SECURITY`` is silent.
ISOLATED_SERVICE_TABLES: Final[tuple[str, ...]] = (
    "baskets",
    "checkouts",
    "orders",
    "webhook_inbox",
    "provider_requests",
    "reconciliation_runs",
)

#: Children before parents. orders references payment_attempts and the policy receipt;
#: provider_requests and reconciliation_runs reference payment_attempts; checkouts
#: references baskets.
_TEARDOWN_ORDER: Final[tuple[str, ...]] = (
    "provider_requests",
    "reconciliation_runs",
    "orders",
    "policy_at_sale_receipts",
    "payment_attempts",
    "checkouts",
    "baskets",
    "webhook_inbox",
    "merchants",
)


class Refusal(NamedTuple):
    """What PostgreSQL said when it declined a statement."""

    sqlstate: str
    message: str

    @property
    def is_privilege_denial(self) -> bool:
        """True only for a missing table grant, not for an RLS WITH CHECK violation."""
        return self.sqlstate == INSUFFICIENT_PRIVILEGE and "permission denied" in self.message


def _attempt(
    engine: Engine,
    statement: str,
    params: dict[str, object] | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
) -> Refusal | None:
    """Run ``statement`` as ``engine``'s role in a transaction that is always rolled back.

    Returns the refusal, or ``None`` when the server permitted the statement. The rollback
    is unconditional so that a statement which turns out to be allowed commits nothing:
    these tests probe privileges, and a probe must not leave state behind in a database
    whose application roles cannot delete.
    """
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            if tenant_id is not None:
                conn.execute(SET_TENANT, {"t": str(tenant_id)})
            conn.execute(text(statement), params or {})
            outcome: Refusal | None = None
        except DBAPIError as exc:
            outcome = Refusal(str(getattr(exc.orig, "sqlstate", "")), str(exc.orig))
        finally:
            trans.rollback()
    return outcome


def _minimal_insert(table: str) -> str:
    """An INSERT naming only the two columns every tenant-scoped table has.

    Deliberately incomplete. A role without INSERT is refused before the row is built, so
    the column list is irrelevant to the denial; a role with INSERT reaches NOT NULL
    checking and is rejected there, which is exactly the difference under test.
    """
    return f"INSERT INTO {table} (id, tenant_id) VALUES (:id, :t)"  # noqa: S608


def _no_op_update(table: str) -> str:
    """An UPDATE that matches no row, so only the privilege check can object."""
    return f"UPDATE {table} SET tenant_id = tenant_id WHERE false"  # noqa: S608


def _no_op_delete(table: str) -> str:
    return f"DELETE FROM {table} WHERE false"  # noqa: S608


def _engine(url: str, label: str) -> Engine:
    engine = create_engine(url, future=True, pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"{label} role not reachable (run scripts/bootstrap_test_roles.sql): {exc}")
    return engine


@pytest.fixture(scope="session")
def worker_engine() -> Engine:
    """The worker role, created by scripts/bootstrap_test_roles.sql."""
    return _engine(WORKER_URL, "worker")


@pytest.fixture
def lone_tenant(admin_engine: Engine) -> Iterator[uuid.UUID]:
    """One tenant row, so a bound tenant satisfies the RLS WITH CHECK on write probes.

    No merchant and no children: every statement in the privilege tests is rolled back, so
    this fixture only has to make ``tenant_id`` a valid foreign key.
    """
    tenant_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :slug, :name, 'asia-south1')"
            ),
            {"id": tenant_id, "slug": f"p-{tenant_id.hex[:10]}", "name": f"p-{tenant_id.hex[:10]}"},
        )
    yield tenant_id
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})


# --------------------------------------------------------------- the roles themselves


class TestTheRolesUnderTestAreRestricted:
    """If this class fails, every other assertion in the file is worthless.

    A PostgreSQL superuser bypasses row-level security unconditionally, and privilege
    checks besides, so a suite run as one passes while proving nothing: forbidden writes
    would succeed, other tenants' rows would be visible, and the tests would still be
    green because they assert on what the server allows. BYPASSRLS is the narrower version
    of the same hole -- grants still apply, but every tenant_isolation policy is skipped.
    Both flags are therefore asserted before anything else is claimed.
    """

    @pytest.mark.parametrize("role_label", ["kernel", "app", "worker"])
    def test_role_is_nosuperuser_and_nobypassrls(
        self,
        kernel_engine: Engine,
        app_engine: Engine,
        worker_engine: Engine,
        role_label: str,
    ) -> None:
        engine = {"kernel": kernel_engine, "app": app_engine, "worker": worker_engine}[role_label]
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT current_user AS who, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
        assert row.rolsuper is False, f"{row.who} is a superuser; it bypasses RLS and every grant"
        assert row.rolbypassrls is False, f"{row.who} has BYPASSRLS; tenant policies are skipped"

    def test_the_declared_roles_never_hold_bypassrls(self, kernel_engine: Engine) -> None:
        """The group roles named in roles.py, not just the login roles the tests use."""
        with kernel_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = ANY(CAST(:names AS text[]))"
                ),
                {"names": list(roles.ALL_ROLES)},
            ).all()
        assert rows, "none of the roles in roles.ALL_ROLES exist in this database"
        offenders = [r.rolname for r in rows if r.rolsuper or r.rolbypassrls]
        assert offenders == [], f"roles with superuser or BYPASSRLS: {offenders}"


# ----------------------------------------------------- financial tables: who may write


class TestFinancialTablesAreKernelOnly:
    """Specification 21.3 and ADR 0003 D1, one parametrised case per financial table.

    The table list is read from :data:`platform_db.roles.FINANCIAL_TABLES` rather than
    retyped here, so a table added to that tuple is covered the moment it is declared and a
    table quietly removed from it makes this class stop testing it visibly, in a diff.
    """

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_app_role_cannot_insert(
        self, app_engine: Engine, lone_tenant: uuid.UUID, table: str
    ) -> None:
        refusal = _attempt(
            app_engine,
            _minimal_insert(table),
            {"id": uuid.uuid4(), "t": lone_tenant},
            tenant_id=lone_tenant,
        )
        assert refusal is not None, f"the app role was allowed to INSERT into {table}"
        assert refusal.is_privilege_denial, f"{table}: expected a grant refusal, got {refusal}"
        assert f"permission denied for table {table}" in refusal.message

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_worker_role_cannot_insert(
        self, worker_engine: Engine, lone_tenant: uuid.UUID, table: str
    ) -> None:
        refusal = _attempt(
            worker_engine,
            _minimal_insert(table),
            {"id": uuid.uuid4(), "t": lone_tenant},
            tenant_id=lone_tenant,
        )
        assert refusal is not None, f"the worker role was allowed to INSERT into {table}"
        assert f"permission denied for table {table}" in refusal.message

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_app_role_cannot_update(
        self, app_engine: Engine, lone_tenant: uuid.UUID, table: str
    ) -> None:
        refusal = _attempt(app_engine, _no_op_update(table), tenant_id=lone_tenant)
        assert refusal is not None, f"the app role was allowed to UPDATE {table}"
        assert f"permission denied for table {table}" in refusal.message

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_worker_role_cannot_update(
        self, worker_engine: Engine, lone_tenant: uuid.UUID, table: str
    ) -> None:
        refusal = _attempt(worker_engine, _no_op_update(table), tenant_id=lone_tenant)
        assert refusal is not None, f"the worker role was allowed to UPDATE {table}"
        assert f"permission denied for table {table}" in refusal.message

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_kernel_role_may_insert(
        self, kernel_engine: Engine, lone_tenant: uuid.UUID, table: str
    ) -> None:
        """The same statement the app role never got to attempt reaches the row.

        A NOT NULL or foreign key rejection here is a pass: it means the grant check
        succeeded and PostgreSQL went on to validate the (deliberately incomplete) row.
        Only a privilege refusal fails this test.
        """
        refusal = _attempt(
            kernel_engine,
            _minimal_insert(table),
            {"id": uuid.uuid4(), "t": lone_tenant},
            tenant_id=lone_tenant,
        )
        assert refusal is None or not refusal.is_privilege_denial, (
            f"the kernel role cannot INSERT into {table}: {refusal}"
        )

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_kernel_role_may_update(
        self, kernel_engine: Engine, lone_tenant: uuid.UUID, table: str
    ) -> None:
        refusal = _attempt(kernel_engine, _no_op_update(table), tenant_id=lone_tenant)
        assert refusal is None, f"the kernel role cannot UPDATE {table}: {refusal}"

    @pytest.mark.parametrize("table", FINANCIAL_TABLES)
    def test_the_grant_catalogue_agrees(self, kernel_engine: Engine, table: str) -> None:
        """The executed refusals above and pg_catalog must tell the same story.

        Executing the statement proves the live behaviour; reading has_table_privilege
        proves it is a grant and not, say, a trigger. If these two ever disagree the
        privilege model has been changed somewhere this module does not look.
        """
        with kernel_engine.connect() as conn:
            held = {
                (role, privilege): conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": table, "p": privilege},
                ).scalar()
                for role in APPLICATION_ROLES
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
        for role in APPLICATION_ROLES:
            assert held[(role, "SELECT")], f"{role} cannot even read {table}"
            assert not held[(role, "DELETE")], f"{role} holds DELETE on {table}"
        for role in (APP, WORKER):
            assert not held[(role, "INSERT")], f"{role} holds INSERT on financial table {table}"
            assert not held[(role, "UPDATE")], f"{role} holds UPDATE on financial table {table}"
        assert held[(KERNEL, "INSERT")] and held[(KERNEL, "UPDATE")], (
            f"the kernel cannot write {table}; the financial path is broken, not merely locked"
        )


# --------------------------------------------------------------- audit_events is append-only


class TestAuditEventsAreAppendOnly:
    """An audit chain that any application role can edit is not evidence of anything.

    ``roles.APPEND_ONLY_TABLES`` says audit_events takes INSERT and nothing else, from any
    role including the kernel. Retention is a partition drop under the migration role, not
    an application DELETE.
    """

    _INSERT: Final = (
        "INSERT INTO audit_events (id, tenant_id, aggregate_type, aggregate_id, seq, "
        "event_type, actor_type, payload, self_hash, correlation_id) VALUES "
        "(:id, :t, 'CHECKOUT', :agg, 1, 'checkout.created', 'SYSTEM', "
        "CAST('{}' AS jsonb), :h, :corr)"
    )

    def test_the_kernel_may_append(self, kernel_engine: Engine, lone_tenant: uuid.UUID) -> None:
        refusal = _attempt(
            kernel_engine,
            self._INSERT,
            {
                "id": uuid.uuid4(),
                "t": lone_tenant,
                "agg": uuid.uuid4(),
                "h": uuid.uuid4().hex + uuid.uuid4().hex,
                "corr": uuid.uuid4(),
            },
            tenant_id=lone_tenant,
        )
        assert refusal is None, f"the kernel cannot write an audit event: {refusal}"

    @pytest.mark.parametrize("role_label", ["kernel", "app", "worker"])
    def test_no_role_may_update_an_audit_event(
        self,
        kernel_engine: Engine,
        app_engine: Engine,
        worker_engine: Engine,
        lone_tenant: uuid.UUID,
        role_label: str,
    ) -> None:
        engine = {"kernel": kernel_engine, "app": app_engine, "worker": worker_engine}[role_label]
        refusal = _attempt(
            engine,
            "UPDATE audit_events SET payload = payload WHERE false",
            tenant_id=lone_tenant,
        )
        assert refusal is not None, f"the {role_label} role may rewrite audit history"
        assert "permission denied for table audit_events" in refusal.message

    @pytest.mark.parametrize("role_label", ["kernel", "app", "worker"])
    def test_no_role_may_delete_an_audit_event(
        self,
        kernel_engine: Engine,
        app_engine: Engine,
        worker_engine: Engine,
        lone_tenant: uuid.UUID,
        role_label: str,
    ) -> None:
        engine = {"kernel": kernel_engine, "app": app_engine, "worker": worker_engine}[role_label]
        refusal = _attempt(engine, _no_op_delete("audit_events"), tenant_id=lone_tenant)
        assert refusal is not None, f"the {role_label} role may erase audit history"
        assert "permission denied for table audit_events" in refusal.message


# ------------------------------------------------------------------ nobody deletes anything


class TestNoApplicationRoleMayDelete:
    """DELETE is absent by construction in rls.grant_statements; this proves it landed."""

    @pytest.mark.parametrize("table", sorted(Base.metadata.tables))
    def test_declared_table_grants_no_delete(self, kernel_engine: Engine, table: str) -> None:
        with kernel_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT r, has_table_privilege(r, :t, 'DELETE') AS can_delete "
                    "FROM unnest(CAST(:rs AS text[])) AS r"
                ),
                {"t": table, "rs": list(APPLICATION_ROLES)},
            ).all()
        offenders = [row.r for row in rows if row.can_delete]
        assert offenders == [], f"{table}: DELETE granted to {offenders}"

    def test_no_live_table_in_the_schema_grants_delete(self, kernel_engine: Engine) -> None:
        """Every table PostgreSQL actually has, including ones the ORM does not model.

        ``alembic_version`` is the reason this test is not simply the parametrised one
        above: it is created by Alembic, never appears in ``Base.metadata``, and a broad
        bootstrap GRANT could leave an application role able to delete migration history.
        """
        with kernel_engine.connect() as conn:
            offenders = conn.execute(
                text(
                    "SELECT c.relname AS table_name, r AS role_name "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
                    "unnest(CAST(:rs AS text[])) AS r "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND has_table_privilege(r, c.oid, 'DELETE') "
                    "ORDER BY 1, 2"
                ),
                {"rs": list(APPLICATION_ROLES)},
            ).all()
        assert offenders == [], f"DELETE granted somewhere: {offenders}"


# ------------------------------------------------------- tenant isolation, service tables


@pytest.fixture
def seeded_pair(admin_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Two tenants; tenant A owns exactly one row in each isolated service table.

    Seeded through the owner connection because the rows have to be committed to be
    visible to another session at all -- an invisibility test against uncommitted data
    would pass for the wrong reason -- and because the application roles have no DELETE and
    so could not tear the rows down again.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    basket_id, checkout_id = uuid.uuid4(), uuid.uuid4()
    attempt_id, receipt_id = uuid.uuid4(), uuid.uuid4()
    with admin_engine.begin() as conn:
        for tenant_id, merchant_id in ((a, merchant_a), (b, merchant_b)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :slug, :name, 'asia-south1')"
                ),
                {
                    "id": tenant_id,
                    "slug": f"i-{tenant_id.hex[:10]}",
                    "name": f"i-{tenant_id.hex[:10]}",
                },
            )
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                    "VALUES (:id, :t, :slug, :name, 'INR')"
                ),
                {
                    "id": merchant_id,
                    "t": tenant_id,
                    "slug": f"m-{tenant_id.hex[:10]}",
                    "name": "m",
                },
            )
        conn.execute(SET_TENANT, {"t": str(a)})
        _seed_tenant_a(conn, a, merchant_a, basket_id, checkout_id, attempt_id, receipt_id)
    yield a, b
    with admin_engine.begin() as conn:
        for tenant_id in (a, b):
            conn.execute(SET_TENANT, {"t": str(tenant_id)})
            for table in _TEARDOWN_ORDER:
                # S608: `table` iterates the module-level tuple above, never request data.
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [a, b]})


def _seed_tenant_a(
    conn: Connection,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    basket_id: uuid.UUID,
    checkout_id: uuid.UUID,
    attempt_id: uuid.UUID,
    receipt_id: uuid.UUID,
) -> None:
    """One row in each of the six isolated service tables, with their real foreign keys."""
    conn.execute(
        text(
            "INSERT INTO baskets (id, tenant_id, merchant_id, buyer_ref, lines, status) "
            "VALUES (:id, :t, :m, 'buyer', CAST('[]' AS jsonb), 'CHECKED_OUT')"
        ),
        {"id": basket_id, "t": tenant_id, "m": merchant_id},
    )
    conn.execute(
        text(
            "INSERT INTO checkouts (id, tenant_id, merchant_id, basket_id, buyer_ref, "
            "current_version, status, correlation_id) VALUES "
            "(:id, :t, :m, :b, 'buyer', 1, 'APPROVAL_REQUIRED', :corr)"
        ),
        {
            "id": checkout_id,
            "t": tenant_id,
            "m": merchant_id,
            "b": basket_id,
            "corr": uuid.uuid4(),
        },
    )
    conn.execute(
        text(
            "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
            "status, amount_minor, currency, receipt) VALUES "
            "(:id, :t, :c, 1, 'CAPTURED', 1000, 'INR', :r)"
        ),
        {"id": attempt_id, "t": tenant_id, "c": checkout_id, "r": f"rcpt-{attempt_id.hex[:12]}"},
    )
    conn.execute(
        text(
            "INSERT INTO policy_at_sale_receipts (id, tenant_id, merchant_id, checkout_id, "
            "checkout_version, content, receipt_hash) VALUES "
            "(:id, :t, :m, :c, 1, CAST('{}' AS jsonb), 'rh')"
        ),
        {"id": receipt_id, "t": tenant_id, "m": merchant_id, "c": checkout_id},
    )
    conn.execute(
        text(
            "INSERT INTO orders (id, tenant_id, merchant_id, checkout_id, checkout_version, "
            "payment_attempt_id, policy_receipt_id, policy_receipt_hash, total_minor, "
            "currency, status, capture_evidence) VALUES (:id, :t, :m, :c, 1, :a, :r, 'rh', "
            "1000, 'INR', 'CONFIRMED', CAST('{\"source\": \"WEBHOOK\"}' AS jsonb))"
        ),
        {
            "id": uuid.uuid4(),
            "t": tenant_id,
            "m": merchant_id,
            "c": checkout_id,
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
            "attempt_number, reason, identifiers_queried, decision, correlation_id) VALUES "
            "(:id, :t, :a, 1, 'client_return', CAST('{}' AS jsonb), 'order_found', :corr)"
        ),
        {"id": uuid.uuid4(), "t": tenant_id, "a": attempt_id, "corr": uuid.uuid4()},
    )
    conn.execute(
        text(
            "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, event_type, body_digest, "
            "raw_body, headers_redacted, signature_verified) VALUES "
            "(:id, :t, :k, 'payment.captured', 'd', :body, CAST('{}' AS jsonb), true)"
        ),
        {"id": uuid.uuid4(), "t": tenant_id, "k": f"evt:{uuid.uuid4().hex}", "body": b"{}"},
    )


class TestCrossTenantInvisibility:
    """Tenant A's service rows must not exist as far as a tenant B session is concerned.

    Run under both the kernel role and the app role: the API reads as ``commerce_app`` and
    writes as ``commerce_kernel`` (ADR 0003 D1), so a policy that protected only one of
    them would leave the other leaking.
    """

    @pytest.mark.parametrize("role_label", ["kernel", "app"])
    @pytest.mark.parametrize("table", ISOLATED_SERVICE_TABLES)
    def test_tenant_b_cannot_see_tenant_a(
        self,
        kernel_engine: Engine,
        app_engine: Engine,
        seeded_pair: tuple[uuid.UUID, uuid.UUID],
        table: str,
        role_label: str,
    ) -> None:
        a, b = seeded_pair
        engine = {"kernel": kernel_engine, "app": app_engine}[role_label]
        count = text(f"SELECT count(*) FROM {table}")  # noqa: S608
        targeted = text(f"SELECT count(*) FROM {table} WHERE tenant_id = :a")  # noqa: S608
        with engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(a)})
            assert conn.execute(count).scalar() == 1, f"{table}: tenant A cannot see its own row"
        with engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(b)})
            assert conn.execute(count).scalar() == 0, f"{table}: tenant B sees tenant A's row"
            # Naming tenant A explicitly must not smuggle the row past the policy.
            assert conn.execute(targeted, {"a": a}).scalar() == 0, f"{table}: predicate leak"

    @pytest.mark.parametrize("table", ISOLATED_SERVICE_TABLES)
    def test_an_unbound_session_sees_nothing(
        self,
        kernel_engine: Engine,
        seeded_pair: tuple[uuid.UUID, uuid.UUID],
        table: str,
    ) -> None:
        """Isolation fails closed: forgetting to bind a tenant returns an empty result.

        The NULLIF in the policy predicate is what makes this an empty result rather than
        an 'invalid input syntax for type uuid' error, and an empty result rather than
        every tenant's rows.
        """
        a, _ = seeded_pair
        with kernel_engine.begin() as conn:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0  # noqa: S608
            assert (
                conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :a"),  # noqa: S608
                    {"a": a},
                ).scalar()
                == 0
            )

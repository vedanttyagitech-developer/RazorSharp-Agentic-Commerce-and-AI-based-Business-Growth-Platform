"""Execution Grant tests, specification 10.3.1.

Every concurrency test here uses two real PostgreSQL sessions on two connections. Nothing
is mocked: a mock cannot tell you whether ``SELECT ... FOR UPDATE`` actually serializes
two workers, and that serialization is the whole guarantee.

The tests connect as ``commerce_test_kernel``, a NOSUPERUSER NOBYPASSRLS login role, for
the same reason the isolation suite does: a superuser bypasses row-level security, so a
cross-tenant test run as one passes while proving nothing.
"""

from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from platform_db import ExecutionGrant, set_tenant
from sqlalchemy import Engine, create_engine, event, insert, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from transaction_kernel import NEEDS_REAPPROVAL, RETRYABLE, CheckoutRef, Operation, RecoveryCode
from transaction_kernel import audit as audit_module
from transaction_kernel.grants import (
    MAX_GRANT_TTL_SECONDS,
    GrantAlreadyConsumedError,
    GrantBinding,
    GrantBindingMismatchError,
    GrantExpiredError,
    GrantLinkConflictError,
    GrantNotFoundError,
    GrantReplacementRefusedError,
    GrantRevokedError,
    GrantStatus,
    GrantTenantMismatchError,
    _locked_grant_query,
    consume_grant,
    expire_stale_grants,
    issue_grant,
    link_command,
    revoke_grant,
    revoke_unused_grants,
)

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Seeding and teardown go through the owner connection: the kernel role deliberately has
# no DELETE on any table, and granting it one to make fixtures convenient would erase the
# guarantee that financial history cannot be removed through an application role.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

AMOUNT = Money(39500, "INR")
TTL = 300
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


# --------------------------------------------------------------------------- fixtures


def _require_db(url: str, *, pool_size: int = 5) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for execution grant tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "grant tests must not run as a superuser"
    assert row.rolbypassrls is False, "grant tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL, pool_size=2)


@dataclass(frozen=True, slots=True)
class Seed:
    """Two tenants, each with one payment attempt, plus the checkout they belong to."""

    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    attempt_a: uuid.UUID
    attempt_b: uuid.UUID
    checkout_a: CheckoutRef
    checkout_b: CheckoutRef


@pytest.fixture
def seed(admin_engine: Engine) -> Iterator[Seed]:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    attempt_a, attempt_b = uuid7(), uuid7()
    checkout_a = CheckoutRef(
        checkout_id=uuid7(), version=3, content_hash=canonical_hash({"total_minor": 39500})
    )
    checkout_b = CheckoutRef(
        checkout_id=uuid7(), version=1, content_hash=canonical_hash({"total_minor": 11})
    )
    pairs = ((tenant_a, attempt_a, checkout_a), (tenant_b, attempt_b, checkout_b))

    with admin_engine.begin() as conn:
        for tenant, attempt, checkout in pairs:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :slug, :name, 'asia-south1')"
                ),
                {"id": tenant, "slug": f"t-{tenant.hex[:8]}", "name": f"t-{tenant.hex[:8]}"},
            )
            # payment_attempts is RLS-protected, so bind the tenant before inserting.
            conn.execute(SET_TENANT, {"t": str(tenant)})
            conn.execute(
                text(
                    "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version,"
                    " status, amount_minor, currency, receipt) "
                    "VALUES (:id, :t, :c, :v, 'CREATED', 39500, 'INR', :r)"
                ),
                {
                    "id": attempt,
                    "t": tenant,
                    "c": checkout.checkout_id,
                    "v": checkout.version,
                    "r": f"rcpt-{attempt.hex[:10]}",
                },
            )

    yield Seed(tenant_a, tenant_b, attempt_a, attempt_b, checkout_a, checkout_b)

    with admin_engine.begin() as conn:
        for tenant, _attempt, _checkout in pairs:
            conn.execute(SET_TENANT, {"t": str(tenant)})
            # Grants reference refunds (ADR D10), refunds reference attempts: this order.
            conn.execute(text("DELETE FROM execution_grants WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM refunds WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM payment_attempts WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [tenant_a, tenant_b]}
        )


@pytest.fixture
def open_session(kernel_engine: Engine) -> Iterator[Callable[[], Session]]:
    """Hand out kernel sessions and guarantee they are closed even mid-transaction."""
    opened: list[Session] = []

    def factory() -> Session:
        session = Session(kernel_engine, expire_on_commit=False)
        opened.append(session)
        return session

    yield factory
    for session in opened:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------- helpers


def _issue(
    session: Session,
    tenant: uuid.UUID,
    checkout: CheckoutRef,
    attempt: uuid.UUID,
    *,
    operation: Operation = Operation.PAYMENT_CREATE_ORDER,
    amount: Money = AMOUNT,
    ttl_seconds: int = TTL,
    refund_id: uuid.UUID | None = None,
) -> ExecutionGrant:
    return issue_grant(
        session,
        tenant=tenant,
        checkout_ref=checkout,
        payment_attempt_id=attempt,
        operation=operation,
        amount=amount,
        kernel_decision_id=uuid7(),
        ttl_seconds=ttl_seconds,
        refund_id=refund_id,
    )


def _binding(
    seed: Seed,
    *,
    operation: Operation = Operation.PAYMENT_CREATE_ORDER,
    amount: Money = AMOUNT,
) -> GrantBinding:
    return GrantBinding(
        tenant_id=seed.tenant_a,
        checkout=seed.checkout_a,
        payment_attempt_id=seed.attempt_a,
        operation=operation,
        amount=amount,
    )


def _issue_committed(engine: Engine, seed: Seed, **kwargs: object) -> uuid.UUID:
    """Issue one grant in its own committed transaction and return its id."""
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, seed.tenant_a)
        grant = _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a, **kwargs)  # type: ignore[arg-type]
        grant_id: uuid.UUID = grant.id
        return grant_id


def _row(engine: Engine, tenant: uuid.UUID, grant_id: uuid.UUID) -> tuple[str, datetime | None]:
    """Read a grant's status and consumed_at on a fresh connection.

    Deliberately not through the session under test: reading back through the same
    identity map could return what the ORM believes rather than what was committed.
    """
    with Session(engine) as session, session.begin():
        set_tenant(session, tenant)
        row = session.execute(
            text("SELECT status, consumed_at FROM execution_grants WHERE id = :id"),
            {"id": grant_id},
        ).one()
        return row.status, row.consumed_at


def _backdate(admin_engine: Engine, tenant: uuid.UUID, grant_id: uuid.UUID, seconds: int) -> None:
    """Move a grant's expiry into the past, by the database clock.

    Simulating elapsed time on the server is the point: the application's clock takes no
    part in the comparison under test.
    """
    with admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant)})
        conn.execute(
            text(
                "UPDATE execution_grants SET expires_at = now() - make_interval(secs => :s) "
                "WHERE id = :id"
            ),
            {"s": seconds, "id": grant_id},
        )


def _where_clause(sql: str) -> str:
    """Everything after the first WHERE.

    Asserting a column name against the whole statement is a trap: ``tenant_id`` is in the
    select list and the SET list of nearly every statement here, so a predicate could be
    deleted without any such assertion noticing.
    """
    match = re.search(r"\bWHERE\b", sql, flags=re.IGNORECASE)
    assert match is not None, f"statement has no WHERE clause: {sql}"
    return sql[match.end() :]


@contextmanager
def _captured_sql(engine: Engine) -> Iterator[list[str]]:
    """Record the SQL actually handed to the driver inside the block.

    Compiling a statement in the test would only prove what the test compiled. This reads
    what the module really sent.
    """
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any, **_kwargs: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _wait_until_blocked(admin_engine: Engine, timeout: float = 10.0) -> bool:
    """True once some backend in this database is waiting on a lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with admin_engine.connect() as conn:
            waiting = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
        if waiting:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------- issuance


class TestIssue:
    def test_binds_every_field_of_the_admitted_operation(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        session = open_session()
        decision_id = uuid7()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            grant = issue_grant(
                session,
                tenant=seed.tenant_a,
                checkout_ref=seed.checkout_a,
                payment_attempt_id=seed.attempt_a,
                operation=Operation.PAYMENT_CREATE_ORDER,
                amount=AMOUNT,
                kernel_decision_id=decision_id,
                ttl_seconds=TTL,
            )

        assert grant.tenant_id == seed.tenant_a
        assert grant.checkout_id == seed.checkout_a.checkout_id
        assert grant.checkout_version == seed.checkout_a.version
        assert grant.content_hash == seed.checkout_a.content_hash
        assert grant.payment_attempt_id == seed.attempt_a
        assert grant.operation == Operation.PAYMENT_CREATE_ORDER.value
        assert grant.amount_minor == AMOUNT.minor
        assert grant.currency == "INR"
        assert grant.kernel_decision_id == decision_id
        assert grant.status == GrantStatus.ISSUED
        assert grant.consumed_at is None
        # Time-ordered ids, per the platform's uuid7 rule.
        assert grant.id.version == 7
        assert _row(kernel_engine, seed.tenant_a, grant.id) == ("ISSUED", None)

    def test_expiry_is_computed_by_the_database_clock(
        self, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """``now()`` is the transaction timestamp, so both stamps share one base.

        The difference is therefore exactly the TTL. An application-computed expires_at
        would differ from the server's issued_at by the pod's clock skew, and a skewed pod
        must not be able to mint a longer-lived grant than the kernel intended.
        """
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            grant = _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a, ttl_seconds=120)
        assert grant.expires_at - grant.issued_at == timedelta(seconds=120)

    def test_a_second_live_grant_for_the_same_attempt_is_refused(
        self, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
            with pytest.raises(GrantReplacementRefusedError) as exc:
                _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION

    def test_the_database_itself_refuses_a_second_issued_row(
        self, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """The partial unique index, not this module's pre-check, is the guarantee.

        The insert below goes straight to the table, bypassing ``issue_grant`` entirely,
        so a green result here means the constraint is really in the database and would
        still hold against a caller that never went through the kernel API.
        """
        session = open_session()
        with pytest.raises(IntegrityError) as exc:
            with session.begin():
                set_tenant(session, seed.tenant_a)
                grant = _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
                session.execute(
                    insert(ExecutionGrant).values(
                        id=uuid7(),
                        tenant_id=seed.tenant_a,
                        checkout_id=grant.checkout_id,
                        checkout_version=grant.checkout_version,
                        content_hash=grant.content_hash,
                        payment_attempt_id=seed.attempt_a,
                        operation=Operation.PAYMENT_CREATE_ORDER.value,
                        amount_minor=1,
                        currency="INR",
                        kernel_decision_id=uuid7(),
                        status=GrantStatus.ISSUED,
                        expires_at=text("now() + interval '5 minutes'"),
                    )
                )
        driver_error: Any = exc.value.orig
        assert driver_error is not None
        assert driver_error.diag.constraint_name == "uq_execution_grants_one_active_per_attempt"

    def test_refuses_a_tenant_other_than_the_bound_one(
        self, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantTenantMismatchError) as exc:
                _issue(session, seed.tenant_b, seed.checkout_b, seed.attempt_b)
        assert exc.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"ttl_seconds": 0}, "ttl_seconds must be within"),
            ({"ttl_seconds": -1}, "ttl_seconds must be within"),
            ({"ttl_seconds": MAX_GRANT_TTL_SECONDS + 1}, "ttl_seconds must be within"),
            ({"amount": Money(0, "INR")}, "positive amount"),
            ({"amount": Money(-1, "INR")}, "positive amount"),
        ],
    )
    def test_refuses_structurally_invalid_arguments(
        self,
        seed: Seed,
        open_session: Callable[[], Session],
        kwargs: dict[str, object],
        match: str,
    ) -> None:
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(ValueError, match=match):
                _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a, **kwargs)  # type: ignore[arg-type]

    def test_refuses_a_grant_with_no_payment_attempt(
        self, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """A NULL attempt would opt out of the partial unique index.

        PostgreSQL treats NULLs as distinct, so a grant without an attempt could be issued
        any number of times over and "one live grant per attempt" would stop being a
        database guarantee.
        """
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(ValueError, match="must name the payment attempt"):
                issue_grant(
                    session,
                    tenant=seed.tenant_a,
                    checkout_ref=seed.checkout_a,
                    payment_attempt_id=None,  # type: ignore[arg-type]
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    amount=AMOUNT,
                    kernel_decision_id=uuid7(),
                    ttl_seconds=TTL,
                )


# -------------------------------------------------------------------------- consumption


class TestConsume:
    def test_consumes_once_and_stamps_the_database_clock(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            grant = consume_grant(session, grant_id, _binding(seed))
            assert grant.status == GrantStatus.CONSUMED
            assert grant.consumed_at is not None

        status, consumed_at = _row(kernel_engine, seed.tenant_a, grant_id)
        assert status == "CONSUMED"
        assert consumed_at is not None

    def test_a_second_consumption_is_refused(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed))

        second = open_session()
        with second.begin():
            set_tenant(second, seed.tenant_a)
            with pytest.raises(GrantAlreadyConsumedError) as exc:
                consume_grant(second, grant_id, _binding(seed))
        # Not DUPLICATE_OPERATION: a consumed grant proves the operation was started, not
        # that money moved, and DUPLICATE_OPERATION may be shown to a buyer as success.
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION
        assert exc.value.code not in {RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION}

    def test_two_concurrent_consumers_exactly_one_wins(
        self, kernel_engine: Engine, seed: Seed
    ) -> None:
        """Two real sessions, two connections, one grant. Exactly one provider call.

        This is the invariant a duplicate worker delivery attacks: both consumers hold the
        same command and both believe they should send it.

        Two independent mechanisms keep the count at one -- the row lock, and the
        ``WHERE status = 'ISSUED'`` guard on the UPDATE -- so this test asserts the outcome
        and passes while either one survives. ``test_the_row_is_locked_before_it_is_
        inspected`` is the one that pins the lock itself.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        binding = _binding(seed)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def worker() -> None:
            session = Session(kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    set_tenant(session, seed.tenant_a)
                    # A bounded wait, so a lock that is never released fails the test
                    # instead of hanging the suite.
                    session.execute(text("SET LOCAL lock_timeout = '15s'"))
                    barrier.wait(timeout=15)
                    consume_grant(session, grant_id, binding)
                result = "consumed"
            except GrantAlreadyConsumedError as exc:
                result = f"refused:{exc.code.value}"
            except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                result = f"unexpected:{exc!r}"
            finally:
                session.close()
            with guard:
                outcomes.append(result)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive(), "a consumer never finished; the row lock was held"

        assert sorted(outcomes) == ["consumed", "refused:CONCURRENT_OPERATION"], outcomes
        status, consumed_at = _row(kernel_engine, seed.tenant_a, grant_id)
        assert status == "CONSUMED"
        assert consumed_at is not None

    def test_a_second_consumer_blocks_on_the_locked_row(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """A consumption in flight excludes a second one until it commits or rolls back.

        The holder consumes without committing. The second consumer is given one second
        and must fail with ``lock_not_available`` (SQLSTATE 55P03) rather than proceed.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        holder = open_session()
        holder.begin()
        set_tenant(holder, seed.tenant_a)
        consume_grant(holder, grant_id, _binding(seed))

        contender = open_session()
        with pytest.raises(OperationalError) as exc:
            with contender.begin():
                set_tenant(contender, seed.tenant_a)
                contender.execute(text("SET LOCAL lock_timeout = '1000ms'"))
                consume_grant(contender, grant_id, _binding(seed))
        driver_error: Any = exc.value.orig
        assert driver_error is not None
        # lock_not_available: the row really was locked by the holder.
        assert driver_error.sqlstate == "55P03"

        # Rolling the holder back releases the lock and restores the grant: the single use
        # is spent at COMMIT, not at the moment the UPDATE ran.
        holder.rollback()
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

    def test_the_row_is_locked_before_it_is_inspected(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """The lock is taken by the read, not merely by the write.

        This is the test that fails if ``FOR UPDATE`` is ever dropped from the query. The
        contender presents a *mismatched* binding, so it is refused long before any UPDATE
        runs: an unlocked read would answer instantly with a binding mismatch, while a
        locking read must wait for the holder and time out instead.

        The distinction matters because ``consume_grant`` decides on what it read. Without
        the lock, the status and expiry it inspected can change between the check and the
        write, and only the guarded UPDATE would stand between a duplicate delivery and a
        second provider call.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        holder = open_session()
        holder.begin()
        set_tenant(holder, seed.tenant_a)
        consume_grant(holder, grant_id, _binding(seed))

        contender = open_session()
        with pytest.raises(OperationalError) as exc:
            with contender.begin():
                set_tenant(contender, seed.tenant_a)
                contender.execute(text("SET LOCAL lock_timeout = '1000ms'"))
                consume_grant(
                    contender,
                    grant_id,
                    _binding(seed, amount=Money(AMOUNT.minor + 1, "INR")),
                )
        driver_error: Any = exc.value.orig
        assert driver_error is not None
        assert driver_error.sqlstate == "55P03"
        holder.rollback()

    @pytest.mark.parametrize(
        ("field", "mutate"),
        [
            (
                "checkout_id",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=CheckoutRef(
                        checkout_id=uuid7(),
                        version=seed.checkout_a.version,
                        content_hash=seed.checkout_a.content_hash,
                    ),
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    amount=AMOUNT,
                ),
            ),
            (
                "checkout_version",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=CheckoutRef(
                        checkout_id=seed.checkout_a.checkout_id,
                        version=seed.checkout_a.version + 1,
                        content_hash=seed.checkout_a.content_hash,
                    ),
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    amount=AMOUNT,
                ),
            ),
            (
                "content_hash",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=CheckoutRef(
                        checkout_id=seed.checkout_a.checkout_id,
                        version=seed.checkout_a.version,
                        content_hash=canonical_hash({"total_minor": 1}),
                    ),
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    amount=AMOUNT,
                ),
            ),
            (
                "payment_attempt_id",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=seed.checkout_a,
                    payment_attempt_id=uuid7(),
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    amount=AMOUNT,
                ),
            ),
            (
                "operation",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=seed.checkout_a,
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.REFUND_EXECUTE,
                    amount=AMOUNT,
                ),
            ),
            (
                "amount_minor",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=seed.checkout_a,
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    # One paisa more. The cheapest possible tampering must still fail.
                    amount=Money(AMOUNT.minor + 1, "INR"),
                ),
            ),
            (
                "currency",
                lambda seed: GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=seed.checkout_a,
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    # Same integer, different currency: 39500 USD is not 39500 INR.
                    amount=Money(AMOUNT.minor, "USD"),
                ),
            ),
        ],
    )
    def test_any_altered_field_refuses_and_leaves_the_grant_unspent(
        self,
        kernel_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
        field: str,
        mutate: Callable[[Seed], GrantBinding],
    ) -> None:
        """An agent compromised after admission cannot re-point the money.

        The grant must survive the attempt: refusing but consuming would let one tampered
        command destroy a legitimate one.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantBindingMismatchError) as exc:
                consume_grant(session, grant_id, mutate(seed))

        assert [delta.field_path for delta in exc.value.deltas] == [field]
        assert exc.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

    def test_reports_every_altered_field_not_only_the_first(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        tampered = GrantBinding(
            tenant_id=seed.tenant_a,
            checkout=CheckoutRef(
                checkout_id=seed.checkout_a.checkout_id,
                version=seed.checkout_a.version,
                content_hash=canonical_hash({"total_minor": 999}),
            ),
            payment_attempt_id=seed.attempt_a,
            operation=Operation.REFUND_EXECUTE,
            amount=Money(1, "USD"),
        )
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantBindingMismatchError) as exc:
                consume_grant(session, grant_id, tampered)

        assert {delta.field_path for delta in exc.value.deltas} == {
            "content_hash",
            "operation",
            "amount_minor",
            "currency",
        }
        # The evidence records what was admitted beside what was attempted.
        by_field = {delta.field_path: delta for delta in exc.value.deltas}
        assert by_field["amount_minor"].approved == AMOUNT.minor
        assert by_field["amount_minor"].current == 1

    def test_a_consumed_grant_is_reported_as_consumed_even_when_past_due(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        """Order of refusals, and the reason it is not cosmetic.

        A worker consumed the grant, sent the create-order request and never heard back.
        By the time a duplicate delivery arrives the window has also closed, so the grant
        is both CONSUMED and past due. Reporting expiry would return RESERVATION_EXPIRED,
        which is in ``RETRYABLE`` and instructs the caller to re-admit the operation --
        while the first request may still be in flight at the provider. That is how one
        charge becomes two, so the consumed answer has to win.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed))
        _backdate(admin_engine, seed.tenant_a, grant_id, seconds=60)

        second = open_session()
        with second.begin():
            set_tenant(second, seed.tenant_a)
            with pytest.raises(GrantAlreadyConsumedError) as exc:
                consume_grant(second, grant_id, _binding(seed))
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION
        assert exc.value.code not in NEEDS_REAPPROVAL
        assert RecoveryCode.RESERVATION_EXPIRED in RETRYABLE

    def test_a_grant_changed_since_this_session_read_it_is_judged_on_the_committed_row(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """The locking read must overwrite what the session already believes.

        A long-lived session that loaded this grant earlier holds it in its identity map.
        Without ``populate_existing`` the locking SELECT returns that cached instance with
        its *pre-lock* attribute values, so the row is locked and then judged on what it
        looked like before. This test pins the difference: the grant was revoked and
        committed elsewhere, and the answer must be AUTHORITY_REVOKED -- stop -- rather
        than CONCURRENT_OPERATION, which is in ``RETRYABLE`` and invites the caller back.
        """
        grant_id = _issue_committed(kernel_engine, seed)

        reader = open_session()
        reader.begin()
        set_tenant(reader, seed.tenant_a)
        cached = reader.get(ExecutionGrant, grant_id)
        assert cached is not None
        assert cached.status == GrantStatus.ISSUED

        revoker = open_session()
        with revoker.begin():
            set_tenant(revoker, seed.tenant_a)
            revoke_grant(revoker, grant_id)

        with pytest.raises(GrantRevokedError) as exc:
            consume_grant(reader, grant_id, _binding(seed))
        assert exc.value.code is RecoveryCode.AUTHORITY_REVOKED
        assert exc.value.code not in RETRYABLE
        reader.rollback()

    def test_a_grant_of_another_tenant_is_invisible(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_b)
            with pytest.raises(GrantNotFoundError) as exc:
                consume_grant(
                    session,
                    grant_id,
                    GrantBinding(
                        tenant_id=seed.tenant_b,
                        checkout=seed.checkout_b,
                        payment_attempt_id=seed.attempt_b,
                        operation=Operation.PAYMENT_CREATE_ORDER,
                        amount=AMOUNT,
                    ),
                )
        assert exc.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

    def test_a_binding_naming_another_tenant_is_refused(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantTenantMismatchError):
                consume_grant(
                    session,
                    grant_id,
                    GrantBinding(
                        tenant_id=seed.tenant_b,
                        checkout=seed.checkout_a,
                        payment_attempt_id=seed.attempt_a,
                        operation=Operation.PAYMENT_CREATE_ORDER,
                        amount=AMOUNT,
                    ),
                )
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)


# ------------------------------------------------------------------------------ expiry


class TestExpiry:
    def test_a_past_due_grant_cannot_be_consumed_while_still_marked_issued(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        """No sweeper has run, so the status still reads ISSUED.

        The refusal has to come from comparing ``expires_at`` to the server clock at
        consumption time; a status check alone would let every grant outlive its window
        until some background job caught up.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        _backdate(admin_engine, seed.tenant_a, grant_id, seconds=1)
        assert _row(kernel_engine, seed.tenant_a, grant_id)[0] == "ISSUED"

        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantExpiredError) as exc:
                consume_grant(session, grant_id, _binding(seed))
        assert exc.value.code is RecoveryCode.RESERVATION_EXPIRED
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

    def test_a_grant_expires_on_its_own_without_any_row_surgery(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """The whole path end to end: issue with a one second TTL, wait, be refused."""
        grant_id = _issue_committed(kernel_engine, seed, ttl_seconds=1)
        time.sleep(1.2)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantExpiredError):
                consume_grant(session, grant_id, _binding(seed))

    def test_the_expiry_predicate_is_evaluated_by_the_database(self, kernel_engine: Engine) -> None:
        """A regression guard on the rule that matters more than any single test.

        If the comparison were ever rewritten as ``datetime.now(UTC) >= expires_at`` the
        SQL would carry a bound timestamp instead of ``now()``, and a pod with a skewed
        clock would decide expiry for the whole platform.
        """
        # Ids are arbitrary: the statement is compiled, never executed. It is compiled
        # against the dialect the kernel actually connects with, so this is the SQL
        # PostgreSQL would really receive.
        compiled = _locked_grant_query(uuid.uuid4(), uuid.uuid4()).compile(
            dialect=kernel_engine.dialect
        )
        sql = str(compiled)
        assert "now()" in sql
        assert "FOR UPDATE" in sql
        # The tenant predicate rides alongside RLS on purpose: RLS is the enforcement
        # point, but a grant id is a capability and this is what stops a lock being taken
        # on another tenant's row if a migration ever forgets FORCE ROW LEVEL SECURITY.
        # Asserted on the WHERE clause specifically -- tenant_id is in the select list of
        # every one of these queries, so `"tenant_id" in sql` would prove nothing.
        assert "execution_grants.tenant_id" in _where_clause(sql)
        assert not any(isinstance(value, datetime) for value in compiled.params.values())

    def test_both_sweeps_name_the_tenant_in_the_sql_they_send(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """An unqualified mass UPDATE is the worst statement to leave to RLS alone.

        ``test_the_sweep_does_not_reach_another_tenant`` proves RLS holds today, and would
        keep passing if the predicate were dropped. This one reads the SQL actually put on
        the wire, so the second line of defence cannot quietly disappear: without it, one
        tenant activating Safe Mode would revoke every tenant's live grants the day a
        migration forgets FORCE ROW LEVEL SECURITY.
        """
        session = open_session()
        with _captured_sql(kernel_engine) as statements, session.begin():
            set_tenant(session, seed.tenant_a)
            revoke_unused_grants(session)
            expire_stale_grants(session)

        updates = [sql for sql in statements if sql.lstrip().upper().startswith("UPDATE")]
        assert len(updates) == 2, updates
        for sql in updates:
            assert "tenant_id" in _where_clause(sql), sql

    def test_a_grant_marked_expired_cannot_be_consumed(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        _backdate(admin_engine, seed.tenant_a, grant_id, seconds=5)

        sweeper = open_session()
        with sweeper.begin():
            set_tenant(sweeper, seed.tenant_a)
            assert expire_stale_grants(sweeper) == (grant_id,)

        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantExpiredError):
                consume_grant(session, grant_id, _binding(seed))

    def test_the_sweep_leaves_live_grants_alone(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            assert expire_stale_grants(session) == ()
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

    def test_an_unused_expired_grant_may_be_re_issued_after_re_admission(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        """Nothing was sent, so re-admission is safe. The refusal is narrow on purpose."""
        first = _issue_committed(kernel_engine, seed)
        _backdate(admin_engine, seed.tenant_a, first, seconds=5)

        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            expire_stale_grants(session)
            replacement = _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
        assert replacement.id != first
        assert replacement.status == GrantStatus.ISSUED


# ------------------------------------------------------------------- revocation + reuse


class TestRevocation:
    def test_a_revoked_grant_cannot_be_consumed(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            revoke_grant(session, grant_id)

        second = open_session()
        with second.begin():
            set_tenant(second, seed.tenant_a)
            with pytest.raises(GrantRevokedError) as exc:
                consume_grant(second, grant_id, _binding(seed))
        assert exc.value.code is RecoveryCode.AUTHORITY_REVOKED
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("REVOKED", None)

    def test_safe_mode_revokes_unused_grants_and_leaves_consumed_ones(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """Specification 10.3.2: activation invalidates unused delegated grants.

        A consumed grant is evidence that an operation was attempted. Rewriting it to
        REVOKED would claim the platform withdrew something it had in fact already sent.
        """
        consumed_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, consumed_id, _binding(seed))
            unused = _issue(
                session,
                seed.tenant_a,
                seed.checkout_a,
                seed.attempt_a,
                operation=Operation.REFUND_EXECUTE,
            )
            unused_id = unused.id

        sweeper = open_session()
        with sweeper.begin():
            set_tenant(sweeper, seed.tenant_a)
            assert revoke_unused_grants(sweeper) == (unused_id,)

        assert _row(kernel_engine, seed.tenant_a, unused_id) == ("REVOKED", None)
        status, consumed_at = _row(kernel_engine, seed.tenant_a, consumed_id)
        assert status == "CONSUMED"
        assert consumed_at is not None

    def test_safe_mode_can_limit_the_sweep_to_named_operations(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            assert revoke_unused_grants(session, operations=[Operation.RESERVE_DEBIT]) == ()
            assert revoke_unused_grants(session, operations=[Operation.PAYMENT_CREATE_ORDER]) == (
                grant_id,
            )

    def test_the_sweep_does_not_reach_another_tenant(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_b)
            assert revoke_unused_grants(session) == ()
        assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

    def test_revoking_a_consumed_grant_is_refused(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed))
            with pytest.raises(GrantAlreadyConsumedError):
                revoke_grant(session, grant_id)

    def test_revoking_twice_is_idempotent(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            revoke_grant(session, grant_id)
            again = revoke_grant(session, grant_id)
        assert again.status == GrantStatus.REVOKED


class TestNoReplacementAfterConsumption:
    """Specification 10.3.1: a timeout after consumption is reconciled, never re-granted."""

    def test_a_consumed_attempt_never_gets_a_second_grant(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """The timeout case, exactly.

        The worker consumed the grant, sent the create-order request and never heard back.
        The honest outcome is UNKNOWN and reconciliation under the same operation. Issuing
        a fresh grant here is how one buyer gets charged twice.
        """
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed))
            with pytest.raises(GrantReplacementRefusedError) as exc:
                _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION
        assert exc.value.grant_id == grant_id

    def test_a_live_grant_blocks_every_operation_on_the_same_attempt(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """The pre-check has to be exactly as wide as the index behind it.

        ``uq_execution_grants_one_active_per_attempt`` keys on
        ``(tenant_id, payment_attempt_id)`` and does not mention the operation, so a live
        PAYMENT_CREATE_ORDER grant blocks a REFUND_EXECUTE grant on the same attempt too.
        A pre-check scoped to the operation would wave that second issue through and let
        the index refuse it instead -- which raises the same class but aborts the caller's
        whole transaction and blames a concurrent writer that does not exist.

        So this asserts the refusal *and* that the transaction survived it: a clean
        refusal the caller can act on, not a poisoned unit of work.
        """
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            live = _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
            with pytest.raises(GrantReplacementRefusedError) as exc:
                _issue(
                    session,
                    seed.tenant_a,
                    seed.checkout_a,
                    seed.attempt_a,
                    operation=Operation.REFUND_EXECUTE,
                )
            # The pre-check answered, so nothing was sent to the database to be rejected.
            assert exc.value.__cause__ is None
            assert exc.value.grant_id == live.id
            # The transaction is still usable; an IntegrityError would have aborted it.
            assert session.execute(text("SELECT 1")).scalar() == 1
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION

    def test_a_different_operation_on_the_same_attempt_is_still_allowed(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """A refund of a paid attempt is not a replacement for its payment."""
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed))
            refund = _issue(
                session,
                seed.tenant_a,
                seed.checkout_a,
                seed.attempt_a,
                operation=Operation.REFUND_EXECUTE,
            )
        assert refund.operation == Operation.REFUND_EXECUTE.value
        assert refund.status == GrantStatus.ISSUED


# ------------------------------------------------------------------- concurrent issuance


class TestConcurrentIssue:
    def test_two_concurrent_issues_for_one_attempt_leave_exactly_one_grant(
        self, kernel_engine: Engine, seed: Seed
    ) -> None:
        """Two admissions racing on the same payment attempt. One grant, one charge."""
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def worker() -> None:
            session = Session(kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    set_tenant(session, seed.tenant_a)
                    session.execute(text("SET LOCAL lock_timeout = '15s'"))
                    barrier.wait(timeout=15)
                    _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
                result = "issued"
            except GrantReplacementRefusedError as exc:
                result = f"refused:{exc.code.value}"
            except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                result = f"unexpected:{exc!r}"
            finally:
                session.close()
            with guard:
                outcomes.append(result)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive(), "an issuer never finished"

        assert sorted(outcomes) == ["issued", "refused:CONCURRENT_OPERATION"], outcomes
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, seed.tenant_a)
            live = session.execute(
                text(
                    "SELECT count(*) FROM execution_grants "
                    "WHERE payment_attempt_id = :a AND status = 'ISSUED'"
                ),
                {"a": seed.attempt_a},
            ).scalar()
        assert live == 1

    def test_losing_the_index_race_is_reported_as_a_conflict(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        """Forces the path where the pre-check passes and the database refuses.

        The holder inserts an ISSUED row and does not commit, so the contender's pre-check
        sees nothing and its INSERT blocks on the partial unique index. The unique
        violation must surface as a conflict a caller can act on, not as a raw
        IntegrityError from the driver.
        """
        holder = open_session()
        holder.begin()
        set_tenant(holder, seed.tenant_a)
        holder.execute(
            insert(ExecutionGrant).values(
                id=uuid7(),
                tenant_id=seed.tenant_a,
                checkout_id=seed.checkout_a.checkout_id,
                checkout_version=seed.checkout_a.version,
                content_hash=seed.checkout_a.content_hash,
                payment_attempt_id=seed.attempt_a,
                operation=Operation.PAYMENT_CREATE_ORDER.value,
                amount_minor=AMOUNT.minor,
                currency="INR",
                kernel_decision_id=uuid7(),
                status=GrantStatus.ISSUED,
                expires_at=text("now() + interval '5 minutes'"),
            )
        )

        captured: list[BaseException] = []

        def contender() -> None:
            session = Session(kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    set_tenant(session, seed.tenant_a)
                    session.execute(text("SET LOCAL lock_timeout = '20s'"))
                    _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
            except BaseException as exc:  # noqa: BLE001 - reported to the main thread
                captured.append(exc)
            finally:
                session.close()

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert _wait_until_blocked(admin_engine), "the contender never blocked on the index"
        holder.commit()
        thread.join(30)
        assert not thread.is_alive()

        assert len(captured) == 1, captured
        failure = captured[0]
        assert isinstance(failure, GrantReplacementRefusedError)
        assert failure.code is RecoveryCode.CONCURRENT_OPERATION
        # The refusal came from the database index, not from the Python pre-check.
        assert isinstance(failure.__cause__, IntegrityError)


# ------------------------------------------------------------------ per-refund binding


def _seed_refund(admin_engine: Engine, seed: Seed, amount: Money = AMOUNT) -> uuid.UUID:
    """One refunds row on attempt A, seeded as the owner: the grant's FK needs a real row."""
    refund_id = uuid7()
    with admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(seed.tenant_a)})
        conn.execute(
            text(
                "INSERT INTO refunds (id, tenant_id, payment_attempt_id, checkout_id, status, "
                "amount_minor, currency, idem_key, reason_code) VALUES (:id, :t, :a, :c, "
                "'PENDING', :amt, :cur, :key, 'buyer_request')"
            ),
            {
                "id": refund_id,
                "t": seed.tenant_a,
                "a": seed.attempt_a,
                "c": seed.checkout_a.checkout_id,
                "amt": amount.minor,
                "cur": amount.currency,
                "key": f"rfnd_test_{refund_id.hex}",
            },
        )
    return refund_id


class TestRefundBinding:
    """ADR 0003 D10: a REFUND_EXECUTE grant is bound to the refunds row it executes."""

    def test_refund_id_is_bound_and_a_different_refund_does_not_bind(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        refund_a = _seed_refund(admin_engine, seed)
        refund_b = _seed_refund(admin_engine, seed)
        grant_id = _issue_committed(
            kernel_engine, seed, operation=Operation.REFUND_EXECUTE, refund_id=refund_a
        )
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, seed.tenant_a)
            stored = session.get(ExecutionGrant, grant_id)
            assert stored is not None and stored.refund_id == refund_a

        binding = _binding(seed, operation=Operation.REFUND_EXECUTE)
        for wrong in (refund_b, None):
            session = open_session()
            with session.begin():
                set_tenant(session, seed.tenant_a)
                with pytest.raises(GrantBindingMismatchError) as exc:
                    consume_grant(session, grant_id, dataclasses.replace(binding, refund_id=wrong))
            assert [delta.field_path for delta in exc.value.deltas] == ["refund_id"]
            assert _row(kernel_engine, seed.tenant_a, grant_id) == ("ISSUED", None)

        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consumed = consume_grant(
                session, grant_id, dataclasses.replace(binding, refund_id=refund_a)
            )
        assert consumed.status == GrantStatus.CONSUMED

    def test_two_sequential_refund_grants_on_one_attempt(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        seed: Seed,
        open_session: Callable[[], Session],
    ) -> None:
        """The consumed grant of refund A does not block refund B, and still blocks A."""
        refund_a = _seed_refund(admin_engine, seed, Money(100, "INR"))
        refund_b = _seed_refund(admin_engine, seed, Money(200, "INR"))
        first = _issue_committed(
            kernel_engine,
            seed,
            operation=Operation.REFUND_EXECUTE,
            amount=Money(100, "INR"),
            refund_id=refund_a,
        )
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(
                session,
                first,
                GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=seed.checkout_a,
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.REFUND_EXECUTE,
                    amount=Money(100, "INR"),
                    refund_id=refund_a,
                ),
            )
            second = _issue(
                session,
                seed.tenant_a,
                seed.checkout_a,
                seed.attempt_a,
                operation=Operation.REFUND_EXECUTE,
                amount=Money(200, "INR"),
                refund_id=refund_b,
            )
            assert second.status == GrantStatus.ISSUED
            assert second.refund_id == refund_b
            assert second.id != first
        # Refund A itself is still spoken for: no replacement after consumption.
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(
                session,
                second.id,
                GrantBinding(
                    tenant_id=seed.tenant_a,
                    checkout=seed.checkout_a,
                    payment_attempt_id=seed.attempt_a,
                    operation=Operation.REFUND_EXECUTE,
                    amount=Money(200, "INR"),
                    refund_id=refund_b,
                ),
            )
            with pytest.raises(GrantReplacementRefusedError) as exc:
                _issue(
                    session,
                    seed.tenant_a,
                    seed.checkout_a,
                    seed.attempt_a,
                    operation=Operation.REFUND_EXECUTE,
                    amount=Money(100, "INR"),
                    refund_id=refund_a,
                )
        assert exc.value.grant_id == first
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION

    def test_payment_grants_are_unchanged_by_the_refund_rule(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed))
            with pytest.raises(GrantReplacementRefusedError):
                _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a)
            with pytest.raises(ValueError, match="only bound by REFUND_EXECUTE"):
                _issue(session, seed.tenant_a, seed.checkout_a, seed.attempt_a, refund_id=uuid7())

    def test_refund_grants_without_a_refund_id_still_block_each_other(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        """Two NULL refund ids are the same operation, not two different refunds."""
        grant_id = _issue_committed(kernel_engine, seed, operation=Operation.REFUND_EXECUTE)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            consume_grant(session, grant_id, _binding(seed, operation=Operation.REFUND_EXECUTE))
            with pytest.raises(GrantReplacementRefusedError):
                _issue(
                    session,
                    seed.tenant_a,
                    seed.checkout_a,
                    seed.attempt_a,
                    operation=Operation.REFUND_EXECUTE,
                )


# --------------------------------------------------------------------- command linking


class TestLinkCommand:
    def test_links_once_and_audits_on_the_attempt_stream(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        command_id, correlation_id = uuid7(), uuid7()
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            linked = link_command(
                session,
                tenant_id=seed.tenant_a,
                grant_id=grant_id,
                outbox_command_id=command_id,
                correlation_id=correlation_id,
            )
            assert linked.outbox_command_id == command_id
            assert linked.status == GrantStatus.ISSUED
            # Idempotent for the same command.
            again = link_command(
                session,
                tenant_id=seed.tenant_a,
                grant_id=grant_id,
                outbox_command_id=command_id,
                correlation_id=correlation_id,
            )
            assert again.outbox_command_id == command_id

        with Session(kernel_engine) as reader, reader.begin():
            set_tenant(reader, seed.tenant_a)
            stored = reader.get(ExecutionGrant, grant_id)
            assert stored is not None and stored.outbox_command_id == command_id
            events = audit_module.read_stream(
                reader,
                tenant=seed.tenant_a,
                aggregate_type="payment_attempt",
                aggregate_id=seed.attempt_a,
            )
        assert [e.event_type for e in events] == ["grant.linked"]
        assert events[0].payload["outbox_command_id"] == str(command_id)
        assert events[0].payload["grant_id"] == str(grant_id)
        assert events[0].correlation_id == correlation_id

    def test_a_grant_travels_on_exactly_one_command(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        first = uuid7()
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            link_command(
                session,
                tenant_id=seed.tenant_a,
                grant_id=grant_id,
                outbox_command_id=first,
                correlation_id=uuid7(),
            )
        second = open_session()
        with second.begin():
            set_tenant(second, seed.tenant_a)
            with pytest.raises(GrantLinkConflictError) as exc:
                link_command(
                    second,
                    tenant_id=seed.tenant_a,
                    grant_id=grant_id,
                    outbox_command_id=uuid7(),
                    correlation_id=uuid7(),
                )
        assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION
        with Session(kernel_engine) as reader, reader.begin():
            set_tenant(reader, seed.tenant_a)
            stored = reader.get(ExecutionGrant, grant_id)
            assert stored is not None and stored.outbox_command_id == first

    def test_refuses_another_tenant_and_an_unknown_grant(
        self, kernel_engine: Engine, seed: Seed, open_session: Callable[[], Session]
    ) -> None:
        grant_id = _issue_committed(kernel_engine, seed)
        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            with pytest.raises(GrantTenantMismatchError):
                link_command(
                    session,
                    tenant_id=seed.tenant_b,
                    grant_id=grant_id,
                    outbox_command_id=uuid7(),
                    correlation_id=uuid7(),
                )
            with pytest.raises(GrantNotFoundError):
                link_command(
                    session,
                    tenant_id=seed.tenant_a,
                    grant_id=uuid7(),
                    outbox_command_id=uuid7(),
                    correlation_id=uuid7(),
                )
        other = open_session()
        with other.begin():
            set_tenant(other, seed.tenant_b)
            with pytest.raises(GrantNotFoundError):
                link_command(
                    other,
                    tenant_id=seed.tenant_b,
                    grant_id=grant_id,
                    outbox_command_id=uuid7(),
                    correlation_id=uuid7(),
                )

"""Delegated authority: revocation epoch, capacity and expiry, specification 10.2 and 21.

These tests run against a real PostgreSQL as ``commerce_test_kernel``, a NOSUPERUSER
NOBYPASSRLS login role. That detail is load-bearing twice over: a superuser bypasses
row-level security unconditionally, so the "another tenant's authority is invisible"
cases would pass while proving nothing; and the concurrency cases need genuine row locks
across separate backends, which no in-memory fake reproduces.

The headline cases are in :class:`TestConcurrency`. They assert something an
implementation without ``SELECT ... FOR UPDATE`` cannot satisfy: that a second session
physically *blocks* on the authority row while the first holds it, and that a revocation
racing an admission never lets both win.
"""

from __future__ import annotations

import ast
import inspect
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from commerce_domain import Money, RecoveryCode, uuid7
from platform_db import set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel import authority as authority_module
from transaction_kernel.authority import (
    AuthorityDecision,
    AuthorityError,
    AuthorityKind,
    AuthorityReason,
    AuthorityStatus,
    RevocationOutcome,
    UnknownAuthorityError,
    admit_debit,
    check_authority,
    grant_authority,
    lock_authority,
    revoke,
)

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

INR = "INR"
BUYER = "buyer-7f3a"


# --------------------------------------------------------------------------- fixtures


def _require_db(url: str, *, pool_size: int) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for authority tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    """Connections as the kernel role. The pool is wide enough for the racing cases."""
    engine = _require_db(KERNEL_URL, pool_size=8)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "authority tests must not run as a superuser"
    assert row.rolbypassrls is False, "authority tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    """Owner connection. Seeds fixtures and cleans up; the kernel role has no DELETE."""
    return _require_db(ADMIN_URL, pool_size=3)


@dataclass(frozen=True)
class Env:
    """One test's tenants, merchants and engines."""

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    other_tenant_id: uuid.UUID
    other_merchant_id: uuid.UUID
    admin: Engine
    kernel: Engine

    def session(self) -> Session:
        return sessionmaker(bind=self.kernel, expire_on_commit=False, future=True)()


_INSERT_TENANT = text(
    "INSERT INTO tenants (id, slug, name, home_region) VALUES (:id, :slug, :name, 'asia-south1')"
)
_INSERT_MERCHANT = text(
    "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
    "VALUES (:id, :t, :slug, :name, 'INR')"
)
_BIND_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

_SEED_AUTHORITY = text(
    """
    INSERT INTO delegated_authorities
        (id, tenant_id, merchant_id, buyer_ref, kind, status, revocation_epoch,
         currency, max_amount_minor, consumed_amount_minor, expires_at)
    VALUES
        (:id, :tenant_id, :merchant_id, :buyer_ref, :kind, :status, :epoch,
         :currency, :max_amount_minor, :consumed,
         now() + CAST(:expires_in_seconds AS double precision) * interval '1 second')
    """
)

_READ_AUTHORITY = text(
    "SELECT revocation_epoch, consumed_amount_minor, max_amount_minor, status "
    "FROM delegated_authorities WHERE id = :id"
)

_INSERT_APPROVAL = text(
    "INSERT INTO approvals (id, tenant_id, checkout_id, checkout_version, content_hash, "
    "amount_minor, currency, action, authority_id, authority_epoch, status, expires_at) "
    "VALUES (:id, :t, :c, 1, :h, :amount, 'INR', 'PAY', :a, :e, 'RECORDED', "
    "now() + interval '1 hour')"
)


@pytest.fixture
def env(admin_engine: Engine, kernel_engine: Engine) -> Iterator[Env]:
    """Two tenants, one merchant each, removed afterwards.

    The second tenant exists so cross-tenant invisibility is tested against a real row
    rather than against a random UUID that is absent for everyone.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    with admin_engine.begin() as conn:
        for tid, mid in ((a, merchant_a), (b, merchant_b)):
            slug = f"t-{tid.hex[:8]}"
            conn.execute(_INSERT_TENANT, {"id": tid, "slug": slug, "name": slug})
            # The merchant insert is itself RLS-checked, so bind the tenant first.
            conn.execute(_BIND_TENANT, {"t": str(tid)})
            conn.execute(
                _INSERT_MERCHANT,
                {"id": mid, "t": tid, "slug": f"m-{tid.hex[:8]}", "name": f"m-{tid.hex[:8]}"},
            )

    yield Env(
        tenant_id=a,
        merchant_id=merchant_a,
        other_tenant_id=b,
        other_merchant_id=merchant_b,
        admin=admin_engine,
        kernel=kernel_engine,
    )

    with admin_engine.begin() as conn:
        for tid in (a, b):
            conn.execute(_BIND_TENANT, {"t": str(tid)})
            # Approvals reference authorities; authorities reference merchants.
            conn.execute(text("DELETE FROM approvals WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM delegated_authorities WHERE tenant_id = :t"), {"t": tid})
            conn.execute(text("DELETE FROM merchants WHERE tenant_id = :t"), {"t": tid})
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [a, b]})


# --------------------------------------------------------------------------- helpers


def seed(
    env: Env,
    *,
    tenant_id: uuid.UUID | None = None,
    merchant_id: uuid.UUID | None = None,
    buyer_ref: str = BUYER,
    kind: AuthorityKind = AuthorityKind.RESERVE,
    status: AuthorityStatus = AuthorityStatus.ACTIVE,
    epoch: int = 0,
    currency: str = INR,
    max_amount_minor: int = 100_000,
    consumed: int = 0,
    expires_in_seconds: float = 3600.0,
) -> uuid.UUID:
    """Insert one authority through the owner connection, bypassing the module under test.

    Raw-SQL seeding is deliberate: a test that built its fixtures with ``grant_authority``
    could not construct an already-expired or already-revoked row, which is exactly what
    the refusal cases need.
    """
    authority_id = uuid7()
    with env.admin.begin() as conn:
        conn.execute(_BIND_TENANT, {"t": str(tenant_id or env.tenant_id)})
        conn.execute(
            _SEED_AUTHORITY,
            {
                "id": authority_id,
                "tenant_id": tenant_id or env.tenant_id,
                "merchant_id": merchant_id or env.merchant_id,
                "buyer_ref": buyer_ref,
                "kind": str(kind),
                "status": str(status),
                "epoch": epoch,
                "currency": currency,
                "max_amount_minor": max_amount_minor,
                "consumed": consumed,
                "expires_in_seconds": expires_in_seconds,
            },
        )
    return authority_id


def stored(env: Env, authority_id: uuid.UUID) -> tuple[int, int, int, str]:
    """(epoch, consumed, max, status) as durably committed, read on a separate connection."""
    with env.admin.begin() as conn:
        conn.execute(_BIND_TENANT, {"t": str(env.tenant_id)})
        row = conn.execute(_READ_AUTHORITY, {"id": authority_id}).one()
    return (
        int(row.revocation_epoch),
        int(row.consumed_amount_minor),
        int(row.max_amount_minor),
        str(row.status),
    )


def backend_pid(session: Session) -> int:
    """The PostgreSQL backend serving this session, so blocking can be attributed to it."""
    return int(session.execute(text("SELECT pg_backend_pid()")).scalar_one())


def wait_until_blocked_by(env: Env, holder_pid: int, *, timeout: float = 10.0) -> bool:
    """True once some backend is waiting on a lock held by ``holder_pid`` specifically.

    This is the observable that separates a real ``FOR UPDATE`` from a plain read: a
    reader never blocks. The concurrency cases assert it before releasing the holder, so
    an implementation that dropped the lock fails here instead of passing on timing luck.

    Naming the holding backend is load-bearing. Counting *any* blocked backend in the
    database would also be satisfied by an unrelated session blocking on an unrelated
    row -- another kernel test module racing on the same database, a stray psql -- and
    the assertion would then pass while proving nothing about this authority row.
    """
    deadline = time.monotonic() + timeout
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() "
        "AND CAST(:holder AS integer) = ANY(pg_blocking_pids(pid))"
    )
    while time.monotonic() < deadline:
        with env.admin.connect() as conn:
            if int(conn.execute(query, {"holder": holder_pid}).scalar_one()) > 0:
                return True
        time.sleep(0.02)
    return False


def tenant_txn_thread(
    env: Env,
    work: Callable[[Session], object],
    results: dict[str, object],
    key: str,
    *,
    ready: threading.Barrier | None = None,
    delay: float = 0.0,
) -> Callable[[], None]:
    """Build a thread body that runs ``work`` in its own tenant-bound transaction.

    The barrier is waited on *inside* the transaction and after the tenant is bound, so
    connection checkout and context setup are outside the race and a millisecond-scale
    ``delay`` reliably decides who reaches the row lock first.
    """

    def run() -> None:
        session = env.session()
        try:
            with session.begin():
                set_tenant(session, env.tenant_id)
                if ready is not None:
                    ready.wait()
                if delay:
                    time.sleep(delay)
                results[key] = work(session)
        except Exception as exc:
            results[key] = exc
        finally:
            session.close()

    return run


def unwrap(results: dict[str, object], key: str) -> object:
    """Return a thread's result, re-raising in the test whatever it caught."""
    value = results.get(key, KeyError(f"thread never recorded {key!r}"))
    if isinstance(value, BaseException):
        raise value
    return value


def decision_of(results: dict[str, object], key: str) -> AuthorityDecision:
    value = unwrap(results, key)
    assert isinstance(value, AuthorityDecision), f"{key} returned {value!r}"
    return value


# --------------------------------------------------------------------------- locking


class TestLocking:
    def test_snapshot_reflects_the_stored_row_and_the_database_clock(self, env: Env) -> None:
        authority_id = seed(env, max_amount_minor=250_000, consumed=50_000)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            snap = lock_authority(session, authority_id)
        session.close()

        assert snap is not None
        assert snap.revocation_epoch == 0
        assert snap.max_amount == Money(250_000, INR)
        assert snap.consumed_amount == Money(50_000, INR)
        assert snap.remaining == Money(200_000, INR)
        assert snap.expired is False
        # The instant the decision was made against came from PostgreSQL, and it is
        # strictly before the expiry the row carries.
        assert snap.observed_at < snap.expires_at

    def test_absent_authority_is_none(self, env: Env) -> None:
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            assert lock_authority(session, uuid7()) is None
        session.close()

    def test_another_tenants_authority_is_invisible(self, env: Env) -> None:
        foreign = seed(env, tenant_id=env.other_tenant_id, merchant_id=env.other_merchant_id)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            assert lock_authority(session, foreign) is None
            decision = check_authority(
                session,
                foreign,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.other_merchant_id,
            )
        session.close()
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.reason is AuthorityReason.AUTHORITY_NOT_FOUND

    def test_unbound_tenant_fails_closed(self, env: Env) -> None:
        """Forgetting the tenant must deny, never admit: RLS filters the row away."""
        authority_id = seed(env)
        session = env.session()
        with session.begin():
            decision = check_authority(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert decision.allowed is False
        assert decision.reason is AuthorityReason.AUTHORITY_NOT_FOUND

    def test_operations_outside_a_transaction_are_refused(self, env: Env) -> None:
        authority_id = seed(env)
        session = env.session()
        try:
            with pytest.raises(AuthorityError, match="active transaction"):
                lock_authority(session, authority_id)
            with pytest.raises(AuthorityError, match="active transaction"):
                revoke(session, authority_id)
            with pytest.raises(AuthorityError, match="active transaction"):
                check_authority(
                    session,
                    authority_id,
                    expected_epoch=0,
                    amount=Money(100, INR),
                    merchant_id=env.merchant_id,
                )
        finally:
            session.rollback()
            session.close()


# ------------------------------------------------------------------- invariants 1 and 2


class TestRevocationEpoch:
    def test_revocation_raises_the_epoch_by_exactly_one_and_marks_the_row(self, env: Env) -> None:
        authority_id = seed(env)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            outcome = revoke(session, authority_id)
        session.close()

        assert outcome.previous_epoch == 0
        assert outcome.epoch == 1
        assert outcome.was_already_revoked is False
        epoch, _consumed, _maximum, status = stored(env, authority_id)
        assert epoch == 1
        assert status == AuthorityStatus.REVOKED

    def test_repeated_revocations_are_strictly_monotonic(self, env: Env) -> None:
        """The epoch only ever climbs. Nothing in the module can hold or lower it."""
        authority_id = seed(env)
        seen: list[int] = []
        for _ in range(4):
            session = env.session()
            with session.begin():
                set_tenant(session, env.tenant_id)
                seen.append(revoke(session, authority_id).epoch)
            session.close()

        assert seen == [1, 2, 3, 4]
        assert all(later > earlier for earlier, later in zip(seen, seen[1:], strict=False))
        assert stored(env, authority_id)[0] == 4

    def test_second_revocation_reports_that_it_was_already_revoked(self, env: Env) -> None:
        authority_id = seed(env)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            first = revoke(session, authority_id)
            second = revoke(session, authority_id)
        session.close()
        assert first.was_already_revoked is False
        assert second.was_already_revoked is True
        assert second.epoch == first.epoch + 1

    def test_no_public_function_accepts_an_epoch_to_store(self) -> None:
        """Monotonicity depends on the epoch being unwritable from outside.

        ``revoke`` computes ``revocation_epoch + 1`` in SQL. If a parameter ever appeared
        that let a caller name the new epoch, a replayed or forged request could put the
        epoch back and revive every approval the revocation had killed.
        """
        for func in (revoke, check_authority, admit_debit, grant_authority, lock_authority):
            params = set(inspect.signature(func).parameters)
            forbidden = {"epoch", "new_epoch", "revocation_epoch", "set_epoch"}
            assert not (params & forbidden), f"{func.__name__} exposes {params & forbidden}"

        # `expected_epoch` is read-only input: it is compared, never stored.
        source = inspect.getsource(authority_module)
        assert "SET revocation_epoch = revocation_epoch + 1" in source
        assert "SET revocation_epoch = :" not in source

    def test_revoking_an_invisible_authority_raises_rather_than_reporting_success(
        self, env: Env
    ) -> None:
        """A revocation that touched no row must never look like a completed revocation."""
        foreign = seed(env, tenant_id=env.other_tenant_id, merchant_id=env.other_merchant_id)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            with pytest.raises(UnknownAuthorityError, match="not visible"):
                revoke(session, foreign)
            with pytest.raises(UnknownAuthorityError):
                revoke(session, uuid7())
        session.close()


# ------------------------------------------------------------------------- invariant 4


class TestStaleEpoch:
    def test_approval_recorded_under_epoch_n_is_dead_once_the_epoch_is_n_plus_one(
        self, env: Env
    ) -> None:
        """The point of the epoch: consent captured before a revocation is void after it.

        The approval row is written the way the approval path writes it, carrying the
        epoch observed at the time, and is then presented for admission after the buyer
        has revoked.
        """
        authority_id = seed(env, max_amount_minor=500_000)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            snap = lock_authority(session, authority_id)
            assert snap is not None
            recorded_epoch = snap.revocation_epoch
            session.execute(
                _INSERT_APPROVAL,
                {
                    "id": uuid7(),
                    "t": env.tenant_id,
                    "c": uuid7(),
                    "h": "approval-content-hash",
                    "amount": 200_000,
                    "a": authority_id,
                    "e": recorded_epoch,
                },
            )
        session.close()

        # The buyer revokes on the trusted surface.
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            revoke(session, authority_id)
        session.close()

        # The agent comes back holding the approval it captured a moment earlier.
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = admit_debit(
                session,
                authority_id,
                expected_epoch=recorded_epoch,
                amount=Money(200_000, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert decision.code is RecoveryCode.AUTHORITY_REVOKED
        assert decision.reason is AuthorityReason.AUTHORITY_EPOCH_STALE
        assert decision.observed_epoch == recorded_epoch + 1
        # A refused admission must not move money or capacity.
        assert stored(env, authority_id)[1] == 0

    def test_epoch_equality_is_the_gate_not_the_status_column(self, env: Env) -> None:
        """A stale epoch is refused on its own, with the row still ACTIVE.

        This separates the two defences. If admission only consulted ``status``, an
        authority whose epoch moved for any reason a future writer invents -- a mandate
        re-issue, a capacity restoration after reconciliation -- would still honour
        approvals captured before the move. The epoch comparison is what makes old
        consent numerically dead rather than dead by convention.
        """
        authority_id = seed(env, epoch=2, status=AuthorityStatus.ACTIVE)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            stale = admit_debit(
                session,
                authority_id,
                expected_epoch=1,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
            current = check_authority(
                session,
                authority_id,
                expected_epoch=2,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert stale.code is RecoveryCode.AUTHORITY_REVOKED
        assert stale.reason is AuthorityReason.AUTHORITY_EPOCH_STALE
        assert current.allowed, "the current epoch must still be admissible"
        assert stored(env, authority_id)[1] == 0

    def test_an_epoch_the_row_never_reached_is_refused(self, env: Env) -> None:
        """A proof quoting a future epoch is forged or corrupted. Deny, do not guess."""
        authority_id = seed(env)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = check_authority(
                session,
                authority_id,
                expected_epoch=7,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert decision.code is RecoveryCode.AUTHORITY_REVOKED
        assert decision.reason is AuthorityReason.AUTHORITY_EPOCH_UNKNOWN

    def test_status_revoked_alone_is_enough_to_refuse(self, env: Env) -> None:
        authority_id = seed(env, status=AuthorityStatus.REVOKED, epoch=3)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = check_authority(
                session,
                authority_id,
                expected_epoch=3,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert decision.code is RecoveryCode.AUTHORITY_REVOKED
        assert decision.reason is AuthorityReason.AUTHORITY_REVOKED


# ------------------------------------------------------------------------- invariant 5


class TestCumulativeCapacity:
    def test_spend_to_the_limit_then_one_paisa_more_fails(self, env: Env) -> None:
        """The exact boundary: 99_999 then 1 is admitted, the next single paisa is not."""
        authority_id = seed(env, max_amount_minor=100_000)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            first = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(99_999, INR),
                merchant_id=env.merchant_id,
            )
            assert first.allowed
            assert first.snapshot is not None
            assert first.snapshot.remaining == Money(1, INR)
            assert first.snapshot.status is AuthorityStatus.ACTIVE

            last = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(1, INR),
                merchant_id=env.merchant_id,
            )
            assert last.allowed
            assert last.snapshot is not None
            assert last.snapshot.remaining == Money(0, INR)
            assert last.snapshot.status is AuthorityStatus.EXHAUSTED

            overrun = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(1, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert overrun.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert overrun.reason is AuthorityReason.AUTHORITY_EXHAUSTED
        epoch, consumed, maximum, status = stored(env, authority_id)
        assert (consumed, maximum, status) == (100_000, 100_000, AuthorityStatus.EXHAUSTED)
        assert epoch == 0

    def test_a_request_one_paisa_over_remaining_capacity_writes_nothing(self, env: Env) -> None:
        authority_id = seed(env, max_amount_minor=100_000, consumed=40_000)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            exact = check_authority(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(60_000, INR),
                merchant_id=env.merchant_id,
            )
            over = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(60_001, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert exact.allowed, "spending exactly the remaining capacity must be admitted"
        assert over.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert over.reason is AuthorityReason.CAPACITY_EXCEEDED
        assert stored(env, authority_id)[1] == 40_000

    def test_database_check_refuses_over_allocation_even_from_raw_sql(self, env: Env) -> None:
        """The constraint underneath the application check, specification 21.3.

        The application refusal above is one layer. This asserts the second: a writer
        that bypassed this module entirely still cannot record a consumed amount above
        the maximum the buyer authorized.
        """
        authority_id = seed(env, max_amount_minor=100_000)
        session = env.session()
        try:
            with pytest.raises(IntegrityError, match="consumed_within_max"):
                with session.begin():
                    set_tenant(session, env.tenant_id)
                    session.execute(
                        text(
                            "UPDATE delegated_authorities "
                            "SET consumed_amount_minor = max_amount_minor + 1 WHERE id = :id"
                        ),
                        {"id": authority_id},
                    )
        finally:
            session.close()
        assert stored(env, authority_id)[1] == 0

    def test_single_use_is_exhausted_by_one_debit_however_small(self, env: Env) -> None:
        """A single-use mandate authorizes one action, not a budget to nibble at."""
        authority_id = seed(env, kind=AuthorityKind.SINGLE_USE, max_amount_minor=100_000)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            first = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(1_000, INR),
                merchant_id=env.merchant_id,
            )
            second = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(1_000, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert first.allowed
        assert first.snapshot is not None
        assert first.snapshot.status is AuthorityStatus.EXHAUSTED
        assert second.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert second.reason is AuthorityReason.AUTHORITY_EXHAUSTED
        assert stored(env, authority_id)[1] == 1_000

    def test_kind_is_the_gate_for_single_use_not_the_status_column(self, env: Env) -> None:
        """A consumed SINGLE_USE mandate is refused on its kind, with the row still ACTIVE.

        ``admit_debit`` leaves a SINGLE_USE row EXHAUSTED, so in the case above the status
        column alone appears to be doing the work -- delete the kind gate and that test
        still passes. It is not enough. Any other writer can leave a SINGLE_USE row ACTIVE
        with something already consumed: a reconciliation that restores capacity after an
        unknown provider outcome, a mandate re-issue, a repair script. One mandate
        authorizes one action regardless of how the row got back to ACTIVE, and that is
        what the kind gate, not the status gate, guarantees.
        """
        authority_id = seed(
            env,
            kind=AuthorityKind.SINGLE_USE,
            status=AuthorityStatus.ACTIVE,
            max_amount_minor=100_000,
            consumed=1_000,
        )
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(1_000, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.reason is AuthorityReason.SINGLE_USE_ALREADY_CONSUMED
        assert stored(env, authority_id)[1] == 1_000

    def test_reserve_supports_repeated_debits_until_capacity_runs_out(self, env: Env) -> None:
        """Reserve Pay debits as value is delivered, specification 12.1."""
        authority_id = seed(env, kind=AuthorityKind.RESERVE, max_amount_minor=30_000)
        codes: list[RecoveryCode] = []
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            for _ in range(4):
                codes.append(
                    admit_debit(
                        session,
                        authority_id,
                        expected_epoch=0,
                        amount=Money(10_000, INR),
                        merchant_id=env.merchant_id,
                    ).code
                )
        session.close()

        assert codes == [
            RecoveryCode.OK,
            RecoveryCode.OK,
            RecoveryCode.OK,
            RecoveryCode.AUTHORITY_INSUFFICIENT,
        ]
        assert stored(env, authority_id)[1] == 30_000


# ------------------------------------------------------------------------- invariant 6


class TestExpiry:
    def test_an_authority_expired_by_the_database_clock_cannot_be_consumed(self, env: Env) -> None:
        authority_id = seed(env, expires_in_seconds=-1.0)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            snap = lock_authority(session, authority_id)
            decision = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()

        assert snap is not None
        assert snap.expired is True
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.reason is AuthorityReason.AUTHORITY_EXPIRED
        assert stored(env, authority_id)[1] == 0

    def test_status_expired_is_refused_even_with_time_left_on_the_clock(self, env: Env) -> None:
        authority_id = seed(env, status=AuthorityStatus.EXPIRED, expires_in_seconds=3600.0)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = check_authority(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert decision.reason is AuthorityReason.AUTHORITY_EXPIRED

    def test_module_never_reads_an_application_clock(self) -> None:
        """A skewed pod must be unable to admit an expired authority.

        The only way to guarantee that is for this module to have no local clock to be
        wrong with. This walks the AST for any call to a wall-clock function and then
        asserts the expiry comparison really does live in the SQL.
        """
        source = Path(authority_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        banned = {"now", "utcnow", "today", "time", "time_ns", "monotonic", "monotonic_ns"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name in banned:
                offenders.append(ast.unparse(func))
        assert not offenders, f"application clock read in authority.py: {offenders}"

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "time" not in imported

        # The comparison is delegated to PostgreSQL, in the same statements that take the
        # lock and allocate capacity.
        assert "(expires_at <= now()) AS expired" in source
        assert "AND expires_at > now()" in source

    def test_grant_refuses_an_expiry_already_past_by_the_database_clock(self, env: Env) -> None:
        session = env.session()
        try:
            with pytest.raises(AuthorityError, match="not in the future"):
                with session.begin():
                    set_tenant(session, env.tenant_id)
                    grant_authority(
                        session,
                        tenant_id=env.tenant_id,
                        merchant_id=env.merchant_id,
                        buyer_ref=BUYER,
                        kind=AuthorityKind.RESERVE,
                        max_amount=Money(100_000, INR),
                        # A fixed instant, not one derived from this pod's clock: the
                        # refusal under test is the *database's* verdict, and a test that
                        # built "an hour ago" locally would start failing on the day the
                        # two clocks disagreed by an hour.
                        expires_at=datetime(2000, 1, 1, tzinfo=UTC),
                    )
        finally:
            session.close()

    def test_grant_creates_an_active_authority_at_epoch_zero(self, env: Env) -> None:
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            authority_id = grant_authority(
                session,
                tenant_id=env.tenant_id,
                merchant_id=env.merchant_id,
                buyer_ref=BUYER,
                kind=AuthorityKind.RESERVE,
                max_amount=Money(250_000, INR),
                ttl_seconds=900.0,
            )
        session.close()
        assert stored(env, authority_id) == (0, 0, 250_000, AuthorityStatus.ACTIVE)

    def test_grant_refuses_ambiguous_or_impossible_bounds(self, env: Env) -> None:
        naive_future = datetime(2999, 1, 1)  # noqa: DTZ001 - naive on purpose
        session = env.session()
        try:
            with session.begin():
                set_tenant(session, env.tenant_id)
                common = {
                    "tenant_id": env.tenant_id,
                    "merchant_id": env.merchant_id,
                    "buyer_ref": BUYER,
                    "kind": AuthorityKind.RESERVE,
                }
                with pytest.raises(AuthorityError, match="exactly one"):
                    grant_authority(session, max_amount=Money(1, INR), **common)
                with pytest.raises(AuthorityError, match="exactly one"):
                    grant_authority(
                        session,
                        max_amount=Money(1, INR),
                        ttl_seconds=10.0,
                        expires_at=naive_future.replace(tzinfo=UTC),
                        **common,
                    )
                with pytest.raises(AuthorityError, match="timezone-aware"):
                    grant_authority(
                        session, max_amount=Money(1, INR), expires_at=naive_future, **common
                    )
                with pytest.raises(AuthorityError, match="max_amount must be positive"):
                    grant_authority(session, max_amount=Money(0, INR), ttl_seconds=10.0, **common)
                with pytest.raises(AuthorityError, match="ttl_seconds must be positive"):
                    grant_authority(session, max_amount=Money(1, INR), ttl_seconds=0.0, **common)
        finally:
            session.rollback()
            session.close()


# ----------------------------------------------------------------------------- scope


class TestScope:
    def test_a_different_merchant_is_out_of_scope(self, env: Env) -> None:
        authority_id = seed(env)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=uuid7(),
            )
        session.close()
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.reason is AuthorityReason.MERCHANT_OUT_OF_SCOPE
        assert stored(env, authority_id)[1] == 0

    def test_a_different_buyer_is_out_of_scope(self, env: Env) -> None:
        authority_id = seed(env, buyer_ref="buyer-owner")
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = check_authority(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
                buyer_ref="buyer-somebody-else",
            )
        session.close()
        assert decision.reason is AuthorityReason.BUYER_OUT_OF_SCOPE

    def test_a_different_currency_is_refused_not_converted(self, env: Env) -> None:
        """No implicit conversion: 100 paise is not 100 cents."""
        authority_id = seed(env, currency=INR)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = check_authority(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, "USD"),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.reason is AuthorityReason.CURRENCY_MISMATCH

    def test_reconciling_capacity_is_not_spendable(self, env: Env) -> None:
        """Remaining capacity is unknown until the provider outcome settles."""
        authority_id = seed(env, status=AuthorityStatus.RECONCILING)
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            decision = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert decision.code is RecoveryCode.RECONCILIATION_IN_PROGRESS
        assert stored(env, authority_id)[1] == 0

    @pytest.mark.parametrize("bad", [0, -1, -100_000])
    def test_non_positive_debits_are_caller_bugs_not_denials(self, env: Env, bad: int) -> None:
        """A negative debit would restore capacity through the admission path."""
        authority_id = seed(env)
        session = env.session()
        try:
            with session.begin():
                set_tenant(session, env.tenant_id)
                with pytest.raises(AuthorityError, match="must be positive"):
                    check_authority(
                        session,
                        authority_id,
                        expected_epoch=0,
                        amount=Money(bad, INR),
                        merchant_id=env.merchant_id,
                    )
        finally:
            session.close()
        assert stored(env, authority_id)[1] == 0

    def test_negative_expected_epoch_is_a_caller_bug(self, env: Env) -> None:
        authority_id = seed(env)
        session = env.session()
        try:
            with session.begin():
                set_tenant(session, env.tenant_id)
                with pytest.raises(AuthorityError, match="non-negative"):
                    check_authority(
                        session,
                        authority_id,
                        expected_epoch=-1,
                        amount=Money(100, INR),
                        merchant_id=env.merchant_id,
                    )
        finally:
            session.close()


# ------------------------------------------------------------- defence in depth (3, 4, 5, 6)


class TestDefenceInDepth:
    """The second layer, asserted structurally because the lock makes it unreachable.

    ``check_authority`` refuses a stale epoch, a non-ACTIVE status, an expired authority
    and an over-budget amount *before* ``admit_debit`` issues its UPDATE, and the row lock
    guarantees none of those can change in between. So no behavioural test in this file
    can reach the copies of those tests in the UPDATE's own WHERE clause -- every one of
    them can be deleted and the whole suite stays green.

    That is exactly what makes them worth pinning. They are the layer that keeps
    allocation correct if a future caller reaches the UPDATE without holding the lock,
    or reorders the gates above it. Structural assertions cannot prove the guards work;
    they make removing them loud instead of silent.
    """

    def test_the_allocating_update_re_tests_every_gate_it_was_given(self) -> None:
        statement = str(authority_module._ADMIT_DEBIT)  # noqa: SLF001 - pinning the SQL text
        for guard in (
            "AND revocation_epoch = :expected_epoch",
            "AND status = 'ACTIVE'",
            "AND expires_at > now()",
            "AND consumed_amount_minor + :amount_minor <= max_amount_minor",
        ):
            assert guard in statement, (
                f"the allocating UPDATE no longer re-tests {guard!r}; capacity would then "
                "rest on the application check alone"
            )

    def test_the_locking_read_really_takes_the_row_lock(self) -> None:
        """The single linearization point, named in the statement that establishes it."""
        assert "FOR UPDATE" in str(authority_module._LOCK_AUTHORITY)  # noqa: SLF001


# ------------------------------------------------------------------------- invariant 3

RACE_AMOUNT = Money(50_000, INR)
#: Enough skew to decide who reaches the row lock first, given both transactions are
#: already open and tenant-bound when the barrier releases them.
RACE_SKEW_SECONDS = 0.05


def run_race_round(
    env: Env, *, delay_admit: float, delay_revoke: float
) -> tuple[AuthorityDecision, tuple[int, int, int, str]]:
    """Race one revocation against one admission on a fresh authority.

    Returns the admission's decision and the durably committed row, so the caller can
    assert that the answer the caller was given and the state of the world agree.
    """
    authority_id = seed(env, max_amount_minor=100_000)
    results: dict[str, object] = {}
    ready = threading.Barrier(2, timeout=20)

    threads = [
        threading.Thread(
            target=tenant_txn_thread(
                env,
                lambda s: admit_debit(
                    s,
                    authority_id,
                    expected_epoch=0,
                    amount=RACE_AMOUNT,
                    merchant_id=env.merchant_id,
                ),
                results,
                "admit",
                ready=ready,
                delay=delay_admit,
            )
        ),
        threading.Thread(
            target=tenant_txn_thread(
                env,
                lambda s: revoke(s, authority_id),
                results,
                "revoke",
                ready=ready,
                delay=delay_revoke,
            )
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a racing session never finished; suspected deadlock"

    unwrap(results, "revoke")  # re-raises whatever the revoking session hit
    return decision_of(results, "admit"), stored(env, authority_id)


class TestConcurrency:
    """Revocation and admission share one lock, and that lock is the whole guarantee."""

    def test_admission_blocks_while_a_revocation_holds_the_row(self, env: Env) -> None:
        """Revocation commits first, so the admission is forced to observe it.

        The holder takes the lock and does not commit. The admitting session is asserted
        to be *physically blocked* on it -- an unlocked read would sail past and this
        assertion would fail -- and once the revocation lands the admission is refused.
        """
        authority_id = seed(env, max_amount_minor=100_000)
        results: dict[str, object] = {}
        admitter = threading.Thread(
            target=tenant_txn_thread(
                env,
                lambda s: admit_debit(
                    s,
                    authority_id,
                    expected_epoch=0,
                    amount=Money(50_000, INR),
                    merchant_id=env.merchant_id,
                ),
                results,
                "admit",
            )
        )

        holder = env.session()
        try:
            with holder.begin():
                set_tenant(holder, env.tenant_id)
                revoke(holder, authority_id)
                holder_pid = backend_pid(holder)
                admitter.start()
                assert wait_until_blocked_by(env, holder_pid), (
                    "the admitting session did not block on the authority row held by "
                    "the revoking backend: check_authority is not holding "
                    "SELECT ... FOR UPDATE"
                )
            # The revocation commits here and the admitter is unblocked.
        finally:
            admitter.join(timeout=30)
            holder.close()

        decision = decision_of(results, "admit")
        assert decision.code is RecoveryCode.AUTHORITY_REVOKED
        epoch, consumed, _maximum, status = stored(env, authority_id)
        assert (epoch, consumed, status) == (1, 0, AuthorityStatus.REVOKED)

    def test_revocation_blocks_while_an_admission_holds_the_row(self, env: Env) -> None:
        """Admission commits first, so that one debit stands and later ones are blocked.

        Specification 10.2: "If admission commits first, that already-admitted action is
        recorded as consumed and revocation blocks subsequent actions."
        """
        authority_id = seed(env, max_amount_minor=100_000)
        results: dict[str, object] = {}
        revoker = threading.Thread(
            target=tenant_txn_thread(env, lambda s: revoke(s, authority_id), results, "revoke")
        )

        holder = env.session()
        try:
            with holder.begin():
                set_tenant(holder, env.tenant_id)
                admitted = admit_debit(
                    holder,
                    authority_id,
                    expected_epoch=0,
                    amount=Money(50_000, INR),
                    merchant_id=env.merchant_id,
                )
                assert admitted.allowed
                holder_pid = backend_pid(holder)
                revoker.start()
                assert wait_until_blocked_by(env, holder_pid), (
                    "the revoking session did not block on the authority row held by "
                    "the admitting backend: revoke is not holding SELECT ... FOR UPDATE"
                )
        finally:
            revoker.join(timeout=30)
            holder.close()

        outcome = unwrap(results, "revoke")
        assert isinstance(outcome, RevocationOutcome)
        assert outcome.epoch == 1
        epoch, consumed, _maximum, status = stored(env, authority_id)
        assert (epoch, consumed, status) == (1, 50_000, AuthorityStatus.REVOKED)

        # The admitted debit stands; every subsequent one is blocked by the new epoch.
        session = env.session()
        with session.begin():
            set_tenant(session, env.tenant_id)
            later = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(10_000, INR),
                merchant_id=env.merchant_id,
            )
        session.close()
        assert later.code is RecoveryCode.AUTHORITY_REVOKED
        assert stored(env, authority_id)[1] == 50_000

    def test_racing_revocation_and_admission_never_both_succeed(self, env: Env) -> None:
        """The real race, run from both sides.

        Two open transactions are released together; on alternating rounds one is delayed
        so both commit orders are genuinely exercised. In every round exactly one outcome
        holds -- the debit was admitted, or it was refused as revoked -- and the durable
        row must agree with the answer the caller was handed. "Admitted, and also revoked
        before admission" is the state that means a buyer was charged after cancelling.
        """
        rounds = 6
        admitted_rounds = 0
        revoked_rounds = 0

        for i in range(rounds):
            admit_first = i % 2 == 0
            decision, (epoch, consumed, _maximum, status) = run_race_round(
                env,
                delay_admit=0.0 if admit_first else RACE_SKEW_SECONDS,
                delay_revoke=RACE_SKEW_SECONDS if admit_first else 0.0,
            )

            admit_won = decision.code is RecoveryCode.OK
            revoke_won = decision.code is RecoveryCode.AUTHORITY_REVOKED
            assert admit_won != revoke_won, (
                f"round {i}: admission returned {decision.code}, which is neither a clean "
                "win nor a clean loss to the revocation"
            )

            if admit_won:
                admitted_rounds += 1
                assert consumed == RACE_AMOUNT.minor, (
                    f"round {i}: admission reported OK but {consumed} is recorded"
                )
            else:
                revoked_rounds += 1
                assert consumed == 0, (
                    f"round {i}: admission was refused as revoked, yet {consumed} was "
                    "still allocated -- both sides won"
                )

            assert epoch == 1, f"round {i}: the revocation must always land, epoch was {epoch}"
            assert status == AuthorityStatus.REVOKED

        # Both commit orders were exercised, so neither branch above is dead code.
        assert admitted_rounds > 0, "the admission never won a round; the race was one-sided"
        assert revoked_rounds > 0, "the revocation never won a round; the race was one-sided"

    def test_two_concurrent_debits_cannot_both_take_the_last_capacity(self, env: Env) -> None:
        """Capacity is allocated under the same lock, so overspend is impossible.

        Both sessions see 100_000 available and each want 60_000. Without the row lock
        both would read enough capacity and both would allocate, leaving 120_000 spent
        against a 100_000 authority.
        """
        authority_id = seed(env, max_amount_minor=100_000)
        results: dict[str, object] = {}
        ready = threading.Barrier(2, timeout=20)

        def body(key: str) -> Callable[[], None]:
            return tenant_txn_thread(
                env,
                lambda s: admit_debit(
                    s,
                    authority_id,
                    expected_epoch=0,
                    amount=Money(60_000, INR),
                    merchant_id=env.merchant_id,
                ),
                results,
                key,
                ready=ready,
            )

        threads = [threading.Thread(target=body("a")), threading.Thread(target=body("b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a racing debit never finished; suspected deadlock"

        codes = sorted(decision_of(results, key).code for key in ("a", "b"))
        assert codes.count(RecoveryCode.OK) == 1, f"expected exactly one winner, got {codes}"
        assert codes.count(RecoveryCode.AUTHORITY_INSUFFICIENT) == 1

        _epoch, consumed, maximum, _status = stored(env, authority_id)
        assert consumed == 60_000
        assert consumed <= maximum


# --------------------------------------------------------------------- decision shape


class TestDecisionShape:
    def test_an_allowed_decision_must_name_the_row_it_used(self) -> None:
        with pytest.raises(AuthorityError, match="must name the authority row"):
            AuthorityDecision(
                code=RecoveryCode.OK,
                reason=AuthorityReason.OK,
                authority_id=uuid7(),
                requested=Money(1, INR),
                snapshot=None,
            )

    def test_a_denied_decision_cannot_claim_reason_ok(self) -> None:
        with pytest.raises(AuthorityError, match="cannot carry reason OK"):
            AuthorityDecision(
                code=RecoveryCode.AUTHORITY_REVOKED,
                reason=AuthorityReason.OK,
                authority_id=uuid7(),
                requested=Money(1, INR),
            )

    def test_an_ok_code_cannot_carry_a_denial_reason(self) -> None:
        with pytest.raises(AuthorityError, match="must carry reason OK"):
            AuthorityDecision(
                code=RecoveryCode.OK,
                reason=AuthorityReason.CAPACITY_EXCEEDED,
                authority_id=uuid7(),
                requested=Money(1, INR),
            )

    def test_every_denial_reason_is_a_closed_key_not_free_text(self) -> None:
        """Agents branch on these. A new failure mode gets a reviewed member, not prose."""
        assert AuthorityReason.OK == "OK"
        assert all(isinstance(member.value, str) for member in AuthorityReason)
        assert len(set(AuthorityReason)) == len(list(AuthorityReason))


class TestReserveSelectedProductBounds:
    def create(self, env: Env) -> uuid.UUID:
        with env.session() as session, session.begin():
            set_tenant(session, env.tenant_id)
            return grant_authority(
                session,
                tenant_id=env.tenant_id,
                merchant_id=env.merchant_id,
                buyer_ref=BUYER,
                kind=AuthorityKind.RESERVE,
                max_amount=Money(200_000, INR),
                per_purchase_limit=Money(50_000, INR),
                allowed_skus=frozenset({"MILK", "BREAD"}),
                ttl_seconds=3600,
            )

    @pytest.mark.parametrize(
        "amount,skus,buyer,reason",
        [
            (50_001, frozenset({"MILK"}), BUYER, AuthorityReason.PURCHASE_LIMIT_EXCEEDED),
            (35_000, frozenset({"MILK", "COFFEE"}), BUYER, AuthorityReason.PRODUCT_OUT_OF_SCOPE),
            (35_000, None, BUYER, AuthorityReason.PRODUCT_OUT_OF_SCOPE),
            (35_000, frozenset(), BUYER, AuthorityReason.PRODUCT_OUT_OF_SCOPE),
            (35_000, frozenset({"MILK"}), None, AuthorityReason.BUYER_REQUIRED),
            (35_000, frozenset({"MILK"}), "another-buyer", AuthorityReason.BUYER_OUT_OF_SCOPE),
        ],
    )
    def test_denied_without_allocating(self, env, amount, skus, buyer, reason):
        authority_id = self.create(env)
        with env.session() as session, session.begin():
            set_tenant(session, env.tenant_id)
            result = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(amount, INR),
                merchant_id=env.merchant_id,
                buyer_ref=buyer,
                product_skus=skus,
            )
            assert result.reason is reason
            assert not result.allowed
            assert lock_authority(session, authority_id).consumed_amount.minor == 0

    def test_selected_items_and_exact_limit_succeed(self, env):
        authority_id = self.create(env)
        with env.session() as session, session.begin():
            set_tenant(session, env.tenant_id)
            result = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(50_000, INR),
                merchant_id=env.merchant_id,
                buyer_ref=BUYER,
                product_skus=frozenset({"MILK", "BREAD"}),
            )
            assert result.allowed
            assert result.snapshot.remaining.minor == 150_000
            assert result.snapshot.allowed_skus == frozenset({"MILK", "BREAD"})

    def test_revocation_still_wins(self, env):
        authority_id = self.create(env)
        with env.session() as session, session.begin():
            set_tenant(session, env.tenant_id)
            revoke(session, authority_id)
        with env.session() as session, session.begin():
            set_tenant(session, env.tenant_id)
            result = admit_debit(
                session,
                authority_id,
                expected_epoch=0,
                amount=Money(100, INR),
                merchant_id=env.merchant_id,
                buyer_ref=BUYER,
                product_skus=frozenset({"MILK"}),
            )
            assert result.code is RecoveryCode.AUTHORITY_REVOKED

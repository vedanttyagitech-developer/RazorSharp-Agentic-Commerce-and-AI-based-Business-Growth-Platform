"""Reservation lifecycle tests, specification 10.4.

These run against real PostgreSQL as ``commerce_test_kernel`` — a NOSUPERUSER,
NOBYPASSRLS login role. SQLite cannot show row locks, advisory locks, ``FOR UPDATE
SKIP LOCKED`` or a server clock, and every invariant below is about one of those.

The four invariants under test, and the failure each one prevents:

1. Expiry is decided by the database clock. A pod with a slow clock would otherwise
   admit a lapsed hold and oversell stock the merchant no longer has.
2. The cleanup worker is not a correctness authority. A worker outage would otherwise
   turn every un-swept expired row into an admissible hold.
3. An unknown payment outcome keeps the hold. Releasing there resells inventory that
   may in fact have been paid for.
4. Concurrent reservers of one scarce item are serialized. Two agents racing for the
   last unit would otherwise both walk away holding it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from commerce_domain import NOT_A_SUCCESS, RecoveryCode, uuid7
from platform_db import TenantContextError, set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel import reservations as res
from transaction_kernel.reservations import (
    Allocation,
    ReleaseCause,
    ReservationContractError,
    ReservationError,
    ReservationStatus,
    check_validity,
    consume,
    release,
    reserve,
    sweep_expired,
)

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Seeding and teardown run as the owner: application roles deliberately have no DELETE
# on any table, and granting the kernel DELETE to make fixtures convenient would erase
# the guarantee that financial history cannot be removed through an application role.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

SKU = "SKU-SCARCE"


def _require_db(url: str, *, pool_size: int = 1) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for reservation tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    """Four connections: the concurrency tests need genuinely separate sessions."""
    engine = _require_db(KERNEL_URL, pool_size=4)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    # A superuser bypasses row-level security unconditionally, so a suite run as one
    # would pass while proving nothing about tenant scoping of these queries.
    assert row.rolsuper is False, "reservation tests must not run as a superuser"
    assert row.rolbypassrls is False, "reservation tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL)


@pytest.fixture
def sessions(kernel_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    s = sessions()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def tenant(admin_engine: Engine) -> Iterator[uuid.UUID]:
    """One tenant with one merchant, removed afterwards."""
    tenant_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    slug = f"rz-{tenant_id.hex[:8]}"
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :slug, :name, 'asia-south1')"
            ),
            {"id": tenant_id, "slug": slug, "name": slug},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :slug, :name, 'INR')"
            ),
            {"id": merchant_id, "t": tenant_id, "slug": slug, "name": slug},
        )
    yield tenant_id
    with admin_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        # Order matters: reservations and checkout versions reference the merchant row.
        conn.execute(text("DELETE FROM reservations WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM checkout_versions WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM merchants WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


def seed_checkout(
    admin: Engine,
    tenant_id: uuid.UUID,
    *,
    version: int = 1,
    lines: list[dict[str, Any]] | None = None,
    content: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert one immutable checkout version and return its checkout id.

    ``lines`` is the shape the capacity check reads: objects carrying ``sku`` and an
    integer ``quantity``. Pass ``content`` directly to build a version whose content
    does not honour that shape.
    """
    checkout_id: uuid.UUID = uuid7()
    body: dict[str, Any] = content if content is not None else {"lines": lines or []}
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        merchant_id = conn.execute(
            text("SELECT id FROM merchants WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id}
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO checkout_versions "
                "(id, tenant_id, merchant_id, checkout_id, version, content, content_hash, "
                " currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, 'INR', 100000, "
                "        'RESERVED', true)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "v": version,
                "content": json.dumps(body),
                "h": "hash-" + uuid.uuid4().hex[:16],
            },
        )
    return checkout_id


def seed_reservation(
    admin: Engine,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    *,
    version: int = 1,
    status: str = "ACTIVE",
    expires_in_seconds: int,
) -> uuid.UUID:
    """Insert a reservation whose expiry is anchored to the DATABASE clock.

    ``expires_in_seconds`` is applied as ``now() + n * interval '1 second'`` inside
    PostgreSQL, so a negative value produces a row that the database considers expired
    no matter what this process believes the time to be.
    """
    reservation_id: uuid.UUID = uuid7()
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO reservations "
                "(id, tenant_id, checkout_id, checkout_version, status, expires_at) "
                "VALUES (:id, :t, :c, :v, :s, "
                "        now() + (CAST(:n AS integer) * INTERVAL '1 second'))"
            ),
            {
                "id": reservation_id,
                "t": tenant_id,
                "c": checkout_id,
                "v": version,
                "s": status,
                "n": expires_in_seconds,
            },
        )
    return reservation_id


def status_of(admin: Engine, reservation_id: uuid.UUID) -> str:
    with admin.begin() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM reservations WHERE id = :i"), {"i": reservation_id}
            ).scalar_one()
        )


def row_of(admin: Engine, reservation_id: uuid.UUID) -> Any:
    with admin.begin() as conn:
        return conn.execute(
            text("SELECT status, expires_at, now() AS db_now FROM reservations WHERE id = :i"),
            {"i": reservation_id},
        ).one()


def live_holds(admin: Engine, tenant_id: uuid.UUID) -> int:
    """Reservations still withholding stock, judged by the database clock."""
    with admin.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM reservations "
                    "WHERE tenant_id = :t AND status = 'ACTIVE' AND expires_at > now()"
                ),
                {"t": tenant_id},
            ).scalar_one()
        )


# ------------------------------------------------------- invariant 1: database clock


class TestDatabaseClockDecidesValidity:
    def test_source_never_consults_an_application_clock(self) -> None:
        """The structural half of invariant 1.

        Behaviour tests can only show that the decision matches the database clock on a
        machine where both clocks agree. This one fails the moment anyone introduces a
        Python-side time source into the module, which is the change that would let a
        skewed pod start disagreeing with the database.
        """
        source = Path(res.__file__).read_text(encoding="utf-8")
        forbidden = (
            "datetime.now",
            "datetime.utcnow",
            "utcnow(",
            "date.today",
            "time.time",
            "time.monotonic",
            "time.perf_counter",
            "import time",
        )
        found = [needle for needle in forbidden if needle in source]
        assert found == [], f"reservations.py must not read an application clock: {found}"

    def test_expired_on_the_database_clock_is_refused_even_where_a_slow_pod_would_admit(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Expiry is one second old by the database clock, but an hour fresh to a slow pod.

        A pod whose clock lags by an hour computes ``expires_at > now_app`` as true for
        this row and would admit it. The kernel must refuse, because only the database
        clock is authoritative.
        """
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=-1)
        row = row_of(admin_engine, reservation_id)
        skewed_pod_now = datetime.now(UTC) - timedelta(hours=1)
        assert row.expires_at > skewed_pod_now, (
            "fixture must produce a row that a one-hour-slow pod would consider valid"
        )

        with session.begin():
            set_tenant(session, tenant)
            outcome = check_validity(session, checkout_id=checkout, checkout_version=1)

        assert outcome.code is RecoveryCode.RESERVATION_EXPIRED
        assert outcome.reservation is not None
        assert outcome.reservation.expired is True
        assert outcome.reservation.seconds_remaining == 0

    def test_live_reservation_is_admitted_and_reports_countdown_from_the_database(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=300)

        with session.begin():
            set_tenant(session, tenant)
            outcome = check_validity(session, checkout_id=checkout, checkout_version=1)

        assert outcome.code is RecoveryCode.OK
        assert outcome.reservation is not None
        assert outcome.reservation.expired is False
        # Countdown comes from PostgreSQL, so it is bounded by the TTL we asked for.
        assert 290 <= outcome.reservation.seconds_remaining <= 300

    def test_reserve_anchors_expiry_to_the_database_clock_not_the_caller(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(session, checkout_id=checkout, checkout_version=1, ttl_seconds=120)
        assert outcome.code is RecoveryCode.OK
        assert outcome.reservation is not None

        row = row_of(admin_engine, outcome.reservation.reservation_id)
        drift = abs((row.expires_at - row.db_now).total_seconds() - 120)
        assert drift < 2, "expires_at must be now() + ttl as computed by PostgreSQL"

    def test_consume_refuses_a_hold_that_lapsed_on_the_database_clock(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=-1)
        with session.begin():
            set_tenant(session, tenant)
            outcome = consume(session, checkout_id=checkout, checkout_version=1)

        assert outcome.code is RecoveryCode.RESERVATION_EXPIRED
        # The refusal must not have spent the hold on the way past.
        assert status_of(admin_engine, reservation_id) == "ACTIVE"

    def test_missing_tenant_context_raises_instead_of_reporting_expiry(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Without a bound tenant, RLS hides every row.

        Silence would otherwise be reported as RESERVATION_EXPIRED and a valid hold
        would look lapsed to the buyer. Failing loudly keeps a configuration bug from
        presenting as a business outcome.
        """
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=300)

        with pytest.raises(TenantContextError):
            with session.begin():
                check_validity(session, checkout_id=checkout, checkout_version=1)


# ------------------------------------------- invariant 2: the worker is not authority


class TestCleanupWorkerIsNotACorrectnessAuthority:
    def test_expired_row_still_labelled_active_is_refused_before_any_sweep(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """No worker has run. The row still says ACTIVE. It must still be refused."""
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=-30)
        assert status_of(admin_engine, reservation_id) == "ACTIVE"

        with session.begin():
            set_tenant(session, tenant)
            validity = check_validity(session, checkout_id=checkout, checkout_version=1)
            spend = consume(session, checkout_id=checkout, checkout_version=1)

        assert validity.code is RecoveryCode.RESERVATION_EXPIRED
        assert spend.code is RecoveryCode.RESERVATION_EXPIRED
        # Still un-swept, and the decision was already correct without the sweep.
        assert status_of(admin_engine, reservation_id) == "ACTIVE"

    def test_un_swept_expired_hold_does_not_withhold_stock_from_another_buyer(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The capacity check ignores lapsed holds by clock, not by status.

        If it filtered on ``status = 'ACTIVE'`` alone, a stalled cleanup worker would
        make the last unit permanently unsellable.
        """
        stale = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, stale, expires_in_seconds=-5)
        fresh = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=fresh,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is RecoveryCode.OK

    def test_sweep_retires_lapsed_rows_and_leaves_live_ones_alone(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        lapsed = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        lapsed_id = seed_reservation(admin_engine, tenant, lapsed, expires_in_seconds=-10)
        live = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        live_id = seed_reservation(admin_engine, tenant, live, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            swept = sweep_expired(session, limit=100)

        assert swept == 1
        assert status_of(admin_engine, lapsed_id) == "EXPIRED"
        assert status_of(admin_engine, live_id) == "ACTIVE"

    def test_expiry_release_refuses_to_retire_a_hold_the_database_says_is_live(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Invariant 1 in the write direction.

        A pod running fast must not be able to end a hold the buyer still legitimately
        has by simply asserting that it expired.
        """
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            outcome = release(
                session,
                checkout_id=checkout,
                checkout_version=1,
                cause=ReleaseCause.EXPIRED,
            )

        assert outcome.code is RecoveryCode.RESERVATION_EXPIRED
        assert status_of(admin_engine, reservation_id) == "ACTIVE"

    def test_expiry_release_retires_a_hold_the_database_says_has_lapsed(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=-1)

        with session.begin():
            set_tenant(session, tenant)
            outcome = release(
                session,
                checkout_id=checkout,
                checkout_version=1,
                cause=ReleaseCause.EXPIRED,
            )

        assert outcome.code is RecoveryCode.OK
        assert status_of(admin_engine, reservation_id) == "EXPIRED"


# ---------------------------------------------------------- invariant 3: release rules


class TestReleaseRules:
    def test_confirmed_payment_failure_releases_a_consumed_hold(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """A hold consumed at admission is still withholding stock.

        Once the provider confirms failure the stock must go back, or a failed payment
        silently removes inventory from sale until the TTL runs out.
        """
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            assert consume(session, checkout_id=checkout, checkout_version=1).ok
            outcome = release(
                session,
                checkout_id=checkout,
                checkout_version=1,
                cause=ReleaseCause.PAYMENT_FAILED,
            )

        assert outcome.code is RecoveryCode.OK
        assert status_of(admin_engine, reservation_id) == "RELEASED"

    def test_cancellation_releases_an_active_hold(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            outcome = release(
                session,
                checkout_id=checkout,
                checkout_version=1,
                cause=ReleaseCause.CANCELLED,
            )

        assert outcome.code is RecoveryCode.OK
        assert status_of(admin_engine, reservation_id) == "RELEASED"

    @pytest.mark.parametrize(
        ("cause", "expected"),
        [
            (ReleaseCause.PAYMENT_UNKNOWN, RecoveryCode.PAYMENT_UNKNOWN),
            (ReleaseCause.RECONCILING, RecoveryCode.RECONCILIATION_IN_PROGRESS),
        ],
    )
    def test_unknown_outcome_keeps_the_hold(
        self,
        session: Session,
        admin_engine: Engine,
        tenant: uuid.UUID,
        cause: ReleaseCause,
        expected: RecoveryCode,
    ) -> None:
        """The oversell this rule prevents.

        "We do not know" is not "it failed". If the hold were released here and the
        payment later turned out to have succeeded, the same unit would already have
        been sold to someone else and the buyer would hold a charge for stock that no
        longer exists.
        """
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        reservation_id = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            outcome = release(session, checkout_id=checkout, checkout_version=1, cause=cause)

        assert outcome.code is expected
        # The caller must never be able to present this to a buyer as a finished action.
        assert outcome.code in NOT_A_SUCCESS
        # Committed state, not just the returned view: the hold survives the transaction.
        assert status_of(admin_engine, reservation_id) == "ACTIVE"
        assert live_holds(admin_engine, tenant) == 1

    def test_hold_kept_under_unknown_outcome_still_blocks_another_buyer(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The point of keeping it: the last unit stays unavailable while in doubt."""
        held = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, held, expires_in_seconds=600)
        other = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with session.begin():
            set_tenant(session, tenant)
            refused = release(
                session,
                checkout_id=held,
                checkout_version=1,
                cause=ReleaseCause.PAYMENT_UNKNOWN,
            )
            competing = reserve(
                session,
                checkout_id=other,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert refused.code is RecoveryCode.PAYMENT_UNKNOWN
        assert competing.code is RecoveryCode.CONCURRENT_OPERATION

    def test_a_released_hold_frees_the_unit_for_the_next_buyer(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        first = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, first, expires_in_seconds=600)
        second = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with session.begin():
            set_tenant(session, tenant)
            assert release(
                session,
                checkout_id=first,
                checkout_version=1,
                cause=ReleaseCause.PAYMENT_FAILED,
            ).ok
            outcome = reserve(
                session,
                checkout_id=second,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is RecoveryCode.OK

    def test_releasing_twice_is_harmless(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            first = release(
                session,
                checkout_id=checkout,
                checkout_version=1,
                cause=ReleaseCause.CANCELLED,
            )
            second = release(
                session,
                checkout_id=checkout,
                checkout_version=1,
                cause=ReleaseCause.CANCELLED,
            )

        assert first.code is RecoveryCode.OK
        assert second.code is RecoveryCode.DUPLICATE_OPERATION


# ------------------------------------------------------- invariant 4: no double hold


class TestScarceItemConcurrency:
    def test_two_contending_sessions_cannot_both_hold_the_last_unit(
        self,
        sessions: sessionmaker[Session],
        admin_engine: Engine,
        tenant: uuid.UUID,
    ) -> None:
        """Real contention: B's reserve is issued while A's transaction still holds the lock.

        The interleaving is forced rather than hoped for. A reserves, signals, and waits
        before committing; B only starts once A has signalled. B therefore blocks inside
        PostgreSQL until A commits, which is what the elapsed-time assertion checks. If
        the exclusion were removed, both would read ``held = 0`` and both would insert.
        """
        checkout_a = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        checkout_b = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        allocation = Allocation(scarcity_key=SKU, available_units=1)

        a_reserved = threading.Event()
        b_started = threading.Event()
        results: dict[str, RecoveryCode] = {}
        elapsed: dict[str, float] = {}

        def run_a() -> None:
            s = sessions()
            try:
                with s.begin():
                    set_tenant(s, tenant)
                    results["a"] = reserve(
                        s,
                        checkout_id=checkout_a,
                        checkout_version=1,
                        ttl_seconds=300,
                        allocations=[allocation],
                    ).code
                    a_reserved.set()
                    b_started.wait(timeout=5)
                    # Hold the advisory lock long enough that B is provably blocked.
                    time.sleep(0.4)
            finally:
                s.close()

        def run_b() -> None:
            s = sessions()
            try:
                a_reserved.wait(timeout=5)
                with s.begin():
                    set_tenant(s, tenant)
                    b_started.set()
                    started = time.monotonic()
                    results["b"] = reserve(
                        s,
                        checkout_id=checkout_b,
                        checkout_version=1,
                        ttl_seconds=300,
                        allocations=[allocation],
                    ).code
                    elapsed["b"] = time.monotonic() - started
            finally:
                s.close()

        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "a reserver deadlocked"

        assert results["a"] is RecoveryCode.OK
        assert results["b"] is RecoveryCode.CONCURRENT_OPERATION
        assert elapsed["b"] >= 0.3, "B must have waited on A's lock, not raced past it"
        assert live_holds(admin_engine, tenant) == 1

    def test_capacity_of_two_admits_two_and_refuses_the_third(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkouts = [
            seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
            for _ in range(3)
        ]
        allocation = Allocation(scarcity_key=SKU, available_units=2)
        codes: list[RecoveryCode] = []
        with session.begin():
            set_tenant(session, tenant)
            for checkout in checkouts:
                codes.append(
                    reserve(
                        session,
                        checkout_id=checkout,
                        checkout_version=1,
                        ttl_seconds=300,
                        allocations=[allocation],
                    ).code
                )

        assert codes == [
            RecoveryCode.OK,
            RecoveryCode.OK,
            RecoveryCode.CONCURRENT_OPERATION,
        ]

    def test_multi_unit_lines_are_counted_not_merely_the_number_of_holds(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Held quantity is units, not rows.

        Counting reservations instead of units would let a single hold for three units
        pass a capacity-of-four check twice over.
        """
        big = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 3}])
        small = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 2}])
        allocation = Allocation(scarcity_key=SKU, available_units=4)

        with session.begin():
            set_tenant(session, tenant)
            first = reserve(
                session,
                checkout_id=big,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[allocation],
            )
            second = reserve(
                session,
                checkout_id=small,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[allocation],
            )

        assert first.code is RecoveryCode.OK
        assert second.code is RecoveryCode.CONCURRENT_OPERATION

    # One key sorts before SKU and one after, so the case holds whichever internal order
    # the capacity checks run in. Without both, a bug that checked only the first item
    # would pass on whichever arrangement happened to put the short item first.
    @pytest.mark.parametrize("other", ["SKU-AAA-SCARCE", "SKU-ZZZ-SCARCE"])
    def test_a_basket_is_refused_whole_when_any_one_item_is_short(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID, other: str
    ) -> None:
        """Every scarce item in the cart is checked, and no row is written unless all pass.

        Checking only one item would let the hold be created while another item is
        oversold — and the reservation would then look perfectly valid at admission.
        """
        contended = seed_checkout(admin_engine, tenant, lines=[{"sku": other, "quantity": 1}])
        seed_reservation(admin_engine, tenant, contended, expires_in_seconds=600)
        cart = seed_checkout(
            admin_engine,
            tenant,
            lines=[{"sku": SKU, "quantity": 1}, {"sku": other, "quantity": 1}],
        )

        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=cart,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[
                    Allocation(scarcity_key=SKU, available_units=10),
                    Allocation(scarcity_key=other, available_units=1),
                ],
            )
            after = check_validity(session, checkout_id=cart, checkout_version=1)

        assert outcome.code is RecoveryCode.CONCURRENT_OPERATION
        assert outcome.reservation is None
        # No partial hold survived the refusal.
        assert after.code is RecoveryCode.RESERVATION_EXPIRED
        assert live_holds(admin_engine, tenant) == 1  # only the pre-existing hold

    def test_a_basket_whose_items_all_fit_is_held(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        other = "SKU-ALSO-SCARCE"
        cart = seed_checkout(
            admin_engine,
            tenant,
            lines=[{"sku": SKU, "quantity": 2}, {"sku": other, "quantity": 1}],
        )
        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=cart,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[
                    Allocation(scarcity_key=other, available_units=1),
                    Allocation(scarcity_key=SKU, available_units=2),
                ],
            )

        assert outcome.code is RecoveryCode.OK

    def test_baskets_listing_the_same_items_in_opposite_orders_do_not_deadlock(
        self,
        sessions: sessionmaker[Session],
        admin_engine: Engine,
        tenant: uuid.UUID,
    ) -> None:
        """Lock order is the sorted scarcity keys, not the caller's argument order.

        Two carts holding the same two items, listed in opposite orders, would
        otherwise each grab the lock the other needs: PostgreSQL breaks the cycle by
        aborting one transaction, and a buyer loses a valid checkout to an error that
        has nothing to do with stock. Sorting makes the cycle impossible.
        """
        other = "SKU-ALSO-SCARCE"
        lines = [{"sku": SKU, "quantity": 1}, {"sku": other, "quantity": 1}]
        checkout_a = seed_checkout(admin_engine, tenant, lines=lines)
        checkout_b = seed_checkout(admin_engine, tenant, lines=lines)
        forward = [
            Allocation(scarcity_key=SKU, available_units=2),
            Allocation(scarcity_key=other, available_units=2),
        ]
        gate = threading.Barrier(2, timeout=10)
        results: dict[str, RecoveryCode] = {}
        failures: dict[str, BaseException] = {}

        def run(name: str, checkout: uuid.UUID, allocations: list[Allocation]) -> None:
            s = sessions()
            try:
                with s.begin():
                    set_tenant(s, tenant)
                    gate.wait()
                    results[name] = reserve(
                        s,
                        checkout_id=checkout,
                        checkout_version=1,
                        ttl_seconds=300,
                        allocations=allocations,
                    ).code
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
                failures[name] = exc
            finally:
                s.close()

        threads = [
            threading.Thread(target=run, args=("a", checkout_a, forward)),
            threading.Thread(target=run, args=("b", checkout_b, list(reversed(forward)))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads)

        assert failures == {}, f"a reserver was aborted: {failures}"
        assert results == {"a": RecoveryCode.OK, "b": RecoveryCode.OK}
        assert live_holds(admin_engine, tenant) == 2

    def test_the_same_item_cannot_be_listed_twice(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Two entries for one item would be checked twice against the same stock figure."""
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        with pytest.raises(ReservationError):
            with session.begin():
                set_tenant(session, tenant)
                reserve(
                    session,
                    checkout_id=checkout,
                    checkout_version=1,
                    ttl_seconds=300,
                    allocations=[
                        Allocation(scarcity_key=SKU, available_units=1),
                        Allocation(scarcity_key=SKU, available_units=1),
                    ],
                )

    def test_out_of_stock_is_a_requote_not_a_retry(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """No hold could ever satisfy this, so the caller must not be told to retry."""
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 2}])
        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=checkout,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is RecoveryCode.STALE_CHECKOUT
        assert outcome.reservation is None
        assert live_holds(admin_engine, tenant) == 0

    def test_holds_on_other_items_do_not_consume_this_item(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        other = seed_checkout(admin_engine, tenant, lines=[{"sku": "SKU-OTHER", "quantity": 5}])
        seed_reservation(admin_engine, tenant, other, expires_in_seconds=600)
        mine = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=mine,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is RecoveryCode.OK

    def test_a_negative_quantity_elsewhere_cannot_erode_the_held_total(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Held units are summed, so a negative line would cancel out a real hold.

        Two live holds exist for the last unit: one legitimately for 1 unit, one whose
        content carries -1. Summed naively they total zero and the next buyer is handed
        a unit that is already held. Only positive quantities may contribute.
        """
        holds_one = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, holds_one, expires_in_seconds=600)
        holds_negative = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": -1}])
        seed_reservation(admin_engine, tenant, holds_negative, expires_in_seconds=600)
        mine = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=mine,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is RecoveryCode.CONCURRENT_OPERATION
        assert outcome.reservation is None

    def test_capacity_check_refuses_when_a_live_hold_cannot_be_inspected(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Fail closed.

        A live hold whose checkout content carries no line array might be holding this
        item. Counting it as zero would quietly turn the oversell guard into a no-op, so
        the transaction is aborted instead.
        """
        opaque = seed_checkout(admin_engine, tenant, content={"cart": "not-a-line-array"})
        seed_reservation(admin_engine, tenant, opaque, expires_in_seconds=600)
        mine = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with pytest.raises(ReservationContractError):
            with session.begin():
                set_tenant(session, tenant)
                reserve(
                    session,
                    checkout_id=mine,
                    checkout_version=1,
                    ttl_seconds=300,
                    allocations=[Allocation(scarcity_key=SKU, available_units=1)],
                )

        assert live_holds(admin_engine, tenant) == 1  # only the pre-existing hold

    def test_reserving_an_item_the_checkout_does_not_name_is_refused(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Units come from the approved checkout, so they cannot drift from what was quoted."""
        checkout = seed_checkout(
            admin_engine, tenant, lines=[{"sku": "SKU-DIFFERENT", "quantity": 1}]
        )
        with pytest.raises(ReservationContractError):
            with session.begin():
                set_tenant(session, tenant)
                reserve(
                    session,
                    checkout_id=checkout,
                    checkout_version=1,
                    ttl_seconds=300,
                    allocations=[Allocation(scarcity_key=SKU, available_units=10)],
                )


# ---------------------------------------------------------------- single-winner spend


class TestSingleWinnerConsumption:
    def test_two_contending_sessions_cannot_both_spend_one_hold(
        self,
        sessions: sessionmaker[Session],
        admin_engine: Engine,
        tenant: uuid.UUID,
    ) -> None:
        """Exactly one admission may spend a hold.

        Both callers select the row FOR UPDATE. B blocks until A commits, then re-reads
        and finds the hold already consumed. Without the row lock both would see
        ``ACTIVE`` and both would go on to create a payment attempt.
        """
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        a_consumed = threading.Event()
        b_started = threading.Event()
        results: dict[str, RecoveryCode] = {}

        def run_a() -> None:
            s = sessions()
            try:
                with s.begin():
                    set_tenant(s, tenant)
                    results["a"] = consume(s, checkout_id=checkout, checkout_version=1).code
                    a_consumed.set()
                    b_started.wait(timeout=5)
                    time.sleep(0.3)
            finally:
                s.close()

        def run_b() -> None:
            s = sessions()
            try:
                a_consumed.wait(timeout=5)
                with s.begin():
                    set_tenant(s, tenant)
                    b_started.set()
                    results["b"] = consume(s, checkout_id=checkout, checkout_version=1).code
            finally:
                s.close()

        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "a consumer deadlocked"

        assert results["a"] is RecoveryCode.OK
        assert results["b"] is RecoveryCode.CONCURRENT_OPERATION
        assert results["b"] not in {RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION}

    def test_consumed_hold_is_reported_as_already_spent(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            assert consume(session, checkout_id=checkout, checkout_version=1).ok
            outcome = check_validity(session, checkout_id=checkout, checkout_version=1)

        assert outcome.code is RecoveryCode.DUPLICATE_OPERATION
        assert outcome.reservation is not None
        assert outcome.reservation.status is ReservationStatus.CONSUMED


# --------------------------------------------------------------- reserve entry rules


class TestReserveEntryRules:
    def test_duplicate_reserve_returns_the_existing_hold_without_creating_a_second(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        with session.begin():
            set_tenant(session, tenant)
            first = reserve(session, checkout_id=checkout, checkout_version=1, ttl_seconds=300)
            second = reserve(session, checkout_id=checkout, checkout_version=1, ttl_seconds=300)

        assert first.code is RecoveryCode.OK
        assert second.code is RecoveryCode.DUPLICATE_OPERATION
        assert first.reservation is not None and second.reservation is not None
        assert second.reservation.reservation_id == first.reservation.reservation_id
        assert live_holds(admin_engine, tenant) == 1

    def test_a_consumed_version_cannot_be_re_held(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Version N is spent. Re-holding it would let the same stock back a second payment."""
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=600)

        with session.begin():
            set_tenant(session, tenant)
            assert consume(session, checkout_id=checkout, checkout_version=1).ok
            outcome = reserve(session, checkout_id=checkout, checkout_version=1, ttl_seconds=300)

        assert outcome.code is RecoveryCode.STALE_CHECKOUT

    def test_a_sold_unit_is_not_offered_to_the_next_buyer(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The last unit, paid for once, must not be reservable again.

        Both capacity statements filtered `status = 'ACTIVE'`, so the moment admission
        spent a hold the row stopped defending its unit and the next buyer was measured
        against stock that had already been sold. Nothing else defends it -- a sale never
        decrements the merchant's catalogue, so these rows are the entire oversell guard --
        and the failure was silent all the way to the provider: two admissions, two grants,
        two real create-order commands, for one unit.

        It contradicted three statements in its own module: `ReservationStatus` ("ACTIVE
        and CONSUMED both hold stock"), `ReservationView.holds_stock`, and `reserve`'s own
        guarantee that "two agents racing for the last unit cannot both succeed". They were
        right and the SQL was wrong.

        The race is not what this test needs, which is the point: buyer B here arrives
        *after* buyer A has finished paying. Serial, unhurried, and it still oversold.
        """
        first = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        seed_reservation(admin_engine, tenant, first, expires_in_seconds=600)
        second = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])

        with session.begin():
            set_tenant(session, tenant)
            assert consume(session, checkout_id=first, checkout_version=1).ok
            # The only unit is now sold. A second buyer asks for it.
            outcome = reserve(
                session,
                checkout_id=second,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is not RecoveryCode.OK, (
            "the last unit was sold and then reserved again; on the previous statements "
            "this returned OK and the second buyer went on to pay for stock that no "
            "longer existed"
        )
        assert outcome.reservation is None

    def test_a_sold_unit_stays_sold_after_its_hold_would_have_lapsed(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """A paid-for unit does not return to the shelf when a timer passes.

        `consume` changes the status and never touches `expires_at`, so a CONSUMED row
        keeps whatever expiry it was created with -- minutes. Counting CONSUMED rows while
        still applying `expires_at > now()` would have closed the hole for those minutes
        and reopened it afterwards, which is worse than leaving it open: the same defect,
        now needing a wait to reproduce.

        The expiry test therefore applies to ACTIVE alone, where its own reason lives -- a
        lapsed hold a cleanup worker has not yet retired must not make stock look scarcer
        than it is. That reasoning has nothing to say about a completed sale.
        """
        sold = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        # Seeded already lapsed, so the row is CONSUMED *and* past its expiry -- the state
        # a real sold hold reaches a few minutes after the buyer paid.
        spent = seed_reservation(admin_engine, tenant, sold, expires_in_seconds=-5)
        with admin_engine.begin() as conn:
            conn.execute(
                text("UPDATE reservations SET status = 'CONSUMED' WHERE id = :i"),
                {"i": spent},
            )
        assert status_of(admin_engine, spent) == "CONSUMED"

        later = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=later,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )

        assert outcome.code is not RecoveryCode.OK, (
            "a sold unit came back onto the shelf once its original expiry passed"
        )

    def test_a_lapsed_hold_may_be_re_reserved(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """RESERVATION_EXPIRED tells the agent to re-reserve; that path must actually work."""
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        old = seed_reservation(admin_engine, tenant, checkout, expires_in_seconds=-5)

        with session.begin():
            set_tenant(session, tenant)
            outcome = reserve(
                session,
                checkout_id=checkout,
                checkout_version=1,
                ttl_seconds=300,
                allocations=[Allocation(scarcity_key=SKU, available_units=1)],
            )
            fresh = check_validity(session, checkout_id=checkout, checkout_version=1)

        assert outcome.code is RecoveryCode.OK
        assert fresh.code is RecoveryCode.OK
        assert outcome.reservation is not None
        assert fresh.reservation is not None
        # The new hold is what admission now sees, not the lapsed one.
        assert fresh.reservation.reservation_id == outcome.reservation.reservation_id
        assert fresh.reservation.reservation_id != old

    @pytest.mark.parametrize("ttl", [0, -1, res.MAX_TTL_SECONDS + 1])
    def test_unusable_ttl_is_rejected(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID, ttl: int
    ) -> None:
        """An unbounded hold withholds stock from every other buyer indefinitely."""
        checkout = seed_checkout(admin_engine, tenant, lines=[{"sku": SKU, "quantity": 1}])
        with pytest.raises(ReservationError):
            with session.begin():
                set_tenant(session, tenant)
                reserve(session, checkout_id=checkout, checkout_version=1, ttl_seconds=ttl)

    def test_allocation_rejects_a_negative_stock_figure(self) -> None:
        with pytest.raises(ReservationError):
            Allocation(scarcity_key=SKU, available_units=-1)

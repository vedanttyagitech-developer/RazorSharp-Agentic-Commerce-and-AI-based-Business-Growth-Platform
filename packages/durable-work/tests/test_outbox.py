"""Transactional outbox tests, specification 23.2.

These run against real PostgreSQL as ``commerce_test_kernel`` -- a NOSUPERUSER,
NOBYPASSRLS login role. SQLite has no ``FOR UPDATE SKIP LOCKED``, no row locks across
sessions and no server clock, and every invariant below is about one of those three.

The six invariants under test, and the failure each one prevents:

1. The command is written in the caller's transaction. A dual write to a broker would
   let the payment attempt commit while its command vanished in a crash: money moved,
   nothing left to act on it, and no record that anything is missing.
2. Two workers never take the same row. Without ``FOR UPDATE SKIP LOCKED`` a second
   worker blocks on the first and then re-reads a row already handed out -- a provider
   call made twice.
3. A lease expires on the database clock. A crashed worker would otherwise hold a refund
   command forever, and a pod with a skewed clock could steal or extend a live lease.
4. Retries are exponential, jittered and bounded. A fixed backoff synchronises every
   failed command onto the same instant and re-DDoSes a provider that is recovering; an
   unbounded attempt count never stops.
5. Exhaustion is a state with evidence, not a deletion. A dropped message turns a visible
   incident into an invisible one.
6. Delivery may duplicate. The tests pin the direction of failure: repeat the command,
   never skip it.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from commerce_domain import uuid7
from durable_work import outbox as ob
from durable_work.outbox import (
    DeadLetter,
    LeasedCommand,
    OutboxStatus,
    OutboxUsageError,
    RetryPolicy,
    backoff_seconds,
    complete,
    enqueue,
    extend_lease,
    fail,
    lease,
    reap_exhausted,
    revive,
)
from platform_db import TenantContextError, set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel.recovery import RecoveryCode

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Seeding and teardown run as the owner: application roles deliberately have no DELETE
# on any table, and granting one DELETE to make fixtures convenient would erase the
# guarantee that committed work cannot be removed through an application role.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

CMD = "PAYMENT_CREATE_ORDER"
#: A payload shaped like a real command: every number an integer, money in minor units.
PAYLOAD: dict[str, Any] = {"amount_minor": 149900, "currency": "INR", "receipt": "rcpt-1"}

#: Short enough that a test can outlive a lease without a long sleep, long enough that
#: PostgreSQL's clock, not scheduling noise, decides when it lapses.
SHORT_LEASE = RetryPolicy(lease_seconds=1)
LEASE_LAPSE_SLEEP = 1.5


def _require_db(url: str, *, pool_size: int = 1) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for outbox tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    """Eight connections: the exclusivity tests need genuinely separate sessions."""
    engine = _require_db(KERNEL_URL, pool_size=8)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    # A superuser bypasses row-level security unconditionally, so a suite run as one
    # would pass while proving nothing about the tenant scoping of these queries.
    assert row.rolsuper is False, "outbox tests must not run as a superuser"
    assert row.rolbypassrls is False, "outbox tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL, pool_size=2)


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


def _seed_tenant(admin: Engine) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    slug = f"ob-{tenant_id.hex[:8]}"
    with admin.begin() as conn:
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
            {"id": uuid.uuid4(), "t": tenant_id, "slug": slug, "name": slug},
        )
    return tenant_id


def _drop_tenant(admin: Engine, tenant_id: uuid.UUID) -> None:
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM checkout_versions WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM merchants WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


@pytest.fixture
def tenant(admin_engine: Engine) -> Iterator[uuid.UUID]:
    tenant_id = _seed_tenant(admin_engine)
    yield tenant_id
    _drop_tenant(admin_engine, tenant_id)


@pytest.fixture
def other_tenant(admin_engine: Engine) -> Iterator[uuid.UUID]:
    tenant_id = _seed_tenant(admin_engine)
    yield tenant_id
    _drop_tenant(admin_engine, tenant_id)


# ------------------------------------------------------------------------- helpers


def seed_checkout(admin: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    """One immutable checkout version, used as the caller state change enqueue rides on."""
    checkout_id: uuid.UUID = uuid7()
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
                "VALUES (:id, :t, :m, :c, 1, '{}'::jsonb, :h, 'INR', 149900, 'RESERVED', true)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "h": "hash-" + uuid.uuid4().hex[:16],
            },
        )
    return checkout_id


def put(session: Session, tenant_id: uuid.UUID, *, delay: int = 0) -> uuid.UUID:
    """Commit one PENDING command and return its id."""
    with session.begin():
        set_tenant(session, tenant_id)
        cmd = enqueue(
            session,
            command_type=CMD,
            payload=PAYLOAD,
            correlation_id=uuid.uuid4(),
            available_in_seconds=delay,
        )
    return cmd.command_id


def row_of(admin: Engine, command_id: uuid.UUID) -> Any:
    with admin.begin() as conn:
        return conn.execute(
            text(
                "SELECT status, attempts, payload, available_at, leased_until, now() AS db_now "
                "FROM outbox_events WHERE id = :i"
            ),
            {"i": command_id},
        ).one()


def count_rows(admin: Engine, tenant_id: uuid.UUID) -> int:
    with admin.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE tenant_id = :t"), {"t": tenant_id}
            ).scalar_one()
        )


def checkout_status(admin: Engine, checkout_id: uuid.UUID) -> str:
    with admin.begin() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM checkout_versions WHERE checkout_id = :c"),
                {"c": checkout_id},
            ).scalar_one()
        )


def make_ready(admin: Engine, command_id: uuid.UUID) -> None:
    """Pull a backed-off command's ``available_at`` into the past on the DATABASE clock.

    Used only to avoid sleeping through a real exponential backoff inside a test. It
    moves the schedule, never the decision: which rows are leasable is still computed by
    :func:`lease` from PostgreSQL's ``now()``.
    """
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET available_at = now() - INTERVAL '1 second' WHERE id = :i"
            ),
            {"i": command_id},
        )


def lease_one(
    session: Session, tenant_id: uuid.UUID, *, worker: str, policy: RetryPolicy
) -> LeasedCommand | None:
    with session.begin():
        set_tenant(session, tenant_id)
        got = lease(session, worker_id=worker, limit=1, policy=policy)
    return got[0] if got else None


# ------------------------------------------- invariant 1: one commit, never two


class TestEnqueueJoinsTheCallersTransaction:
    def test_source_never_commits_or_rolls_back(self) -> None:
        """The structural half of invariant 1.

        A behavioural test can only show that today's code path does not commit. This one
        fails the moment anyone adds a commit inside the module, which is the change that
        would silently reintroduce the dual write.
        """
        source = Path(ob.__file__).read_text(encoding="utf-8")
        forbidden = (".commit()", ".rollback()", ".begin()", "sessionmaker(", "create_engine(")
        found = [needle for needle in forbidden if needle in source]
        assert found == [], f"outbox.py must not manage transactions itself: {found}"

    def test_source_never_consults_an_application_clock(self) -> None:
        """Invariant 3, structurally. Every deadline in this module is PostgreSQL's."""
        source = Path(ob.__file__).read_text(encoding="utf-8")
        forbidden = (
            "datetime.now",
            "datetime.utcnow",
            "utcnow(",
            "date.today",
            "time.time",
            "time.monotonic",
            "import time",
        )
        found = [needle for needle in forbidden if needle in source]
        assert found == [], f"outbox.py must not read an application clock: {found}"

    def test_source_makes_no_model_call(self) -> None:
        """Agents propose; this module is one of the deterministic systems that execute."""
        source = Path(ob.__file__).read_text(encoding="utf-8")
        forbidden = ("anthropic", "openai", "llm", "prompt(", "completion(")
        found = [n for n in forbidden if n in source.lower()]
        assert found == [], f"outbox.py must contain no model call: {found}"

    def test_command_is_invisible_to_other_sessions_until_the_caller_commits(
        self, sessions: sessionmaker[Session], admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The load-bearing property: enqueue joins a transaction, it does not open one.

        A broker publish would already be visible here. That is the whole difference: a
        published message the caller then rolls back is a command acting on state that
        does not exist.
        """
        writer, reader = sessions(), sessions()
        try:
            writer.begin()
            set_tenant(writer, tenant)
            cmd = enqueue(writer, command_type=CMD, payload=PAYLOAD, correlation_id=uuid.uuid4())

            # A different connection, mid-flight. READ COMMITTED must see nothing.
            with reader.begin():
                set_tenant(reader, tenant)
                mid_flight = reader.execute(
                    text("SELECT count(*) FROM outbox_events WHERE id = :i"),
                    {"i": cmd.command_id},
                ).scalar_one()
            assert mid_flight == 0, "enqueue committed on its own; that is the dual write"

            writer.commit()

            with reader.begin():
                set_tenant(reader, tenant)
                after = reader.execute(
                    text("SELECT count(*) FROM outbox_events WHERE id = :i"),
                    {"i": cmd.command_id},
                ).scalar_one()
            assert after == 1
        finally:
            writer.rollback()
            writer.close()
            reader.rollback()
            reader.close()

    def test_rolling_back_the_state_change_takes_the_command_with_it(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The recoverability guarantee, in the negative direction.

        If the checkout never moved to AWAITING_PAYMENT, no worker may later try to pay
        for it. One transaction makes that impossible to get wrong.
        """
        checkout = seed_checkout(admin_engine, tenant)
        session.begin()
        set_tenant(session, tenant)
        session.execute(
            text("UPDATE checkout_versions SET status = 'AWAITING_PAYMENT' WHERE checkout_id = :c"),
            {"c": checkout},
        )
        enqueue(session, command_type=CMD, payload=PAYLOAD, correlation_id=uuid.uuid4())
        session.rollback()

        assert checkout_status(admin_engine, checkout) == "RESERVED"
        assert count_rows(admin_engine, tenant) == 0

    def test_committing_the_state_change_publishes_the_command_with_it(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        checkout = seed_checkout(admin_engine, tenant)
        with session.begin():
            set_tenant(session, tenant)
            session.execute(
                text(
                    "UPDATE checkout_versions SET status = 'AWAITING_PAYMENT' "
                    "WHERE checkout_id = :c"
                ),
                {"c": checkout},
            )
            enqueue(session, command_type=CMD, payload=PAYLOAD, correlation_id=uuid.uuid4())

        assert checkout_status(admin_engine, checkout) == "AWAITING_PAYMENT"
        assert count_rows(admin_engine, tenant) == 1

    def test_enqueue_refuses_a_session_with_no_tenant_bound(self, session: Session) -> None:
        with pytest.raises(TenantContextError):
            with session.begin():
                enqueue(session, command_type=CMD, payload=PAYLOAD, correlation_id=uuid.uuid4())

    def test_enqueue_refuses_a_float_in_the_payload(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Money in a command payload is integer minor units.

        ``1499.0`` here is a rupee amount that escaped Money. Committing it would hand a
        worker a float to send to a provider, and the rounding error would surface as a
        real charge for the wrong amount hours later, with a signed approval that no
        longer matches.
        """
        with pytest.raises(OutboxUsageError, match="not a valid command body"):
            with session.begin():
                set_tenant(session, tenant)
                enqueue(
                    session,
                    command_type=CMD,
                    payload={"amount": 1499.0, "currency": "INR"},
                    correlation_id=uuid.uuid4(),
                )
        assert count_rows(admin_engine, tenant) == 0

    def test_enqueue_refuses_a_payload_that_is_not_a_json_object(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        with pytest.raises(OutboxUsageError, match="JSON object"):
            with session.begin():
                set_tenant(session, tenant)
                enqueue(
                    session,
                    command_type=CMD,
                    payload=[1, 2, 3],  # type: ignore[arg-type]
                    correlation_id=uuid.uuid4(),
                )

    def test_enqueue_refuses_a_payload_that_is_not_json_at_any_depth(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The float rule holds inside nested objects and arrays, not just at the top.

        A command payload is nested in practice -- ``{"order": {"amount_minor": ...}}``
        -- so a validator that only checked the outer keys would let exactly the payload
        shape this platform actually sends slip through. A ``uuid.UUID`` is included
        because it is the other easy mistake: it looks JSON-ish, and it is not.
        """
        for bad in (
            {"order": {"amount_minor": 1499.0}},
            {"lines": [{"unit_price_minor": 10.5}]},
            {"grant_id": uuid.uuid4()},
        ):
            with pytest.raises(OutboxUsageError, match="not a valid command body"):
                with session.begin():
                    set_tenant(session, tenant)
                    enqueue(
                        session,
                        command_type=CMD,
                        payload=bad,
                        correlation_id=uuid.uuid4(),
                    )
        assert count_rows(admin_engine, tenant) == 0

    def test_enqueue_refuses_a_correlation_id_that_is_not_a_uuid(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """A domain error naming the field, not a driver error naming a parameter number.

        The correlation id is how an operator ties this command to the approval and the
        payment attempt it came from. A string that merely looks like one reaches the
        driver as an uncatchable ``InvalidTextRepresentation``, which tells the caller
        nothing about which of its arguments was wrong.
        """
        with pytest.raises(OutboxUsageError, match="correlation_id must be a UUID"):
            with session.begin():
                set_tenant(session, tenant)
                enqueue(
                    session,
                    command_type=CMD,
                    payload=PAYLOAD,
                    correlation_id="8f14e45f-ceea-467a-9575-1d5b7dcb4b3e",  # type: ignore[arg-type]
                )

    def test_enqueue_refuses_a_blank_command_type(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        with pytest.raises(OutboxUsageError, match="command_type"):
            with session.begin():
                set_tenant(session, tenant)
                enqueue(session, command_type="   ", payload=PAYLOAD, correlation_id=uuid.uuid4())

    def test_delayed_command_is_not_leasable_before_its_time(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        put(session, tenant, delay=300)
        assert lease_one(session, tenant, worker="w1", policy=SHORT_LEASE) is None


# --------------------------------------- invariant 2: two workers, never one row


class TestLeaseExclusivity:
    def test_a_row_locked_by_an_open_lease_is_skipped_not_re_handed(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID
    ) -> None:
        """The deterministic proof that SKIP LOCKED is doing the work.

        Worker one holds an uncommitted lease. Worker two's candidate scan still sees the
        pre-update row as ready. Without SKIP LOCKED it would block, then re-read the row
        worker one had already taken and deliver the same provider call twice.
        """
        first, second = sessions(), sessions()
        try:
            for _ in range(2):
                put(first, tenant)

            first.begin()
            set_tenant(first, tenant)
            a = lease(first, worker_id="w1", limit=1, policy=SHORT_LEASE)

            second.begin()
            set_tenant(second, tenant)
            b = lease(second, worker_id="w2", limit=1, policy=SHORT_LEASE)

            assert len(a) == 1
            assert len(b) == 1
            assert a[0].command_id != b[0].command_id, "the same command went to two workers"

            first.commit()
            second.commit()
        finally:
            for s in (first, second):
                s.rollback()
                s.close()

    def test_concurrent_workers_partition_the_queue_without_overlap(
        self, sessions: sessionmaker[Session], session: Session, tenant: uuid.UUID
    ) -> None:
        """Four real sessions, released together, taking from forty commands.

        The assertion is on the union: every command reaches exactly one worker. A
        duplicate here is a payment made twice.
        """
        total, workers, batch = 40, 4, 10
        for _ in range(total):
            put(session, tenant)

        barrier = threading.Barrier(workers)
        results: list[list[uuid.UUID]] = [[] for _ in range(workers)]
        errors: list[BaseException] = []

        def run(index: int) -> None:
            s = sessions()
            try:
                s.begin()
                set_tenant(s, tenant)
                barrier.wait(timeout=10)
                got = lease(s, worker_id=f"w{index}", limit=batch, policy=SHORT_LEASE)
                results[index] = [c.command_id for c in got]
                s.commit()
            except BaseException as exc:  # pragma: no cover - surfaced by the assertion
                errors.append(exc)
                s.rollback()
            finally:
                s.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"worker threads failed: {errors}"
        handed_out = [cid for batch_ids in results for cid in batch_ids]
        assert len(handed_out) == len(set(handed_out)), "a command was leased by two workers"
        assert len(handed_out) == total, "some commands were never handed out"

    def test_a_batch_never_exceeds_the_limit_it_asked_for(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Regression: the candidate set must be computed exactly once.

        Written as `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` this
        statement leased two rows for `limit=1` on PostgreSQL 16 under row-level
        security -- FOR UPDATE blocks the subquery from being hashed, so it can be
        re-executed per candidate row and take a fresh batch each time. A worker that
        over-leases owns commands it cannot finish before its lease lapses, and every
        excess row is then redelivered.
        """
        for _ in range(9):
            put(session, tenant)
        with session.begin():
            set_tenant(session, tenant)
            batch = lease(session, worker_id="w1", limit=3, policy=RetryPolicy())
        assert len(batch) == 3
        assert len({c.command_id for c in batch}) == 3

    def test_a_leased_command_is_not_offered_again_while_its_lease_is_live(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        put(session, tenant)
        assert lease_one(session, tenant, worker="w1", policy=RetryPolicy()) is not None
        assert lease_one(session, tenant, worker="w2", policy=RetryPolicy()) is None


# ------------------------------------------- invariant 3: the lease expires (DB clock)


class TestLeaseExpiry:
    def test_deadline_is_written_from_the_database_clock(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        command_id = put(session, tenant)
        lease_one(session, tenant, worker="w1", policy=RetryPolicy(lease_seconds=120))
        row = row_of(admin_engine, command_id)
        # Both sides come from PostgreSQL, so this holds on a pod whose clock is hours out.
        delta = (row.leased_until - row.db_now).total_seconds()
        assert 110 <= delta <= 120

    def test_a_crashed_workers_command_is_re_leased_once_the_deadline_passes(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Worker one takes the command and dies without reporting anything.

        Nothing in the system knows it died. Only the lapse of ``leased_until`` on the
        database clock releases the command -- otherwise a refund sits still forever.
        """
        command_id = put(session, tenant)
        first = lease_one(session, tenant, worker="w1", policy=SHORT_LEASE)
        assert first is not None
        assert first.attempts == 1

        time.sleep(LEASE_LAPSE_SLEEP)

        second = lease_one(session, tenant, worker="w2", policy=SHORT_LEASE)
        assert second is not None
        assert second.command_id == command_id, "the lapsed command was not re-offered"
        # Counted at hand-out, so a worker that crashes still consumes an attempt. If it
        # did not, a payload that kills every worker would loop forever.
        assert second.attempts == 2
        assert second.lease_token != first.lease_token

    def test_the_superseded_worker_cannot_complete_the_reassigned_command(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The fence. Worker one stalls, its lease lapses, worker two takes over.

        Worker one waking up and marking DONE would retire a command worker two is still
        executing, and worker two's own completion would then find nothing to write.
        """
        command_id = put(session, tenant)
        stalled = lease_one(session, tenant, worker="w1", policy=SHORT_LEASE)
        assert stalled is not None

        time.sleep(LEASE_LAPSE_SLEEP)
        taken_over = lease_one(session, tenant, worker="w2", policy=RetryPolicy())
        assert taken_over is not None

        with session.begin():
            set_tenant(session, tenant)
            stale = complete(session, command_id, stalled.lease_token)
        assert stale.code is RecoveryCode.CONCURRENT_OPERATION
        assert stale.status is OutboxStatus.LEASED
        assert row_of(admin_engine, command_id).status == "LEASED"

        with session.begin():
            set_tenant(session, tenant)
            live = complete(session, command_id, taken_over.lease_token)
        assert live.code is RecoveryCode.OK
        assert row_of(admin_engine, command_id).status == "DONE"

    def test_a_lapsed_lease_cannot_be_completed_even_when_nobody_took_over(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Fail towards redelivery, not towards a silent skip.

        The worker may have called the provider or not. Refusing the completion means the
        command runs again against an idempotent consumer; accepting it would retire a
        command that might never have executed.
        """
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=SHORT_LEASE)
        assert held is not None
        time.sleep(LEASE_LAPSE_SLEEP)

        with session.begin():
            set_tenant(session, tenant)
            outcome = complete(session, command_id, held.lease_token)
        assert outcome.code is RecoveryCode.CONCURRENT_OPERATION
        assert row_of(admin_engine, command_id).status == "LEASED"

        again = lease_one(session, tenant, worker="w2", policy=RetryPolicy())
        assert again is not None and again.command_id == command_id

    def test_extend_lease_rotates_the_token_and_keeps_the_command(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """A genuinely long command heartbeats instead of holding a long lease."""
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=SHORT_LEASE)
        assert held is not None

        with session.begin():
            set_tenant(session, tenant)
            renewed = extend_lease(
                session, command_id, held.lease_token, policy=RetryPolicy(lease_seconds=300)
            )
        assert renewed is not None
        assert renewed > held.lease_token

        time.sleep(LEASE_LAPSE_SLEEP)
        # The original one-second deadline is long gone; the heartbeat is what holds it.
        assert lease_one(session, tenant, worker="w2", policy=RetryPolicy()) is None

        with session.begin():
            set_tenant(session, tenant)
            assert complete(session, command_id, renewed).code is RecoveryCode.OK

    def test_extend_lease_refuses_a_lapsed_lease(self, session: Session, tenant: uuid.UUID) -> None:
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=SHORT_LEASE)
        assert held is not None
        time.sleep(LEASE_LAPSE_SLEEP)
        lease_one(session, tenant, worker="w2", policy=RetryPolicy())

        with session.begin():
            set_tenant(session, tenant)
            assert extend_lease(session, command_id, held.lease_token) is None


# ---------------------------------------- invariant 4: bounded, jittered exponential


class TestBackoff:
    def test_envelope_doubles_with_each_attempt(self) -> None:
        # jitter -> n-1 returns the top of the envelope, so the schedule is exact.
        top = RetryPolicy(
            backoff_base_seconds=2, backoff_cap_seconds=10_000, jitter=lambda n: n - 1
        )
        assert [backoff_seconds(a, top) for a in range(1, 6)] == [2, 4, 8, 16, 32]

    def test_envelope_is_capped(self) -> None:
        top = RetryPolicy(backoff_base_seconds=2, backoff_cap_seconds=60, jitter=lambda n: n - 1)
        assert backoff_seconds(10, top) == 60
        assert backoff_seconds(40, top) == 60, "a large attempt count must not overflow the cap"

    def test_the_doubling_exponent_is_capped(self) -> None:
        """The guard that stops ``2 ** attempts`` being computed before the cap bounds it.

        ``backoff_seconds`` is public, so ``attempts`` is not bounded by ``max_attempts``
        -- a caller replaying an attempt counter can hand it any integer. With a cap high
        enough that it never bites, the envelope must still stop doubling at
        ``_MAX_EXPONENT`` rather than evaluating ``2 ** 999_999``, which is a
        multi-second allocation inside what is supposed to be an arithmetic helper.

        Asserted against ``60`` in the test above only because the cap hides the
        difference; here the cap is out of the way, so the exponent bound is the only
        thing that can produce this answer.
        """
        wide = RetryPolicy(
            backoff_base_seconds=1, backoff_cap_seconds=2**48, jitter=lambda n: n - 1
        )
        ceiling = 2**ob._MAX_EXPONENT
        assert backoff_seconds(ob._MAX_EXPONENT + 1, wide) == ceiling
        assert backoff_seconds(1_000_000, wide) == ceiling, "the exponent is not bounded"

    def test_delay_is_never_zero(self) -> None:
        """A zero delay turns a permanently failing command into a hot loop."""
        floor = RetryPolicy(jitter=lambda _n: 0)
        assert all(backoff_seconds(a, floor) >= 1 for a in range(1, 12))

    def test_jitter_spreads_retries_across_the_whole_envelope(self) -> None:
        """Full jitter, not a fixed schedule.

        A thousand commands failed by one provider outage must not all retry at the same
        instant; that is how a recovering provider is knocked over a second time.
        """
        policy = RetryPolicy(backoff_base_seconds=2, backoff_cap_seconds=10_000)
        samples = [backoff_seconds(8, policy) for _ in range(200)]
        envelope = 2 * 2**7
        assert all(1 <= s <= envelope for s in samples), "jitter escaped its envelope"
        assert len(set(samples)) > 20, "delays are not actually jittered"
        assert min(samples) < envelope // 4, "jitter is not spread across the low end"

    def test_backoff_refuses_an_attempt_count_below_one(self) -> None:
        with pytest.raises(OutboxUsageError):
            backoff_seconds(0)

    def test_policy_refuses_a_lease_longer_than_the_maximum(self) -> None:
        with pytest.raises(OutboxUsageError):
            RetryPolicy(lease_seconds=ob.MAX_LEASE_SECONDS + 1)

    def test_policy_refuses_an_unbounded_attempt_count(self) -> None:
        with pytest.raises(OutboxUsageError):
            RetryPolicy(max_attempts=ob.MAX_ATTEMPT_CEILING + 1)

    def test_policy_refuses_a_cap_below_the_base(self) -> None:
        with pytest.raises(OutboxUsageError):
            RetryPolicy(backoff_base_seconds=60, backoff_cap_seconds=10)

    def test_a_failed_command_is_scheduled_into_the_future_by_the_database(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        policy = RetryPolicy(lease_seconds=60, max_attempts=5, jitter=lambda n: n - 1)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None

        with session.begin():
            set_tenant(session, tenant)
            outcome = fail(
                session,
                command_id,
                held.lease_token,
                code=RecoveryCode.PAYMENT_FAILED,
                policy=policy,
            )

        assert outcome.status is OutboxStatus.FAILED
        assert outcome.code is RecoveryCode.PAYMENT_FAILED
        row = row_of(admin_engine, command_id)
        assert row.status == "FAILED"
        assert row.available_at > row.db_now, "a failed command must not be immediately leasable"
        # attempts == 1 with jitter at the top of the envelope means base * 2^0 == 2s.
        assert 1 <= (row.available_at - row.db_now).total_seconds() <= 2

        # And it is genuinely withheld until then.
        assert lease_one(session, tenant, worker="w2", policy=policy) is None
        make_ready(admin_engine, command_id)
        assert lease_one(session, tenant, worker="w2", policy=policy) is not None

    def test_attempts_are_bounded(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Three attempts means three, then escalation -- not an indefinite provider loop."""
        policy = RetryPolicy(max_attempts=3, lease_seconds=60, jitter=lambda _n: 0)
        command_id = put(session, tenant)

        seen: list[int] = []
        for _ in range(6):
            held = lease_one(session, tenant, worker="w1", policy=policy)
            if held is None:
                break
            seen.append(held.attempts)
            with session.begin():
                set_tenant(session, tenant)
                fail(
                    session,
                    command_id,
                    held.lease_token,
                    code=RecoveryCode.PAYMENT_FAILED,
                    policy=policy,
                )
            make_ready(admin_engine, command_id)

        assert seen == [1, 2, 3]
        assert row_of(admin_engine, command_id).status == "DEAD"

    def test_the_attempt_bound_holds_when_more_commands_are_exhausted_than_one_batch(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The lease scan enforces the bound itself; reaping alone is not enough.

        Five commands whose workers all died on their final attempt, reaped two at a
        time. If only the reaper enforced the budget, the three rows it could not fit in
        this batch would be handed straight back out and attempt four, five and six would
        run -- an unbounded provider loop hiding behind a bounded-looking policy.
        """
        policy = RetryPolicy(max_attempts=1, lease_seconds=1)
        with session.begin():
            set_tenant(session, tenant)
            for _ in range(5):
                enqueue(session, command_type=CMD, payload=PAYLOAD, correlation_id=uuid.uuid4())
        with session.begin():
            set_tenant(session, tenant)
            assert len(lease(session, worker_id="w1", limit=5, policy=policy)) == 5
        time.sleep(LEASE_LAPSE_SLEEP)

        with session.begin():
            set_tenant(session, tenant)
            handed_out = lease(session, worker_id="w2", limit=2, policy=policy)

        assert handed_out == (), "an exhausted command was leased for another attempt"
        with admin_engine.begin() as conn:
            over_budget = conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE tenant_id = :t AND attempts > 1"),
                {"t": tenant},
            ).scalar_one()
        assert over_budget == 0, "the attempt bound was exceeded"


# ------------------------------ invariant 5: exhaustion is a state with evidence


class TestDeadLettering:
    def test_exhausting_attempts_buries_the_command_and_keeps_its_payload(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        policy = RetryPolicy(max_attempts=2, lease_seconds=60, jitter=lambda _n: 0)
        command_id = put(session, tenant)

        first = lease_one(session, tenant, worker="w1", policy=policy)
        assert first is not None
        with session.begin():
            set_tenant(session, tenant)
            interim = fail(
                session,
                command_id,
                first.lease_token,
                code=RecoveryCode.PAYMENT_FAILED,
                policy=policy,
            )
        assert interim.status is OutboxStatus.FAILED
        assert interim.dead_letter is None
        make_ready(admin_engine, command_id)

        second = lease_one(session, tenant, worker="w2", policy=policy)
        assert second is not None
        with session.begin():
            set_tenant(session, tenant)
            final = fail(
                session,
                command_id,
                second.lease_token,
                code=RecoveryCode.PAYMENT_FAILED,
                policy=policy,
            )

        assert final.status is OutboxStatus.DEAD
        assert final.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        letter = final.dead_letter
        assert letter is not None
        assert letter.attempts == 2
        assert letter.terminal_code is RecoveryCode.PAYMENT_FAILED
        # The evidence is the original request, verbatim: an operator has to be able to
        # see what was asked for and put it back unchanged.
        assert letter.payload == PAYLOAD

        row = row_of(admin_engine, command_id)
        assert row.status == "DEAD"
        assert row.payload == PAYLOAD
        assert count_rows(admin_engine, tenant) == 1, "the row was deleted instead of buried"

    def test_a_dead_command_is_never_handed_out_again(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        policy = RetryPolicy(max_attempts=1, lease_seconds=60, jitter=lambda _n: 0)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None
        with session.begin():
            set_tenant(session, tenant)
            fail(
                session,
                command_id,
                held.lease_token,
                code=RecoveryCode.PAYMENT_FAILED,
                policy=policy,
            )
        make_ready(admin_engine, command_id)
        assert lease_one(session, tenant, worker="w2", policy=RetryPolicy()) is None

    def test_an_unknown_payment_outcome_is_buried_immediately_not_retried(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """PAYMENT_UNKNOWN is deliberately absent from RETRYABLE.

        "We do not know whether the money moved" is not "it failed". Retrying it is how
        a buyer is charged twice for one order; it is reconciled by a human instead, and
        seven unused attempts must not change that.
        """
        policy = RetryPolicy(max_attempts=8, lease_seconds=60)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None

        with session.begin():
            set_tenant(session, tenant)
            outcome = fail(
                session,
                command_id,
                held.lease_token,
                code=RecoveryCode.PAYMENT_UNKNOWN,
                policy=policy,
            )

        assert outcome.status is OutboxStatus.DEAD
        assert outcome.dead_letter is not None
        assert outcome.dead_letter.terminal_code is RecoveryCode.PAYMENT_UNKNOWN
        assert outcome.dead_letter.attempts == 1
        assert row_of(admin_engine, command_id).status == "DEAD"

    def test_the_audit_hook_receives_the_dead_letter_exactly_once(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        policy = RetryPolicy(max_attempts=1, lease_seconds=60)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None

        recorded: list[DeadLetter] = []
        with session.begin():
            set_tenant(session, tenant)
            fail(
                session,
                command_id,
                held.lease_token,
                code=RecoveryCode.PAYMENT_FAILED,
                policy=policy,
                on_dead=recorded.append,
            )

        assert len(recorded) == 1
        assert recorded[0].command_id == command_id
        assert recorded[0].command_type == CMD
        assert recorded[0].correlation_id == held.correlation_id
        assert recorded[0].died_at is not None

    def test_a_burial_whose_audit_record_fails_does_not_commit(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Burial and its record are one transaction.

        If the burial could commit while the record of it failed, the platform would hold
        a command nobody will ever run and no evidence that it existed -- the silent loss
        the DEAD state exists to prevent, reintroduced through the back door.
        """
        policy = RetryPolicy(max_attempts=1, lease_seconds=120)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None

        def refuse(_: DeadLetter) -> None:
            raise RuntimeError("audit sink unavailable")

        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            with session.begin():
                set_tenant(session, tenant)
                fail(
                    session,
                    command_id,
                    held.lease_token,
                    code=RecoveryCode.PAYMENT_FAILED,
                    policy=policy,
                    on_dead=refuse,
                )

        assert row_of(admin_engine, command_id).status == "LEASED", (
            "the command was buried without its audit record"
        )

        # With a working sink the same call buries it, so nothing is stuck on the retry.
        with session.begin():
            set_tenant(session, tenant)
            outcome = fail(
                session,
                command_id,
                held.lease_token,
                code=RecoveryCode.PAYMENT_FAILED,
                policy=policy,
            )
        assert outcome.status is OutboxStatus.DEAD

    def test_a_worker_that_dies_on_its_last_attempt_is_reaped_not_stranded(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The stranding case, and why lease() reaps before it selects.

        This row's attempts are exhausted and its holder is gone. It is invisible to the
        lease predicate (attempts spent) and invisible to any dead-letter alert (status
        LEASED). Nothing would ever look at it again.
        """
        policy = RetryPolicy(max_attempts=1, lease_seconds=1)
        command_id = put(session, tenant)
        assert lease_one(session, tenant, worker="w1", policy=policy) is not None
        time.sleep(LEASE_LAPSE_SLEEP)

        buried: list[DeadLetter] = []
        with session.begin():
            set_tenant(session, tenant)
            handed_out = lease(
                session, worker_id="w2", limit=5, policy=policy, on_dead=buried.append
            )

        assert handed_out == ()
        assert len(buried) == 1
        assert buried[0].command_id == command_id
        assert buried[0].terminal_code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        assert row_of(admin_engine, command_id).status == "DEAD"

    def test_reaping_leaves_a_live_lease_alone(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """A worker on its final attempt is still working. Burying it would discard
        a command that is about to succeed and could not then be completed."""
        policy = RetryPolicy(max_attempts=1, lease_seconds=300)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None

        with session.begin():
            set_tenant(session, tenant)
            letters = reap_exhausted(session, policy=policy)

        assert letters == ()
        assert row_of(admin_engine, command_id).status == "LEASED"
        with session.begin():
            set_tenant(session, tenant)
            assert complete(session, command_id, held.lease_token).code is RecoveryCode.OK

    def test_fail_refuses_a_success_code(self, session: Session, tenant: uuid.UUID) -> None:
        """Reporting a success through the failure path would consume an attempt and
        schedule a retry of work that already happened."""
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=RetryPolicy())
        assert held is not None
        for code in (RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION):
            with pytest.raises(OutboxUsageError, match="complete"):
                with session.begin():
                    set_tenant(session, tenant)
                    fail(session, command_id, held.lease_token, code=code)

    def test_revive_returns_a_buried_command_to_the_queue_unchanged(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """DEAD is reversible. That is what makes it a state rather than a deletion."""
        policy = RetryPolicy(max_attempts=1, lease_seconds=60)
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=policy)
        assert held is not None
        with session.begin():
            set_tenant(session, tenant)
            fail(
                session,
                command_id,
                held.lease_token,
                code=RecoveryCode.PAYMENT_UNKNOWN,
                policy=policy,
            )

        with session.begin():
            set_tenant(session, tenant)
            outcome = revive(session, command_id)
        assert outcome.code is RecoveryCode.OK
        assert outcome.status is OutboxStatus.PENDING

        again = lease_one(session, tenant, worker="w2", policy=policy)
        assert again is not None
        assert again.command_id == command_id
        assert again.attempts == 1, "revive must restore the full attempt budget"
        assert again.payload == PAYLOAD, "revive must not rewrite the command"

    def test_revive_refuses_a_completed_command(self, session: Session, tenant: uuid.UUID) -> None:
        """Reviving a DONE command would re-run a completed money operation."""
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=RetryPolicy())
        assert held is not None
        with session.begin():
            set_tenant(session, tenant)
            complete(session, command_id, held.lease_token)

        with session.begin():
            set_tenant(session, tenant)
            outcome = revive(session, command_id)
        assert outcome.code is RecoveryCode.CONCURRENT_OPERATION
        assert outcome.status is OutboxStatus.DONE


# --------------------------------- invariant 6: at-least-once, consumers idempotent


class TestAtLeastOnceDelivery:
    def test_completing_an_already_completed_command_reports_a_duplicate(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """A duplicate is an expected outcome of at-least-once delivery, not an error.

        The worker is told the work is already done and moves on; it is never told the
        command failed, which would send it round the retry loop again.
        """
        command_id = put(session, tenant)
        held = lease_one(session, tenant, worker="w1", policy=RetryPolicy())
        assert held is not None
        with session.begin():
            set_tenant(session, tenant)
            assert complete(session, command_id, held.lease_token).code is RecoveryCode.OK
        with session.begin():
            set_tenant(session, tenant)
            repeat = complete(session, command_id, held.lease_token)
        assert repeat.code is RecoveryCode.DUPLICATE_OPERATION
        assert repeat.status is OutboxStatus.DONE

    def test_the_same_payload_can_be_delivered_more_than_once(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """The contract consumers must be built against.

        Two deliveries of one command, with identical payloads, because the first
        worker's lease lapsed before it reported. A consumer that is not idempotent
        double-charges here, and nothing in this module can prevent that.
        """
        command_id = put(session, tenant)
        first = lease_one(session, tenant, worker="w1", policy=SHORT_LEASE)
        assert first is not None
        time.sleep(LEASE_LAPSE_SLEEP)
        second = lease_one(session, tenant, worker="w2", policy=RetryPolicy())
        assert second is not None

        assert second.command_id == first.command_id == command_id
        assert second.payload == first.payload == PAYLOAD

    def test_a_command_buried_under_a_stalled_worker_reports_human_review(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The stalled worker must not be told 'retry'; the command is escalated."""
        policy = RetryPolicy(max_attempts=1, lease_seconds=1)
        command_id = put(session, tenant)
        stalled = lease_one(session, tenant, worker="w1", policy=policy)
        assert stalled is not None
        time.sleep(LEASE_LAPSE_SLEEP)
        with session.begin():
            set_tenant(session, tenant)
            reap_exhausted(session, policy=policy)

        with session.begin():
            set_tenant(session, tenant)
            outcome = complete(session, command_id, stalled.lease_token)
        assert outcome.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        assert outcome.status is OutboxStatus.DEAD


# ---------------------------------------------------------------- tenant isolation


class TestTenantIsolation:
    def test_a_worker_cannot_lease_another_tenants_command(
        self, session: Session, tenant: uuid.UUID, other_tenant: uuid.UUID
    ) -> None:
        """Row-level security scopes the lease. A multi-tenant worker binds one tenant
        per transaction; without this, one tenant's outage would drain another's queue."""
        put(session, tenant)
        assert lease_one(session, other_tenant, worker="w1", policy=RetryPolicy()) is None
        assert lease_one(session, tenant, worker="w1", policy=RetryPolicy()) is not None

    def test_lease_refuses_a_session_with_no_tenant_bound(self, session: Session) -> None:
        """Failing loudly beats returning an empty batch: an unbound tenant matches no
        rows under RLS, which reads as 'the queue is empty' rather than 'misconfigured'."""
        with pytest.raises(TenantContextError):
            with session.begin():
                lease(session, worker_id="w1", limit=1)

    def test_reap_refuses_a_session_with_no_tenant_bound(
        self, session: Session, admin_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """A sweeper is the one caller with nobody downstream to notice it did nothing.

        This command is exhausted, its worker is gone, and it is sitting in LEASED where
        no dead-letter alert looks. Run the sweep without a tenant bound and RLS matches
        no rows: an empty tuple is indistinguishable from a healthy queue, so the sweeper
        would report success on every pass forever while the command was never buried.
        """
        policy = RetryPolicy(max_attempts=1, lease_seconds=1)
        command_id = put(session, tenant)
        assert lease_one(session, tenant, worker="w1", policy=policy) is not None
        time.sleep(LEASE_LAPSE_SLEEP)

        with pytest.raises(TenantContextError):
            with session.begin():
                reap_exhausted(session, policy=policy)
        assert row_of(admin_engine, command_id).status == "LEASED", (
            "the unbound sweep must change nothing"
        )

        # Bound, the same call does the work the unbound one silently skipped.
        with session.begin():
            set_tenant(session, tenant)
            assert len(reap_exhausted(session, policy=policy)) == 1
        assert row_of(admin_engine, command_id).status == "DEAD"

    def test_lease_refuses_an_out_of_range_batch(self, session: Session, tenant: uuid.UUID) -> None:
        for bad in (0, ob.MAX_BATCH + 1):
            with pytest.raises(OutboxUsageError, match="limit"):
                with session.begin():
                    set_tenant(session, tenant)
                    lease(session, worker_id="w1", limit=bad)

    def test_lease_refuses_a_blank_worker_id(self, session: Session, tenant: uuid.UUID) -> None:
        with pytest.raises(OutboxUsageError, match="worker_id"):
            with session.begin():
                set_tenant(session, tenant)
                lease(session, worker_id="  ", limit=1)

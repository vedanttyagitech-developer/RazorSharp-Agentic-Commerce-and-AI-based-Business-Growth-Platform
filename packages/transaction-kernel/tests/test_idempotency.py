"""Idempotency records, specification 10.6.

Every test here is written to fail if a specific invariant breaks. The four that carry
the money are:

* ``TestReplay`` -- a duplicate returns the original result and the operation body is
  never entered a second time.
* ``TestKeyReuse`` -- the same key with a changed payload is refused outright. This is
  the retried-request-with-a-different-amount case, and both intuitive answers
  (re-execute, or return the old response) are wrong.
* ``TestTenantScoping`` -- one key string in two tenants is two records.
* ``TestConcurrency`` -- two real contending sessions race for one key on one
  PostgreSQL unique index; exactly one executes and the loser observes the winner.

These tests connect as ``commerce_test_kernel``, a NOSUPERUSER NOBYPASSRLS login role,
because a suite run as a superuser silently bypasses row-level security and proves
nothing about tenant scoping.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from commerce_domain import (
    NOT_A_SUCCESS,
    RETRYABLE,
    CanonicalizationError,
    RecoveryCode,
    canonical_hash,
    uuid7,
)
from platform_db import TenantContextError, set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel import Operation
from transaction_kernel.idempotency import (
    MAX_KEY_LENGTH,
    IdempotencyInFlightError,
    IdempotencyKeyReuseError,
    IdempotencyUsageError,
    IdempotentReplayError,
    execute_once,
    idempotent,
    read_idempotency_record,
)

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Application roles deliberately have no DELETE on any table, so fixtures seed and clean
# up through an owner connection. Granting the kernel role DELETE to make tests tidy
# would erase the guarantee the privilege separation exists to provide.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

OP = Operation.PAYMENT_CREATE_ORDER


def request_for(amount_minor: int = 39_500) -> dict[str, Any]:
    """A realistic create-order request. Integer minor units only, as everywhere."""
    return {
        "checkout_id": "0192f3c4-1111-7000-8000-000000000001",
        "checkout_version": 1,
        "amount_minor": amount_minor,
        "currency": "INR",
    }


ORIGINAL_RESPONSE: dict[str, Any] = {
    "provider_order_id": "order_TESTONLY0001",
    "amount_minor": 39_500,
    "currency": "INR",
}


def new_key(label: str) -> str:
    """A fresh key per test, so a failure cannot poison a later run."""
    return f"{label}-{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------------- fixtures


def _require_db(url: str) -> Engine:
    engine = create_engine(url, future=True, pool_size=5, max_overflow=2)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for idempotency tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        isolation = conn.execute(text("SHOW default_transaction_isolation")).scalar_one()
    assert row.rolsuper is False, "idempotency tests must not run as a superuser"
    assert row.rolbypassrls is False, "idempotency tests must not run as a BYPASSRLS role"
    # The lose-the-race path re-reads the winner's committed row; that only works if
    # each statement takes a fresh snapshot.
    assert isolation == "read committed", f"module requires READ COMMITTED, got {isolation}"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    """Owner connection, used only to seed and remove fixture data."""
    return _require_db(ADMIN_URL)


def _make_tenant(conn: Any) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO tenants (id, slug, name, home_region) "
            "VALUES (:id, :slug, :name, 'asia-south1')"
        ),
        {"id": tenant_id, "slug": f"t-{tenant_id.hex[:8]}", "name": f"t-{tenant_id.hex[:8]}"},
    )
    return tenant_id


def _drop_tenants(admin_engine: Engine, tenant_ids: tuple[uuid.UUID, ...]) -> None:
    with admin_engine.begin() as conn:
        for tenant_id in tenant_ids:
            # idempotency_records has FORCE ROW LEVEL SECURITY, so even the table owner
            # needs the tenant bound before its rows are visible to a DELETE.
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            conn.execute(
                text("DELETE FROM idempotency_records WHERE tenant_id = :t"), {"t": tenant_id}
            )
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": list(tenant_ids)})


@pytest.fixture
def tenant(admin_engine: Engine) -> Iterator[uuid.UUID]:
    with admin_engine.begin() as conn:
        tenant_id = _make_tenant(conn)
    yield tenant_id
    _drop_tenants(admin_engine, (tenant_id,))


@pytest.fixture
def two_tenants(admin_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    with admin_engine.begin() as conn:
        a, b = _make_tenant(conn), _make_tenant(conn)
    yield a, b
    _drop_tenants(admin_engine, (a, b))


@pytest.fixture
def session(kernel_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _session(engine: Engine) -> Session:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


class _Recorder:
    """Stands in for a provider call, and remembers whether it was made."""

    def __init__(self, response: Mapping[str, Any] | None = None) -> None:
        self.calls = 0
        self._response = dict(response or ORIGINAL_RESPONSE)

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return dict(self._response)


def _seed(session: Session, tenant_id: uuid.UUID, key: str, recorder: _Recorder) -> None:
    """First, successful use of ``key``, committed."""
    with session.begin():
        set_tenant(session, tenant_id)
        outcome = execute_once(session, key, OP, request_for(), recorder)
    assert outcome.executed is True
    assert outcome.code is RecoveryCode.OK


# -------------------------------------------------------------------------- the tests


class TestFirstUse:
    def test_first_use_executes_once_and_stores_the_response(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        key = new_key("first")
        recorder = _Recorder()
        _seed(session, tenant, key, recorder)

        assert recorder.calls == 1
        with session.begin():
            set_tenant(session, tenant)
            view = read_idempotency_record(session, key)
        assert view is not None
        assert view.response == ORIGINAL_RESPONSE
        assert view.request_hash == canonical_hash(request_for())
        assert view.operation == str(OP)
        assert view.is_complete is True

    def test_an_empty_response_still_completes_the_record(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """``{}`` must not be confused with "claimed but unfinished".

        A NULL response means the outcome is unknown and blocks every future retry of
        the key. An operation with no payload must not land in that state.
        """
        key = new_key("empty")
        with session.begin():
            set_tenant(session, tenant)
            with idempotent(session, key, OP, request_for()) as slot:
                slot.store({})

        with session.begin():
            set_tenant(session, tenant)
            view = read_idempotency_record(session, key)
            assert view is not None
            assert view.response == {}
            assert view.is_complete is True
            # And it replays as a duplicate, not as an unknown outcome.
            replay = execute_once(session, key, OP, request_for(), _Recorder())
        assert replay.code is RecoveryCode.DUPLICATE_OPERATION
        assert replay.executed is False
        assert replay.response == {}

    def test_created_at_comes_from_the_database_clock(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """A pod with a skewed clock must not be able to stamp financial evidence.

        PostgreSQL's ``now()`` is the transaction timestamp, so a row whose default
        filled ``created_at`` carries exactly the value ``SELECT now()`` returns in the
        same transaction. An application-supplied timestamp could not match it.
        """
        key = new_key("clock")
        with session.begin():
            set_tenant(session, tenant)
            db_now = session.execute(text("SELECT now()")).scalar_one()
            execute_once(session, key, OP, request_for(), _Recorder())

        with session.begin():
            set_tenant(session, tenant)
            view = read_idempotency_record(session, key)
        assert view is not None
        assert view.created_at == db_now


class TestReplay:
    def test_duplicate_returns_the_original_response_and_does_not_re_execute(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        key = new_key("replay")
        recorder = _Recorder()
        _seed(session, tenant, key, recorder)

        with session.begin():
            set_tenant(session, tenant)
            replay = execute_once(session, key, OP, request_for(), recorder)

        assert recorder.calls == 1, "the operation body ran twice"
        assert replay.executed is False
        assert replay.response == ORIGINAL_RESPONSE
        assert replay.code is RecoveryCode.DUPLICATE_OPERATION

    def test_the_guarded_block_is_never_entered_on_a_replay(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """The context manager refuses before the body, not after.

        A returned "you already did this" flag can be ignored; entry into the block
        cannot be undone once a provider call has left the process.
        """
        key = new_key("noentry")
        _seed(session, tenant, key, _Recorder())

        entered = False
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotentReplayError):
                with idempotent(session, key, OP, request_for()):
                    entered = True  # pragma: no cover - the assertion below proves it
        assert entered is False

    def test_replay_survives_many_repetitions(self, session: Session, tenant: uuid.UUID) -> None:
        key = new_key("many")
        recorder = _Recorder()
        _seed(session, tenant, key, recorder)

        for _ in range(5):
            with session.begin():
                set_tenant(session, tenant)
                outcome = execute_once(session, key, OP, request_for(), recorder)
            assert outcome.executed is False
            assert outcome.response == ORIGINAL_RESPONSE
        assert recorder.calls == 1

    def test_duplicate_operation_may_be_reported_as_a_completed_action(self) -> None:
        """A true replay describes an operation that really happened.

        The shared recovery contract lets a caller present ``DUPLICATE_OPERATION`` as a
        completed money action; this test pins that classification so the key-reuse
        refusal below cannot quietly be given the same code.
        """
        assert RecoveryCode.DUPLICATE_OPERATION not in NOT_A_SUCCESS

    def test_json_object_key_order_does_not_change_request_identity(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Canonicalization, not raw bytes, decides sameness.

        A client that reserialized the same request with different key order is
        retrying, not sending a new request, and must get its original result back.
        """
        key = new_key("order")
        first = {"items": [{"sku": "A", "qty": 2}], "total_minor": 39_500, "currency": "INR"}
        reordered = {"currency": "INR", "total_minor": 39_500, "items": [{"qty": 2, "sku": "A"}]}
        recorder = _Recorder()

        with session.begin():
            set_tenant(session, tenant)
            execute_once(session, key, OP, first, recorder)
        with session.begin():
            set_tenant(session, tenant)
            outcome = execute_once(session, key, OP, reordered, recorder)

        assert recorder.calls == 1
        assert outcome.code is RecoveryCode.DUPLICATE_OPERATION


class TestKeyReuse:
    """Same key, different payload. The case that stops a changed amount executing."""

    def test_a_changed_amount_under_the_same_key_is_refused(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        key = new_key("tamper")
        recorder = _Recorder()
        _seed(session, tenant, key, recorder)

        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError) as err:
                # 395.00 became 3950.00 while the key stayed the same.
                execute_once(session, key, OP, request_for(395_000), recorder)
            # The stored record is untouched by the rejected attempt.
            view = read_idempotency_record(session, key)

        assert recorder.calls == 1, "a request with a changed amount re-executed"
        assert err.value.code is RecoveryCode.POLICY_EXCEPTION
        assert view is not None
        assert view.response == ORIGINAL_RESPONSE
        assert view.request_hash == canonical_hash(request_for())

    def test_the_refusal_cannot_be_presented_as_a_completed_money_action(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Nothing completed, so the code must not be one a caller may render as success.

        ``DUPLICATE_OPERATION`` would be: it is excluded from ``NOT_A_SUCCESS``. Using it
        here would tell the caller that 3950.00 succeeded when 395.00 is what happened.
        """
        key = new_key("notsuccess")
        _seed(session, tenant, key, _Recorder())

        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError) as err:
                execute_once(session, key, OP, request_for(395_000), _Recorder())

        assert err.value.code in NOT_A_SUCCESS
        assert err.value.code not in RETRYABLE, "retrying the same key cannot help"
        assert err.value.code is not RecoveryCode.DUPLICATE_OPERATION

    def test_the_refusal_does_not_disclose_the_stored_response(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """The caller asked a different question; it does not get the other answer.

        A caller that logged a disclosed response as its own outcome would file the
        wrong provider order against the wrong request.
        """
        key = new_key("nodisclose")
        _seed(session, tenant, key, _Recorder())

        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError) as err:
                execute_once(session, key, OP, request_for(395_000), _Recorder())

        assert not hasattr(err.value, "response")
        assert ORIGINAL_RESPONSE["provider_order_id"] not in str(err.value)

    def test_the_same_key_for_a_different_operation_is_refused(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        key = new_key("otherop")
        recorder = _Recorder()
        _seed(session, tenant, key, recorder)

        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError) as err:
                execute_once(session, key, Operation.REFUND_EXECUTE, request_for(), recorder)

        assert recorder.calls == 1
        assert err.value.stored_operation == str(OP)

    def test_array_order_is_material(self, session: Session, tenant: uuid.UUID) -> None:
        """JSON arrays are ordered, so reordering one is a different request.

        Two line items swapped can mean two different shipments or two different
        allocations; treating that as a retry would return a result for a cart the
        caller did not send.
        """
        key = new_key("array")
        recorder = _Recorder()
        with session.begin():
            set_tenant(session, tenant)
            execute_once(session, key, OP, {"items": ["sku-a", "sku-b"]}, recorder)

        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError):
                execute_once(session, key, OP, {"items": ["sku-b", "sku-a"]}, recorder)
        assert recorder.calls == 1

    def test_an_added_field_is_material(self, session: Session, tenant: uuid.UUID) -> None:
        key = new_key("extra")
        recorder = _Recorder()
        _seed(session, tenant, key, recorder)

        altered = request_for()
        altered["coupon"] = "FREESHIP"
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError):
                execute_once(session, key, OP, altered, recorder)
        assert recorder.calls == 1


class TestTenantScoping:
    def test_the_same_key_string_in_two_tenants_is_two_records(
        self, session: Session, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """One tenant's key must never satisfy or block another's.

        The unique constraint is ``(tenant_id, idem_key)``; a constraint on ``idem_key``
        alone would let tenant B's retry return tenant A's provider order.
        """
        a, b = two_tenants
        key = "shared-key-across-tenants"
        recorder_a = _Recorder({"provider_order_id": "order_TENANT_A"})
        recorder_b = _Recorder({"provider_order_id": "order_TENANT_B"})

        with session.begin():
            set_tenant(session, a)
            out_a = execute_once(session, key, OP, request_for(), recorder_a)
        with session.begin():
            set_tenant(session, b)
            out_b = execute_once(session, key, OP, request_for(), recorder_b)

        assert out_a.executed is True
        assert out_b.executed is True, "tenant B was blocked by tenant A's key"
        assert recorder_a.calls == 1
        assert recorder_b.calls == 1

        with session.begin():
            set_tenant(session, a)
            replay_a = execute_once(session, key, OP, request_for(), recorder_a)
        with session.begin():
            set_tenant(session, b)
            replay_b = execute_once(session, key, OP, request_for(), recorder_b)

        assert replay_a.response == {"provider_order_id": "order_TENANT_A"}
        assert replay_b.response == {"provider_order_id": "order_TENANT_B"}

    def test_a_key_used_in_one_tenant_is_invisible_in_another(
        self, session: Session, two_tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        a, b = two_tenants
        key = new_key("private")
        with session.begin():
            set_tenant(session, a)
            execute_once(session, key, OP, request_for(), _Recorder())

        with session.begin():
            set_tenant(session, b)
            assert read_idempotency_record(session, key) is None

    def test_no_tenant_bound_is_refused_rather_than_written_unscoped(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Without a tenant, row-level security would filter every read away.

        Failing loudly beats writing a record nobody can ever read back, which would
        present as "the idempotency table stopped working".
        """
        recorder = _Recorder()
        with session.begin():
            with pytest.raises(TenantContextError):
                execute_once(session, new_key("notenant"), OP, request_for(), recorder)
        assert recorder.calls == 0


class TestConcurrency:
    def test_concurrent_first_use_has_exactly_one_winner(
        self, kernel_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Two real sessions contend for one key on one unique index.

        The loser's INSERT blocks until the winner's transaction ends, then raises
        ``unique_violation``; this module rolls back to its savepoint, re-reads and
        returns the winner's response. If single-winner were left to application code,
        both would execute and the buyer would hold two provider orders.
        """
        key = new_key("race")
        request = request_for()
        gate = threading.Barrier(2, timeout=30)
        guard = threading.Lock()
        executed: list[str] = []
        results: dict[str, Any] = {}

        def worker(name: str) -> None:
            db = _session(kernel_engine)
            try:
                gate.wait()
                with db.begin():
                    set_tenant(db, tenant)

                    def run() -> dict[str, Any]:
                        with guard:
                            executed.append(name)
                        # Widen the window so the other session is genuinely blocked on
                        # the index rather than merely arriving late.
                        time.sleep(0.4)
                        return {"winner": name}

                    results[name] = execute_once(db, key, OP, request, run)
            except BaseException as exc:  # noqa: BLE001 - reported through results
                results[name] = exc
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a contending session deadlocked"

        for name, value in results.items():
            assert not isinstance(value, BaseException), f"{name} failed: {value!r}"

        assert len(executed) == 1, f"the operation ran {len(executed)} times: {executed}"
        winner = executed[0]
        loser = "beta" if winner == "alpha" else "alpha"

        assert results[winner].executed is True
        assert results[winner].code is RecoveryCode.OK
        assert results[loser].executed is False
        assert results[loser].code is RecoveryCode.DUPLICATE_OPERATION
        assert results[loser].response == {"winner": winner}, "the loser invented its own result"

        db = _session(kernel_engine)
        try:
            with db.begin():
                set_tenant(db, tenant)
                count = db.execute(
                    text(
                        "SELECT count(*) FROM idempotency_records "
                        "WHERE tenant_id = :t AND idem_key = :k"
                    ),
                    {"t": tenant, "k": key},
                ).scalar_one()
        finally:
            db.close()
        assert count == 1

    def test_a_second_claim_blocks_while_the_first_is_uncommitted(
        self, kernel_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """Serialization is PostgreSQL's, not this module's.

        Session A holds an uncommitted claim. Session B cannot slip past it: its INSERT
        waits on the unique index and is only released when A ends. Here B is given a
        short lock timeout so the wait is observable instead of indefinite.
        """
        key = new_key("block")
        request = request_for()
        a = _session(kernel_engine)
        b = _session(kernel_engine)
        try:
            with a.begin():
                set_tenant(a, tenant)
                with idempotent(a, key, OP, request) as slot:
                    b.begin()
                    try:
                        set_tenant(b, tenant)
                        b.execute(text("SET LOCAL lock_timeout = '400ms'"))
                        b.execute(text("SET LOCAL statement_timeout = '2s'"))
                        with pytest.raises(OperationalError) as err:
                            execute_once(b, key, OP, request, _Recorder())
                        assert "timeout" in str(err.value).lower()
                    finally:
                        b.rollback()
                    slot.store({"winner": "a"})

            # A has committed. B now sees the winner rather than claiming the key.
            with b.begin():
                set_tenant(b, tenant)
                outcome = execute_once(b, key, OP, request, _Recorder())
            assert outcome.executed is False
            assert outcome.response == {"winner": "a"}
        finally:
            a.close()
            b.close()

    def test_a_non_unique_integrity_error_is_not_reported_as_a_duplicate(
        self, kernel_engine: Engine
    ) -> None:
        """Only SQLSTATE 23505 means "another writer claimed this key".

        A foreign-key violation and a row-level-security ``WITH CHECK`` failure are also
        ``IntegrityError``. If the handler matched on the exception class instead of the
        SQLSTATE, a write that was rejected for being mis-scoped would be re-read and
        reported as somebody else's completed operation -- a failed write answered with a
        success. Here the tenant bound to the transaction does not exist in ``tenants``,
        so the INSERT fails 23503 and must surface as itself.
        """
        ghost_tenant = uuid.uuid4()  # never inserted, so the tenant_id FK cannot resolve
        recorder = _Recorder()
        db = _session(kernel_engine)
        try:
            with db.begin():
                set_tenant(db, ghost_tenant)
                with pytest.raises(IntegrityError) as err:
                    execute_once(db, new_key("fkviolation"), OP, request_for(), recorder)
        finally:
            db.rollback()
            db.close()

        assert getattr(err.value.orig, "sqlstate", None) == "23503", (
            "expected a foreign-key violation, not a unique violation"
        )
        assert recorder.calls == 0

    def test_a_committed_claim_without_a_response_is_an_unknown_outcome(
        self, admin_engine: Engine, session: Session, tenant: uuid.UUID
    ) -> None:
        """A crash between claim and result must not become a silent re-run.

        The provider may or may not have been reached, so the answer is the same as any
        unknown outcome: read current state and reconcile. Never execute.
        """
        key = new_key("inflight")
        with admin_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
            conn.execute(
                text(
                    "INSERT INTO idempotency_records "
                    "(id, tenant_id, idem_key, operation, request_hash) "
                    "VALUES (:id, :t, :k, :op, :h)"
                ),
                {
                    "id": uuid7(),
                    "t": tenant,
                    "k": key,
                    "op": str(OP),
                    "h": canonical_hash(request_for()),
                },
            )

        recorder = _Recorder()
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyInFlightError) as err:
                execute_once(session, key, OP, request_for(), recorder)

        assert recorder.calls == 0
        assert err.value.code is RecoveryCode.CONCURRENT_OPERATION
        assert err.value.code in RETRYABLE

    def test_a_mismatched_payload_beats_the_in_flight_check(
        self, admin_engine: Engine, session: Session, tenant: uuid.UUID
    ) -> None:
        """Content is checked first, unconditionally.

        A caller whose payload does not match must learn nothing about the stored
        operation's state, not even that it is still running.
        """
        key = new_key("inflight-mismatch")
        with admin_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
            conn.execute(
                text(
                    "INSERT INTO idempotency_records "
                    "(id, tenant_id, idem_key, operation, request_hash) "
                    "VALUES (:id, :t, :k, :op, :h)"
                ),
                {
                    "id": uuid7(),
                    "t": tenant,
                    "k": key,
                    "op": str(OP),
                    "h": canonical_hash(request_for()),
                },
            )

        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyKeyReuseError):
                execute_once(session, key, OP, request_for(395_000), _Recorder())


class TestAtomicity:
    def test_a_failing_block_leaves_no_claim_even_if_the_caller_commits(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """A claim must never outlive the operation it claimed.

        A committed claim with no response wedges the key: every later retry is refused
        as an unknown outcome. So the claim is rolled back with the block that failed,
        even when the caller swallows the error and commits the surrounding transaction.
        """
        key = new_key("boom")
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(RuntimeError, match="provider exploded"):
                with idempotent(session, key, OP, request_for()):
                    raise RuntimeError("provider exploded")
            assert read_idempotency_record(session, key) is None

        recorder = _Recorder()
        with session.begin():
            set_tenant(session, tenant)
            outcome = execute_once(session, key, OP, request_for(), recorder)
        assert outcome.executed is True, "a failed attempt permanently burned the key"
        assert recorder.calls == 1

    def test_rolling_back_the_caller_transaction_releases_the_key(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """The record commits with the effect it describes, or not at all."""
        key = new_key("rollback")
        session.begin()
        try:
            set_tenant(session, tenant)
            with idempotent(session, key, OP, request_for()) as slot:
                slot.store(ORIGINAL_RESPONSE)
        finally:
            session.rollback()

        with session.begin():
            set_tenant(session, tenant)
            assert read_idempotency_record(session, key) is None

    def test_a_slot_that_never_stores_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        key = new_key("nostore")
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyUsageError, match="never given a response"):
                with idempotent(session, key, OP, request_for()):
                    pass
            assert read_idempotency_record(session, key) is None


class TestSlotDiscipline:
    def test_a_response_carrying_a_float_is_refused_before_it_is_stored(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Money that escaped the Money type must not be frozen into a replay.

        A float stored here would be returned to every future duplicate as the
        authoritative result of a money movement, with no way to tell it was rounded.
        """
        key = new_key("float")
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(CanonicalizationError):
                with idempotent(session, key, OP, request_for()) as slot:
                    slot.store({"amount": 395.00})
            assert read_idempotency_record(session, key) is None

    def test_a_request_carrying_a_float_is_refused(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        recorder = _Recorder()
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(CanonicalizationError):
                execute_once(session, new_key("floatreq"), OP, {"amount": 3.5}, recorder)
        assert recorder.calls == 0

    def test_a_slot_stores_exactly_once(self, session: Session, tenant: uuid.UUID) -> None:
        key = new_key("twice")
        with session.begin():
            set_tenant(session, tenant)
            with idempotent(session, key, OP, request_for()) as slot:
                slot.store(ORIGINAL_RESPONSE)
                with pytest.raises(IdempotencyUsageError, match="already stored"):
                    slot.store({"provider_order_id": "order_SECOND"})

        with session.begin():
            set_tenant(session, tenant)
            view = read_idempotency_record(session, key)
        assert view is not None
        assert view.response == ORIGINAL_RESPONSE

    def test_a_non_mapping_response_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        key = new_key("nonmap")
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyUsageError, match="JSON object mapping"):
                with idempotent(session, key, OP, request_for()) as slot:
                    slot.store(["not", "an", "object"])  # type: ignore[arg-type]


class TestUsageGuards:
    def test_an_over_long_key_is_refused_rather_than_truncated(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Silent truncation would make two different operations share one record."""
        recorder = _Recorder()
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyUsageError, match="characters"):
                execute_once(session, "k" * (MAX_KEY_LENGTH + 1), OP, request_for(), recorder)
        assert recorder.calls == 0

    def test_a_blank_key_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(IdempotencyUsageError, match="empty or blank"):
                execute_once(session, "   ", OP, request_for(), _Recorder())

    def test_a_key_at_the_column_limit_is_accepted(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        key = "k" * MAX_KEY_LENGTH
        with session.begin():
            set_tenant(session, tenant)
            outcome = execute_once(session, key, OP, request_for(), _Recorder())
        assert outcome.executed is True

    def test_use_outside_a_transaction_is_refused(
        self, kernel_engine: Engine, tenant: uuid.UUID
    ) -> None:
        """The record must commit with its effect, which requires the caller's transaction."""
        db = _session(kernel_engine)
        try:
            assert db.in_transaction() is False
            with pytest.raises(IdempotencyUsageError, match="active transaction"):
                execute_once(db, new_key("notx"), OP, request_for(), _Recorder())
        finally:
            db.close()

    def test_lookup_reports_an_unused_key_as_none(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        with session.begin():
            set_tenant(session, tenant)
            assert read_idempotency_record(session, new_key("unused")) is None

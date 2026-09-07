"""Adversarial idempotency: many callers, one key, contradictory payloads.

The three outcomes in specification 10.6 are easy to get right one caller at a time. This
file hammers them concurrently, because the interesting failures are all races:

* twelve callers, one key, one payload -- exactly one may execute, and the other eleven
  must read back *that* caller's answer, not their own;
* twelve callers, one key, twelve *different* payloads -- exactly one may execute, and the
  losers must be refused rather than told about a request they did not make. This is the
  row that costs money if it is wrong: returning the stored response would tell a caller
  that its 3950.00 succeeded when what ran was 395.00;
* a mixture of both, which is what a retrying proxy in front of a buggy client looks like;
* a caller whose guarded block raises -- the key must stay usable, or a transient fault
  wedges a buyer's purchase for good.

The claim under test is not "the code has an if for this". It is that PostgreSQL's unique
index decides the winner, so every caller here is a real session on a real connection.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from admission_support import SET_TENANT, Fixture
from adversarial_support import count, race
from commerce_domain import uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel.contracts import Operation
from transaction_kernel.idempotency import (
    IdempotencyInFlightError,
    IdempotencyKeyReuseError,
    IdempotencyUsageError,
    IdempotentReplayError,
    execute_once,
    idempotent,
    read_idempotency_record,
)
from transaction_kernel.recovery import RecoveryCode

pytestmark = pytest.mark.db

OPERATION = Operation.PAYMENT_CREATE_ORDER


@pytest.fixture(autouse=True)
def _purge_idempotency_records(adm_admin_engine: Engine, admissible: Fixture):
    """Remove this suite's records before the shared fixture removes the tenant.

    ``conftest.admissible`` deletes the tables the admission suite writes, and
    ``idempotency_records`` is not among them -- nothing in that suite creates one. A
    fixture requesting ``admissible`` is set up after it and finalised before it, so this
    runs at exactly the right moment. Cleaning up here rather than widening the shared
    fixture keeps this suite's mess its own problem.
    """
    yield
    with adm_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
        conn.execute(
            text("DELETE FROM idempotency_records WHERE tenant_id = :t"),
            {"t": admissible.tenant_id},
        )


def _records(admin_engine: Engine, tenant_id: uuid.UUID, key: str) -> int:
    return count(
        admin_engine,
        tenant_id,
        "SELECT count(*) FROM idempotency_records WHERE idem_key = :k",
        k=key,
    )


class TestSameKeySamePayload:
    def test_twelve_callers_execute_the_operation_exactly_once(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        key = f"adv-same-{uuid7().hex[:16]}"
        request = {"amount_minor": 39500, "currency": "INR"}
        ran: list[int] = []

        def body(session: Session, index: int) -> dict[str, Any]:
            def run() -> dict[str, Any]:
                ran.append(index)
                return {"order_id": f"order_by_{index}", "amount_minor": 39500}

            outcome = execute_once(session, key, OPERATION, request, run)
            return {"code": outcome.code, "executed": outcome.executed, "resp": outcome.response}

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=12)
        crashed = [o for o in outcomes if not o.ok]
        assert not crashed, f"a caller crashed: {crashed[0].error!r}"

        executed = [o.value for o in outcomes if o.value["executed"]]
        assert len(executed) == 1, f"the guarded block ran {len(executed)} times"
        assert len(ran) == 1, f"run() was invoked {len(ran)} times: {ran}"
        assert _records(adm_admin_engine, admissible.tenant_id, key) == 1

        winner = executed[0]["resp"]
        replays = [o.value for o in outcomes if not o.value["executed"]]
        assert len(replays) == 11
        for replay in replays:
            assert replay["code"] is RecoveryCode.DUPLICATE_OPERATION
            assert replay["resp"] == winner, (
                "a replay returned something other than the original answer; the key's "
                "whole promise is that the second caller learns what the first one did"
            )

    def test_a_replay_never_reports_a_response_the_winner_did_not_store(
        self, adm_kernel_engine, admissible
    ):
        """Every caller offers a different candidate answer. Only one can be the truth."""
        key = f"adv-answer-{uuid7().hex[:16]}"
        request = {"basket": "b-1"}

        def body(session: Session, index: int) -> dict[str, Any]:
            outcome = execute_once(
                session, key, OPERATION, request, lambda: {"winner_index": index}
            )
            return {"executed": outcome.executed, "resp": outcome.response}

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=10)
        answers = {o.value["resp"]["winner_index"] for o in outcomes}
        assert len(answers) == 1, f"callers disagreed about what happened: {answers}"


class TestSameKeyDifferentPayload:
    def test_a_changed_payload_is_refused_and_told_nothing(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """The refusal must not carry the original response.

        Handing it back would answer a question the caller did not ask, and a caller that
        logged it as its own outcome would record the wrong amount against the wrong
        request.
        """
        key = f"adv-diff-{uuid7().hex[:16]}"
        ran: list[int] = []

        def body(session: Session, index: int) -> Any:
            request = {"amount_minor": 39500 + index * 100}
            return execute_once(
                session,
                key,
                OPERATION,
                request,
                lambda: (ran.append(index), {"charged": request["amount_minor"]})[1],
            )

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=12)

        executed = [o for o in outcomes if o.ok and o.value.executed]
        assert len(executed) == 1, f"{len(executed)} callers executed under one key"
        assert len(ran) == 1

        refused = [o for o in outcomes if not o.ok]
        assert len(refused) == 11
        for outcome in refused:
            assert isinstance(outcome.error, IdempotencyKeyReuseError), (
                f"a differing payload was answered with {type(outcome.error).__name__} "
                "instead of being refused"
            )
            assert outcome.error.code is RecoveryCode.POLICY_EXCEPTION, (
                "DUPLICATE_OPERATION is presentable to a buyer as a completed money "
                "action; nothing completed for this caller"
            )
            assert not hasattr(outcome.error, "response"), (
                "the refusal carries the original response, disclosing the answer to a "
                "request this caller never made"
            )

        assert _records(adm_admin_engine, admissible.tenant_id, key) == 1

    def test_the_stored_record_belongs_to_the_caller_that_executed(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        key = f"adv-owner-{uuid7().hex[:16]}"

        def body(session: Session, index: int) -> Any:
            request = {"amount_minor": 100 * (index + 1)}
            return execute_once(
                session, key, OPERATION, request, lambda: {"charged": request["amount_minor"]}
            )

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=8)
        winner = next(o for o in outcomes if o.ok and o.value.executed)

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                stored = read_idempotency_record(session, key)
        finally:
            session.close()
        assert stored is not None
        assert stored.response == winner.value.response, (
            "the stored answer is not the one the executing caller produced"
        )

    def test_a_sequential_reuse_with_a_changed_amount_is_refused(
        self, adm_kernel_engine, admissible
    ):
        """The plain case, stated once so the concurrent ones above have a baseline."""
        key = f"adv-seq-{uuid7().hex[:16]}"
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            execute_once(
                session, key, OPERATION, {"amount_minor": 39500}, lambda: {"order_id": "o-1"}
            )
        session.close()

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(IdempotencyKeyReuseError) as caught:
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                execute_once(
                    session,
                    key,
                    OPERATION,
                    {"amount_minor": 395000},
                    lambda: pytest.fail("the guarded block must not run"),
                )
        finally:
            session.close()
        assert "o-1" not in str(caught.value), "the refusal leaked the original response"


class TestMixedPayloadsUnderOneKey:
    def test_half_identical_half_changed_still_executes_once(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """A retrying proxy in front of a client that changed its mind.

        Whichever caller wins, exactly one execution may happen; the identical callers
        get the winner's answer or a refusal depending on who won, and neither group may
        cause a second one.
        """
        key = f"adv-mixed-{uuid7().hex[:16]}"
        canonical = {"amount_minor": 39500}
        ran: list[int] = []

        def body(session: Session, index: int) -> Any:
            request = canonical if index % 2 == 0 else {"amount_minor": 39500 + index}
            return execute_once(
                session,
                key,
                OPERATION,
                request,
                lambda: (ran.append(index), {"by": index})[1],
            )

        race(adm_kernel_engine, admissible.tenant_id, body, workers=12)
        assert len(ran) == 1, f"the operation ran {len(ran)} times under one key: {ran}"
        assert _records(adm_admin_engine, admissible.tenant_id, key) == 1


class TestFailureLeavesTheKeyUsable:
    def test_a_block_that_raises_leaves_no_claim_behind(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """A transient fault must not wedge the buyer's key for good."""
        key = f"adv-fail-{uuid7().hex[:16]}"
        request = {"amount_minor": 39500}

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(RuntimeError, match="provider unreachable"):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                with idempotent(session, key, OPERATION, request):
                    raise RuntimeError("provider unreachable")
        finally:
            session.close()

        assert _records(adm_admin_engine, admissible.tenant_id, key) == 0

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                outcome = execute_once(
                    session, key, OPERATION, request, lambda: {"order_id": "o-retry"}
                )
        finally:
            session.close()
        assert outcome.executed
        assert outcome.response == {"order_id": "o-retry"}

    def test_a_block_that_forgets_to_store_is_refused_and_leaves_nothing(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """A committed claim with no response would make every later retry unknown."""
        key = f"adv-nostore-{uuid7().hex[:16]}"
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(IdempotencyUsageError, match="never given"):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                with idempotent(session, key, OPERATION, {"a": 1}):
                    pass
        finally:
            session.close()
        assert _records(adm_admin_engine, admissible.tenant_id, key) == 0

    def test_an_orphaned_claim_reconciles_rather_than_re_runs(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """The crash-between-commits shape, planted directly because clean code cannot
        produce it: a committed claim carrying no response.

        The answer must be ``CONCURRENT_OPERATION`` -- the operation may or may not have
        reached the provider -- and the block must not run.
        """
        key = f"adv-orphan-{uuid7().hex[:16]}"
        request = {"amount_minor": 39500}
        from commerce_domain import canonical_hash

        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO idempotency_records (id, tenant_id, idem_key, operation, "
                    "request_hash, response) VALUES (:id, :t, :k, :op, :h, NULL)"
                ),
                {
                    "id": uuid7(),
                    "t": admissible.tenant_id,
                    "k": key,
                    "op": str(OPERATION),
                    "h": canonical_hash(request),
                },
            )

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(IdempotencyInFlightError) as caught:
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                execute_once(
                    session,
                    key,
                    OPERATION,
                    request,
                    lambda: pytest.fail("an unknown outcome must not be re-run"),
                )
        finally:
            session.close()
        assert caught.value.code is RecoveryCode.CONCURRENT_OPERATION


class TestKeysAreConfinedToTheirTenant:
    def test_one_key_in_two_tenants_is_two_operations(
        self, adm_kernel_engine, adm_admin_engine, admissible: Fixture
    ):
        """Keys come from clients and collide across tenants by accident.

        If a key were global, tenant B's first request would be answered with tenant A's
        response -- a cross-tenant disclosure and a payment that never happened.
        """
        stranger = uuid.uuid4()
        key = f"adv-shared-{uuid7().hex[:16]}"
        request = {"amount_minor": 39500}
        with adm_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :s, :n, 'asia-south1')"
                ),
                {
                    "id": stranger,
                    "s": f"adv-{stranger.hex[:8]}",
                    "n": f"adv-{stranger.hex[:8]}",
                },
            )
        try:
            answers = {}
            for tenant, label in ((admissible.tenant_id, "A"), (stranger, "B")):
                session = Session(adm_kernel_engine, expire_on_commit=False)
                try:
                    with session.begin():
                        session.execute(SET_TENANT, {"t": str(tenant)})
                        answers[label] = execute_once(
                            session, key, OPERATION, request, lambda label=label: {"by": label}
                        )
                finally:
                    session.close()

            assert answers["A"].executed and answers["B"].executed, (
                "one tenant's key suppressed another tenant's first execution"
            )
            assert answers["B"].response == {"by": "B"}
        finally:
            with adm_admin_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(stranger)})
                conn.execute(
                    text("DELETE FROM idempotency_records WHERE tenant_id = :t"),
                    {"t": stranger},
                )
                conn.execute(SET_TENANT, {"t": None})
                conn.execute(
                    text("DELETE FROM tenants WHERE id = :i"),
                    {"i": stranger},
                )


class TestReplayAfterTheEffectCommitted:
    def test_the_replay_reads_the_committed_effect_not_a_stale_snapshot(
        self, adm_kernel_engine, admissible
    ):
        """The record commits with the effect it describes -- never before it.

        A replay that could observe the record while the effect was still uncommitted
        would report success for money that had not moved.
        """
        key = f"adv-atomic-{uuid7().hex[:16]}"
        request = {"amount_minor": 39500}
        marker = uuid7()

        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            with idempotent(session, key, OPERATION, request) as slot:
                from transaction_kernel import audit

                audit.append(
                    session,
                    tenant=admissible.tenant_id,
                    aggregate_type="adversarial_effect",
                    aggregate_id=marker,
                    event_type="effect.happened",
                    actor_type=admissible.principal.actor_type,
                    principal_id=admissible.principal.principal_id,
                    payload={"key": key},
                    correlation_id=admissible.correlation_id,
                )
                slot.store({"marker": str(marker)})
        session.close()

        def body(session: Session, _: int) -> Any:
            try:
                with idempotent(session, key, OPERATION, request):
                    pytest.fail("the guarded block ran on a replay")
            except IdempotentReplayError as replay:
                return count(
                    adm_kernel_engine,
                    admissible.tenant_id,
                    "SELECT count(*) FROM audit_events WHERE aggregate_id = :m",
                    m=uuid.UUID(replay.response["marker"]),
                )
            raise AssertionError("no replay was raised")

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=6)
        for outcome in outcomes:
            assert outcome.ok, outcome.error
            assert outcome.value == 1, (
                "a replay claimed an operation whose effect is not visible; the record "
                "and the effect did not commit together"
            )

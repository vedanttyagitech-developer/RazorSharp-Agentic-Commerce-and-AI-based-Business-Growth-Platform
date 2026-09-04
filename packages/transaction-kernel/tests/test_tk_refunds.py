"""Refund kernel tests, specification 10.5, 10.6, 10.8, 11.4 and ADR 0003 D10.

Real PostgreSQL as ``commerce_test_kernel`` (NOSUPERUSER, NOBYPASSRLS); seeding and
teardown through the owner connection because no application role may DELETE. The
concurrency tests use real threads on real connections: the invariant under test is that
two callers racing for the last refundable rupee produce one refund row, and only the
database's row lock can prove that.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from platform_db import set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from transaction_kernel import audit, safe_mode
from transaction_kernel.contracts import ActorType, AgentPrincipal, CheckoutRef, Operation
from transaction_kernel.grants import GrantBinding, GrantStatus, consume_grant
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.refunds import (
    STALE_CAPTURE_REASON,
    RefundAdmission,
    RefundStateError,
    RefundStatus,
    RefundTenantMismatchError,
    RefundUsageError,
    admit_refund,
    admit_stale_capture_refund,
    escalate_refund,
    human_review_case_key,
    ledger,
    reconcile_refund,
    record_provider_originated_refund,
    record_refund_result,
    refund_idempotency_key,
)
from transaction_kernel.states import PaymentState

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

CAPTURED = Money(39500, "INR")
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------- fixtures


def _require_db(url: str, *, pool_size: int = 5) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for refund tests: {exc}")
    return engine


@pytest.fixture(scope="module")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "refund tests must not run as a superuser"
    assert row.rolbypassrls is False, "refund tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="module")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL, pool_size=2)


@dataclass(frozen=True, slots=True)
class Captured:
    """One tenant with one CAPTURED payment attempt on one immutable checkout version."""

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    attempt_id: uuid.UUID
    checkout: CheckoutRef
    principal: AgentPrincipal
    correlation_id: uuid.UUID


@pytest.fixture
def captured(admin_engine: Engine) -> Iterator[Captured]:
    tenant_id, merchant_id = uuid.uuid4(), uuid7()
    attempt_id, checkout_id, version = uuid7(), uuid7(), 2
    content = {
        "checkout_id": str(checkout_id),
        "version": version,
        "currency": "INR",
        "total_minor": CAPTURED.minor,
        "line_items": {"sku_lamp": 1},
        "policy_version": "pol-v3",
    }
    checkout = CheckoutRef(checkout_id, version, canonical_hash(content))
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :s, :n, 'asia-south1')"
            ),
            {"id": tenant_id, "s": f"rf-{tenant_id.hex[:8]}", "n": f"rf-{tenant_id.hex[:8]}"},
        )
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :s, :n, 'INR')"
            ),
            {"id": merchant_id, "t": tenant_id, "s": f"m-{merchant_id.hex[:8]}", "n": "m"},
        )
        conn.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, 'INR', :total, "
                "'PAID', true)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "v": version,
                "content": json.dumps(content, sort_keys=True),
                "h": checkout.content_hash,
                "total": CAPTURED.minor,
            },
        )
        conn.execute(
            text(
                "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
                "status, amount_minor, currency, receipt, provider_order_id, "
                "provider_payment_id) VALUES (:id, :t, :c, :v, 'CAPTURED', :amt, 'INR', :r, "
                ":o, :p)"
            ),
            {
                "id": attempt_id,
                "t": tenant_id,
                "c": checkout_id,
                "v": version,
                "amt": CAPTURED.minor,
                "r": f"rcpt_{attempt_id.hex[:24]}",
                "o": f"order_{attempt_id.hex[:14]}",
                "p": f"pay_{attempt_id.hex[:14]}",
            },
        )

    yield Captured(
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        attempt_id=attempt_id,
        checkout=checkout,
        principal=AgentPrincipal(
            principal_id="buyer-1", tenant_id=tenant_id, actor_type=ActorType.BUYER
        ),
        correlation_id=uuid7(),
    )

    with admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in (
            "provider_requests",
            "reconciliation_runs",
            "orders",
            "execution_grants",  # references refunds: must go first
            "refunds",
            "payment_attempts",
            "audit_events",
            "platform_operating_modes",
            "checkout_versions",
            "merchants",
        ):
            # S608: table iterates the literal tuple above, never request data.
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})


@pytest.fixture
def open_session(kernel_engine: Engine) -> Iterator[Callable[[], Session]]:
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


def _admit(
    engine: Engine, fx: Captured, amount: Money | None, *, reason: str = "buyer_request"
) -> RefundAdmission:
    """One committed admission, the way the API would run it."""
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, fx.tenant_id)
        return admit_refund(
            session,
            tenant_id=fx.tenant_id,
            payment_attempt_id=fx.attempt_id,
            amount=amount,
            reason_code=reason,
            principal=fx.principal,
            correlation_id=fx.correlation_id,
        )


def _binding(fx: Captured, admission: RefundAdmission) -> GrantBinding:
    assert admission.amount is not None
    return GrantBinding(
        tenant_id=fx.tenant_id,
        checkout=fx.checkout,
        payment_attempt_id=fx.attempt_id,
        operation=Operation.REFUND_EXECUTE,
        amount=admission.amount,
        refund_id=admission.refund_id,
    )


def _consume(engine: Engine, fx: Captured, admission: RefundAdmission) -> None:
    """The worker's step: spend the grant with the binding from the command."""
    assert admission.grant_id is not None
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, fx.tenant_id)
        consumed = consume_grant(session, admission.grant_id, _binding(fx, admission))
        assert consumed.status == GrantStatus.CONSUMED


def _result(
    engine: Engine, fx: Captured, refund_id: uuid.UUID | None, outcome: Any, pid: str | None
) -> Any:
    assert refund_id is not None
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, fx.tenant_id)
        return record_refund_result(
            session,
            tenant_id=fx.tenant_id,
            refund_id=refund_id,
            outcome=outcome,
            provider_refund_id=pid,
            correlation_id=fx.correlation_id,
        )


def _attempt_state(engine: Engine, fx: Captured) -> str:
    with engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(fx.tenant_id)})
        state = conn.execute(
            text("SELECT status FROM payment_attempts WHERE id = :id"), {"id": fx.attempt_id}
        ).scalar_one()
    return str(state)


def _set_attempt_state(engine: Engine, fx: Captured, state: PaymentState) -> None:
    """Owner-side surgery to start a scenario mid-lifecycle (a stale capture, say)."""
    with engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(fx.tenant_id)})
        conn.execute(
            text("UPDATE payment_attempts SET status = :s WHERE id = :id"),
            {"s": state.value, "id": fx.attempt_id},
        )


def _refund_rows(engine: Engine, fx: Captured) -> list[Any]:
    with engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(fx.tenant_id)})
        return list(
            conn.execute(
                text(
                    "SELECT id, status, amount_minor, idem_key, provider_refund_id, "
                    "provider_originated, reason_code FROM refunds "
                    "WHERE payment_attempt_id = :a ORDER BY created_at, id"
                ),
                {"a": fx.attempt_id},
            ).all()
        )


def _grant_rows(engine: Engine, fx: Captured) -> list[Any]:
    with engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(fx.tenant_id)})
        return list(
            conn.execute(
                text(
                    "SELECT id, status, operation, refund_id, amount_minor FROM execution_grants "
                    "WHERE payment_attempt_id = :a ORDER BY issued_at, id"
                ),
                {"a": fx.attempt_id},
            ).all()
        )


def _events(engine: Engine, fx: Captured) -> list[Any]:
    with Session(engine) as session, session.begin():
        set_tenant(session, fx.tenant_id)
        return list(
            audit.read_stream(
                session,
                tenant=fx.tenant_id,
                aggregate_type="payment_attempt",
                aggregate_id=fx.attempt_id,
            )
        )


# ---------------------------------------------------------------------------- admission


class TestAdmission:
    def test_full_refund_admits_one_pending_row_and_one_bound_grant(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)

        assert admission.allowed
        assert admission.decision.code is RecoveryCode.OK
        assert admission.amount == CAPTURED
        assert admission.sequence == 1
        assert admission.idem_key == refund_idempotency_key(
            tenant_id=captured.tenant_id,
            payment_attempt_id=captured.attempt_id,
            sequence=1,
            amount=CAPTURED,
        )
        assert admission.idem_key is not None and admission.idem_key.startswith("rfnd_")
        assert admission.decision.grant_id == admission.grant_id
        assert admission.decision.payment_attempt_id == captured.attempt_id
        assert admission.decision.checkout == captured.checkout

        (row,) = _refund_rows(admin_engine, captured)
        assert row.id == admission.refund_id
        assert row.status == RefundStatus.PENDING
        assert row.amount_minor == CAPTURED.minor
        assert row.idem_key == admission.idem_key
        assert row.provider_originated is False

        (grant,) = _grant_rows(admin_engine, captured)
        assert grant.id == admission.grant_id
        assert grant.status == GrantStatus.ISSUED
        assert grant.operation == Operation.REFUND_EXECUTE.value
        assert grant.refund_id == admission.refund_id
        assert grant.amount_minor == CAPTURED.minor

        assert _attempt_state(admin_engine, captured) == PaymentState.REFUND_PENDING
        (event,) = _events(kernel_engine, captured)
        assert event.event_type == "refund.admitted"
        assert event.payload["refund_id"] == str(admission.refund_id)
        assert event.payload["is_full_remaining"] is True

        # The worker's binding -- built from the command, refund id included -- is accepted.
        _consume(kernel_engine, captured, admission)

    def test_two_concurrent_admissions_for_the_remaining_amount_produce_one_pending_row(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        """Two real sessions racing for the whole capture. One refund, one grant.

        The loser blocks on the checkout-version and attempt locks, re-reads the winner's
        committed REFUND_PENDING, and is told CONCURRENT_OPERATION -- never a second row,
        and never DUPLICATE_OPERATION, which could be shown to the buyer as done.
        """
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def caller() -> None:
            session = Session(kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    set_tenant(session, captured.tenant_id)
                    session.execute(text("SET LOCAL lock_timeout = '15s'"))
                    barrier.wait(timeout=15)
                    admission = admit_refund(
                        session,
                        tenant_id=captured.tenant_id,
                        payment_attempt_id=captured.attempt_id,
                        amount=None,
                        reason_code="buyer_request",
                        principal=captured.principal,
                        correlation_id=uuid7(),
                    )
                result = (
                    "admitted"
                    if admission.allowed
                    else f"denied:{admission.decision.code.value}:{admission.decision.explanation}"
                )
            except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                result = f"unexpected:{exc!r}"
            finally:
                session.close()
            with guard:
                outcomes.append(result)

        threads = [threading.Thread(target=caller, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive(), "a caller never finished; a lock was held"

        assert sorted(outcomes) == [
            "admitted",
            "denied:CONCURRENT_OPERATION:refund_already_in_flight",
        ], outcomes
        rows = _refund_rows(admin_engine, captured)
        assert [r.status for r in rows] == [RefundStatus.PENDING]
        assert len(_grant_rows(admin_engine, captured)) == 1

    def test_partial_then_partial_then_refusal_then_the_exact_remainder(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        """Specification 11.4: repeated partial refunds never exceed the capture.

        Each partial refund is its own row with its own grant (ADR D10), admitted only
        once the previous one settled. The refusal names both figures in a delta so the
        buyer can be shown exactly why.
        """
        first = _admit(kernel_engine, captured, Money(10000, "INR"))
        assert first.allowed and first.sequence == 1
        _consume(kernel_engine, captured, first)
        settled = _result(kernel_engine, captured, first.refund_id, "processed", "rfnd_1")
        assert settled.attempt_state_after is PaymentState.PARTIALLY_REFUNDED

        second = _admit(kernel_engine, captured, Money(20000, "INR"))
        assert second.allowed and second.sequence == 2
        assert second.refund_id != first.refund_id
        assert second.grant_id != first.grant_id
        assert second.idem_key != first.idem_key
        _consume(kernel_engine, captured, second)
        _result(kernel_engine, captured, second.refund_id, "processed", "rfnd_2")
        assert _attempt_state(admin_engine, captured) == PaymentState.PARTIALLY_REFUNDED

        refused = _admit(kernel_engine, captured, Money(15000, "INR"))
        assert not refused.allowed
        assert refused.decision.code is RecoveryCode.POLICY_EXCEPTION
        assert refused.decision.explanation == "exceeds_remaining"
        (delta,) = refused.decision.deltas
        assert (delta.field_path, delta.approved, delta.current) == ("amount_minor", 9500, 15000)
        assert refused.refund_id is None and refused.grant_id is None
        assert len(_refund_rows(admin_engine, captured)) == 2

        remainder = _admit(kernel_engine, captured, None)
        assert remainder.allowed and remainder.amount == Money(9500, "INR")
        assert remainder.sequence == 3
        _consume(kernel_engine, captured, remainder)
        final = _result(kernel_engine, captured, remainder.refund_id, "processed", "rfnd_3")
        assert final.attempt_state_after is PaymentState.REFUNDED

        nothing = _admit(kernel_engine, captured, Money(1, "INR"))
        assert not nothing.allowed
        # REFUNDED is terminal: the state, not the arithmetic, is what refuses.
        assert nothing.decision.explanation == "state_forbids_refund"

        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            book = ledger(
                session, tenant_id=captured.tenant_id, payment_attempt_id=captured.attempt_id
            )
        assert book.settled == CAPTURED and book.remaining.is_zero and book.next_sequence == 4
        keys = {r.idem_key for r in _refund_rows(admin_engine, captured)}
        assert len(keys) == 3

    def test_a_second_refund_is_refused_while_the_first_is_pending(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        first = _admit(kernel_engine, captured, Money(100, "INR"))
        assert first.allowed
        second = _admit(kernel_engine, captured, Money(100, "INR"))
        assert not second.allowed
        assert second.decision.code is RecoveryCode.CONCURRENT_OPERATION
        assert second.decision.explanation == "refund_already_in_flight"
        assert len(_refund_rows(admin_engine, captured)) == 1
        events = _events(kernel_engine, captured)
        assert [e.event_type for e in events] == ["refund.admitted", "refund.denied"]

    @pytest.mark.parametrize(
        ("amount", "explanation"),
        [
            (Money(0, "INR"), "non_positive_amount"),
            (Money(-5, "INR"), "non_positive_amount"),
            (Money(100, "USD"), "currency_mismatch"),
            (Money(CAPTURED.minor + 1, "INR"), "exceeds_remaining"),
        ],
    )
    def test_structural_refusals_are_structured_denials(
        self,
        kernel_engine: Engine,
        admin_engine: Engine,
        captured: Captured,
        amount: Money,
        explanation: str,
    ) -> None:
        denied = _admit(kernel_engine, captured, amount)
        assert not denied.allowed
        assert denied.decision.code is RecoveryCode.POLICY_EXCEPTION
        assert denied.decision.explanation == explanation
        assert _refund_rows(admin_engine, captured) == []
        assert _attempt_state(admin_engine, captured) == PaymentState.CAPTURED

    def test_an_unrefundable_state_is_refused_by_the_lifecycle(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        _set_attempt_state(admin_engine, captured, PaymentState.AUTHORIZED)
        denied = _admit(kernel_engine, captured, None)
        assert denied.decision.explanation == "state_forbids_refund"
        assert denied.decision.code is RecoveryCode.POLICY_EXCEPTION

    def test_an_unknown_attempt_is_refused_without_leaking_existence(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            denied = admit_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=uuid7(),
                amount=None,
                reason_code="buyer_request",
                principal=captured.principal,
                correlation_id=uuid7(),
            )
        assert denied.decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert denied.decision.explanation == "payment_attempt_not_found"

    def test_tenant_comes_from_the_transaction_not_the_argument(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        other = uuid.uuid4()
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            with pytest.raises(RefundTenantMismatchError) as exc:
                admit_refund(
                    session,
                    tenant_id=other,
                    payment_attempt_id=captured.attempt_id,
                    amount=None,
                    reason_code="buyer_request",
                    principal=captured.principal,
                    correlation_id=uuid7(),
                )
        assert exc.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            with pytest.raises(RefundTenantMismatchError, match="principal"):
                admit_refund(
                    session,
                    tenant_id=captured.tenant_id,
                    payment_attempt_id=captured.attempt_id,
                    amount=None,
                    reason_code="buyer_request",
                    principal=AgentPrincipal(
                        principal_id="p", tenant_id=other, actor_type=ActorType.BUYER
                    ),
                    correlation_id=uuid7(),
                )

    def test_requires_an_open_transaction(self, kernel_engine: Engine, captured: Captured) -> None:
        session = Session(kernel_engine, autobegin=False)
        try:
            with pytest.raises(RefundUsageError, match="inside a transaction"):
                admit_refund(
                    session,
                    tenant_id=captured.tenant_id,
                    payment_attempt_id=captured.attempt_id,
                    amount=None,
                    reason_code="buyer_request",
                    principal=captured.principal,
                    correlation_id=uuid7(),
                )
        finally:
            session.close()

    def test_refunds_stay_admissible_in_safe_mode(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        """Specification 10.3.2: the kill switch may not reach the buyer's refund."""
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            safe_mode.enter_safe_mode(
                session,
                tenant=captured.tenant_id,
                reason=safe_mode.ModeChangeReason.PROVIDER_INCIDENT_DECLARED,
                actor="ops:test",
                actor_type=ActorType.OPERATOR,
            )
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            assert safe_mode.current_mode(session, captured.tenant_id) is (
                safe_mode.OperatingModeName.SAFE_MODE
            )
        admission = _admit(kernel_engine, captured, None)
        assert admission.allowed
        assert _attempt_state(admin_engine, captured) == PaymentState.REFUND_PENDING


# ------------------------------------------------------------------------------ results


class TestResults:
    def test_failed_result_then_retry_produces_a_new_row_and_a_new_grant(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        """REFUND_FAILED is a provider-confirmed absence: a retry is a fresh admission,
        never the old grant re-armed (specification 10.6)."""
        first = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, first)
        failed = _result(kernel_engine, captured, first.refund_id, "failed", None)
        assert failed.code is RecoveryCode.PAYMENT_FAILED
        assert failed.refund_status_after is RefundStatus.FAILED
        assert failed.attempt_state_after is PaymentState.REFUND_FAILED

        retry = _admit(kernel_engine, captured, None)
        assert retry.allowed
        assert retry.refund_id != first.refund_id
        assert retry.grant_id != first.grant_id
        assert retry.idem_key != first.idem_key
        assert retry.sequence == 2
        grants = {g.id: g for g in _grant_rows(admin_engine, captured)}
        assert grants[first.grant_id].status == GrantStatus.CONSUMED
        assert grants[retry.grant_id].status == GrantStatus.ISSUED
        assert grants[retry.grant_id].refund_id == retry.refund_id
        # A failed refund releases its amount: the retry is for the full capture.
        assert retry.amount == CAPTURED

    def test_a_duplicate_result_delivery_changes_nothing(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        first = _result(kernel_engine, captured, admission.refund_id, "processed", "rfnd_x")
        assert first.changed and first.attempt_state_after is PaymentState.REFUNDED
        again = _result(kernel_engine, captured, admission.refund_id, "processed", "rfnd_x")
        assert not again.changed
        assert again.explanation == "already_recorded"
        assert again.attempt_state_after is PaymentState.REFUNDED

    def test_a_pending_result_records_the_provider_id_and_moves_nothing(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        pending = _result(kernel_engine, captured, admission.refund_id, "pending", "rfnd_p")
        assert not pending.changed
        assert pending.code is RecoveryCode.PAYMENT_PENDING
        (row,) = _refund_rows(admin_engine, captured)
        assert row.status == RefundStatus.PENDING and row.provider_refund_id == "rfnd_p"
        assert _attempt_state(admin_engine, captured) == PaymentState.REFUND_PENDING

    def test_processed_requires_a_provider_id(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        with pytest.raises(RefundUsageError, match="provider id"):
            _result(kernel_engine, captured, admission.refund_id, "processed", None)

    def test_a_result_on_an_unknown_refund_is_refused(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        """An UNKNOWN refund is resolved by reconciliation, never by a second report."""
        admission = _admit(kernel_engine, captured, None)
        _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        with pytest.raises(RefundStateError, match="reconcile_refund"):
            _result(kernel_engine, captured, admission.refund_id, "processed", "rfnd_late")


# ------------------------------------------------------------------------ reconciliation


class TestReconciliation:
    def test_refund_unknown_never_issues_a_grant(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        """Specification 10.6, the row that matters: REFUND_UNKNOWN reconciles only.

        Admission is refused, and neither reconciliation outcome that confirms the refund
        exists mints anything. One grant is ever issued for this refund.
        """
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        lost = _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        assert lost.code is RecoveryCode.PAYMENT_UNKNOWN
        assert lost.attempt_state_after is PaymentState.REFUND_UNKNOWN
        assert lost.refund_status_after is RefundStatus.UNKNOWN

        denied = _admit(kernel_engine, captured, None)
        assert not denied.allowed
        assert denied.decision.code is RecoveryCode.RECONCILIATION_IN_PROGRESS
        assert denied.decision.explanation == "reconcile_unknown_first"

        assert admission.refund_id is not None
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            pending = reconcile_refund(
                session,
                tenant_id=captured.tenant_id,
                refund_id=admission.refund_id,
                verified="exists_pending",
                provider_refund_id="rfnd_seen",
                correlation_id=uuid7(),
                attempt_number=1,
            )
        assert pending.next_action == "await_provider"
        assert pending.code is RecoveryCode.RECONCILIATION_IN_PROGRESS
        assert pending.refund_status_after is RefundStatus.RECONCILING
        assert pending.attempt_state_after is PaymentState.RECONCILING

        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            done = reconcile_refund(
                session,
                tenant_id=captured.tenant_id,
                refund_id=admission.refund_id,
                verified="exists_processed",
                provider_refund_id="rfnd_seen",
                correlation_id=uuid7(),
                attempt_number=2,
            )
        assert done.next_action == "none" and done.code is RecoveryCode.OK
        assert done.attempt_state_after is PaymentState.REFUNDED
        assert done.refund_status_after is RefundStatus.PROCESSED

        grants = _grant_rows(admin_engine, captured)
        assert [g.status for g in grants] == [GrantStatus.CONSUMED]
        assert len(_refund_rows(admin_engine, captured)) == 1
        kinds = [e.event_type for e in _events(kernel_engine, captured)]
        assert kinds == [
            "refund.admitted",
            "refund.result",
            "refund.denied",
            "refund.reconciled",
            "refund.reconciled",
        ]

    def test_verified_absence_permits_a_new_admission_with_a_new_grant(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        assert admission.refund_id is not None
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            absent = reconcile_refund(
                session,
                tenant_id=captured.tenant_id,
                refund_id=admission.refund_id,
                verified="absent",
                provider_refund_id=None,
                correlation_id=uuid7(),
                attempt_number=3,
            )
        assert absent.next_action == "admit_new_refund"
        assert absent.code is RecoveryCode.PAYMENT_FAILED
        assert absent.refund_status_after is RefundStatus.FAILED
        assert absent.attempt_state_after is PaymentState.REFUND_FAILED
        # Nothing re-armed the old grant.
        (old,) = _grant_rows(admin_engine, captured)
        assert old.status == GrantStatus.CONSUMED

        fresh = _admit(kernel_engine, captured, None)
        assert fresh.allowed
        assert fresh.refund_id != admission.refund_id
        assert fresh.grant_id != admission.grant_id
        assert fresh.amount == CAPTURED
        grants = {g.id: g.status for g in _grant_rows(admin_engine, captured)}
        assert grants == {admission.grant_id: GrantStatus.CONSUMED, fresh.grant_id: "ISSUED"}

    def test_a_refund_seen_pending_then_reported_absent_is_a_contradiction(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        assert admission.refund_id is not None
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            reconcile_refund(
                session,
                tenant_id=captured.tenant_id,
                refund_id=admission.refund_id,
                verified="exists_pending",
                provider_refund_id="rfnd_seen",
                correlation_id=uuid7(),
            )
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            contradiction = reconcile_refund(
                session,
                tenant_id=captured.tenant_id,
                refund_id=admission.refund_id,
                verified="absent",
                provider_refund_id=None,
                correlation_id=uuid7(),
            )
        assert contradiction.next_action == "escalate"
        assert contradiction.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        assert contradiction.refund_status_after is RefundStatus.RECONCILING
        (row,) = _refund_rows(admin_engine, captured)
        assert row.status == RefundStatus.RECONCILING

    def test_reconciling_a_pending_refund_is_refused(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        assert admission.refund_id is not None
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            with pytest.raises(RefundStateError, match="UNKNOWN or RECONCILING"):
                reconcile_refund(
                    session,
                    tenant_id=captured.tenant_id,
                    refund_id=admission.refund_id,
                    verified="absent",
                    provider_refund_id=None,
                    correlation_id=uuid7(),
                )


# --------------------------------------------------------------------------- escalation


class TestEscalation:
    def test_escalation_freezes_the_attempt_and_opens_exactly_one_case(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        assert admission.refund_id is not None
        expected_key = human_review_case_key(
            tenant_id=captured.tenant_id,
            order_ref=captured.checkout.checkout_id,
            subject_id=admission.refund_id,
            reason_family="refund_unresolved",
        )

        def escalate() -> Any:
            with Session(kernel_engine) as session, session.begin():
                set_tenant(session, captured.tenant_id)
                return escalate_refund(
                    session,
                    tenant_id=captured.tenant_id,
                    refund_id=admission.refund_id,  # type: ignore[arg-type]
                    reason_family="refund_unresolved",
                    correlation_id=uuid7(),
                    attempts=6,
                )

        first = escalate()
        assert first.opened is True
        assert first.case_key == expected_key
        assert first.attempt_state_before is PaymentState.REFUND_UNKNOWN
        assert first.attempt_state_after is PaymentState.ESCALATED
        assert _attempt_state(admin_engine, captured) == PaymentState.ESCALATED
        (row,) = _refund_rows(admin_engine, captured)
        assert row.status == RefundStatus.ESCALATED

        second = escalate()
        assert second.opened is False
        assert second.case_key == expected_key
        opened = [
            e for e in _events(kernel_engine, captured) if e.event_type == "human_review.opened"
        ]
        assert len(opened) == 1
        assert opened[0].payload["case_key"] == expected_key
        assert opened[0].payload["monetary_exposure"] == {"currency": "INR", "minor": 39500}

        # ESCALATED is terminal: no admission, and no grant, leaves it.
        denied = _admit(kernel_engine, captured, None)
        assert not denied.allowed
        assert [g.status for g in _grant_rows(admin_engine, captured)] == [GrantStatus.CONSUMED]

    def test_two_concurrent_escalations_open_one_case(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        """Specification 6.4.3's concurrency requirement, on the attempt row lock."""
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def detector() -> None:
            session = Session(kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    set_tenant(session, captured.tenant_id)
                    session.execute(text("SET LOCAL lock_timeout = '15s'"))
                    barrier.wait(timeout=15)
                    escalation = escalate_refund(
                        session,
                        tenant_id=captured.tenant_id,
                        refund_id=admission.refund_id,  # type: ignore[arg-type]
                        reason_family="refund_unresolved",
                        correlation_id=uuid7(),
                    )
                result = "opened" if escalation.opened else "existing"
            except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                result = f"unexpected:{exc!r}"
            finally:
                session.close()
            with guard:
                outcomes.append(result)

        threads = [threading.Thread(target=detector, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive()
        assert sorted(outcomes) == ["existing", "opened"], outcomes
        opened = [
            e for e in _events(kernel_engine, captured) if e.event_type == "human_review.opened"
        ]
        assert len(opened) == 1

    def test_a_settled_refund_cannot_be_escalated(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "processed", "rfnd_ok")
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            with pytest.raises(RefundStateError, match="UNKNOWN, RECONCILING or FAILED"):
                escalate_refund(
                    session,
                    tenant_id=captured.tenant_id,
                    refund_id=admission.refund_id,  # type: ignore[arg-type]
                    reason_family="refund_unresolved",
                    correlation_id=uuid7(),
                )


# ----------------------------------------------------------------- provider-originated


class TestProviderOriginated:
    def test_a_webhook_for_a_refund_this_platform_made_does_not_double_count(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, Money(10000, "INR"))
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "processed", "rfnd_ours")

        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            echo = record_provider_originated_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                provider_refund_id="rfnd_ours",
                amount=Money(10000, "INR"),
                correlation_id=uuid7(),
            )
        assert not echo.changed
        assert echo.code is RecoveryCode.DUPLICATE_OPERATION
        assert echo.refund_id == admission.refund_id
        assert len(_refund_rows(admin_engine, captured)) == 1

        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            book = ledger(
                session, tenant_id=captured.tenant_id, payment_attempt_id=captured.attempt_id
            )
        assert book.reserved == Money(10000, "INR")

    def test_a_dashboard_refund_is_recorded_once_and_counted_in_the_ledger(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            recorded = record_provider_originated_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                provider_refund_id="rfnd_dash",
                amount=Money(5000, "INR"),
                correlation_id=uuid7(),
            )
        assert recorded.changed and recorded.code is RecoveryCode.OK
        assert recorded.attempt_state_before is PaymentState.CAPTURED
        assert recorded.attempt_state_after is PaymentState.PARTIALLY_REFUNDED
        (row,) = _refund_rows(admin_engine, captured)
        assert row.provider_originated is True
        assert row.status == RefundStatus.PROCESSED
        assert row.reason_code == "PROVIDER_ORIGINATED"
        assert row.provider_refund_id == "rfnd_dash"
        assert _grant_rows(admin_engine, captured) == []

        # Redelivery of the same webhook: nothing new.
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            again = record_provider_originated_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                provider_refund_id="rfnd_dash",
                amount=Money(5000, "INR"),
                correlation_id=uuid7(),
            )
        assert not again.changed and again.code is RecoveryCode.DUPLICATE_OPERATION
        assert len(_refund_rows(admin_engine, captured)) == 1

        # The platform's own next refund is measured against what the provider already did.
        admission = _admit(kernel_engine, captured, None)
        assert admission.allowed and admission.amount == Money(34500, "INR")
        assert admission.sequence == 2
        events = [e.event_type for e in _events(kernel_engine, captured)]
        assert events == ["refund.provider_originated", "refund.admitted"]

    def test_a_provider_refund_matching_an_unresolved_local_one_is_left_to_reconciliation(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, Money(7000, "INR"))
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "unknown", None)
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            seen = record_provider_originated_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                provider_refund_id="rfnd_probably_ours",
                amount=Money(7000, "INR"),
                correlation_id=uuid7(),
            )
        assert not seen.changed
        assert seen.code is RecoveryCode.RECONCILIATION_IN_PROGRESS
        assert seen.explanation == "matches_unresolved_local_refund"
        assert len(_refund_rows(admin_engine, captured)) == 1
        assert _attempt_state(admin_engine, captured) == PaymentState.REFUND_UNKNOWN


# ------------------------------------------------------------------------ stale capture


class TestStaleCapture:
    def test_the_automatic_full_refund_is_idempotent(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        _set_attempt_state(admin_engine, captured, PaymentState.STALE_CAPTURE)

        def auto() -> RefundAdmission:
            with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
                set_tenant(session, captured.tenant_id)
                return admit_stale_capture_refund(
                    session,
                    tenant_id=captured.tenant_id,
                    payment_attempt_id=captured.attempt_id,
                    correlation_id=uuid7(),
                )

        first = auto()
        assert first.allowed
        assert first.amount == CAPTURED
        (row,) = _refund_rows(admin_engine, captured)
        assert row.reason_code == STALE_CAPTURE_REASON
        assert _attempt_state(admin_engine, captured) == PaymentState.REFUND_PENDING
        (event,) = _events(kernel_engine, captured)
        assert event.actor_type == ActorType.SYSTEM.value

        replay = auto()
        assert not replay.allowed
        assert replay.decision.code is RecoveryCode.DUPLICATE_OPERATION
        assert replay.refund_id == first.refund_id
        assert replay.grant_id == first.grant_id
        assert replay.idem_key == first.idem_key
        assert len(_refund_rows(admin_engine, captured)) == 1
        assert len(_grant_rows(admin_engine, captured)) == 1

        _consume(kernel_engine, captured, first)
        _result(kernel_engine, captured, first.refund_id, "processed", "rfnd_auto")
        assert _attempt_state(admin_engine, captured) == PaymentState.REFUNDED
        settled_replay = auto()
        assert settled_replay.refund_id == first.refund_id
        assert settled_replay.decision.code is RecoveryCode.DUPLICATE_OPERATION
        assert len(_refund_rows(admin_engine, captured)) == 1

    def test_a_failed_stale_refund_is_re_admitted_with_a_new_grant(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        _set_attempt_state(admin_engine, captured, PaymentState.STALE_CAPTURE)
        with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            first = admit_stale_capture_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                correlation_id=uuid7(),
            )
        _consume(kernel_engine, captured, first)
        _result(kernel_engine, captured, first.refund_id, "failed", None)
        # REFUND_FAILED -> REFUND_PENDING is the bounded retry edge; the stale reason
        # rides on the new row, so the idempotent lookup finds it next time.
        with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            retry = admit_stale_capture_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                correlation_id=uuid7(),
            )
        # The attempt is REFUND_FAILED, not STALE_CAPTURE, so the automatic path refuses
        # and the retry is an explicit admission (a person or policy decided to retry).
        assert not retry.allowed and retry.decision.explanation == "not_a_stale_capture"
        manual = _admit(kernel_engine, captured, None, reason=STALE_CAPTURE_REASON)
        assert manual.allowed and manual.grant_id != first.grant_id

    def test_a_partial_refund_of_a_stale_capture_is_refused(
        self, kernel_engine: Engine, admin_engine: Engine, captured: Captured
    ) -> None:
        _set_attempt_state(admin_engine, captured, PaymentState.STALE_CAPTURE)
        denied = _admit(kernel_engine, captured, Money(1, "INR"))
        assert denied.decision.explanation == "stale_capture_requires_full_refund"

    def test_the_automatic_path_refuses_a_healthy_capture(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            denied = admit_stale_capture_refund(
                session,
                tenant_id=captured.tenant_id,
                payment_attempt_id=captured.attempt_id,
                correlation_id=uuid7(),
            )
        assert not denied.allowed
        assert denied.decision.explanation == "not_a_stale_capture"


# ------------------------------------------------------------------------------ evidence


class TestEvidence:
    def test_the_whole_journey_leaves_an_intact_chain(
        self, kernel_engine: Engine, captured: Captured
    ) -> None:
        admission = _admit(kernel_engine, captured, Money(1000, "INR"))
        _consume(kernel_engine, captured, admission)
        _result(kernel_engine, captured, admission.refund_id, "processed", "rfnd_1")
        rest = _admit(kernel_engine, captured, None)
        _consume(kernel_engine, captured, rest)
        _result(kernel_engine, captured, rest.refund_id, "processed", "rfnd_2")
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, captured.tenant_id)
            verification = audit.verify_chain(
                session,
                tenant=captured.tenant_id,
                aggregate_type="payment_attempt",
                aggregate_id=captured.attempt_id,
            )
        assert verification.intact and verification.length == 4
        kinds = [e.event_type for e in _events(kernel_engine, captured)]
        assert kinds == ["refund.admitted", "refund.result", "refund.admitted", "refund.result"]

    def test_idempotency_keys_are_stable_and_distinct(self) -> None:
        tenant, attempt = uuid.uuid4(), uuid7()
        a = refund_idempotency_key(
            tenant_id=tenant, payment_attempt_id=attempt, sequence=1, amount=Money(100, "INR")
        )
        assert a == refund_idempotency_key(
            tenant_id=tenant, payment_attempt_id=attempt, sequence=1, amount=Money(100, "INR")
        )
        assert a != refund_idempotency_key(
            tenant_id=tenant, payment_attempt_id=attempt, sequence=2, amount=Money(100, "INR")
        )
        assert a.startswith("rfnd_") and len(a) <= 128
        with pytest.raises(RefundUsageError):
            refund_idempotency_key(
                tenant_id=tenant, payment_attempt_id=attempt, sequence=0, amount=Money(1, "INR")
            )

"""Adversarial refunds: money must never leave twice.

A refund is the one operation where the platform, not the buyer, initiates a movement of
money, and where the ledger is a sum rather than a single row. Both properties make it the
easiest place in the system to get a double spend, so this file attacks the arithmetic and
the concurrency together:

* ten callers refunding one capture at once;
* a refund larger than the capture, and a partial refund that would take the total past it;
* a refund racing one that is already in flight;
* a refund of an order that is already fully refunded;
* a storm of admissions and settlements interleaved, checked afterwards against the one
  invariant that matters: **the sum of everything settled never exceeds what was captured**.

The ledger is read from committed rows under the attempt lock, so every caller here is a
real session. Nothing about the sum can be proven against a stub.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from admission_support import APPROVED_TOTAL, SET_TENANT, Fixture, StubMerchant
from adversarial_support import race, scalar
from commerce_domain import Money, RecoveryCode, uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import refunds as refunds_module
from transaction_kernel.admission import AdmissionRequest, admit
from transaction_kernel.contracts import Operation
from transaction_kernel.grants import GrantBinding, consume_grant
from transaction_kernel.refunds import (
    RefundAdmission,
    admit_refund,
    record_refund_result,
    refundable_now,
)

pytestmark = pytest.mark.db

CAPTURE = APPROVED_TOTAL


@pytest.fixture(autouse=True)
def _purge_refunds(adm_admin_engine: Engine, admissible: Fixture):
    """Remove this suite's refund rows before the shared fixture removes the attempt.

    ``conftest.admissible`` deletes ``payment_attempts`` but not ``refunds``, which has a
    foreign key to it -- nothing in the admission suite creates a refund. Cleaning up
    here, in a fixture finalised before that one, keeps the mess local to this file.
    """
    yield
    with adm_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
        # execution_grants carries a foreign key to refunds (ADR 0003 D10), so the grants
        # go first. conftest deletes them again afterwards, which is harmless.
        conn.execute(
            text("DELETE FROM execution_grants WHERE tenant_id = :t"),
            {"t": admissible.tenant_id},
        )
        conn.execute(text("DELETE FROM refunds WHERE tenant_id = :t"), {"t": admissible.tenant_id})


@pytest.fixture
def captured(adm_kernel_engine: Engine, adm_admin_engine: Engine, admissible: Fixture) -> uuid.UUID:
    """One genuinely admitted attempt, advanced to CAPTURED.

    The admission is the kernel's own, so the attempt carries the amount, the checkout ref
    and the grant production would have written. The move to CAPTURED is applied directly
    because how the capture was learned is irrelevant to the refund ledger, and going
    through the webhook path would drag a provider into a test about arithmetic.
    """
    session = Session(adm_kernel_engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            decision = admit(
                session,
                AdmissionRequest(
                    tenant_id=admissible.tenant_id,
                    merchant_id=admissible.merchant_id,
                    checkout=admissible.checkout,
                    amount=CAPTURE,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    idempotency_key=f"idem-{uuid7().hex[:16]}",
                    principal=admissible.principal,
                    correlation_id=admissible.correlation_id,
                    approval_id=admissible.approval_id,
                ),
                StubMerchant(admissible.checkout.checkout_id),
            )
    finally:
        session.close()
    assert decision.allowed and decision.payment_attempt_id is not None

    with adm_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
        # The payment grant is consumed by the create-order command; leaving it ISSUED
        # would make every refund below deny on "grant_already_live" for the wrong reason.
        conn.execute(
            text(
                "UPDATE execution_grants SET status = 'CONSUMED', consumed_at = now() "
                "WHERE payment_attempt_id = :a AND status = 'ISSUED'"
            ),
            {"a": decision.payment_attempt_id},
        )
        conn.execute(
            text("UPDATE payment_attempts SET status = 'CAPTURED' WHERE id = :i"),
            {"i": decision.payment_attempt_id},
        )
    return decision.payment_attempt_id


def _admit_refund(session: Session, fixture: Fixture, attempt: uuid.UUID, amount: Money | None):
    return admit_refund(
        session,
        tenant_id=fixture.tenant_id,
        payment_attempt_id=attempt,
        amount=amount,
        reason_code="buyer_request",
        principal=fixture.principal,
        correlation_id=fixture.correlation_id,
    )


def _settle(
    kernel_engine: Engine, fixture: Fixture, admission: RefundAdmission, attempt: uuid.UUID
) -> None:
    """Walk one admitted refund through the sequence the worker actually performs.

    Consume the single-use grant, then record the provider's answer. Skipping the
    consumption would leave an ISSUED grant on the attempt, and every later refund would
    be denied ``grant_already_live`` -- a denial for the wrong reason, which would make
    the ledger tests below pass without exercising the arithmetic at all.
    """
    assert admission.refund_id is not None and admission.amount is not None
    session = Session(kernel_engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            consume_grant(
                session,
                admission.grant_id,
                GrantBinding(
                    tenant_id=fixture.tenant_id,
                    checkout=fixture.checkout,
                    payment_attempt_id=attempt,
                    operation=Operation.REFUND_EXECUTE,
                    amount=admission.amount,
                    refund_id=admission.refund_id,
                ),
            )
            record_refund_result(
                session,
                tenant_id=fixture.tenant_id,
                refund_id=admission.refund_id,
                outcome="processed",
                provider_refund_id=f"rfnd_{admission.refund_id.hex[:14]}",
                correlation_id=fixture.correlation_id,
            )
    finally:
        session.close()


def _book(kernel_engine: Engine, fixture: Fixture, attempt: uuid.UUID):
    session = Session(kernel_engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            return refundable_now(session, tenant_id=fixture.tenant_id, payment_attempt_id=attempt)
    finally:
        session.close()


def _settled_minor(admin_engine: Engine, fixture: Fixture, attempt: uuid.UUID) -> int:
    return int(
        scalar(
            admin_engine,
            fixture.tenant_id,
            "SELECT coalesce(sum(amount_minor), 0) FROM refunds "
            "WHERE payment_attempt_id = :a AND status = 'PROCESSED'",
            a=attempt,
        )
    )


def _reserved_minor(admin_engine: Engine, fixture: Fixture, attempt: uuid.UUID) -> int:
    return int(
        scalar(
            admin_engine,
            fixture.tenant_id,
            "SELECT coalesce(sum(amount_minor), 0) FROM refunds WHERE payment_attempt_id = :a "
            "AND status IN ('PENDING','PROCESSED','UNKNOWN','RECONCILING','ESCALATED')",
            a=attempt,
        )
    )


class TestConcurrentRefundsOnOneCapture:
    def test_ten_callers_refunding_one_capture_reserve_it_once(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        outcomes = race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: _admit_refund(session, admissible, captured, None),
            workers=10,
        )
        crashed = [o for o in outcomes if not o.ok]
        assert not crashed, f"a refund caller crashed: {crashed[0].error!r}"

        admissions: list[RefundAdmission] = [o.value for o in outcomes]
        allowed = [a for a in admissions if a.allowed]
        assert len(allowed) == 1, f"{len(allowed)} refunds were admitted against one capture"

        for denied in (a for a in admissions if not a.allowed):
            assert denied.decision.code is RecoveryCode.CONCURRENT_OPERATION, (
                f"a losing refund carried {denied.decision.code}; nothing was confirmed, "
                "so it must not be reportable as a completed money action"
            )
            assert denied.refund_id is None and denied.grant_id is None

        assert _reserved_minor(adm_admin_engine, admissible, captured) == CAPTURE.minor
        book = _book(adm_kernel_engine, admissible, captured)
        assert book.remaining.is_zero

    def test_one_grant_exists_for_the_admitted_refund_and_no_other(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        outcomes = race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: _admit_refund(session, admissible, captured, None),
            workers=8,
        )
        winner = next(o.value for o in outcomes if o.ok and o.value.allowed)
        rows = scalar(
            adm_admin_engine,
            admissible.tenant_id,
            "SELECT count(*) FROM execution_grants WHERE payment_attempt_id = :a "
            "AND operation = 'REFUND_EXECUTE'",
            a=captured,
        )
        assert rows == 1
        bound_to = scalar(
            adm_admin_engine,
            admissible.tenant_id,
            "SELECT refund_id FROM execution_grants WHERE id = :g",
            g=winner.grant_id,
        )
        assert bound_to == winner.refund_id, (
            "the refund grant is not bound to the refunds row it authorises (ADR 0003 D10)"
        )


class TestAmountCeiling:
    def test_a_refund_larger_than_the_capture_is_denied_with_both_figures(
        self, adm_kernel_engine, admissible, captured
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                denied = _admit_refund(
                    session, admissible, captured, Money(CAPTURE.minor + 1, "INR")
                )
        finally:
            session.close()
        assert not denied.allowed
        assert denied.decision.code is RecoveryCode.POLICY_EXCEPTION
        delta = next(d for d in denied.decision.deltas if d.field_path == "amount_minor")
        assert delta.approved == CAPTURE.minor
        assert delta.current == CAPTURE.minor + 1

    def test_a_partial_refund_cannot_be_topped_up_past_the_capture(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        """Sixty per cent, settled, then sixty per cent again."""
        sixty = Money(CAPTURE.minor * 6 // 10, "INR")

        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_refund(session, admissible, captured, sixty)
        session.close()
        assert first.allowed
        _settle(adm_kernel_engine, admissible, first, captured)

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                second = _admit_refund(session, admissible, captured, sixty)
        finally:
            session.close()
        assert not second.allowed
        assert second.decision.code is RecoveryCode.POLICY_EXCEPTION
        assert _settled_minor(adm_admin_engine, admissible, captured) == sixty.minor

    def test_a_fully_refunded_capture_has_nothing_remaining(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_refund(session, admissible, captured, None)
        session.close()
        _settle(adm_kernel_engine, admissible, first, captured)

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                again = _admit_refund(session, admissible, captured, None)
        finally:
            session.close()
        assert not again.allowed
        assert again.decision.code is RecoveryCode.POLICY_EXCEPTION
        assert _settled_minor(adm_admin_engine, admissible, captured) == CAPTURE.minor

    def test_a_zero_or_negative_refund_is_refused(self, adm_kernel_engine, admissible, captured):
        for amount in (Money(0, "INR"), Money(-100, "INR")):
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                    denied = _admit_refund(session, admissible, captured, amount)
            finally:
                session.close()
            assert not denied.allowed, f"{amount.minor} was admitted as a refund"

    def test_a_refund_in_another_currency_is_refused(self, adm_kernel_engine, admissible, captured):
        """Not converted, not rounded: 39500 USD is not 39500 INR."""
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                denied = _admit_refund(session, admissible, captured, Money(CAPTURE.minor, "USD"))
        finally:
            session.close()
        assert not denied.allowed
        assert denied.decision.code is RecoveryCode.POLICY_EXCEPTION


class TestRefundWhileAnotherIsInFlight:
    def test_a_second_refund_is_denied_while_the_first_is_pending(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        tenth = Money(CAPTURE.minor // 10, "INR")
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_refund(session, admissible, captured, tenth)
        session.close()
        assert first.allowed

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                second = _admit_refund(session, admissible, captured, tenth)
        finally:
            session.close()
        assert not second.allowed
        assert second.decision.code is RecoveryCode.CONCURRENT_OPERATION
        assert _reserved_minor(adm_admin_engine, admissible, captured) == tenth.minor

    def test_an_unknown_refund_is_reconciled_rather_than_re_admitted(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        """The single most expensive mistake available: a timed-out refund retried blind.

        The provider may already hold the request, so the only lawful exit is
        reconciliation. A second admission here would be a second refund.
        """
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_refund(session, admissible, captured, None)
        session.close()

        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            record_refund_result(
                session,
                tenant_id=admissible.tenant_id,
                refund_id=first.refund_id,
                outcome="unknown",
                provider_refund_id=None,
                correlation_id=admissible.correlation_id,
            )
        session.close()

        outcomes = race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: _admit_refund(session, admissible, captured, None),
            workers=6,
        )
        for outcome in outcomes:
            assert outcome.ok, outcome.error
            assert not outcome.value.allowed
            assert outcome.value.decision.code is RecoveryCode.RECONCILIATION_IN_PROGRESS

        assert _settled_minor(adm_admin_engine, admissible, captured) == 0


class TestTheSumNeverExceedsTheCapture:
    @pytest.mark.slow
    def test_a_storm_of_admissions_and_settlements_never_over_refunds(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        """Ten rounds of contention, each settling whatever won.

        Each round has six callers asking for a third of the capture at once. Whoever wins
        is settled immediately, so the next round races against a moved ledger. After ten
        rounds the arithmetic must still hold: nothing settled beyond the capture, and no
        round admitted two refunds.
        """
        third = Money(CAPTURE.minor // 3, "INR")
        admitted_rounds = 0

        for _ in range(10):
            outcomes = race(
                adm_kernel_engine,
                admissible.tenant_id,
                lambda session, _: _admit_refund(session, admissible, captured, third),
                workers=6,
            )
            crashed = [o for o in outcomes if not o.ok]
            assert not crashed, f"a refund caller crashed: {crashed[0].error!r}"
            winners = [o.value for o in outcomes if o.value.allowed]
            assert len(winners) <= 1, f"{len(winners)} refunds admitted in one round"
            if winners:
                admitted_rounds += 1
                _settle(adm_kernel_engine, admissible, winners[0], captured)

            settled = _settled_minor(adm_admin_engine, admissible, captured)
            assert settled <= CAPTURE.minor, (
                f"{settled} minor units have been refunded against a capture of "
                f"{CAPTURE.minor}; money left twice"
            )

        assert admitted_rounds == 3, (
            f"a third of the capture should be admissible exactly three times, not "
            f"{admitted_rounds}"
        )
        assert _settled_minor(adm_admin_engine, admissible, captured) == third.minor * 3

    def test_the_ledger_refuses_to_report_a_state_it_cannot_explain(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        """A planted over-refund must stop the module rather than be refunded further.

        Application roles cannot write ``refunds``, so this row is planted through the
        owner connection -- the shape a restored backup or a manual correction would have.
        The point is that the ledger fails closed instead of quietly admitting more.
        """
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO refunds (id, tenant_id, payment_attempt_id, checkout_id, "
                    "status, amount_minor, currency, idem_key, provider_originated, "
                    "reason_code) VALUES (:id, :t, :a, :c, 'PROCESSED', :amt, 'INR', :k, "
                    "false, 'planted')"
                ),
                {
                    "id": uuid7(),
                    "t": admissible.tenant_id,
                    "a": captured,
                    "c": admissible.checkout.checkout_id,
                    "amt": CAPTURE.minor + 1,
                    "k": f"planted-{uuid7().hex[:16]}",
                },
            )

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(refunds_module.RefundLedgerError):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                _admit_refund(session, admissible, captured, Money(100, "INR"))
        finally:
            session.close()


class TestRefundsOfStatesThatCannotRefund:
    @pytest.mark.parametrize("state", ["AUTHORIZED", "FAILED", "CREATED", "SUBMITTED"])
    def test_an_uncaptured_attempt_cannot_be_refunded(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured, state: str
    ):
        """Money that has not moved cannot come back. Each of these is a way an attempt
        could be misread as refundable by something that only looked at the amount."""
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text("UPDATE payment_attempts SET status = :s WHERE id = :i"),
                {"s": state, "i": captured},
            )
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                denied = _admit_refund(session, admissible, captured, None)
        finally:
            session.close()
        assert not denied.allowed, f"a refund was admitted against a {state} attempt"
        assert denied.decision.code is RecoveryCode.POLICY_EXCEPTION

    def test_an_attempt_belonging_to_another_tenant_is_not_found(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        stranger = uuid.uuid4()
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
            foreign = Fixture(
                tenant_id=stranger,
                merchant_id=admissible.merchant_id,
                checkout=admissible.checkout,
                principal=admissible.principal.__class__(
                    principal_id="p-foreign",
                    tenant_id=stranger,
                    actor_type=admissible.principal.actor_type,
                ),
                correlation_id=admissible.correlation_id,
            )
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(stranger)})
                    denied = _admit_refund(session, foreign, captured, None)
            finally:
                session.close()
            assert not denied.allowed
            assert denied.decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        finally:
            with adm_admin_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(stranger)})
                conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": stranger})
                conn.execute(SET_TENANT, {"t": None})
                conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": stranger})


class TestSettlementIsNotRewritten:
    def test_a_redelivered_settlement_is_a_no_op(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_refund(session, admissible, captured, None)
        session.close()

        results: list[Any] = []
        for _ in range(3):
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                    results.append(
                        record_refund_result(
                            session,
                            tenant_id=admissible.tenant_id,
                            refund_id=first.refund_id,
                            outcome="processed",
                            provider_refund_id=f"rfnd_{first.refund_id.hex[:14]}",
                            correlation_id=admissible.correlation_id,
                        )
                    )
            finally:
                session.close()

        assert results[0].changed is True
        assert [r.changed for r in results[1:]] == [False, False]
        assert _settled_minor(adm_admin_engine, admissible, captured) == CAPTURE.minor

    def test_concurrent_settlements_of_one_refund_settle_it_once(
        self, adm_kernel_engine, adm_admin_engine, admissible, captured
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_refund(session, admissible, captured, None)
        session.close()

        def body(session: Session, _: int):
            return record_refund_result(
                session,
                tenant_id=admissible.tenant_id,
                refund_id=first.refund_id,
                outcome="processed",
                provider_refund_id=f"rfnd_{first.refund_id.hex[:14]}",
                correlation_id=admissible.correlation_id,
            )

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=6)
        crashed = [o for o in outcomes if not o.ok]
        assert not crashed, f"a settlement crashed: {crashed[0].error!r}"
        changed = [o for o in outcomes if o.value.changed]
        assert len(changed) == 1, f"{len(changed)} settlements changed the refund"
        assert _settled_minor(adm_admin_engine, admissible, captured) == CAPTURE.minor

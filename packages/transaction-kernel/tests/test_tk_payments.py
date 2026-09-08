"""The payment-attempt lifecycle after admission, against a real PostgreSQL.

Every attempt here is created by the real admission path (``admit`` through the
``admissible`` fixture) so that the row, the grant and the consumed reservation are
exactly what production produces. The claims that matter are then proven on the
database: a late ``authorized`` cannot rewind a capture, a duplicate changes nothing, a
stale capture writes no order, a mismatch opens one case, and two concurrent captures
produce one order row. Mocking any of that would prove the mock.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from admission_support import APPROVED_TOTAL, Fixture, StubMerchant
from commerce_domain import uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import grants, payments
from transaction_kernel.admission import AdmissionRequest, admit
from transaction_kernel.contracts import Operation
from transaction_kernel.evidence import EvidenceSource, ProviderEvidence
from transaction_kernel.payments import (
    AttemptNotFoundError,
    InboxRowNotFoundError,
    PaymentsError,
    PaymentStateConflictError,
    PaymentsTenantError,
    PaymentsUsageError,
    ProviderOrderConflictError,
    ProviderOrderOutcome,
)
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.states import CheckoutState, PaymentState

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

ORDER_ID = "order_9A33XWu170gUtm"
PAYMENT_ID = "pay_29QQoUBi66xm2f"
OTHER_PAYMENT_ID = "pay_0therPayment00"
DIGEST = "b" * 64

Factory = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class Attempt:
    """One admitted attempt: the row admission created, and the grant it issued."""

    fx: Fixture
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    correlation_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.fx.tenant_id

    @property
    def checkout_id(self) -> uuid.UUID:
        return self.fx.checkout.checkout_id

    @property
    def version(self) -> int:
        return self.fx.checkout.version


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
def attempt(
    admissible: Fixture,
    merchant: StubMerchant,
    kernel_session_factory: Factory,
    adm_admin_engine: Engine,
) -> Iterator[Attempt]:
    """Admit the fixture checkout for real, then move the checkout to EXECUTION_PENDING.

    The kernel's admission does not transition the checkout column; the API does that in
    the same transaction (see the kernel map's caller obligations), so the fixture does
    it here to hand the tests the exact state the worker meets.
    """
    session = kernel_session_factory()
    with session.begin():
        session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
        decision = admit(
            session,
            AdmissionRequest(
                tenant_id=admissible.tenant_id,
                merchant_id=admissible.merchant_id,
                checkout=admissible.checkout,
                amount=APPROVED_TOTAL,
                operation=Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=f"idem-{uuid7().hex[:16]}",
                principal=admissible.principal,
                correlation_id=admissible.correlation_id,
                approval_id=admissible.approval_id,
            ),
            merchant,
        )
        assert decision.allowed, decision
        assert decision.payment_attempt_id is not None
        assert decision.grant_id is not None
        session.execute(
            text(
                "UPDATE checkout_versions SET status = :s WHERE tenant_id = :t "
                "AND checkout_id = :c AND version = :v"
            ),
            {
                "s": CheckoutState.EXECUTION_PENDING.value,
                "t": admissible.tenant_id,
                "c": admissible.checkout.checkout_id,
                "v": admissible.checkout.version,
            },
        )
    yield Attempt(
        fx=admissible,
        attempt_id=decision.payment_attempt_id,
        grant_id=decision.grant_id,
        correlation_id=admissible.correlation_id,
    )

    # Children of the rows the admission fixture tears down, deleted first; the
    # ``admissible`` teardown runs after this generator resumes.
    with adm_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
        for table in (
            "provider_requests",
            "reconciliation_runs",
            "orders",
            "webhook_inbox",
            "checkouts",
        ):
            # S608: `table` iterates the literal tuple above, never request data.
            statement = text(f"DELETE FROM {table} WHERE tenant_id = :t")  # noqa: S608
            conn.execute(statement, {"t": admissible.tenant_id})
        # Attempts inserted directly by a test (a second attempt for a conflict case) are
        # deleted by the admission fixture by tenant, so nothing more is needed here.


# ------------------------------------------------------------------------- helpers


def run[T](factory: Factory, a: Attempt, fn: Callable[[Session], T]) -> T:
    """One kernel-role transaction with the tenant bound, as every caller must do."""
    session = factory()
    with session.begin():
        session.execute(SET_TENANT, {"t": str(a.tenant_id)})
        return fn(session)


def ok_outcome(order_id: str = ORDER_ID) -> ProviderOrderOutcome:
    return ProviderOrderOutcome("ok", order_id, RecoveryCode.OK, "order_created")


def submit(factory: Factory, a: Attempt) -> payments.AttemptTransition:
    """The worker's create-order success: CREATED -> SUBMITTED with the provider order id."""
    return run(
        factory,
        a,
        lambda s: payments.record_create_order_result(
            s,
            tenant_id=a.tenant_id,
            payment_attempt_id=a.attempt_id,
            outcome=ok_outcome(),
            correlation_id=a.correlation_id,
        ),
    )


def evidence(**overrides: Any) -> ProviderEvidence:
    base: dict[str, Any] = {
        "source": "PROVIDER_FETCH",
        "provider_payment_id": PAYMENT_ID,
        "provider_order_id": ORDER_ID,
        "amount_minor": APPROVED_TOTAL.minor,
        "currency": APPROVED_TOTAL.currency,
        "status": "captured",
        "provider_status": "captured",
        "created_at": 1_767_225_600,
        "raw_digest": DIGEST,
        "http_status": 200,
    }
    base.update(overrides)
    return ProviderEvidence.from_mapping(base)


def apply(factory: Factory, a: Attempt, ev: ProviderEvidence) -> payments.EvidenceApplied:
    return run(
        factory,
        a,
        lambda s: payments.apply_provider_evidence(
            s,
            tenant_id=a.tenant_id,
            payment_attempt_id=a.attempt_id,
            evidence=ev,
            correlation_id=a.correlation_id,
        ),
    )


def attempt_row(factory: Factory, a: Attempt) -> payments.AttemptView:
    view = run(
        factory,
        a,
        lambda s: payments.read_attempt(s, tenant_id=a.tenant_id, payment_attempt_id=a.attempt_id),
    )
    assert view is not None
    return view


def checkout_status(factory: Factory, a: Attempt) -> CheckoutState:
    raw = run(
        factory,
        a,
        lambda s: s.execute(
            text(
                "SELECT status FROM checkout_versions WHERE tenant_id = :t AND checkout_id = :c "
                "AND version = :v"
            ),
            {"t": a.tenant_id, "c": a.checkout_id, "v": a.version},
        ).scalar_one(),
    )
    return CheckoutState(raw)


def set_checkout_status(factory: Factory, a: Attempt, status: CheckoutState) -> None:
    run(
        factory,
        a,
        lambda s: s.execute(
            text(
                "UPDATE checkout_versions SET status = :s WHERE tenant_id = :t "
                "AND checkout_id = :c AND version = :v"
            ),
            {"s": status.value, "t": a.tenant_id, "c": a.checkout_id, "v": a.version},
        ),
    )


def reservation_status(factory: Factory, a: Attempt) -> str:
    return run(
        factory,
        a,
        lambda s: s.execute(
            text(
                "SELECT status FROM reservations WHERE tenant_id = :t AND checkout_id = :c "
                "AND checkout_version = :v ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": a.tenant_id, "c": a.checkout_id, "v": a.version},
        ).scalar_one(),
    )


def orders(factory: Factory, a: Attempt) -> list[Any]:
    return run(
        factory,
        a,
        lambda s: s.execute(
            text(
                "SELECT id, status, total_minor, currency, policy_receipt_id, capture_evidence "
                "FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a"
            ),
            {"t": a.tenant_id, "a": a.attempt_id},
        ).all(),
    )


def audit_rows(factory: Factory, a: Attempt) -> list[tuple[str, dict[str, Any]]]:
    rows = run(
        factory,
        a,
        lambda s: s.execute(
            text(
                "SELECT event_type, payload FROM audit_events WHERE tenant_id = :t "
                "AND aggregate_type = 'checkout' AND aggregate_id = :c ORDER BY seq"
            ),
            {"t": a.tenant_id, "c": a.checkout_id},
        ).all(),
    )
    return [(r.event_type, r.payload) for r in rows]


def events_of(factory: Factory, a: Attempt, event_type: str) -> list[dict[str, Any]]:
    return [p for t, p in audit_rows(factory, a) if t == event_type]


def consume_grant(factory: Factory, a: Attempt) -> None:
    run(
        factory,
        a,
        lambda s: grants.consume_grant(
            s,
            a.grant_id,
            grants.GrantBinding(
                tenant_id=a.tenant_id,
                checkout=a.fx.checkout,
                payment_attempt_id=a.attempt_id,
                operation=Operation.PAYMENT_CREATE_ORDER,
                amount=APPROVED_TOTAL,
            ),
        ),
    )


# -------------------------------------------------------------------------- guards


class TestGuards:
    def test_refuses_to_run_outside_a_transaction(
        self, adm_kernel_engine: Engine, attempt: Attempt
    ) -> None:
        session = Session(adm_kernel_engine)
        try:
            with pytest.raises(PaymentsUsageError):
                payments.read_attempt(
                    session, tenant_id=attempt.tenant_id, payment_attempt_id=attempt.attempt_id
                )
        finally:
            session.close()

    def test_tenant_argument_must_match_the_bound_tenant(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """The GUC is the authority; an argument naming another tenant is refused."""
        with pytest.raises(PaymentsTenantError):
            run(
                kernel_session_factory,
                attempt,
                lambda s: payments.read_attempt(
                    s, tenant_id=uuid.uuid4(), payment_attempt_id=attempt.attempt_id
                ),
            )

    def test_an_unknown_attempt_is_refused_with_a_code(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        with pytest.raises(AttemptNotFoundError) as info:
            run(
                kernel_session_factory,
                attempt,
                lambda s: payments.record_create_order_result(
                    s,
                    tenant_id=attempt.tenant_id,
                    payment_attempt_id=uuid7(),
                    outcome=ok_outcome(),
                    correlation_id=attempt.correlation_id,
                ),
            )
        assert info.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    def test_a_browser_callback_is_never_applied_as_evidence(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """ADR D8, enforced at the door rather than by the fulfilment gate alone."""
        submit(kernel_session_factory, attempt)
        with pytest.raises(PaymentsUsageError, match="browser callback"):
            apply(kernel_session_factory, attempt, evidence(source="BROWSER_CALLBACK"))
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.SUBMITTED

    def test_evidence_needs_a_recorded_provider_order_to_check_against(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        with pytest.raises(PaymentsUsageError, match="no provider order"):
            apply(kernel_session_factory, attempt, evidence())


# ------------------------------------------------------------- create-order result


class TestCreateOrderResult:
    def test_ok_submits_the_attempt_and_opens_the_payment_surface(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        moved = submit(kernel_session_factory, attempt)
        assert (moved.state_before, moved.state_after) == (
            PaymentState.CREATED,
            PaymentState.SUBMITTED,
        )
        assert moved.checkout_state_after is CheckoutState.AWAITING_PAYMENT
        row = attempt_row(kernel_session_factory, attempt)
        assert row.status is PaymentState.SUBMITTED
        assert row.provider_order_id == ORDER_ID
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.AWAITING_PAYMENT
        recorded = events_of(kernel_session_factory, attempt, "payment.order_result_recorded")
        assert len(recorded) == 1
        assert recorded[0]["provider_order_id"] == ORDER_ID
        assert recorded[0]["state_after"] == "SUBMITTED"

    def test_a_redelivered_ok_is_a_no_op(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        again = submit(kernel_session_factory, attempt)
        assert not again.changed
        assert len(events_of(kernel_session_factory, attempt, "payment.order_result_recorded")) == 1

    def test_the_attempt_can_be_found_by_its_provider_order(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        found = run(
            kernel_session_factory,
            attempt,
            lambda s: payments.find_attempt_by_provider_order(
                s, tenant_id=attempt.tenant_id, provider_order_id=ORDER_ID
            ),
        )
        assert found is not None and found.attempt_id == attempt.attempt_id
        absent = run(
            kernel_session_factory,
            attempt,
            lambda s: payments.find_attempt_by_provider_order(
                s, tenant_id=attempt.tenant_id, provider_order_id="order_nobody"
            ),
        )
        assert absent is None

    def test_failed_releases_the_reservation(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        assert reservation_status(kernel_session_factory, attempt) == "CONSUMED"
        moved = run(
            kernel_session_factory,
            attempt,
            lambda s: payments.record_create_order_result(
                s,
                tenant_id=attempt.tenant_id,
                payment_attempt_id=attempt.attempt_id,
                outcome=ProviderOrderOutcome(
                    "failed", None, RecoveryCode.PAYMENT_FAILED, "provider_refused"
                ),
                correlation_id=attempt.correlation_id,
            ),
        )
        assert moved.state_after is PaymentState.FAILED
        assert moved.checkout_state_after is CheckoutState.PAYMENT_FAILED
        assert reservation_status(kernel_session_factory, attempt) == "RELEASED"

    def test_unknown_holds_the_reservation_and_marks_the_checkout_unknown(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """Specification 10.7: a lost response is UNKNOWN, never FAILED, and stock is kept."""
        moved = run(
            kernel_session_factory,
            attempt,
            lambda s: payments.record_create_order_result(
                s,
                tenant_id=attempt.tenant_id,
                payment_attempt_id=attempt.attempt_id,
                outcome=ProviderOrderOutcome(
                    "unknown", None, RecoveryCode.PAYMENT_UNKNOWN, "transport_timeout"
                ),
                correlation_id=attempt.correlation_id,
            ),
        )
        assert moved.state_after is PaymentState.UNKNOWN
        assert moved.checkout_state_after is CheckoutState.PAYMENT_UNKNOWN
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.PAYMENT_UNKNOWN
        assert reservation_status(kernel_session_factory, attempt) == "CONSUMED"

    def test_a_result_after_the_attempt_moved_is_a_structured_conflict(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        with pytest.raises(PaymentStateConflictError) as info:
            run(
                kernel_session_factory,
                attempt,
                lambda s: payments.record_create_order_result(
                    s,
                    tenant_id=attempt.tenant_id,
                    payment_attempt_id=attempt.attempt_id,
                    outcome=ProviderOrderOutcome(
                        "failed", None, RecoveryCode.PAYMENT_FAILED, "provider_refused"
                    ),
                    correlation_id=attempt.correlation_id,
                ),
            )
        assert info.value.code is RecoveryCode.CONCURRENT_OPERATION
        assert info.value.current is PaymentState.SUBMITTED

    def test_one_provider_order_belongs_to_one_attempt(
        self, kernel_session_factory: Factory, adm_admin_engine: Engine, attempt: Attempt
    ) -> None:
        """``uq_payment_attempts_provider_order``: a second claim is refused, not stored."""
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(attempt.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
                    "status, amount_minor, currency, receipt, provider_order_id) VALUES "
                    "(:id, :t, :c, 1, 'FAILED', 100, 'INR', :r, :o)"
                ),
                {
                    "id": uuid7(),
                    "t": attempt.tenant_id,
                    "c": uuid7(),
                    "r": "rcpt_other",
                    "o": ORDER_ID,
                },
            )
        with pytest.raises(ProviderOrderConflictError) as info:
            submit(kernel_session_factory, attempt)
        assert info.value.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.CREATED

    def test_outcome_shape_is_validated(self) -> None:
        with pytest.raises(PaymentsUsageError):
            ProviderOrderOutcome("ok", None, RecoveryCode.OK, "x")
        with pytest.raises(PaymentsUsageError):
            ProviderOrderOutcome("failed", ORDER_ID, RecoveryCode.PAYMENT_FAILED, "x")
        with pytest.raises(PaymentsUsageError):
            ProviderOrderOutcome("ok", ORDER_ID, RecoveryCode.OK, "Not A Token")


# --------------------------------------------------------------- browser callback


class TestBrowserCallback:
    def _callback(
        self, factory: Factory, a: Attempt, order_id: str, payment_id: str
    ) -> payments.BrowserCallbackRecorded:
        return run(
            factory,
            a,
            lambda s: payments.record_browser_callback(
                s,
                tenant_id=a.tenant_id,
                payment_attempt_id=a.attempt_id,
                provider_order_id=order_id,
                provider_payment_id=payment_id,
                correlation_id=a.correlation_id,
                principal_id="buyer-1",
            ),
        )

    def test_records_the_payment_id_and_never_the_state(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        first = self._callback(kernel_session_factory, attempt, ORDER_ID, PAYMENT_ID)
        assert first.accepted and first.code is RecoveryCode.OK
        row = attempt_row(kernel_session_factory, attempt)
        assert row.provider_payment_id == PAYMENT_ID
        assert row.status is PaymentState.SUBMITTED, "a callback is not capture evidence"
        assert not orders(kernel_session_factory, attempt)

    def test_a_repeat_is_accepted_and_a_different_payment_is_refused(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        self._callback(kernel_session_factory, attempt, ORDER_ID, PAYMENT_ID)
        repeat = self._callback(kernel_session_factory, attempt, ORDER_ID, PAYMENT_ID)
        assert repeat.accepted and repeat.code is RecoveryCode.DUPLICATE_OPERATION
        other = self._callback(kernel_session_factory, attempt, ORDER_ID, OTHER_PAYMENT_ID)
        assert not other.accepted and other.code is RecoveryCode.CONCURRENT_OPERATION
        assert attempt_row(kernel_session_factory, attempt).provider_payment_id == PAYMENT_ID
        assert len(events_of(kernel_session_factory, attempt, "payment.browser_callback")) == 3

    def test_an_order_not_bound_to_the_attempt_is_refused(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        verdict = self._callback(kernel_session_factory, attempt, "order_someoneElse", PAYMENT_ID)
        assert not verdict.accepted
        assert verdict.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert attempt_row(kernel_session_factory, attempt).provider_payment_id is None


# --------------------------------------------------------------- provider evidence


class TestApplyEvidence:
    def test_a_capture_confirms_exactly_one_order_and_pays_the_checkout(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        applied = apply(kernel_session_factory, attempt, evidence())
        assert (applied.state_before, applied.state_after) == (
            PaymentState.SUBMITTED,
            PaymentState.CAPTURED,
        )
        assert applied.changed and applied.order_id is not None and not applied.stale_capture
        assert applied.checkout_state_after is CheckoutState.PAID
        rows = orders(kernel_session_factory, attempt)
        assert len(rows) == 1
        order = rows[0]
        assert order.id == applied.order_id
        assert order.status == "CONFIRMED"
        assert (order.total_minor, order.currency) == (APPROVED_TOTAL.minor, "INR")
        assert order.policy_receipt_id is not None
        assert order.capture_evidence["source"] == "PROVIDER_FETCH"
        assert order.capture_evidence["provider_payment_id"] == PAYMENT_ID
        assert order.capture_evidence["raw_digest"] == DIGEST
        assert attempt_row(kernel_session_factory, attempt).provider_payment_id == PAYMENT_ID
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.PAID

    def test_the_audit_row_carries_source_event_and_both_states(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        apply(kernel_session_factory, attempt, evidence(source="WEBHOOK", event_id="evt_123"))
        applied = events_of(kernel_session_factory, attempt, "payment.evidence_applied")
        assert len(applied) == 1
        assert applied[0]["source"] == "WEBHOOK"
        assert applied[0]["event_id"] == "evt_123"
        assert applied[0]["state_before"] == "SUBMITTED"
        assert applied[0]["state_after"] == "CAPTURED"
        assert applied[0]["changed"] is True
        assert applied[0]["payment_attempt_id"] == str(attempt.attempt_id)

    def test_captured_never_regresses_to_a_late_authorized(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """Invariant 2 of the state module, proven on the row and on the order."""
        submit(kernel_session_factory, attempt)
        apply(kernel_session_factory, attempt, evidence())
        late = apply(
            kernel_session_factory,
            attempt,
            evidence(status="authorized", provider_status="authorized"),
        )
        assert not late.changed
        assert late.state_after is PaymentState.CAPTURED
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.CAPTURED
        assert len(orders(kernel_session_factory, attempt)) == 1
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.PAID

    def test_duplicate_evidence_is_a_no_op_that_still_names_the_order(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        first = apply(kernel_session_factory, attempt, evidence())
        second = apply(kernel_session_factory, attempt, evidence())
        assert first.changed and not second.changed
        assert second.order_id == first.order_id
        assert second.reason == "duplicate_or_weaker_evidence"
        assert len(orders(kernel_session_factory, attempt)) == 1
        assert len(events_of(kernel_session_factory, attempt, "payment.evidence_applied")) == 2

    def test_an_authorization_moves_the_attempt_but_not_the_checkout(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        applied = apply(
            kernel_session_factory,
            attempt,
            evidence(status="authorized", provider_status="authorized"),
        )
        assert applied.state_after is PaymentState.AUTHORIZED
        assert applied.order_id is None
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.AWAITING_PAYMENT
        assert not orders(kernel_session_factory, attempt)
        # ...and the capture that follows completes the sale.
        done = apply(kernel_session_factory, attempt, evidence())
        assert done.state_after is PaymentState.CAPTURED and done.order_id is not None

    def test_a_pending_report_changes_nothing(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        applied = apply(
            kernel_session_factory, attempt, evidence(status="pending", provider_status="created")
        )
        assert not applied.changed and applied.reason == "pending_no_transition"
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.SUBMITTED

    def test_a_failure_fails_the_checkout_and_releases_the_reservation(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        applied = apply(
            kernel_session_factory,
            attempt,
            evidence(status="failed", provider_status="failed", error_code="BAD_REQUEST_ERROR"),
        )
        assert applied.state_after is PaymentState.FAILED
        assert applied.checkout_state_after is CheckoutState.PAYMENT_FAILED
        assert reservation_status(kernel_session_factory, attempt) == "RELEASED"
        assert not orders(kernel_session_factory, attempt)

    def test_a_failed_report_for_another_payment_does_not_end_the_attempt(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """A first try that failed is not the payment the browser said succeeded."""
        submit(kernel_session_factory, attempt)
        run(
            kernel_session_factory,
            attempt,
            lambda s: payments.record_browser_callback(
                s,
                tenant_id=attempt.tenant_id,
                payment_attempt_id=attempt.attempt_id,
                provider_order_id=ORDER_ID,
                provider_payment_id=PAYMENT_ID,
                correlation_id=attempt.correlation_id,
            ),
        )
        applied = apply(
            kernel_session_factory,
            attempt,
            evidence(
                provider_payment_id=OTHER_PAYMENT_ID, status="failed", provider_status="failed"
            ),
        )
        assert not applied.changed
        assert applied.reason == "failed_report_names_other_payment"
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.SUBMITTED

    def test_a_capture_on_an_invalidated_checkout_is_stale_and_writes_no_order(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """Specification 10.8: never fulfil the invalidated version; refund it instead."""
        submit(kernel_session_factory, attempt)
        set_checkout_status(
            kernel_session_factory, attempt, CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT
        )
        applied = apply(kernel_session_factory, attempt, evidence())
        assert applied.stale_capture
        assert applied.state_after is PaymentState.STALE_CAPTURE
        assert applied.order_id is None
        assert not orders(kernel_session_factory, attempt)
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.STALE_CAPTURE
        assert (
            checkout_status(kernel_session_factory, attempt)
            is CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT
        )
        # A redelivery of the same capture is still a no-op with no order.
        again = apply(kernel_session_factory, attempt, evidence())
        assert not again.changed and again.order_id is None
        assert not orders(kernel_session_factory, attempt)

    def test_a_failure_on_an_invalidated_checkout_closes_it(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        set_checkout_status(
            kernel_session_factory, attempt, CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT
        )
        applied = apply(
            kernel_session_factory, attempt, evidence(status="failed", provider_status="failed")
        )
        assert applied.state_after is PaymentState.FAILED
        assert applied.checkout_state_after is CheckoutState.INVALIDATED

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("amount_minor", APPROVED_TOTAL.minor + 1, "amount_mismatch"),
            ("currency", "USD", "currency_mismatch"),
            ("provider_order_id", "order_someoneElse", "provider_order_id_mismatch"),
        ],
    )
    def test_mismatched_evidence_escalates_exactly_once_and_moves_nothing_from_it(
        self,
        kernel_session_factory: Factory,
        attempt: Attempt,
        field: str,
        value: Any,
        reason: str,
    ) -> None:
        submit(kernel_session_factory, attempt)
        wrong = evidence(**{field: value})
        first = apply(kernel_session_factory, attempt, wrong)
        second = apply(kernel_session_factory, attempt, wrong)

        assert first.reason == reason and second.reason == reason
        assert first.order_id is None and second.order_id is None
        assert first.state_after is PaymentState.ESCALATED
        assert first.case_key == second.case_key
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.ESCALATED
        assert not orders(kernel_session_factory, attempt)
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.AWAITING_PAYMENT

        opened = events_of(kernel_session_factory, attempt, "human_review.opened")
        assert len(opened) == 1, "concurrent or repeated detectors must share one case"
        assert opened[0]["case_key"] == first.case_key
        assert opened[0]["path"] == ["UNKNOWN", "RECONCILING", "ESCALATED"]
        assert len(events_of(kernel_session_factory, attempt, "evidence.mismatch")) == 2

        # ESCALATED is terminal: even a genuine capture cannot thaw it automatically.
        later = apply(kernel_session_factory, attempt, evidence())
        assert not later.changed and later.order_id is None

    def test_a_second_settled_payment_on_the_order_is_a_mismatch(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        apply(
            kernel_session_factory,
            attempt,
            evidence(status="authorized", provider_status="authorized"),
        )
        conflict = apply(
            kernel_session_factory, attempt, evidence(provider_payment_id=OTHER_PAYMENT_ID)
        )
        assert conflict.reason == "settled_payment_id_conflict"
        assert conflict.state_after is PaymentState.ESCALATED
        assert not orders(kernel_session_factory, attempt)

    def test_a_webhook_cannot_resolve_an_unknown_attempt(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """Specification 10.7 step 5: UNKNOWN resolves only through reconciliation."""
        run(
            kernel_session_factory,
            attempt,
            lambda s: payments.record_create_order_result(
                s,
                tenant_id=attempt.tenant_id,
                payment_attempt_id=attempt.attempt_id,
                outcome=ProviderOrderOutcome(
                    "unknown", None, RecoveryCode.PAYMENT_UNKNOWN, "transport_timeout"
                ),
                correlation_id=attempt.correlation_id,
            ),
        )
        # Reconciliation later learns the order id; the webhook arrives before the fetch.
        run(
            kernel_session_factory,
            attempt,
            lambda s: s.execute(
                text("UPDATE payment_attempts SET provider_order_id = :o WHERE id = :a"),
                {"o": ORDER_ID, "a": attempt.attempt_id},
            ),
        )
        applied = apply(
            kernel_session_factory, attempt, evidence(source="WEBHOOK", event_id="evt_late")
        )
        assert applied.state_after is PaymentState.RECONCILING
        assert applied.order_id is None
        assert not orders(kernel_session_factory, attempt)
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.PAYMENT_UNKNOWN

    def test_two_concurrent_captures_write_exactly_one_order(
        self, kernel_session_factory: Factory, adm_kernel_engine: Engine, attempt: Attempt
    ) -> None:
        """A webhook and a reconciliation fetch report the same capture at the same moment.

        Two real sessions race on the version row lock; the loser sees the winner's
        committed CAPTURED, applies a no-op, and ``ON CONFLICT DO NOTHING`` leaves one
        order. Neither thread may raise: losing this race is normal traffic.
        """
        submit(kernel_session_factory, attempt)
        results: list[payments.EvidenceApplied | BaseException] = []
        barrier = threading.Barrier(2)

        def capture(source: str, event_id: str | None) -> None:
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                barrier.wait(timeout=10)
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(attempt.tenant_id)})
                    results.append(
                        payments.apply_provider_evidence(
                            session,
                            tenant_id=attempt.tenant_id,
                            payment_attempt_id=attempt.attempt_id,
                            evidence=evidence(source=source, event_id=event_id),
                            correlation_id=attempt.correlation_id,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                results.append(exc)
            finally:
                session.close()

        threads = [
            threading.Thread(target=capture, args=("WEBHOOK", "evt_race")),
            threading.Thread(target=capture, args=("PROVIDER_FETCH", None)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert all(isinstance(r, payments.EvidenceApplied) for r in results), results
        applied = [r for r in results if isinstance(r, payments.EvidenceApplied)]
        assert sorted(r.changed for r in applied) == [False, True]
        assert len({r.order_id for r in applied}) == 1
        assert len(orders(kernel_session_factory, attempt)) == 1
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.CAPTURED
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.PAID


# ---------------------------------------------------------------- reconciliation


def go_unknown(factory: Factory, a: Attempt) -> None:
    run(
        factory,
        a,
        lambda s: payments.record_create_order_result(
            s,
            tenant_id=a.tenant_id,
            payment_attempt_id=a.attempt_id,
            outcome=ProviderOrderOutcome("unknown", None, RecoveryCode.PAYMENT_UNKNOWN, "timeout"),
            correlation_id=a.correlation_id,
        ),
    )


def begin(factory: Factory, a: Attempt) -> bool:
    return run(
        factory,
        a,
        lambda s: payments.begin_reconciling(
            s,
            tenant_id=a.tenant_id,
            payment_attempt_id=a.attempt_id,
            correlation_id=a.correlation_id,
        ),
    )


class TestReconciliation:
    def test_begin_reconciling_is_the_single_exit_from_unknown_and_is_idempotent(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        go_unknown(kernel_session_factory, attempt)
        assert begin(kernel_session_factory, attempt) is True
        assert begin(kernel_session_factory, attempt) is False
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.RECONCILING
        assert reservation_status(kernel_session_factory, attempt) == "CONSUMED"
        assert (
            len(events_of(kernel_session_factory, attempt, "payment.reconciliation_started")) == 1
        )

    def test_begin_reconciling_refuses_a_live_attempt(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        with pytest.raises(PaymentStateConflictError):
            begin(kernel_session_factory, attempt)

    def test_a_recovered_order_stays_reconciling_until_evidence_arrives(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """Lookup by receipt found the order (10.6); the payment is still unverified."""
        go_unknown(kernel_session_factory, attempt)
        begin(kernel_session_factory, attempt)

        def recover(s: Session) -> payments.AttemptTransition:
            return payments.record_recovered_order(
                s,
                tenant_id=attempt.tenant_id,
                payment_attempt_id=attempt.attempt_id,
                provider_order_id=ORDER_ID,
                correlation_id=attempt.correlation_id,
            )

        moved = run(kernel_session_factory, attempt, recover)
        assert moved.state_after is PaymentState.RECONCILING
        # PAYMENT_UNKNOWN has no edge to AWAITING_PAYMENT; only EXECUTION_PENDING does.
        assert moved.checkout_state_after is CheckoutState.PAYMENT_UNKNOWN
        assert attempt_row(kernel_session_factory, attempt).provider_order_id == ORDER_ID
        assert not run(kernel_session_factory, attempt, recover).changed

        applied = apply(kernel_session_factory, attempt, evidence())
        assert applied.state_after is PaymentState.CAPTURED and applied.order_id is not None
        assert checkout_status(kernel_session_factory, attempt) is CheckoutState.PAID
        assert reservation_status(kernel_session_factory, attempt) == "CONSUMED"

    def test_reconciliation_runs_are_counted_once_per_attempt_number(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        go_unknown(kernel_session_factory, attempt)

        def record(number: int) -> uuid.UUID | None:
            return run(
                kernel_session_factory,
                attempt,
                lambda s: payments.record_reconciliation_run(
                    s,
                    tenant_id=attempt.tenant_id,
                    payment_attempt_id=attempt.attempt_id,
                    attempt_number=number,
                    reason="create_order_unknown",
                    identifiers_queried={"receipt": "rcpt_x", "order_id": None},
                    decision="not_found_retry",
                    correlation_id=attempt.correlation_id,
                    next_attempt_in_seconds=30,
                ),
            )

        first = record(1)
        assert first is not None
        assert record(1) is None, "a redelivered RECONCILE command must not advance the count"
        assert record(2) is not None
        scheduled = run(
            kernel_session_factory,
            attempt,
            lambda s: s.execute(
                text(
                    "SELECT next_scheduled_attempt > now() FROM reconciliation_runs WHERE id = :i"
                ),
                {"i": first},
            ).scalar_one(),
        )
        assert scheduled is True
        with pytest.raises(PaymentsUsageError):
            record(payments.RECONCILIATION_ATTEMPT_BOUND + 1)
        assert (
            len(events_of(kernel_session_factory, attempt, "payment.reconciliation_run_recorded"))
            == 2
        )


class TestEscalation:
    def _escalate(self, factory: Factory, a: Attempt, reason: str) -> payments.Escalation:
        return run(
            factory,
            a,
            lambda s: payments.escalate(
                s,
                tenant_id=a.tenant_id,
                payment_attempt_id=a.attempt_id,
                reason=reason,
                correlation_id=a.correlation_id,
            ),
        )

    def test_reconciling_escalates_once_and_freezes(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        go_unknown(kernel_session_factory, attempt)
        begin(kernel_session_factory, attempt)
        first = self._escalate(kernel_session_factory, attempt, "reconciliation_exhausted")
        second = self._escalate(kernel_session_factory, attempt, "reconciliation_exhausted")
        assert first.opened and not second.opened
        assert first.case_key == second.case_key
        assert first.state_after is PaymentState.ESCALATED
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.ESCALATED
        assert len(events_of(kernel_session_factory, attempt, "human_review.opened")) == 1

    def test_the_case_key_is_deterministic_per_reason_family(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        go_unknown(kernel_session_factory, attempt)
        begin(kernel_session_factory, attempt)
        a = self._escalate(kernel_session_factory, attempt, "reconciliation_exhausted")
        b = self._escalate(kernel_session_factory, attempt, "evidence_mismatch")
        assert a.case_key != b.case_key
        assert b.opened is False, "the attempt is already frozen; no second case"

    def test_money_that_moved_is_not_frozen_but_still_gets_one_case(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        apply(kernel_session_factory, attempt, evidence())
        first = self._escalate(kernel_session_factory, attempt, "refund_dispute")
        second = self._escalate(kernel_session_factory, attempt, "refund_dispute")
        assert first.opened and first.state_after is PaymentState.CAPTURED
        assert not second.opened
        assert attempt_row(kernel_session_factory, attempt).status is PaymentState.CAPTURED
        assert len(events_of(kernel_session_factory, attempt, "human_review.opened")) == 1

    def test_concurrent_detectors_open_exactly_one_case(
        self, kernel_session_factory: Factory, adm_kernel_engine: Engine, attempt: Attempt
    ) -> None:
        """Specification 6.4.3's concurrency requirement, with real contending sessions."""
        go_unknown(kernel_session_factory, attempt)
        begin(kernel_session_factory, attempt)
        results: list[payments.Escalation | BaseException] = []
        barrier = threading.Barrier(2)

        def detect() -> None:
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                barrier.wait(timeout=10)
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(attempt.tenant_id)})
                    results.append(
                        payments.escalate(
                            session,
                            tenant_id=attempt.tenant_id,
                            payment_attempt_id=attempt.attempt_id,
                            reason="reconciliation_exhausted",
                            correlation_id=attempt.correlation_id,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                results.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=detect) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert all(isinstance(r, payments.Escalation) for r in results), results
        escalations = [r for r in results if isinstance(r, payments.Escalation)]
        assert sorted(e.opened for e in escalations) == [False, True]
        assert len({e.case_key for e in escalations}) == 1
        assert len(events_of(kernel_session_factory, attempt, "human_review.opened")) == 1


# -------------------------------------------------------------- provider requests


class TestProviderRequest:
    def _record(self, s: Session, a: Attempt, **overrides: Any) -> uuid.UUID:
        params: dict[str, Any] = {
            "tenant_id": a.tenant_id,
            "payment_attempt_id": a.attempt_id,
            "grant_id": a.grant_id,
            "refund_id": None,
            "operation": Operation.PAYMENT_CREATE_ORDER,
            "method": "POST",
            "url": "https://api.razorpay.com/v1/orders",
            "body_hash": DIGEST,
            "header_names": ["Content-Type", "Accept", "Authorization"],
            "http_status": 200,
            "provider_id": ORDER_ID,
            "outcome_code": RecoveryCode.OK,
            "provider_error_code": None,
            "response_digest": "c" * 64,
            "transport_error": None,
            "correlation_id": a.correlation_id,
        }
        params.update(overrides)
        return payments.record_provider_request(s, **params)

    def test_a_mutation_is_recorded_against_its_consumed_grant_exactly_once(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        consume_grant(kernel_session_factory, attempt)
        request_id = run(kernel_session_factory, attempt, lambda s: self._record(s, attempt))
        stored = run(
            kernel_session_factory,
            attempt,
            lambda s: s.execute(
                text(
                    "SELECT operation, method, url, header_names, grant_id, outcome_code "
                    "FROM provider_requests WHERE id = :i"
                ),
                {"i": request_id},
            ).one(),
        )
        assert stored.operation == "PAYMENT_CREATE_ORDER"
        assert stored.grant_id == attempt.grant_id
        assert stored.header_names == ["Content-Type", "Accept", "Authorization"]
        assert stored.outcome_code == "OK"
        assert len(events_of(kernel_session_factory, attempt, "provider.request_recorded")) == 1

        with pytest.raises(PaymentsError) as info:
            run(kernel_session_factory, attempt, lambda s: self._record(s, attempt))
        assert info.value.code is RecoveryCode.DUPLICATE_OPERATION

    def test_a_mutation_without_a_consumed_grant_is_refused(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        with pytest.raises(PaymentsUsageError):
            run(kernel_session_factory, attempt, lambda s: self._record(s, attempt, grant_id=None))
        # Issued but not consumed: the provider must not have been called yet.
        with pytest.raises(PaymentsError) as info:
            run(kernel_session_factory, attempt, lambda s: self._record(s, attempt))
        assert info.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    def test_a_read_carries_no_grant(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        read = run(
            kernel_session_factory,
            attempt,
            lambda s: self._record(
                s,
                attempt,
                grant_id=None,
                operation="PAYMENT_FETCH",
                method="GET",
                url=f"https://api.razorpay.com/v1/payments/{PAYMENT_ID}",
                provider_id=PAYMENT_ID,
            ),
        )
        assert isinstance(read, uuid.UUID)
        with pytest.raises(PaymentsUsageError):
            run(
                kernel_session_factory,
                attempt,
                lambda s: self._record(s, attempt, operation="PAYMENT_FETCH", method="GET"),
            )

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.razorpay.com/v1/orders?key_id=rzp_test_x",
            "https://rzp_test_x:secret@api.razorpay.com/v1/orders",
            "http://api.razorpay.com/v1/orders",
        ],
    )
    def test_a_url_that_could_carry_a_secret_is_refused(
        self, kernel_session_factory: Factory, attempt: Attempt, url: str
    ) -> None:
        consume_grant(kernel_session_factory, attempt)
        with pytest.raises(PaymentsUsageError):
            run(kernel_session_factory, attempt, lambda s: self._record(s, attempt, url=url))

    def test_header_values_are_never_stored(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        consume_grant(kernel_session_factory, attempt)
        with pytest.raises(PaymentsUsageError):
            run(
                kernel_session_factory,
                attempt,
                lambda s: self._record(s, attempt, header_names=["Authorization: Basic abc"]),
            )


# ------------------------------------------------------------------ webhook inbox


class TestWebhookApplied:
    def _inbox_row(self, factory: Factory, a: Attempt) -> uuid.UUID:
        inbox_id = uuid7()
        run(
            factory,
            a,
            lambda s: s.execute(
                text(
                    "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, provider_event_id, "
                    "event_type, body_digest, raw_body, headers_redacted, signature_verified, "
                    "payment_id, order_id) VALUES (:id, :t, :k, :e, 'payment.captured', :d, "
                    ":raw, CAST(:h AS jsonb), true, :p, :o)"
                ),
                {
                    "id": inbox_id,
                    "t": a.tenant_id,
                    "k": f"evt:{inbox_id.hex}",
                    "e": inbox_id.hex,
                    "d": DIGEST,
                    "raw": b"{}",
                    "h": json.dumps({"x-razorpay-event-id": inbox_id.hex}),
                    "p": PAYMENT_ID,
                    "o": ORDER_ID,
                },
            ),
        )
        return inbox_id

    def test_stamps_the_apply_outcome_on_the_row(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        inbox_id = self._inbox_row(kernel_session_factory, attempt)
        command_id = uuid7()
        run(
            kernel_session_factory,
            attempt,
            lambda s: payments.record_webhook_applied(
                s,
                tenant_id=attempt.tenant_id,
                inbox_id=inbox_id,
                apply_status="APPLIED",
                apply_reason="applied_captured",
                state_before=PaymentState.SUBMITTED,
                state_after=PaymentState.CAPTURED,
                changed=True,
                outbox_command_id=command_id,
                correlation_id=attempt.correlation_id,
            ),
        )
        row = run(
            kernel_session_factory,
            attempt,
            lambda s: s.execute(
                text(
                    "SELECT applied_at, apply_status, apply_reason, state_before, state_after, "
                    "changed, outbox_command_id FROM webhook_inbox WHERE id = :i"
                ),
                {"i": inbox_id},
            ).one(),
        )
        assert row.applied_at is not None
        assert row.apply_status == "APPLIED"
        assert (row.state_before, row.state_after, row.changed) == ("SUBMITTED", "CAPTURED", True)
        assert row.outbox_command_id == command_id

    def test_an_unknown_inbox_row_is_refused(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        with pytest.raises(InboxRowNotFoundError):
            run(
                kernel_session_factory,
                attempt,
                lambda s: payments.record_webhook_applied(
                    s,
                    tenant_id=attempt.tenant_id,
                    inbox_id=uuid7(),
                    apply_status="IGNORED",
                    apply_reason="no_attempt",
                    state_before=None,
                    state_after=None,
                    changed=False,
                    outbox_command_id=None,
                    correlation_id=attempt.correlation_id,
                ),
            )

    def test_a_row_cannot_go_back_to_received(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        inbox_id = self._inbox_row(kernel_session_factory, attempt)
        with pytest.raises(PaymentsUsageError):
            run(
                kernel_session_factory,
                attempt,
                lambda s: payments.record_webhook_applied(
                    s,
                    tenant_id=attempt.tenant_id,
                    inbox_id=inbox_id,
                    apply_status="RECEIVED",
                    apply_reason=None,
                    state_before=None,
                    state_after=None,
                    changed=None,
                    outbox_command_id=None,
                    correlation_id=attempt.correlation_id,
                ),
            )


# -------------------------------------------------------------------- lookups


class TestLookups:
    def test_find_by_provider_payment_returns_the_recording_attempt(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        submit(kernel_session_factory, attempt)
        apply(kernel_session_factory, attempt, evidence())
        found = run(
            kernel_session_factory,
            attempt,
            lambda s: payments.find_attempt_by_provider_payment(
                s, tenant_id=attempt.tenant_id, provider_payment_id=PAYMENT_ID
            ),
        )
        assert found is not None and found.attempt_id == attempt.attempt_id
        assert found.provider_order_id == ORDER_ID
        assert found.status is PaymentState.CAPTURED

    def test_another_tenant_cannot_see_the_attempt(
        self, kernel_session_factory: Factory, attempt: Attempt
    ) -> None:
        """RLS plus the explicit tenant predicate: a foreign id reads as absent."""
        other = uuid.uuid4()
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(other)})
            assert (
                payments.read_attempt(
                    session, tenant_id=other, payment_attempt_id=attempt.attempt_id
                )
                is None
            )
            assert EvidenceSource.WEBHOOK is EvidenceSource("WEBHOOK")

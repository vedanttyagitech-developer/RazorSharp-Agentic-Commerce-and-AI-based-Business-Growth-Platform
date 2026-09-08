"""Refunds, specification 10.6, 10.9 and 11.4.

The distinction under test throughout: ``REFUND_UNKNOWN`` reconciles and never retries;
``REFUND_FAILED`` is provider-confirmed and may retry under a new Execution Grant.
Conflating them is how a buyer gets refunded twice, so several tests here assert the
refusal rather than the happy path.
"""

from __future__ import annotations

import json

import pytest
from commerce_domain import RETRYABLE, Money, RecoveryCode
from payment_adapters.razorpay import (
    IDEMPOTENCY_HEADER,
    CaptureLedger,
    HttpResponse,
    RazorpayConfig,
    RefundDecision,
    RefundNotPermittedError,
    RefundRefusal,
    RefundSpeed,
    RequestConstructionError,
    TransportError,
    TransportTimeoutError,
    build_refund_request,
    execute_refund,
    may_retry_after,
    must_reconcile_before_retry,
    plan_refund,
    refund_idempotency_key,
)
from transaction_kernel.states import PaymentState

from conftest import FakeTransport, json_response

PAYMENT_ID = "pay_29QQoUBi66xm2f"
CAPTURED = Money(39500, "INR")
NOTHING = Money.zero("INR")


def fresh_ledger(refunded: int = 0) -> CaptureLedger:
    return CaptureLedger(captured=CAPTURED, refunded=Money(refunded, "INR"))


def refund_entity(
    *, refund_id: str = "rfnd_FP8QHiV938haTz", amount: int = 39500, status: str = "processed"
) -> dict[str, object]:
    return {
        "id": refund_id,
        "entity": "refund",
        "amount": amount,
        "currency": "INR",
        "payment_id": PAYMENT_ID,
        "status": status,
    }


# ---------------------------------------------------------------------------- ledger


def test_remaining_is_captured_minus_refunded() -> None:
    assert fresh_ledger(10000).remaining == Money(29500, "INR")


def test_a_ledger_that_already_over_refunds_refuses_to_construct() -> None:
    """Refusing here stops the module reasoning confidently about a negative remainder."""
    with pytest.raises(RequestConstructionError, match="exceeds captured"):
        CaptureLedger(captured=CAPTURED, refunded=Money(39501, "INR"))


def test_a_ledger_with_mismatched_currencies_refuses_to_construct() -> None:
    with pytest.raises(RequestConstructionError, match="currencies disagree"):
        CaptureLedger(captured=CAPTURED, refunded=Money(0, "USD"))


# ------------------------------------------------------------------- idempotency key


def test_the_key_is_stable_across_processes_and_calls() -> None:
    """A transport-level retry must reuse the key, or it creates a second refund."""
    first = refund_idempotency_key(payment_id=PAYMENT_ID, amount=CAPTURED, sequence=1)
    second = refund_idempotency_key(payment_id=PAYMENT_ID, amount=CAPTURED, sequence=1)
    assert first == second
    assert first.startswith("rfnd_")


def test_two_identical_partial_refunds_get_different_keys() -> None:
    """The collision this design exists to prevent.

    Two ₹100 refunds against one payment -- two separate damaged items -- would share a
    key if it were derived from payment and amount alone. The provider would return the
    first refund for the second request, the ledger would read as settled, and the buyer
    would be ₹100 short.
    """
    amount = Money(10000, "INR")
    first = refund_idempotency_key(payment_id=PAYMENT_ID, amount=amount, sequence=1)
    second = refund_idempotency_key(payment_id=PAYMENT_ID, amount=amount, sequence=2)
    assert first != second


def test_the_key_varies_with_amount_and_payment() -> None:
    base = refund_idempotency_key(payment_id=PAYMENT_ID, amount=CAPTURED, sequence=1)
    assert base != refund_idempotency_key(
        payment_id=PAYMENT_ID, amount=Money(39501, "INR"), sequence=1
    )
    assert base != refund_idempotency_key(payment_id="pay_other", amount=CAPTURED, sequence=1)
    assert base != refund_idempotency_key(
        payment_id=PAYMENT_ID, amount=Money(39500, "USD"), sequence=1
    )


@pytest.mark.parametrize("sequence", [0, -1])
def test_a_non_positive_ordinal_is_refused(sequence: int) -> None:
    with pytest.raises(RequestConstructionError, match="1-based ordinal"):
        refund_idempotency_key(payment_id=PAYMENT_ID, amount=CAPTURED, sequence=sequence)


# --------------------------------------------------------------------------- planning


def test_a_full_refund_resolves_to_the_remaining_amount() -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
    )
    assert decision.allowed
    assert decision.code is RecoveryCode.REFUND_ALLOWED
    assert decision.plan is not None
    assert decision.plan.amount == CAPTURED
    assert decision.plan.is_full_remaining
    assert decision.plan.resulting_state is PaymentState.REFUNDED


def test_a_partial_refund_predicts_partially_refunded() -> None:
    """Predicted before the request is sent, so success is never mis-recorded."""
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
        amount=Money(10000, "INR"),
    )
    assert decision.plan is not None
    assert not decision.plan.is_full_remaining
    assert decision.plan.resulting_state is PaymentState.PARTIALLY_REFUNDED


def test_repeated_partial_refunds_can_never_exceed_the_capture() -> None:
    """Specification 11.4: repeated partials, bounded by the captured total.

    Walk the ledger forward three times. The fourth request would take the total past the
    capture and is refused; the accounting is exact, with no paisa created.
    """
    refunded = 0
    for _ in range(3):
        decision = plan_refund(
            payment_id=PAYMENT_ID,
            current_state=PaymentState.CAPTURED
            if refunded == 0
            else PaymentState.PARTIALLY_REFUNDED,
            ledger=fresh_ledger(refunded),
            sequence=refunded // 10000 + 1,
            amount=Money(10000, "INR"),
        )
        assert decision.allowed
        refunded += 10000

    assert fresh_ledger(refunded).remaining == Money(9500, "INR")

    over = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.PARTIALLY_REFUNDED,
        ledger=fresh_ledger(refunded),
        sequence=4,
        amount=Money(10000, "INR"),
    )
    assert not over.allowed
    assert over.explanation is RefundRefusal.EXCEEDS_REMAINING


def test_a_refund_larger_than_the_capture_is_refused() -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
        amount=Money(39501, "INR"),
    )
    assert not decision.allowed
    assert decision.explanation is RefundRefusal.EXCEEDS_REMAINING
    assert decision.plan is None


def test_a_fully_refunded_payment_has_nothing_left_to_refund() -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.PARTIALLY_REFUNDED,
        ledger=fresh_ledger(39500),
        sequence=2,
    )
    assert not decision.allowed
    assert decision.explanation is RefundRefusal.NOTHING_REMAINING


@pytest.mark.parametrize("minor", [0, -100])
def test_a_non_positive_refund_is_refused(minor: int) -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
        amount=Money(minor, "INR"),
    )
    assert not decision.allowed
    assert decision.explanation is RefundRefusal.NON_POSITIVE_AMOUNT


def test_a_currency_mismatch_is_refused() -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
        amount=Money(100, "USD"),
    )
    assert not decision.allowed
    assert decision.explanation is RefundRefusal.CURRENCY_MISMATCH


# ------------------------------------------------------- the two failure states differ


def test_an_unknown_refund_is_never_replanned() -> None:
    """The headline invariant. A refund may already exist at the provider.

    Issuing a second grant here is exactly how a buyer is refunded twice, so the refusal
    is structural: no executable plan is produced, and the code sends the caller to
    reconciliation.
    """
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.REFUND_UNKNOWN,
        ledger=fresh_ledger(),
        sequence=2,
    )
    assert not decision.allowed
    assert decision.code is RecoveryCode.RECONCILIATION_IN_PROGRESS
    assert decision.explanation is RefundRefusal.RECONCILE_UNKNOWN_FIRST
    assert decision.plan is None
    assert must_reconcile_before_retry(PaymentState.REFUND_UNKNOWN)
    assert not may_retry_after(PaymentState.REFUND_UNKNOWN)


def test_a_confirmed_failure_may_retry_under_a_new_grant() -> None:
    """The other half. The provider said no refund exists, so a fresh attempt is safe."""
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.REFUND_FAILED,
        ledger=fresh_ledger(),
        sequence=2,
    )
    assert decision.allowed
    assert may_retry_after(PaymentState.REFUND_FAILED)
    assert not must_reconcile_before_retry(PaymentState.REFUND_FAILED)


def test_an_unknown_payment_is_also_reconcile_only() -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.UNKNOWN,
        ledger=fresh_ledger(),
        sequence=1,
    )
    assert decision.code is RecoveryCode.RECONCILIATION_IN_PROGRESS


@pytest.mark.parametrize(
    "state",
    [
        PaymentState.AUTHORIZED,
        PaymentState.SUBMITTED,
        PaymentState.CREATED,
        PaymentState.FAILED,
        PaymentState.REFUNDED,
        PaymentState.ESCALATED,
        PaymentState.REFUND_PENDING,
    ],
)
def test_states_the_lifecycle_forbids_cannot_open_a_refund(state: PaymentState) -> None:
    """The permitted set is read from ``transaction_kernel.states``, never restated here.

    ``AUTHORIZED`` matters most: no money has moved, so there is nothing to refund.
    """
    decision = plan_refund(
        payment_id=PAYMENT_ID, current_state=state, ledger=fresh_ledger(), sequence=1
    )
    assert not decision.allowed
    assert decision.explanation is RefundRefusal.STATE_FORBIDS_REFUND


def test_a_stale_capture_may_be_refunded() -> None:
    """Specification 10.8: a capture against an invalidated version is refunded in full."""
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.STALE_CAPTURE,
        ledger=fresh_ledger(),
        sequence=1,
    )
    assert decision.allowed
    assert decision.plan is not None
    assert decision.plan.amount == CAPTURED


def test_a_refused_decision_cannot_carry_a_plan() -> None:
    with pytest.raises(ValueError, match="must not carry"):
        RefundDecision(
            allowed=False,
            code=RecoveryCode.POLICY_EXCEPTION,
            explanation=RefundRefusal.EXCEEDS_REMAINING,
            plan=plan_refund(
                payment_id=PAYMENT_ID,
                current_state=PaymentState.CAPTURED,
                ledger=fresh_ledger(),
                sequence=1,
            ).plan,
        )


# --------------------------------------------------------------------------- request


def allowed_plan(amount: Money | None = None, sequence: int = 1) -> RefundDecision:
    return plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=sequence,
        amount=amount,
    )


def test_the_request_carries_the_idempotency_header(config: RazorpayConfig) -> None:
    decision = allowed_plan()
    assert decision.plan is not None
    request = build_refund_request(config, decision.plan)

    assert request.method == "POST"
    assert request.url.endswith(f"/v1/payments/{PAYMENT_ID}/refund")
    assert request.headers[IDEMPOTENCY_HEADER] == decision.plan.idempotency_key


def test_a_full_refund_still_sends_an_explicit_amount(config: RazorpayConfig) -> None:
    """Omitting the amount asks for "whatever is left", a different question.

    If another refund landed between planning and sending, the two answers differ by
    real money.
    """
    decision = allowed_plan()
    assert decision.plan is not None
    body = json.loads(build_refund_request(config, decision.plan).body)
    assert body["amount"] == 39500
    assert body["speed"] == RefundSpeed.NORMAL.value


def test_notes_are_carried_when_present(config: RazorpayConfig) -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
        notes={"reason": "item_damaged"},
    )
    assert decision.plan is not None
    body = json.loads(build_refund_request(config, decision.plan).body)
    assert body["notes"] == {"reason": "item_damaged"}


# --------------------------------------------------------------------------- outcome


def test_a_processed_full_refund_settles_as_refunded(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, refund_entity())])
    result = execute_refund(transport, config, allowed_plan())

    assert result.code is RecoveryCode.OK
    assert result.payment_state is PaymentState.REFUNDED
    assert result.refund_id == "rfnd_FP8QHiV938haTz"
    assert transport.call_count == 1


def test_a_processed_partial_refund_is_not_recorded_as_complete(
    config: RazorpayConfig,
) -> None:
    """``REFUNDED`` is terminal. Recording a partial refund as one strands the remainder.

    The provider says *this refund* processed; whether the *payment* is fully refunded is
    a fact about the ledger, and the plan worked it out against the remaining balance.
    """
    transport = FakeTransport([json_response(200, refund_entity(amount=10000))])
    result = execute_refund(transport, config, allowed_plan(Money(10000, "INR")))

    assert result.payment_state is PaymentState.PARTIALLY_REFUNDED
    assert result.payment_state is not PaymentState.REFUNDED


def test_a_pending_refund_stays_pending(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, refund_entity(status="pending"))])
    result = execute_refund(transport, config, allowed_plan())
    assert result.payment_state is PaymentState.REFUND_PENDING
    assert result.code is RecoveryCode.PAYMENT_PENDING


def test_a_timeout_is_refund_unknown_and_never_refund_failed(config: RazorpayConfig) -> None:
    """The invariant that stops a double refund.

    The refund may exist. Calling it a failure would authorize a second attempt, and the
    buyer would be paid twice out of the merchant's money.
    """
    transport = FakeTransport(error=TransportTimeoutError("read timed out"))
    result = execute_refund(transport, config, allowed_plan())

    assert result.payment_state is PaymentState.REFUND_UNKNOWN
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.code not in RETRYABLE
    assert result.must_reconcile
    assert not result.may_retry_under_new_grant


def test_a_connection_failure_is_refund_unknown(config: RazorpayConfig) -> None:
    transport = FakeTransport(error=TransportError("connection reset"))
    assert execute_refund(transport, config, allowed_plan()).must_reconcile


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_every_unanticipated_status_is_refund_unknown(config: RazorpayConfig, status: int) -> None:
    transport = FakeTransport([json_response(status, {"error": {"code": "SERVER_ERROR"}})])
    result = execute_refund(transport, config, allowed_plan())
    assert result.payment_state is PaymentState.REFUND_UNKNOWN
    assert result.must_reconcile


def test_a_success_without_a_refund_id_is_unknown(config: RazorpayConfig) -> None:
    """The refund very likely exists and we cannot name it, which is the unknown case."""
    transport = FakeTransport([json_response(200, {"entity": "refund", "status": "processed"})])
    assert execute_refund(transport, config, allowed_plan()).must_reconcile


def test_an_unreadable_success_body_is_unknown(config: RazorpayConfig) -> None:
    transport = FakeTransport([HttpResponse(status=200, body=b"<html>gateway</html>")])
    assert execute_refund(transport, config, allowed_plan()).must_reconcile


def test_a_provider_confirmed_rejection_is_refund_failed(config: RazorpayConfig) -> None:
    """400 means no refund exists, which is the verified absence that permits a retry."""
    transport = FakeTransport([json_response(400, {"error": {"code": "BAD_REQUEST_ERROR"}})])
    result = execute_refund(transport, config, allowed_plan())

    assert result.payment_state is PaymentState.REFUND_FAILED
    assert result.code is RecoveryCode.PAYMENT_FAILED
    assert result.code in RETRYABLE
    assert result.may_retry_under_new_grant
    assert not result.must_reconcile


def test_a_refund_entity_reporting_failure_is_refund_failed(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, refund_entity(status="failed"))])
    result = execute_refund(transport, config, allowed_plan())
    assert result.payment_state is PaymentState.REFUND_FAILED


def test_an_unrecognised_refund_status_stays_pending(config: RazorpayConfig) -> None:
    """The entity has an id, so the refund exists. Nothing terminal may be concluded."""
    transport = FakeTransport([json_response(200, refund_entity(status="something_new"))])
    result = execute_refund(transport, config, allowed_plan())
    assert result.payment_state is PaymentState.REFUND_PENDING


# ------------------------------------------------------------------------ the gate


def test_a_refused_decision_is_never_sent(config: RazorpayConfig) -> None:
    """The gate cannot be stepped around by a caller that forgot to check ``allowed``."""
    refused = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.REFUND_UNKNOWN,
        ledger=fresh_ledger(),
        sequence=1,
    )
    transport = FakeTransport([json_response(200, refund_entity())])

    with pytest.raises(RefundNotPermittedError, match="RECONCILE_UNKNOWN_FIRST"):
        execute_refund(transport, config, refused)

    assert transport.call_count == 0, "a refused refund must never reach the provider"


def test_an_over_limit_refund_never_reaches_the_provider(config: RazorpayConfig) -> None:
    refused = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=fresh_ledger(),
        sequence=1,
        amount=Money(1_000_000, "INR"),
    )
    transport = FakeTransport([json_response(200, refund_entity())])
    with pytest.raises(RefundNotPermittedError):
        execute_refund(transport, config, refused)
    assert transport.call_count == 0


def test_the_zero_ledger_edge_is_not_refundable() -> None:
    decision = plan_refund(
        payment_id=PAYMENT_ID,
        current_state=PaymentState.CAPTURED,
        ledger=CaptureLedger(captured=NOTHING, refunded=NOTHING),
        sequence=1,
    )
    assert not decision.allowed
    assert decision.explanation is RefundRefusal.NOTHING_REMAINING


# ------------------------------------------------------------------- echo integrity


def test_a_refund_echoing_a_different_amount_is_escalated_not_settled(
    config: RazorpayConfig,
) -> None:
    """An idempotency-key collision returns the *original* refund, not a new one.

    That is what the header is for, and it is also what a caller gets when it supplies an
    ordinal a previous, different refund already used. Without an echo check the provider
    answers a Rs395 full-refund request with the Rs1 refund it made earlier, ``status``
    reads ``processed``, and the plan's ``resulting_state`` -- terminal ``REFUNDED`` -- is
    applied. The buyer is owed Rs394 forever and the ledger reads as settled.
    """
    transport = FakeTransport([json_response(200, refund_entity(amount=100))])
    result = execute_refund(transport, config, allowed_plan())

    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.payment_state is PaymentState.REFUND_UNKNOWN
    assert result.payment_state is not PaymentState.REFUNDED
    assert result.must_reconcile
    assert not result.may_retry_under_new_grant
    assert result.code not in RETRYABLE
    # The identifier is kept so reconciliation has an authoritative handle to look up.
    assert result.refund_id == "rfnd_FP8QHiV938haTz"
    assert result.provider_error_code is not None
    assert "amount" in result.provider_error_code


def test_a_partial_refund_echoing_a_different_amount_is_escalated(
    config: RazorpayConfig,
) -> None:
    transport = FakeTransport([json_response(200, refund_entity(amount=100))])
    result = execute_refund(transport, config, allowed_plan(Money(10000, "INR")))
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.payment_state is PaymentState.REFUND_UNKNOWN


def test_a_refund_echoing_a_different_currency_is_escalated(config: RazorpayConfig) -> None:
    body = refund_entity()
    body["currency"] = "USD"
    transport = FakeTransport([json_response(200, body)])
    result = execute_refund(transport, config, allowed_plan())
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert "currency" in (result.provider_error_code or "")


def test_a_refund_echoing_a_different_payment_is_escalated(config: RazorpayConfig) -> None:
    """A refund entity for somebody else's payment must never settle this one."""
    body = refund_entity()
    body["payment_id"] = "pay_somebodyElses"
    transport = FakeTransport([json_response(200, body)])
    result = execute_refund(transport, config, allowed_plan())
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert "payment_id" in (result.provider_error_code or "")


def test_a_matching_echo_still_settles_normally(config: RazorpayConfig) -> None:
    """The check must not turn a genuine answer into an escalation."""
    transport = FakeTransport([json_response(200, refund_entity())])
    assert execute_refund(transport, config, allowed_plan()).code is RecoveryCode.OK

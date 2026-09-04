"""Reconciliation fetches, specification 10.7 and 11.2.

The invariants:

* the attempt moves to captured, authorized or failed **only** from what the provider
  said, and every state offered is a legal successor of ``RECONCILING``;
* a payment that does not echo the attempt's amount, currency and order is refused with
  an exception, never folded into a state -- recording it would settle the wrong checkout;
* a fetch that produced no answer is ``PAYMENT_UNKNOWN`` and repeats; a fetch the provider
  refused is escalated rather than repeated blind, and never becomes ``FAILED``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from commerce_domain import DomainError, Money
from conftest import FakeTransport, json_response, payment_entity
from payment_adapters.razorpay import (
    EVIDENCE_SOURCE,
    CaptureEvidence,
    EvidenceMismatchError,
    FetchClassification,
    HttpResponse,
    RazorpayAdapterError,
    RazorpayConfig,
    RequestConstructionError,
    TransportError,
    TransportTimeoutError,
    build_fetch_payment_request,
    build_order_payments_request,
    fetch_order_payments,
    fetch_payment,
    may_fulfil,
)
from transaction_kernel.recovery import RETRYABLE, RecoveryCode
from transaction_kernel.states import PAYMENT_TRANSITIONS, PaymentState

AMOUNT = Money(39500, "INR")
PAYMENT_ID = "pay_29QQoUBi66xm2f"
ORDER_ID = "order_9A33XWu170gUtm"


def entity(**overrides: Any) -> dict[str, Any]:
    """A Razorpay payment entity for this attempt, with the descriptive fields present."""
    base = payment_entity(payment_id=PAYMENT_ID, order_id=ORDER_ID, amount=AMOUNT.minor)
    base.update({"method": "upi", "created_at": 1_767_225_600})
    base.update(overrides)
    return base


def collection(*items: dict[str, Any]) -> dict[str, Any]:
    return {"entity": "collection", "count": len(items), "items": list(items)}


def fetch(transport: FakeTransport, config: RazorpayConfig) -> Any:
    return fetch_payment(
        transport, config, PAYMENT_ID, expected_order_id=ORDER_ID, expected_amount=AMOUNT
    )


def fetch_order(transport: FakeTransport, config: RazorpayConfig) -> Any:
    return fetch_order_payments(transport, config, ORDER_ID, expected_amount=AMOUNT)


# ------------------------------------------------------------------------- the requests


def test_fetch_payment_request_is_a_plain_authenticated_get(config: RazorpayConfig) -> None:
    request = build_fetch_payment_request(config, PAYMENT_ID)
    assert request.method == "GET"
    assert request.url == f"{config.base_url}/payments/{PAYMENT_ID}"
    assert request.body == b""
    assert request.auth == (config.key_id, config.key_secret)
    assert "Authorization" not in request.headers


def test_order_payments_request_targets_the_order_sub_resource(config: RazorpayConfig) -> None:
    request = build_order_payments_request(config, ORDER_ID)
    assert request.method == "GET"
    assert request.url == f"{config.base_url}/orders/{ORDER_ID}/payments"


def test_identifiers_are_percent_encoded_into_the_path(config: RazorpayConfig) -> None:
    """A ``?`` or ``#`` in an identifier must not become a query string or fragment."""
    request = build_fetch_payment_request(config, "pay_x?y#z")
    assert request.url.endswith("/payments/pay_x%3Fy%23z")


@pytest.mark.parametrize("bad", ["", "pay_a b", "pay_a/b", "pay_a\n"])
def test_malformed_identifiers_are_refused_at_build_time(config: RazorpayConfig, bad: str) -> None:
    with pytest.raises(RequestConstructionError):
        build_fetch_payment_request(config, bad)
    with pytest.raises(RequestConstructionError):
        build_order_payments_request(config, bad)


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("provider_status", "classification", "code", "state"),
    [
        ("captured", FetchClassification.CAPTURED, RecoveryCode.OK, PaymentState.CAPTURED),
        ("authorized", FetchClassification.AUTHORIZED, RecoveryCode.OK, PaymentState.AUTHORIZED),
        ("failed", FetchClassification.FAILED, RecoveryCode.PAYMENT_FAILED, PaymentState.FAILED),
        ("created", FetchClassification.PENDING, RecoveryCode.PAYMENT_PENDING, None),
        ("pending", FetchClassification.PENDING, RecoveryCode.PAYMENT_PENDING, None),
    ],
)
def test_each_provider_status_classifies_deterministically(
    config: RazorpayConfig,
    provider_status: str,
    classification: FetchClassification,
    code: RecoveryCode,
    state: PaymentState | None,
) -> None:
    transport = FakeTransport([json_response(200, entity(status=provider_status))])
    result = fetch(transport, config)

    assert result.classification is classification
    assert result.code is code
    assert result.payment_state is state
    assert result.is_verified
    assert result.evidence is not None
    assert result.evidence.status == classification.value
    assert result.evidence.provider_status == provider_status
    assert transport.call_count == 1


def test_every_offered_state_is_a_legal_successor_of_reconciling(config: RazorpayConfig) -> None:
    """The attempt sits in RECONCILING while a fetch runs; nothing here may break that."""
    legal = PAYMENT_TRANSITIONS[PaymentState.RECONCILING]
    for status in ("captured", "authorized", "failed", "created", "pending", "refunded"):
        result = fetch(FakeTransport([json_response(200, entity(status=status))]), config)
        assert result.payment_state is None or result.payment_state in legal, status


def test_a_captured_fetch_is_sufficient_capture_evidence(config: RazorpayConfig) -> None:
    """Specification 11.2 item 7: this, not the browser callback, is what releases goods."""
    result = fetch(FakeTransport([json_response(200, entity(status="captured"))]), config)
    assert result.evidence is not None
    assert result.evidence.source == EVIDENCE_SOURCE == CaptureEvidence.PROVIDER_FETCH.value
    assert may_fulfil(PaymentState.CAPTURED, CaptureEvidence(result.evidence.source))


def test_an_authorized_fetch_is_not_capture(config: RazorpayConfig) -> None:
    """AUTHORIZED is a hold; nothing ships on it even when read straight from the provider."""
    result = fetch(FakeTransport([json_response(200, entity(status="authorized"))]), config)
    assert result.payment_state is PaymentState.AUTHORIZED
    assert not may_fulfil(PaymentState.AUTHORIZED, CaptureEvidence.PROVIDER_FETCH)


def test_a_refunded_payment_is_recorded_as_a_capture_with_the_refund_visible(
    config: RazorpayConfig,
) -> None:
    """Money was captured and then returned; the capture is still a fact of this attempt."""
    transport = FakeTransport(
        [json_response(200, entity(status="refunded", amount_refunded=AMOUNT.minor))]
    )
    result = fetch(transport, config)
    assert result.classification is FetchClassification.CAPTURED
    assert result.evidence is not None
    assert result.evidence.provider_status == "refunded"
    assert result.evidence.amount_refunded_minor == AMOUNT.minor


def test_a_failed_fetch_result_permits_a_fresh_admission(config: RazorpayConfig) -> None:
    """A provider-confirmed failed payment is the verified absence that allows a retry."""
    transport = FakeTransport(
        [
            json_response(
                200,
                entity(status="failed", error_code="BAD_REQUEST_ERROR", error_reason="declined"),
            )
        ]
    )
    result = fetch(transport, config)
    assert result.code in RETRYABLE
    assert result.evidence is not None
    assert result.evidence.error_code == "BAD_REQUEST_ERROR"
    assert result.evidence.error_reason == "declined"


def test_an_unrecognised_status_is_escalated_never_guessed(config: RazorpayConfig) -> None:
    """A status this adapter has never seen could mean anything; it gets a person."""
    result = fetch(FakeTransport([json_response(200, entity(status="on_hold"))]), config)
    assert result.classification is FetchClassification.UNKNOWN
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.evidence is None
    assert result.provider_error_code == "unrecognised_status: on_hold"


# ----------------------------------------------------------------------- the evidence


def test_evidence_is_primitives_the_kernel_can_mirror(config: RazorpayConfig) -> None:
    """ADR 0003 D2: the kernel accepts this shape without importing the adapter."""
    body = entity(status="captured", captured_at=1_767_225_700, authorized_at=1_767_225_650)
    response = json_response(200, body)
    result = fetch(FakeTransport([response]), config)
    evidence = result.evidence
    assert evidence is not None

    assert evidence.source == "PROVIDER_FETCH"
    assert evidence.provider_payment_id == PAYMENT_ID
    assert evidence.provider_order_id == ORDER_ID
    assert evidence.amount_minor == AMOUNT.minor
    assert evidence.currency == "INR"
    assert evidence.status == "captured"
    assert evidence.captured_at == 1_767_225_700
    assert evidence.authorized_at == 1_767_225_650
    assert evidence.created_at == 1_767_225_600
    assert evidence.amount_refunded_minor == 0
    assert evidence.method == "upi"
    assert evidence.error_code is None
    assert evidence.raw_digest == hashlib.sha256(response.body).hexdigest()
    assert evidence.http_status == 200

    for name in type(evidence).__slots__:
        value = getattr(evidence, name)
        assert value is None or isinstance(value, str | int), (name, type(value))
        assert not isinstance(value, bool), name


def test_absent_timestamps_are_none_not_a_local_clock(config: RazorpayConfig) -> None:
    body = entity(status="captured")
    del body["created_at"]
    result = fetch(FakeTransport([json_response(200, body)]), config)
    assert result.evidence is not None
    assert result.evidence.captured_at is None
    assert result.evidence.authorized_at is None
    assert result.evidence.created_at is None


def test_a_boolean_where_an_integer_belongs_is_not_read_as_a_number(
    config: RazorpayConfig,
) -> None:
    """``True`` is an ``int`` to ``isinstance``; it must never become a timestamp of 1."""
    result = fetch(
        FakeTransport([json_response(200, entity(status="captured", captured_at=True))]), config
    )
    assert result.evidence is not None
    assert result.evidence.captured_at is None


# ------------------------------------------------------------------ echo refusals


@pytest.mark.parametrize(
    "override",
    [
        {"amount": 395000},
        {"currency": "USD"},
        {"order_id": "order_SOMEBODY_ELSE"},
        {"id": "pay_SOMEBODY_ELSE"},
    ],
)
def test_a_payment_that_does_not_echo_the_attempt_is_refused_not_classified(
    config: RazorpayConfig, override: dict[str, Any]
) -> None:
    """The invariant. A captured payment for the wrong amount or order is not evidence
    about this attempt, and recording it as any state would settle the wrong checkout."""
    transport = FakeTransport([json_response(200, entity(status="captured", **override))])
    with pytest.raises(EvidenceMismatchError):
        fetch(transport, config)


def test_the_mismatch_refusal_is_an_adapter_error() -> None:
    """So the worker's one ``except RazorpayAdapterError`` escalates it with the rest."""
    assert issubclass(EvidenceMismatchError, RazorpayAdapterError)
    assert issubclass(EvidenceMismatchError, DomainError)


def test_a_foreign_payment_in_the_order_list_refuses_the_whole_list(
    config: RazorpayConfig,
) -> None:
    transport = FakeTransport(
        [
            json_response(
                200,
                collection(
                    entity(status="failed"),
                    entity(id="pay_OTHER", status="captured", order_id="order_OTHER"),
                ),
            )
        ]
    )
    with pytest.raises(EvidenceMismatchError):
        fetch_order(transport, config)


# ----------------------------------------------------------------- unknown outcomes


def test_a_timeout_is_unknown_and_must_reconcile_again(config: RazorpayConfig) -> None:
    """Specification 10.7: a fetch that produced no answer changes nothing."""
    result = fetch(FakeTransport(error=TransportTimeoutError("read timed out")), config)
    assert result.classification is FetchClassification.UNKNOWN
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.must_reconcile
    assert result.payment_state is None
    assert result.evidence is None
    assert result.http_status is None


def test_a_connection_failure_is_unknown(config: RazorpayConfig) -> None:
    result = fetch_order(FakeTransport(error=TransportError("connection reset")), config)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.must_reconcile


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 599])
def test_every_unanticipated_status_is_unknown(config: RazorpayConfig, status: int) -> None:
    result = fetch(
        FakeTransport([json_response(status, {"error": {"code": "SERVER_ERROR"}})]), config
    )
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.classification is FetchClassification.UNKNOWN
    assert result.provider_error_code == "SERVER_ERROR"


def test_an_unreadable_success_body_is_unknown(config: RazorpayConfig) -> None:
    result = fetch(FakeTransport([HttpResponse(status=200, body=b"<html>gateway</html>")]), config)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.provider_error_code == "unreadable_body"


def test_an_entity_missing_an_identifying_field_is_unknown(config: RazorpayConfig) -> None:
    """No amount means nothing to echo-check, and an unchecked entity is not evidence."""
    body = entity(status="captured")
    del body["amount"]
    result = fetch(FakeTransport([json_response(200, body)]), config)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.provider_error_code == "unreadable_entity"


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_refused_read_is_never_a_failed_payment(config: RazorpayConfig, status: int) -> None:
    """A 404 on the identifier we recorded is an inconsistency, not a declined card."""
    result = fetch(
        FakeTransport([json_response(status, {"error": {"code": "BAD_REQUEST_ERROR"}})]), config
    )
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.code not in RETRYABLE
    assert result.classification is FetchClassification.UNKNOWN
    assert result.payment_state is None


@pytest.mark.parametrize("status", [401, 403])
def test_refused_credentials_need_an_operator(config: RazorpayConfig, status: int) -> None:
    result = fetch_order(
        FakeTransport([json_response(status, {"error": {"code": "UNAUTHORIZED"}})]), config
    )
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.provider_error_code == "UNAUTHORIZED"


# ------------------------------------------------------------------ order payments


def test_an_order_with_no_payments_is_a_verified_pending(config: RazorpayConfig) -> None:
    """The provider affirmatively said nothing was attempted: information, not unknown."""
    result = fetch_order(FakeTransport([json_response(200, collection())]), config)
    assert result.classification is FetchClassification.PENDING
    assert result.code is RecoveryCode.PAYMENT_PENDING
    assert result.is_verified
    assert not result.must_reconcile
    assert result.evidence is None
    assert result.payments == ()


def test_a_failed_first_try_does_not_hide_a_captured_retry(config: RazorpayConfig) -> None:
    """Razorpay Checkout lets a buyer retry on the same order; the capture must win."""
    transport = FakeTransport(
        [
            json_response(
                200,
                collection(
                    entity(id="pay_first", status="failed"),
                    entity(id="pay_second", status="captured"),
                ),
            )
        ]
    )
    result = fetch_order(transport, config)
    assert result.classification is FetchClassification.CAPTURED
    assert result.payment_state is PaymentState.CAPTURED
    assert result.evidence is not None
    assert result.evidence.provider_payment_id == "pay_second"
    assert [p.provider_payment_id for p in result.payments] == ["pay_first", "pay_second"]
    assert transport.call_count == 1


def test_only_failed_payments_on_an_order_is_failed(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, collection(entity(status="failed")))])
    result = fetch_order(transport, config)
    assert result.classification is FetchClassification.FAILED
    assert result.code is RecoveryCode.PAYMENT_FAILED


def test_two_settled_payments_on_one_order_are_escalated_not_chosen(
    config: RazorpayConfig,
) -> None:
    """Two captures for one order is a double charge; nobody should pick one silently."""
    transport = FakeTransport(
        [
            json_response(
                200,
                collection(
                    entity(id="pay_a", status="captured"),
                    entity(id="pay_b", status="authorized"),
                ),
            )
        ]
    )
    result = fetch_order(transport, config)
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.classification is FetchClassification.UNKNOWN
    assert result.evidence is None
    assert len(result.payments) == 2
    assert result.provider_error_code == "multiple_settled_payments: 2"


def test_a_collection_that_is_not_a_list_is_unknown(config: RazorpayConfig) -> None:
    result = fetch_order(FakeTransport([json_response(200, {"items": "nope"})]), config)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.provider_error_code == "unreadable_collection"


def test_every_payment_in_the_list_shares_the_response_digest(config: RazorpayConfig) -> None:
    """The audit row points at one set of bytes for the whole list."""
    response = json_response(
        200, collection(entity(id="pay_a", status="failed"), entity(id="pay_b", status="created"))
    )
    result = fetch_order(FakeTransport([response]), config)
    digest = hashlib.sha256(response.body).hexdigest()
    assert {p.raw_digest for p in result.payments} == {digest}
    assert json.loads(response.body)["count"] == 2


# --------------------------------------------------------------- error hierarchy


def test_transport_errors_are_adapter_errors() -> None:
    """A worker guarding a provider call with the package base sees a lost connection too.

    Before this, ``TransportError`` subclassed bare ``Exception`` and escaped an
    ``except RazorpayAdapterError`` as an unclassified crash.
    """
    assert issubclass(TransportError, RazorpayAdapterError)
    assert issubclass(TransportTimeoutError, TransportError)
    assert issubclass(TransportTimeoutError, DomainError)

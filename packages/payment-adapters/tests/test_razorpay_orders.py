"""Create-order payload and outcome classification, specification 11.1 and 10.6.

Two clusters of invariants:

* the receipt cap is enforced by **refusal**, never by truncation -- truncation makes two
  distinct receipts collide, and the receipt is the key the recovery lookup uses;
* an outcome that might have created an order is ``PAYMENT_UNKNOWN`` and never
  ``PAYMENT_FAILED``. The tests below walk every branch that could get that backwards.
"""

from __future__ import annotations

import json

import pytest
from commerce_domain import Money
from conftest import FakeTransport, json_response
from payment_adapters.razorpay import (
    MAX_RECEIPT_LENGTH,
    HttpResponse,
    RazorpayConfig,
    RequestConstructionError,
    TransportError,
    TransportTimeoutError,
    build_create_order_request,
    create_order,
    find_order_by_receipt,
)
from transaction_kernel.recovery import RETRYABLE, RecoveryCode
from transaction_kernel.states import PAYMENT_TRANSITIONS, PaymentState

AMOUNT = Money(39500, "INR")
RECEIPT = "chk_7f3a2b1c_v1"
ORDER_ID = "order_9A33XWu170gUtm"


def order_entity(
    *,
    amount: int = AMOUNT.minor,
    currency: str = "INR",
    receipt: str = RECEIPT,
    order_id: str = ORDER_ID,
) -> dict[str, object]:
    """The subset of Razorpay's order entity the adapter reads back."""
    return {
        "id": order_id,
        "entity": "order",
        "amount": amount,
        "currency": currency,
        "receipt": receipt,
        "status": "created",
    }


# ------------------------------------------------------------------------ the payload


def test_the_payload_is_exactly_amount_currency_and_receipt(config: RazorpayConfig) -> None:
    """Pin the bytes. Anything extra is something the platform did not decide to send."""
    request = build_create_order_request(config, amount=AMOUNT, receipt=RECEIPT)
    assert request.method == "POST"
    assert request.url.endswith("/v1/orders")
    assert json.loads(request.body) == {
        "amount": 39500,
        "currency": "INR",
        "receipt": RECEIPT,
    }


def test_the_amount_is_integer_minor_units(config: RazorpayConfig) -> None:
    """39500 paise, never 395.0. A float in a charge is a rounding bug with a receipt."""
    body = json.loads(build_create_order_request(config, amount=AMOUNT, receipt=RECEIPT).body)
    assert body["amount"] == 39500
    assert isinstance(body["amount"], int)
    assert b"395.0" not in build_create_order_request(config, amount=AMOUNT, receipt=RECEIPT).body


def test_notes_are_omitted_when_empty(config: RazorpayConfig) -> None:
    request = build_create_order_request(config, amount=AMOUNT, receipt=RECEIPT, notes={})
    assert "notes" not in json.loads(request.body)


def test_notes_are_included_when_present(config: RazorpayConfig) -> None:
    request = build_create_order_request(
        config, amount=AMOUNT, receipt=RECEIPT, notes={"checkout_version": "1"}
    )
    assert json.loads(request.body)["notes"] == {"checkout_version": "1"}


def test_the_payload_is_byte_stable_regardless_of_note_insertion_order(
    config: RazorpayConfig,
) -> None:
    """Canonical serialization: the same facts always produce the same bytes.

    Without this, an idempotency or audit hash computed over the request body would differ
    between two processes that built the same order.
    """
    first = build_create_order_request(
        config, amount=AMOUNT, receipt=RECEIPT, notes={"b": "2", "a": "1"}
    )
    second = build_create_order_request(
        config, amount=AMOUNT, receipt=RECEIPT, notes={"a": "1", "b": "2"}
    )
    assert first.body == second.body


def test_credentials_are_carried_out_of_band_and_not_in_headers(config: RazorpayConfig) -> None:
    """The secret never sits in a header dict that a log statement might render."""
    request = build_create_order_request(config, amount=AMOUNT, receipt=RECEIPT)
    assert request.auth == (config.key_id, config.key_secret)
    rendered = json.dumps(request.redacted())
    assert config.key_secret not in rendered
    assert "Authorization" not in request.headers


# ----------------------------------------------------------------------- the receipt


def test_a_receipt_of_exactly_forty_characters_is_accepted(config: RazorpayConfig) -> None:
    receipt = "r" * MAX_RECEIPT_LENGTH
    request = build_create_order_request(config, amount=AMOUNT, receipt=receipt)
    assert json.loads(request.body)["receipt"] == receipt


def test_a_receipt_over_forty_characters_is_refused_not_truncated(
    config: RazorpayConfig,
) -> None:
    """The invariant. Truncation is the tempting fix and the dangerous one.

    Two receipts that share their first 40 characters would become one string, the
    lookup-by-receipt recovery would return the wrong order, and a payment for checkout A
    would settle checkout B.
    """
    receipt = "r" * (MAX_RECEIPT_LENGTH + 1)
    with pytest.raises(RequestConstructionError, match="at most 40"):
        build_create_order_request(config, amount=AMOUNT, receipt=receipt)


def test_two_receipts_sharing_a_forty_character_prefix_never_collapse(
    config: RazorpayConfig,
) -> None:
    """Demonstrates the collision that truncation would create.

    Both of these would truncate to the same 40 characters. Both are refused, so neither
    can ever be silently confused with the other.
    """
    prefix = "chk_" + "a" * 36
    for suffix in ("_left", "_right"):
        with pytest.raises(RequestConstructionError):
            build_create_order_request(config, amount=AMOUNT, receipt=prefix + suffix)


def test_an_empty_receipt_is_refused(config: RazorpayConfig) -> None:
    """No receipt means no recovery path after a lost create response."""
    with pytest.raises(RequestConstructionError, match="lookup key"):
        build_create_order_request(config, amount=AMOUNT, receipt="")


def test_a_receipt_with_surrounding_whitespace_is_refused(config: RazorpayConfig) -> None:
    with pytest.raises(RequestConstructionError, match="whitespace"):
        build_create_order_request(config, amount=AMOUNT, receipt=" chk_1 ")


# ------------------------------------------------------------------------ the amount


@pytest.mark.parametrize("minor", [0, -1, -39500])
def test_a_non_positive_amount_is_refused(config: RazorpayConfig, minor: int) -> None:
    with pytest.raises(RequestConstructionError, match="positive"):
        build_create_order_request(config, amount=Money(minor, "INR"), receipt=RECEIPT)


def test_an_amount_below_the_inr_minimum_is_refused(config: RazorpayConfig) -> None:
    """A provider constraint enforced in the adapter, per specification 12.5."""
    with pytest.raises(RequestConstructionError, match="minor units"):
        build_create_order_request(config, amount=Money(99, "INR"), receipt=RECEIPT)


def test_notes_beyond_the_provider_cap_are_refused(config: RazorpayConfig) -> None:
    notes = {f"k{i}": "v" for i in range(16)}
    with pytest.raises(RequestConstructionError, match="at most 15"):
        build_create_order_request(config, amount=AMOUNT, receipt=RECEIPT, notes=notes)


def test_a_non_string_note_value_is_refused(config: RazorpayConfig) -> None:
    with pytest.raises(RequestConstructionError, match="str to str"):
        build_create_order_request(
            config,
            amount=AMOUNT,
            receipt=RECEIPT,
            notes={"qty": 3},  # type: ignore[dict-item]
        )


# ------------------------------------------------------------------- outcome: success


def test_a_created_order_yields_ok_and_submitted(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, order_entity())])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)

    assert result.code is RecoveryCode.OK
    assert result.payment_state is PaymentState.SUBMITTED
    assert result.order_id == ORDER_ID
    assert result.order_exists
    assert not result.must_reconcile


def test_exactly_one_request_is_sent(config: RazorpayConfig) -> None:
    """No internal retry. A retry is a new admission and a new single-use grant."""
    transport = FakeTransport([json_response(200, order_entity())])
    create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert transport.call_count == 1


def test_the_resulting_state_is_a_legal_successor_of_created(config: RazorpayConfig) -> None:
    """Whatever branch is taken, the attempt lands somewhere the lifecycle allows."""
    legal = PAYMENT_TRANSITIONS[PaymentState.CREATED]
    cases = [
        FakeTransport([json_response(200, order_entity())]),
        FakeTransport([json_response(400, {"error": {"code": "BAD_REQUEST_ERROR"}})]),
        FakeTransport([json_response(502, {})]),
        FakeTransport(error=TransportTimeoutError("timed out")),
    ]
    for transport in cases:
        result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
        assert result.payment_state in legal, result


# ------------------------------------------------------------------- outcome: unknown


def test_a_timeout_is_unknown_and_never_failed(config: RazorpayConfig) -> None:
    """The invariant that stops a double charge.

    The order may exist. Calling this a failure would authorize a second create, and the
    buyer would be charged twice for one basket.
    """
    transport = FakeTransport(error=TransportTimeoutError("read timed out"))
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)

    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.payment_state is PaymentState.UNKNOWN
    assert result.must_reconcile
    assert result.code not in RETRYABLE, "an unknown outcome is reconciled, never retried"


def test_a_connection_failure_is_unknown(config: RazorpayConfig) -> None:
    transport = FakeTransport(error=TransportError("connection reset"))
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 599])
def test_every_unanticipated_status_is_unknown(config: RazorpayConfig, status: int) -> None:
    """Certainty is an allowlist. Nothing outside it may authorize a second create."""
    transport = FakeTransport([json_response(status, {"error": {"code": "SERVER_ERROR"}})])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.payment_state is PaymentState.UNKNOWN


def test_a_success_with_an_unreadable_body_is_unknown(config: RazorpayConfig) -> None:
    """A 200 whose body is garbage most likely means the order exists."""
    transport = FakeTransport([HttpResponse(status=200, body=b"<html>gateway</html>")])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.order_id is None


def test_a_success_without_an_order_id_is_unknown(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, {"entity": "order", "amount": 39500})])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN


# -------------------------------------------------------------------- outcome: failed


def test_a_validation_rejection_is_a_confirmed_failure(config: RazorpayConfig) -> None:
    """400 means nothing was created, so a retry under a new grant is safe."""
    transport = FakeTransport(
        [json_response(400, {"error": {"code": "BAD_REQUEST_ERROR", "description": "invalid"}})]
    )
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)

    assert result.code is RecoveryCode.PAYMENT_FAILED
    assert result.code in RETRYABLE
    assert result.payment_state is PaymentState.FAILED
    assert result.provider_error_code == "BAD_REQUEST_ERROR"


@pytest.mark.parametrize("status", [401, 403])
def test_refused_credentials_need_an_operator_not_a_retry(
    config: RazorpayConfig, status: int
) -> None:
    """Telling a buyer their card failed when nothing was ever asked of it is a lie.

    A credential the provider rejects is an operator problem, and retrying it on a loop
    burns the checkout without ever reaching the bank.
    """
    transport = FakeTransport([json_response(status, {"error": {"code": "UNAUTHORIZED"}})])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)

    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.code not in RETRYABLE
    assert result.payment_state is PaymentState.FAILED


# -------------------------------------------------------------------- integrity check


def test_an_order_echoing_a_different_amount_is_refused(config: RazorpayConfig) -> None:
    """Sending the buyer to a checkout for an amount they did not approve is unacceptable.

    This catches a receipt collision, a mis-built payload, or a response routed from
    another request -- before the payment surface opens, not after it settles.
    """
    transport = FakeTransport([json_response(200, order_entity(amount=395000))])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)

    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
    assert result.payment_state is PaymentState.UNKNOWN
    assert result.provider_error_code is not None
    assert "amount" in result.provider_error_code


def test_an_order_echoing_a_different_currency_is_refused(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, order_entity(currency="USD"))])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED


def test_an_order_echoing_a_different_receipt_is_refused(config: RazorpayConfig) -> None:
    transport = FakeTransport([json_response(200, order_entity(receipt="somebody_elses"))])
    result = create_order(transport, config, amount=AMOUNT, receipt=RECEIPT)
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED


# ------------------------------------------------------------- recovery by receipt


def test_a_matching_order_is_recovered(config: RazorpayConfig) -> None:
    """Specification 10.6: find the existing order rather than creating a second one."""
    transport = FakeTransport([json_response(200, {"count": 1, "items": [order_entity()]})])
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)

    assert result.code is RecoveryCode.OK
    assert result.order_id == ORDER_ID
    assert result.found
    assert not result.verified_absent
    assert not result.must_reconcile


def test_a_recovered_order_keeps_the_attempt_reconciling(config: RazorpayConfig) -> None:
    """Finding the order proves it exists, not that it was paid.

    The attempt is ``RECONCILING`` while this lookup runs, and ``RECONCILING -> SUBMITTED``
    is not a legal transition. An earlier version reported ``SUBMITTED`` here and the
    kernel would have refused it. The correct outcome is no state at all: record the
    provider order id, stay put, and go on to fetch the order's payments.
    """
    transport = FakeTransport([json_response(200, {"count": 1, "items": [order_entity()]})])
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)

    assert result.payment_state is None
    assert PaymentState.SUBMITTED not in PAYMENT_TRANSITIONS[PaymentState.RECONCILING]


def test_every_lookup_state_is_a_legal_successor_of_reconciling(config: RazorpayConfig) -> None:
    """Whatever the lookup finds, the state it offers is one the attempt can reach."""
    legal = PAYMENT_TRANSITIONS[PaymentState.RECONCILING]
    cases = [
        FakeTransport([json_response(200, {"count": 1, "items": [order_entity()]})]),
        FakeTransport([json_response(200, {"count": 0, "items": []})]),
        FakeTransport([json_response(200, {"count": 1, "items": [order_entity(amount=1)]})]),
        FakeTransport(
            [
                json_response(
                    200,
                    {"count": 2, "items": [order_entity(), order_entity(order_id="order_B")]},
                )
            ]
        ),
        FakeTransport([json_response(500, {})]),
        FakeTransport(error=TransportTimeoutError("timed out")),
    ]
    for transport in cases:
        result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)
        assert result.payment_state is None or result.payment_state in legal, result


def test_a_verified_absence_permits_a_fresh_attempt(config: RazorpayConfig) -> None:
    """Only the provider saying "no such receipt" turns unknown into a safe retry."""
    transport = FakeTransport([json_response(200, {"count": 0, "items": []})])
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)

    assert result.code is RecoveryCode.PAYMENT_FAILED
    assert result.code in RETRYABLE
    assert result.order_id is None
    assert result.verified_absent
    assert not result.found
    assert result.payment_state is PaymentState.FAILED


def test_a_failed_lookup_is_never_evidence_of_absence(config: RazorpayConfig) -> None:
    """A 500 on the lookup tells us nothing, and must not authorize a second create."""
    transport = FakeTransport([json_response(500, {})])
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)

    assert result.code is RecoveryCode.PAYMENT_UNKNOWN
    assert result.must_reconcile


def test_a_lookup_timeout_is_never_evidence_of_absence(config: RazorpayConfig) -> None:
    transport = FakeTransport(error=TransportTimeoutError("timed out"))
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)
    assert result.code is RecoveryCode.PAYMENT_UNKNOWN


def test_duplicate_receipts_are_escalated_never_guessed(config: RazorpayConfig) -> None:
    """Receipts are unique per tenant by constraint; two means it is already broken.

    Picking one of them automatically would settle a checkout against an order that may
    belong to a different attempt.
    """
    transport = FakeTransport(
        [
            json_response(
                200,
                {
                    "count": 2,
                    "items": [order_entity(), order_entity(order_id="order_OTHER")],
                },
            )
        ]
    )
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED


def test_a_recovered_order_for_a_different_amount_is_refused(config: RazorpayConfig) -> None:
    """A collision found during recovery is still a collision."""
    transport = FakeTransport([json_response(200, {"count": 1, "items": [order_entity(amount=1)]})])
    result = find_order_by_receipt(transport, config, receipt=RECEIPT, amount=AMOUNT)
    assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

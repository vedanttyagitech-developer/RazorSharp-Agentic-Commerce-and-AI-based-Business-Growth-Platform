"""The kernel's evidence record mirrors the adapter's, proven across the package boundary.

ADR 0003 D2: the kernel never imports ``payment_adapters``, so it cannot type-check the
shape the adapter produces. This test lives on the adapter's side of the boundary (which
does depend on the kernel) and feeds a real ``PaymentFetchResult`` -- built through the
scripted transport, not by hand -- into ``transaction_kernel.evidence.ProviderEvidence``
exactly as the worker will: ``dataclasses.asdict(result.evidence)``. A field added on
either side fails here, before it can fail in a worker at 2am.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from commerce_domain import Money
from conftest import FakeTransport, json_response, payment_entity
from payment_adapters.razorpay import (
    EVIDENCE_SOURCE,
    RazorpayConfig,
    fetch_order_payments,
    fetch_payment,
)
from payment_adapters.razorpay import fulfilment as shim
from payment_adapters.razorpay import payments as adapter_payments
from transaction_kernel import evidence as kernel_evidence
from transaction_kernel.evidence import EvidenceSource, ProviderEvidence
from transaction_kernel.states import PaymentState

AMOUNT = Money(39500, "INR")
PAYMENT_ID = "pay_29QQoUBi66xm2f"
ORDER_ID = "order_9A33XWu170gUtm"

#: The adapter documents epoch seconds; the kernel stores RFC 3339 UTC. Every other field
#: must round-trip by equality.
_TIMESTAMP_FIELDS = frozenset({"captured_at", "authorized_at", "created_at"})


def entity(**overrides: Any) -> dict[str, Any]:
    base = payment_entity(payment_id=PAYMENT_ID, order_id=ORDER_ID, amount=AMOUNT.minor)
    base.update({"method": "upi", "created_at": 1_767_225_600})
    base.update(overrides)
    return base


def _rfc3339(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_mirror(adapter_evidence: adapter_payments.ProviderEvidence) -> ProviderEvidence:
    """Feed the adapter's record through the kernel and check every field round-trips."""
    raw = dataclasses.asdict(adapter_evidence)
    mirrored = ProviderEvidence.from_mapping(raw)
    for name, value in raw.items():
        kernel_value = getattr(mirrored, name)
        if name == "source":
            assert kernel_value is EvidenceSource(value), name
        elif name in _TIMESTAMP_FIELDS:
            assert kernel_value == (None if value is None else _rfc3339(value)), name
        else:
            assert kernel_value == value, name
    assert mirrored.event_id is None, "a fetch is not an event"
    return mirrored


# --------------------------------------------------------------------- the shape


def test_field_vocabularies_agree_in_both_directions() -> None:
    """The kernel accepts exactly the adapter's fields plus ``event_id``, and nothing else."""
    adapter_fields = {f.name for f in dataclasses.fields(adapter_payments.ProviderEvidence)}
    assert ProviderEvidence.field_names() == adapter_fields | {"event_id"}


@pytest.mark.parametrize(
    ("provider_status", "state"),
    [
        ("captured", PaymentState.CAPTURED),
        ("authorized", PaymentState.AUTHORIZED),
        ("failed", PaymentState.FAILED),
        ("created", None),
        ("refunded", PaymentState.CAPTURED),
    ],
)
def test_a_fetched_payment_round_trips_through_the_kernel(
    config: RazorpayConfig, provider_status: str, state: PaymentState | None
) -> None:
    body = entity(
        status=provider_status,
        captured_at=1_767_225_700,
        authorized_at=1_767_225_650,
        amount_refunded=AMOUNT.minor if provider_status == "refunded" else 0,
        error_code="BAD_REQUEST_ERROR" if provider_status == "failed" else None,
        error_reason="declined" if provider_status == "failed" else None,
    )
    result = fetch_payment(
        FakeTransport([json_response(200, body)]),
        config,
        PAYMENT_ID,
        expected_order_id=ORDER_ID,
        expected_amount=AMOUNT,
    )
    assert result.evidence is not None

    mirrored = _assert_mirror(result.evidence)
    assert mirrored.source is EvidenceSource.PROVIDER_FETCH
    assert mirrored.payment_state is state is result.payment_state
    assert mirrored.created_at == "2026-01-01T00:00:00Z"
    assert mirrored.captured_at == "2026-01-01T00:01:40Z"
    assert mirrored.refund_reported is (provider_status == "refunded")


def test_an_order_payments_list_round_trips_item_by_item(config: RazorpayConfig) -> None:
    failed = entity(id="pay_first", status="failed", error_code="BAD_REQUEST_ERROR")
    captured = entity(status="captured")
    transport = FakeTransport(
        [json_response(200, {"entity": "collection", "count": 2, "items": [failed, captured]})]
    )
    result = fetch_order_payments(transport, config, ORDER_ID, expected_amount=AMOUNT)
    assert result.evidence is not None
    for item in result.payments:
        _assert_mirror(item)
    decisive = _assert_mirror(result.evidence)
    assert decisive.provider_payment_id == PAYMENT_ID
    assert decisive.payment_state is PaymentState.CAPTURED


def test_absent_timestamps_stay_absent_in_the_kernel(config: RazorpayConfig) -> None:
    """Neither side synthesises a time from a local clock."""
    body = entity()
    del body["created_at"]
    result = fetch_payment(
        FakeTransport([json_response(200, body)]),
        config,
        PAYMENT_ID,
        expected_order_id=ORDER_ID,
        expected_amount=AMOUNT,
    )
    assert result.evidence is not None
    mirrored = _assert_mirror(result.evidence)
    assert (mirrored.created_at, mirrored.captured_at, mirrored.authorized_at) == (
        None,
        None,
        None,
    )


# -------------------------------------------------------------- the source string


def test_the_adapter_source_string_is_the_kernel_channel_that_fulfils(
    config: RazorpayConfig,
) -> None:
    """``EVIDENCE_SOURCE`` is compared by string; the kernel must fulfil on it."""
    result = fetch_payment(
        FakeTransport([json_response(200, entity(status="captured"))]),
        config,
        PAYMENT_ID,
        expected_order_id=ORDER_ID,
        expected_amount=AMOUNT,
    )
    assert result.evidence is not None
    mirrored = _assert_mirror(result.evidence)
    assert result.evidence.source == EVIDENCE_SOURCE == mirrored.source.value
    assert mirrored.channel is kernel_evidence.CaptureEvidence.PROVIDER_FETCH
    assert kernel_evidence.may_fulfil(PaymentState.CAPTURED, mirrored.channel)


# ----------------------------------------------------------------- the re-export


def test_fulfilment_shim_re_exports_the_kernel_objects_unchanged() -> None:
    """ADR D2: one definition, in the kernel; the adapter path is the same object."""
    assert shim.CaptureEvidence is kernel_evidence.CaptureEvidence
    assert shim.may_fulfil is kernel_evidence.may_fulfil
    assert shim.requires_release_not_capture is kernel_evidence.requires_release_not_capture
    assert set(shim.__all__) == {"CaptureEvidence", "may_fulfil", "requires_release_not_capture"}

"""The evidence vocabulary: the fulfilment gate and the strict evidence record.

The fulfilment gate moved here from the Razorpay adapter (ADR 0003 D2) and its behaviour
is asserted again in the kernel's own suite so a later edit cannot loosen it while the
adapter's re-export test still passes. The record tests are about strictness: a worker
that sends a malformed field gets a refusal, never a best-effort evidence object.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from transaction_kernel.evidence import (
    EVIDENCE_STATUSES,
    CaptureEvidence,
    EvidenceError,
    EvidenceSource,
    ProviderEvidence,
    capture_channel,
    may_fulfil,
    requires_release_not_capture,
)
from transaction_kernel.states import PaymentState

DIGEST = "a" * 64


def mapping(**overrides: Any) -> dict[str, Any]:
    """A mapping shaped exactly like ``dataclasses.asdict`` of the adapter's evidence."""
    base: dict[str, Any] = {
        "source": "PROVIDER_FETCH",
        "provider_payment_id": "pay_29QQoUBi66xm2f",
        "provider_order_id": "order_9A33XWu170gUtm",
        "amount_minor": 39500,
        "currency": "INR",
        "status": "captured",
        "provider_status": "captured",
        "captured_at": 1_767_225_700,
        "authorized_at": 1_767_225_650,
        "created_at": 1_767_225_600,
        "amount_refunded_minor": 0,
        "method": "upi",
        "error_code": None,
        "error_reason": None,
        "raw_digest": DIGEST,
        "http_status": 200,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ fulfilment gate


class TestFulfilmentGate:
    @pytest.mark.parametrize(
        "evidence",
        [
            CaptureEvidence.VERIFIED_WEBHOOK,
            CaptureEvidence.PROVIDER_FETCH,
            CaptureEvidence.OPERATOR_RESOLUTION,
        ],
    )
    def test_a_verified_capture_may_be_fulfilled(self, evidence: CaptureEvidence) -> None:
        assert may_fulfil(PaymentState.CAPTURED, evidence)

    def test_a_browser_callback_alone_is_never_capture_evidence(self) -> None:
        """Specification 11.2 item 7 and ADR D8: the signature proves a key, not money."""
        assert not may_fulfil(PaymentState.CAPTURED, CaptureEvidence.BROWSER_CALLBACK)

    def test_capture_is_the_only_fulfillable_state(self) -> None:
        fulfillable = {
            state for state in PaymentState if may_fulfil(state, CaptureEvidence.PROVIDER_FETCH)
        }
        assert fulfillable == {PaymentState.CAPTURED}

    def test_enum_values_are_the_strings_the_adapter_compares_against(self) -> None:
        """``payments.EVIDENCE_SOURCE == "PROVIDER_FETCH"`` is compared by string."""
        assert {e.value for e in CaptureEvidence} == {
            "BROWSER_CALLBACK",
            "VERIFIED_WEBHOOK",
            "PROVIDER_FETCH",
            "OPERATOR_RESOLUTION",
        }

    def test_release_not_capture_answers_only_for_an_authorization(self) -> None:
        assert requires_release_not_capture(PaymentState.AUTHORIZED, checkout_invalidated=True)
        assert not requires_release_not_capture(PaymentState.AUTHORIZED, checkout_invalidated=False)
        assert not requires_release_not_capture(PaymentState.CAPTURED, checkout_invalidated=True)

    def test_every_source_maps_to_a_channel_and_only_server_channels_fulfil(self) -> None:
        channels = {source: capture_channel(source) for source in EvidenceSource}
        assert channels[EvidenceSource.WEBHOOK] is CaptureEvidence.VERIFIED_WEBHOOK
        assert channels[EvidenceSource.PROVIDER_FETCH] is CaptureEvidence.PROVIDER_FETCH
        assert channels[EvidenceSource.BROWSER_CALLBACK] is CaptureEvidence.BROWSER_CALLBACK
        assert may_fulfil(PaymentState.CAPTURED, channels[EvidenceSource.WEBHOOK])
        assert may_fulfil(PaymentState.CAPTURED, channels[EvidenceSource.PROVIDER_FETCH])
        assert not may_fulfil(PaymentState.CAPTURED, channels[EvidenceSource.BROWSER_CALLBACK])


# ------------------------------------------------------------------ evidence record


class TestFromMapping:
    def test_accepts_the_adapter_shape_verbatim(self) -> None:
        evidence = ProviderEvidence.from_mapping(mapping())
        assert evidence.source is EvidenceSource.PROVIDER_FETCH
        assert evidence.provider_payment_id == "pay_29QQoUBi66xm2f"
        assert evidence.provider_order_id == "order_9A33XWu170gUtm"
        assert evidence.amount_minor == 39500
        assert evidence.currency == "INR"
        assert evidence.status == "captured"
        assert evidence.provider_status == "captured"
        assert evidence.amount_refunded_minor == 0
        assert evidence.method == "upi"
        assert evidence.error_code is None
        assert evidence.raw_digest == DIGEST
        assert evidence.http_status == 200
        assert evidence.event_id is None

    def test_epoch_seconds_become_rfc3339_utc(self) -> None:
        """Razorpay emits epoch seconds; the kernel stores one uniform, readable form."""
        evidence = ProviderEvidence.from_mapping(mapping())
        assert evidence.created_at == "2026-01-01T00:00:00Z"
        assert evidence.authorized_at == "2026-01-01T00:00:50Z"
        assert evidence.captured_at == "2026-01-01T00:01:40Z"

    def test_rfc3339_strings_are_accepted_and_normalised_to_utc(self) -> None:
        evidence = ProviderEvidence.from_mapping(
            mapping(created_at="2026-01-01T05:30:00+05:30", captured_at=None, authorized_at=None)
        )
        assert evidence.created_at == "2026-01-01T00:00:00Z"
        assert evidence.captured_at is None

    def test_absent_timestamps_stay_none(self) -> None:
        evidence = ProviderEvidence.from_mapping(
            mapping(captured_at=None, authorized_at=None, created_at=None)
        )
        assert (evidence.captured_at, evidence.authorized_at, evidence.created_at) == (
            None,
            None,
            None,
        )

    @pytest.mark.parametrize("value", ["yesterday", "2026-01-01T00:00:00", True, 1.5, -1])
    def test_a_timestamp_that_is_neither_epoch_nor_rfc3339_is_refused(self, value: Any) -> None:
        """A naive timestamp is ambiguous, and a float or bool is a mangled payload."""
        with pytest.raises(EvidenceError):
            ProviderEvidence.from_mapping(mapping(created_at=value))

    def test_webhook_source_is_accepted_under_both_spellings(self) -> None:
        """The ADR says WEBHOOK, the enum says VERIFIED_WEBHOOK; both mean one channel."""
        a = ProviderEvidence.from_mapping(mapping(source="WEBHOOK", event_id="evt_1"))
        b = ProviderEvidence.from_mapping(mapping(source="VERIFIED_WEBHOOK", event_id="evt_1"))
        assert a.source is EvidenceSource.WEBHOOK
        assert a == b
        assert a.channel is CaptureEvidence.VERIFIED_WEBHOOK

    def test_a_browser_callback_is_a_legal_source_but_not_a_sufficient_channel(self) -> None:
        evidence = ProviderEvidence.from_mapping(mapping(source="BROWSER_CALLBACK"))
        assert evidence.channel is CaptureEvidence.BROWSER_CALLBACK
        assert not may_fulfil(PaymentState.CAPTURED, evidence.channel)

    @pytest.mark.parametrize("source", ["OPERATOR_RESOLUTION", "provider_fetch", "", None, 3])
    def test_other_sources_are_refused(self, source: Any) -> None:
        with pytest.raises(EvidenceError):
            ProviderEvidence.from_mapping(mapping(source=source))

    def test_unknown_fields_are_refused_so_a_new_field_is_a_decision(self) -> None:
        with pytest.raises(EvidenceError, match="unknown fields"):
            ProviderEvidence.from_mapping(mapping(settled_at=1))

    @pytest.mark.parametrize(
        "field",
        [
            "source",
            "provider_payment_id",
            "provider_order_id",
            "amount_minor",
            "currency",
            "status",
            "provider_status",
            "raw_digest",
        ],
    )
    def test_required_fields_cannot_be_missing(self, field: str) -> None:
        m = mapping()
        del m[field]
        with pytest.raises(EvidenceError, match="missing required"):
            ProviderEvidence.from_mapping(m)

    def test_status_vocabulary_is_closed_and_excludes_unknown(self) -> None:
        assert frozenset({"authorized", "captured", "failed", "pending"}) == EVIDENCE_STATUSES
        with pytest.raises(EvidenceError):
            ProviderEvidence.from_mapping(mapping(status="unknown"))
        with pytest.raises(EvidenceError):
            ProviderEvidence.from_mapping(mapping(status="CAPTURED"))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("amount_minor", True),
            ("amount_minor", 0),
            ("amount_minor", "39500"),
            ("amount_refunded_minor", -1),
            ("amount_refunded_minor", False),
            ("currency", "inr"),
            ("currency", "INRR"),
            ("raw_digest", "A" * 64),
            ("raw_digest", "ab"),
            ("provider_payment_id", "pay 1"),
            ("provider_payment_id", ""),
            ("provider_order_id", "o" * 65),
            ("http_status", 42),
            ("http_status", 600),
            ("event_id", ""),
        ],
    )
    def test_wrong_types_and_values_are_refused(self, field: str, value: Any) -> None:
        with pytest.raises(EvidenceError):
            ProviderEvidence.from_mapping(mapping(**{field: value}))

    def test_a_fetch_carries_no_event_id(self) -> None:
        """A fetch is not an event; an id on it would forge a webhook in the audit trail."""
        with pytest.raises(EvidenceError, match="event_id"):
            ProviderEvidence.from_mapping(mapping(event_id="evt_1"))

    def test_a_non_mapping_is_refused(self) -> None:
        with pytest.raises(EvidenceError):
            ProviderEvidence.from_mapping("captured")  # type: ignore[arg-type]


class TestDerivedFacts:
    @pytest.mark.parametrize(
        ("status", "state"),
        [
            ("captured", PaymentState.CAPTURED),
            ("authorized", PaymentState.AUTHORIZED),
            ("failed", PaymentState.FAILED),
            ("pending", None),
        ],
    )
    def test_status_maps_to_the_documented_state(
        self, status: str, state: PaymentState | None
    ) -> None:
        evidence = ProviderEvidence.from_mapping(mapping(status=status, provider_status=status))
        assert evidence.payment_state is state
        assert evidence.money_held is (status in ("captured", "authorized"))

    def test_a_refunded_payment_reports_the_refund(self) -> None:
        evidence = ProviderEvidence.from_mapping(
            mapping(provider_status="refunded", amount_refunded_minor=39500)
        )
        assert evidence.status == "captured"
        assert evidence.refund_reported
        assert not ProviderEvidence.from_mapping(mapping()).refund_reported

    def test_record_is_json_safe_primitives_and_names_the_channel(self) -> None:
        record = ProviderEvidence.from_mapping(
            mapping(source="WEBHOOK", event_id="evt_9")
        ).as_record()
        assert record["source"] == "WEBHOOK"
        assert record["channel"] == "VERIFIED_WEBHOOK"
        assert record["event_id"] == "evt_9"
        assert record["created_at"] == "2026-01-01T00:00:00Z"
        for name, value in record.items():
            assert value is None or isinstance(value, str | int), name
            assert not isinstance(value, bool), name

    def test_instances_are_immutable_and_hashable(self) -> None:
        evidence = ProviderEvidence.from_mapping(mapping())
        with pytest.raises(FrozenInstanceError):
            evidence.status = "failed"  # type: ignore[misc]
        assert len({evidence, ProviderEvidence.from_mapping(mapping())}) == 1

    def test_field_names_are_the_adapter_fields_plus_event_id(self) -> None:
        """The contract the cross-package mirror test in payment-adapters enforces."""
        assert ProviderEvidence.field_names() == {
            "source",
            "provider_payment_id",
            "provider_order_id",
            "amount_minor",
            "currency",
            "status",
            "provider_status",
            "captured_at",
            "authorized_at",
            "created_at",
            "amount_refunded_minor",
            "method",
            "error_code",
            "error_reason",
            "raw_digest",
            "http_status",
            "event_id",
        }

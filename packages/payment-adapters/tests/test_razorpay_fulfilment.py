"""The gate between a payment and the goods, specification 11.1, 11.2 and 10.8.

Auto-capture makes ``AUTHORIZED`` and ``CAPTURED`` arrive milliseconds apart, which is
exactly why the tests below exist: the two states are easy to collapse and expensive to
have collapsed.
"""

from __future__ import annotations

import pytest
from payment_adapters.razorpay import CaptureEvidence, may_fulfil, requires_release_not_capture
from transaction_kernel.states import PaymentState


@pytest.mark.parametrize(
    "evidence",
    [
        CaptureEvidence.VERIFIED_WEBHOOK,
        CaptureEvidence.PROVIDER_FETCH,
        CaptureEvidence.OPERATOR_RESOLUTION,
    ],
)
def test_a_verified_capture_may_be_fulfilled(evidence: CaptureEvidence) -> None:
    assert may_fulfil(PaymentState.CAPTURED, evidence)


def test_a_browser_callback_alone_is_not_capture_evidence() -> None:
    """Specification 11.2, item 7. The signature proves a key, not a settlement.

    ``razorpay_signature`` proves whoever produced it holds the API key secret. It does
    not prove the payment settled, and the browser is not a channel the platform
    controls. A redirect is a reason to go and look, never the answer.
    """
    assert not may_fulfil(PaymentState.CAPTURED, CaptureEvidence.BROWSER_CALLBACK)


def test_an_authorization_is_never_fulfilled() -> None:
    """The money has not moved. An authorization is a hold that can still fail."""
    assert not may_fulfil(PaymentState.AUTHORIZED, CaptureEvidence.VERIFIED_WEBHOOK)
    assert not may_fulfil(PaymentState.AUTHORIZED, CaptureEvidence.PROVIDER_FETCH)


def test_a_stale_capture_is_never_fulfilled() -> None:
    """Specification 10.8: that capture belongs to an invalidated version and is owed a
    full refund, not goods."""
    assert not may_fulfil(PaymentState.STALE_CAPTURE, CaptureEvidence.VERIFIED_WEBHOOK)


@pytest.mark.parametrize(
    "state",
    [
        PaymentState.CREATED,
        PaymentState.SUBMITTED,
        PaymentState.FAILED,
        PaymentState.EXPIRED,
        PaymentState.UNKNOWN,
        PaymentState.RECONCILING,
        PaymentState.ESCALATED,
        PaymentState.REFUND_PENDING,
        PaymentState.PARTIALLY_REFUNDED,
        PaymentState.REFUNDED,
        PaymentState.REFUND_UNKNOWN,
        PaymentState.REFUND_FAILED,
        PaymentState.AUTO_REFUND_PENDING,
    ],
)
def test_no_other_state_fulfils_under_any_evidence(state: PaymentState) -> None:
    """Exhaustive: only ``CAPTURED`` opens the gate, and only on a server-side channel."""
    for evidence in CaptureEvidence:
        assert not may_fulfil(state, evidence), (state, evidence)


def test_capture_is_the_only_fulfillable_state() -> None:
    """A guard against a future state being added and silently inheriting the gate."""
    fulfillable = {
        state for state in PaymentState if may_fulfil(state, CaptureEvidence.VERIFIED_WEBHOOK)
    }
    assert fulfillable == {PaymentState.CAPTURED}


# ------------------------------------------------------- invalidated open checkouts


def test_an_authorization_on_an_invalidated_checkout_is_released_not_captured() -> None:
    """Specification 10.8: do not intentionally capture; reconcile and release.

    Capturing would take money for a version that will never be fulfilled and would
    immediately owe a full refund.
    """
    assert requires_release_not_capture(PaymentState.AUTHORIZED, checkout_invalidated=True)


def test_a_valid_checkout_authorization_is_not_released() -> None:
    assert not requires_release_not_capture(PaymentState.AUTHORIZED, checkout_invalidated=False)


def test_a_capture_that_already_happened_is_not_a_release_question() -> None:
    """Money already moved. That is the stale-capture refund path, not this one."""
    assert not requires_release_not_capture(PaymentState.CAPTURED, checkout_invalidated=True)

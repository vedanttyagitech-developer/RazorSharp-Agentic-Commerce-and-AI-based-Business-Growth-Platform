import pytest
from voice_runtime.gateway.agent_client import checkout_guidance
from voice_runtime.tts.currency import spoken_currency
from voice_runtime.tts.guard import SpeechGuard
from voice_runtime.tts.templates import Locale


@pytest.mark.parametrize("text", ["₹57.50", "57.50 INR", "Rs. 57.50", "57.50 rupees"])
def test_inr_has_unambiguous_spoken_units(text):
    assert spoken_currency(text, Locale.EN_IN) == "fifty-seven rupees and fifty paise"


def test_indian_grouping_and_hindi():
    assert "lakh" in spoken_currency("₹1,20,000", Locale.EN_IN)
    assert spoken_currency("₹57.50", Locale.HI_IN) == "सत्तावन रुपये पचास पैसे"


def test_never_relabels_foreign_currency_or_bare_numbers():
    assert spoken_currency("USD 57.50; $20; pack of 2", Locale.EN_IN) == "USD 57.50; $20; pack of 2"


@pytest.mark.parametrize("text", ["It costs 57.50 dollars.", "USD 57.50", "$57.50"])
def test_inr_grounding_does_not_authorize_dollars(text):
    assert SpeechGuard.reason_to_refuse(text, frozenset({5750})) == "unsupported_currency"


def test_existing_manual_order_is_explained_without_claiming_failure():
    text, amounts = checkout_guidance(
        {"state": "AWAITING_PAYMENT", "attempt": {"razorpay_order_id": "order_existing"}},
        "verifying",
    )
    assert "Resume this Razorpay checkout" in text
    assert "do not pay again" in text
    assert not amounts


def test_previous_failed_attempt_does_not_override_fresh_review():
    text, amounts = checkout_guidance(
        {
            "state": "APPROVAL_REQUIRED",
            "attempt": {"state": "FAILED", "version": 1},
            "current_version": 2,
            "approval_card": {"amount_minor": 5750, "currency": "INR", "version": 2},
        },
        "review",
        2,
    )
    assert "57.50 rupees" in text
    assert "failed" not in text
    assert amounts == {5750}


def test_sentence_punctuation_does_not_hide_rupee_amount():
    assert (
        spoken_currency("The price is ₹157.", Locale.EN_IN)
        == "The price is one hundred fifty-seven rupees."
    )

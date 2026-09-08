"""19.10: deterministic transactional speech renders every fact and never calls a model."""

from __future__ import annotations

import inspect
import subprocess
import sys
import uuid

import pytest
from commerce_domain import Money
from transaction_kernel.contracts import AdmissionDecision, CheckoutRef, Delta
from transaction_kernel.recovery import RecoveryCode
from voice_runtime.tts import templates
from voice_runtime.tts.templates import (
    Locale,
    format_money_digits,
    int_to_words_en,
    int_to_words_hi,
    money_to_words,
    render_decision,
    template_ids,
)

CHECKOUT = CheckoutRef(checkout_id=uuid.uuid4(), version=3, content_hash="abc123")


def decision(
    code: RecoveryCode,
    explanation: str = "reason",
    *,
    deltas: tuple[Delta, ...] = (),
    next_version: int | None = None,
    checkout: CheckoutRef | None = CHECKOUT,
) -> AdmissionDecision:
    allowed = code is RecoveryCode.OK
    return AdmissionDecision(
        decision_id=uuid.uuid4(),
        allowed=allowed,
        code=code,
        explanation=explanation,
        checkout=checkout,
        deltas=deltas,
        grant_id=uuid.uuid4() if allowed else None,
        next_version=next_version,
    )


REAPPROVAL_DELTAS = (
    Delta(field_path="total", approved=34000, current=39500, reason="total_changed"),
    Delta(
        field_path="line_items",
        approved="all_available",
        current="item_unavailable",
        reason="availability_changed",
    ),
    Delta(field_path="delivery_slot", approved="9am", current="11am", reason="slot_changed"),
)


def test_reapproval_renders_amount_as_words_and_digits_version_and_every_delta() -> None:
    d = decision(
        RecoveryCode.REAPPROVAL_REQUIRED,
        "merchant_state_changed_since_approval",
        deltas=REAPPROVAL_DELTAS,
        next_version=4,
    )
    r = render_decision(
        d, locale=Locale.EN_IN, amount=Money(39500, "INR"), previous_amount=Money(34000, "INR")
    )
    assert r.deterministic is True
    assert r.template_id == "decision.REAPPROVAL_REQUIRED"
    assert r.template_version == 1
    assert "₹395.00, three hundred ninety-five rupees" in r.text
    assert "₹340.00, three hundred forty rupees" in r.text
    assert "Version 4 is ready" in r.text
    assert "The total changed from ₹340.00" in r.text
    assert "no longer available" in r.text
    assert "delivery slot changed from 9am to 11am" in r.text
    assert r.fields["delta_count"] == "3"
    assert r.fields["delta.2.field_path"] == "delivery_slot"
    assert r.fields["delta.0.reason"] == "total_changed"
    assert r.fields["amount_digits"] == "₹395.00"
    assert r.fields["amount_words"] == "three hundred ninety-five rupees"
    assert r.fields["previous_amount_digits"] == "₹340.00"
    assert r.fields["version"] == "3" and r.fields["next_version"] == "4"
    assert r.fields["reason_key"] == "merchant_state_changed_since_approval"
    assert r.fields["checkout_id"] == str(CHECKOUT.checkout_id)


def test_hindi_render_carries_the_same_structured_amount() -> None:
    d = decision(RecoveryCode.REAPPROVAL_REQUIRED, deltas=REAPPROVAL_DELTAS, next_version=4)
    en = render_decision(d, locale=Locale.EN_IN, amount=Money(39500, "INR"))
    hi = render_decision(d, locale=Locale.HI_IN, amount=Money(39500, "INR"))
    assert en.fields["amount_minor"] == hi.fields["amount_minor"] == "39500"
    assert "₹395.00" in en.text and "₹395.00" in hi.text
    assert "तीन सौ पचानवे रुपये" in hi.text
    assert hi.text.endswith("।")
    assert hi.fields["delta_count"] == "3"
    assert "9am" in hi.text and "11am" in hi.text


@pytest.mark.parametrize("code", list(RecoveryCode))
@pytest.mark.parametrize("locale", list(Locale))
def test_every_decision_type_renders_in_every_locale(code: RecoveryCode, locale: Locale) -> None:
    r = render_decision(decision(code), locale=locale, amount=Money(12550, "INR"))
    assert r.text
    assert r.template_id == f"decision.{code}"
    assert r.locale is locale
    assert "{" not in r.text and "}" not in r.text


def test_reason_key_override_wins_over_code_template() -> None:
    r = render_decision(decision(RecoveryCode.STALE_CHECKOUT, "a_newer_version_exists"))
    assert r.template_id == "decision.a_newer_version_exists"
    assert "newer version" in r.text
    assert "decision.a_newer_version_exists" in template_ids()


def test_absent_amount_never_invents_a_figure() -> None:
    r = render_decision(decision(RecoveryCode.PAYMENT_FAILED))
    assert "₹" not in r.text
    assert "the approved amount" in r.text
    assert "amount_digits" not in r.fields


def test_absent_checkout_says_unknown_version() -> None:
    r = render_decision(decision(RecoveryCode.RESERVATION_EXPIRED, checkout=None))
    assert "version unknown" in r.text


def test_indian_digit_grouping_and_paise() -> None:
    assert format_money_digits(Money(39500050, "INR")) == "₹3,95,000.50"
    assert format_money_digits(Money(123456789012, "INR")) == "₹1,23,45,67,890.12"
    assert format_money_digits(Money(500, "INR")) == "₹5.00"
    assert format_money_digits(Money(-2500, "INR")) == "-₹25.00"
    assert format_money_digits(Money(123456, "USD")) == "USD 1,234.56"
    assert format_money_digits(Money(1234, "JPY")) == "JPY 1,234"


def test_english_indian_number_words() -> None:
    assert int_to_words_en(0) == "zero"
    assert int_to_words_en(1) == "one"
    assert int_to_words_en(100) == "one hundred"
    assert int_to_words_en(1299) == "one thousand two hundred ninety-nine"
    assert int_to_words_en(100000) == "one lakh"
    assert int_to_words_en(10000000) == "one crore"
    assert (
        int_to_words_en(12345678)
        == "one crore twenty-three lakh forty-five thousand six hundred seventy-eight"
    )
    assert money_to_words(Money(39550, "INR"), Locale.EN_IN) == (
        "three hundred ninety-five rupees and fifty paise"
    )
    assert money_to_words(Money(100, "INR"), Locale.EN_IN) == "one rupee"


def test_hindi_number_words() -> None:
    assert int_to_words_hi(0) == "शून्य"
    assert int_to_words_hi(21) == "इक्कीस"
    assert int_to_words_hi(99) == "निन्यानवे"
    assert int_to_words_hi(395) == "तीन सौ पचानवे"
    assert int_to_words_hi(1299) == "एक हज़ार दो सौ निन्यानवे"
    assert int_to_words_hi(250000) == "दो लाख पचास हज़ार"
    assert int_to_words_hi(10000000) == "एक करोड़"
    assert money_to_words(Money(39550, "INR"), Locale.HI_IN) == "तीन सौ पचानवे रुपये पचास पैसे"


def test_spoken_amount_matches_the_approval_card_exactly() -> None:
    amount = Money(129950, "INR")
    r = render_decision(decision(RecoveryCode.OK, "admitted"), amount=amount)
    assert r.fields["amount_digits"] == format_money_digits(amount) == "₹1,299.50"
    assert r.fields["amount_minor"] == str(amount.minor)
    assert "₹1,299.50" in r.text


def test_templates_module_never_touches_a_model_sdk() -> None:
    source = inspect.getsource(templates)
    assert "genai" not in source and "google" not in source and "texttospeech" not in source
    probe = (
        "import sys, voice_runtime.tts.templates as t; "
        "assert not [m for m in sys.modules if m.startswith('google')], 'model SDK imported'"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)  # noqa: S603


# ---- the approval card, read aloud ---------------------------------------------------------


def test_the_consent_reading_names_the_version_and_the_amount_twice() -> None:
    from voice_runtime.tts.templates import render_consent_reading

    r = render_consent_reading(
        checkout_id="01a07169-ead6-7052-99d3-d017e36c0c93",
        version=2,
        content_hash="abc123",
        amount=Money(39500, "INR"),
    )
    assert r.text == (
        "Version 2, ₹395.00, three hundred ninety-five rupees. "
        "Say yes to approve this exact version, or no to decline."
    )
    assert r.deterministic is True
    assert r.template_id == "consent.read_card" and r.template_id in template_ids()
    # The hash is not spoken -- nobody can hear forty-three characters of base64 -- but
    # the audit record of the reading names the bytes it was a reading of.
    assert r.fields["content_hash"] == "abc123"
    assert r.fields["amount_minor"] == "39500"
    assert r.fields["version"] == "2"
    assert r.fields["currency"] == "INR"
    assert "abc123" not in r.text


def test_the_consent_reading_in_hindi_speaks_the_amount_in_hindi() -> None:
    from voice_runtime.tts.templates import render_consent_reading

    r = render_consent_reading(
        checkout_id="x",
        version=2,
        content_hash="h",
        amount=Money(39550, "INR"),
        locale=Locale.HI_IN,
    )
    assert r.text == (
        "संस्करण 2, ₹395.50, तीन सौ पचानवे रुपये पचास पैसे। इसी संस्करण को स्वीकृत करने के लिए "
        "हाँ कहें, या मना करने के लिए नहीं।"
    )
    assert r.locale is Locale.HI_IN


@pytest.mark.parametrize("locale", list(Locale))
def test_the_consent_reading_can_never_be_heard_as_its_own_yes(locale: Locale) -> None:
    """If the reading ever leaked back through the microphone, it must not approve.

    It is not a near-miss, as first assumed: the ask ends in "no" / "नहीं", and a negative
    anywhere wins, so a leaked reading classifies as a DECLINE. That is the safe direction
    -- a decline sends nothing and closes the window -- and it is asserted as such rather
    than papered over with a rule that lets a sentence outrank a no.
    """
    from voice_runtime.consent import classify
    from voice_runtime.tts.templates import render_consent_reading

    r = render_consent_reading(
        checkout_id="x", version=1, content_hash="h", amount=Money(100, "INR"), locale=locale
    )
    assert classify(r.text) == "no"
    assert classify(r.text.split(".")[0].split("।")[0]) == "none", (
        "the amount clause alone is nothing"
    )

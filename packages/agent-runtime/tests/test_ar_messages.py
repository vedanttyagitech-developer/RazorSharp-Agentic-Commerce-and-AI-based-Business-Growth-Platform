"""Behavioural tests for the deterministic template layer.

These assert *properties* of the rendered text, never its wording. The copy in
``rendering/messages.py`` is expected to be rewritten -- richer, warmer, better Hindi --
and every test here must still pass afterwards. So a test says "every delta appears" and
"no amount is present that no tool returned", not "the sentence reads like this".

Two of them are the ones that would catch a genuine regression on the money path:
:func:`test_every_recovery_code_has_text_in_every_language`, which fails the moment
someone adds a ``RecoveryCode`` member without translating it, and
:func:`test_refusal_invents_no_amount`, which proves the renderer copies numbers and
never computes them.
"""

from __future__ import annotations

import uuid

import pytest
from agent_runtime.grounding import GroundingLedger, extract_amounts_minor, verify_reply
from agent_runtime.language import Language
from agent_runtime.rendering import (
    REASON_TEXT,
    RECOVERY_TEXT,
    display_amount,
    display_minor,
    reason_text,
    recovery_text,
    render_decision,
    render_denial,
    render_fallback,
    render_unverified,
)
from agent_runtime.rendering.money import display_delta_value, is_money_field
from commerce_domain import Money
from transaction_kernel import AdmissionDecision, CheckoutRef, Delta, RecoveryCode

LANGUAGES = tuple(Language)

_CHECKOUT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000c0")


def _ref(version: int = 1) -> CheckoutRef:
    return CheckoutRef(checkout_id=_CHECKOUT_ID, version=version, content_hash=f"hash-v{version}")


def _refusal(
    code: RecoveryCode = RecoveryCode.REAPPROVAL_REQUIRED,
    *,
    deltas: tuple[Delta, ...] = (),
    version: int = 1,
    next_version: int | None = 2,
) -> AdmissionDecision:
    """A denied decision shaped exactly as the kernel emits one."""
    return AdmissionDecision(
        decision_id=uuid.uuid4(),
        allowed=False,
        code=code,
        explanation="material_change",
        checkout=_ref(version),
        deltas=deltas,
        next_version=next_version,
    )


def _admission() -> AdmissionDecision:
    """An allowed decision. It names a grant because every allowed decision must."""
    return AdmissionDecision(
        decision_id=uuid.uuid4(),
        allowed=True,
        code=RecoveryCode.OK,
        explanation="admitted",
        checkout=_ref(),
        grant_id=uuid.uuid4(),
        payment_attempt_id=uuid.uuid4(),
    )


# --------------------------------------------------------------------- completeness


def test_every_recovery_code_has_text_in_every_language() -> None:
    """A new RecoveryCode must not reach a buyer as its own enum name.

    This is the test the specification's closed recovery contract (6.7) is worth having:
    the kernel is free to add a failure mode, and the moment it does, this fails loudly
    here instead of quietly showing somebody ``AUTHORITY_INSUFFICIENT``.
    """
    missing: list[str] = []
    for code in RecoveryCode:
        entry = RECOVERY_TEXT.get(code)
        if entry is None:
            missing.append(f"{code.value}: no entry at all")
            continue
        for language in LANGUAGES:
            text = entry.get(language)
            if not text or not text.strip():
                missing.append(f"{code.value}: no {language.value} text")
    assert not missing, "RECOVERY_TEXT is incomplete:\n  " + "\n  ".join(missing)


@pytest.mark.parametrize("code", list(RecoveryCode))
@pytest.mark.parametrize("language", LANGUAGES)
def test_recovery_text_never_leaks_the_enum_name(code: RecoveryCode, language: Language) -> None:
    """``recovery_text`` returns a sentence, not the code. The code is for the machine."""
    text = recovery_text(code, language)
    assert text.strip()
    assert code.value not in text


def test_hindi_is_devanagari_and_hinglish_is_not() -> None:
    """The three languages must actually differ in script, or the locale work is theatre."""
    for code in RecoveryCode:
        hindi = recovery_text(code, Language.HI)
        hinglish = recovery_text(code, Language.HI_LATN)
        assert any("ऀ" <= ch <= "ॿ" for ch in hindi), code
        assert not any("ऀ" <= ch <= "ॿ" for ch in hinglish), code
        assert hindi != hinglish


def test_reason_text_accepts_both_kernel_and_service_spellings() -> None:
    """The kernel writes ``total_changed``; the service layer writes ``TOTAL_CHANGED``."""
    for language in LANGUAGES:
        assert reason_text("total_changed", language) == reason_text("TOTAL_CHANGED", language)
        assert reason_text("total_changed", language) != "total_changed"
    assert "TOTAL_CHANGED" in REASON_TEXT
    assert "AVAILABILITY_CHANGED" in REASON_TEXT


def test_unknown_reason_key_degrades_to_readable_text() -> None:
    """An untranslated reason still reaches the buyer beside the numbers, not as a crash."""
    assert reason_text("SOME_NEW_KERNEL_REASON", Language.EN) == "some new kernel reason"


# -------------------------------------------------------------------- refusal render


def test_refusal_renders_every_delta_with_both_values() -> None:
    """Specification 6.3: on a refusal the buyer sees *every* delta, not a summary."""
    deltas = (
        Delta("lines[AMUL-DAIRY-001].unit_price_minor", 2800, 3400, "PRICE_CHANGED"),
        Delta("lines[AASH-STPL-002].quantity", 3, 1, "QUANTITY_REDUCED"),
        Delta("total_minor", 34000, 39500, "TOTAL_CHANGED"),
    )
    for language in LANGUAGES:
        text = render_decision(_refusal(deltas=deltas), language)
        for delta in deltas:
            assert delta.field_path in text, (language, delta.field_path)
            assert display_delta_value(delta.field_path, delta.approved, "INR") in text
            assert display_delta_value(delta.field_path, delta.current, "INR") in text
            assert reason_text(delta.reason, language) in text


def test_refusal_says_version_n_is_invalidated_and_n_plus_one_needs_approval() -> None:
    """The demonstration's hero sentence. Both version numbers must be in the text."""
    decision = _refusal(
        deltas=(Delta("total_minor", 34000, 39500, "TOTAL_CHANGED"),),
        version=7,
        next_version=8,
    )
    for language in LANGUAGES:
        text = render_decision(decision, language)
        # Version numbers chosen so they cannot be confused with the money in the delta,
        # and so a template that hard-coded "1" and "2" would fail here.
        assert "7" in text, language
        assert "8" in text, language
        assert text.index("7") < text.index("8"), (
            f"the invalidated version must be named before its successor in {language.value}"
        )


def test_refusal_derives_the_successor_when_the_kernel_did_not_name_one() -> None:
    """``next_version`` is optional on the wire; reapproval always has a successor."""
    text = render_decision(_refusal(version=4, next_version=None), Language.EN)
    assert "4" in text and "5" in text


def test_refusal_invents_no_amount() -> None:
    """Every rupee figure in the text must be a number a tool result carried.

    This is the arithmetic ban made mechanical: the renderer is allowed to *format*
    ``delta.approved`` and ``delta.current`` and nothing else. If someone ever adds "a
    difference of ₹55" to this file, the grounding post-check would strip it in
    production; here it fails the build instead.
    """
    deltas = (
        Delta("lines[AMUL-DAIRY-001].unit_price_minor", 2800, 3400, "PRICE_CHANGED"),
        Delta("total_minor", 34000, 39500, "TOTAL_CHANGED"),
    )
    permitted = {2800, 3400, 34000, 39500}
    for language in LANGUAGES:
        text = render_decision(_refusal(deltas=deltas), language)
        assert set(extract_amounts_minor(text)) <= permitted, language


def test_refusal_renders_bare_kernel_money_paths_as_money() -> None:
    """``transaction_kernel.admission`` writes ``total``, not ``total_minor``.

    A renderer that keyed only on the ``_minor`` suffix would show a buyer ``39500`` where
    the sentence promised rupees. This is the exact shape the real kernel emits.
    """
    decision = _refusal(deltas=(Delta("total", 34000, 39500, "total_changed"),))
    text = render_decision(decision, Language.EN)
    assert display_minor(34000, "INR") in text
    assert display_minor(39500, "INR") in text


def test_refusal_never_claims_money_moved() -> None:
    """A refusal is not a charge, and the text must not read as one to the post-check."""
    decision = _refusal(deltas=(Delta("total_minor", 34000, 39500, "TOTAL_CHANGED"),))
    for language in LANGUAGES:
        check = verify_reply(render_decision(decision, language), GroundingLedger())
        assert not check.success_claim_removed, language


def test_admission_is_rendered_as_permission_not_as_payment() -> None:
    """Admission issues an Execution Grant. It is not a completed payment, ever."""
    for language in LANGUAGES:
        text = render_decision(_admission(), language)
        assert text.strip()
        check = verify_reply(text, GroundingLedger())
        assert not check.success_claim_removed, language
        assert not check.rewritten, language


# ------------------------------------------------------------------ unverified return


@pytest.mark.parametrize("language", LANGUAGES)
def test_unverified_return_does_not_say_paid(language: Language) -> None:
    """ADR 0003 D8: the browser's return is not capture evidence, so this cannot claim it."""
    text = render_unverified(language).casefold()
    for forbidden in ("paid", "successful", "succeeded", "safal", "सफल"):
        assert forbidden not in text, (language, forbidden)


@pytest.mark.parametrize("language", LANGUAGES)
def test_unverified_return_survives_the_success_post_check(language: Language) -> None:
    """The strongest form of the same rule: the hallucination guard sees no claim here."""
    ledger = GroundingLedger()
    assert not ledger.payment_captured()
    check = verify_reply(render_unverified(language), ledger)
    assert not check.success_claim_removed
    assert not check.rewritten


def test_unverified_return_formats_the_amount_it_was_given() -> None:
    """The amount is a tool fact passed in, formatted here; never parsed out of prose."""
    text = render_unverified(Language.EN, amount_minor=39500)
    assert display_amount(Money(39500, "INR")) in text
    assert set(extract_amounts_minor(text)) == {39500}


# --------------------------------------------------------------------- other renders


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "reason_key",
    ["capability_missing", "tool_budget_exhausted", "tool_failed", "injected_instruction", "???"],
)
def test_denial_text_is_non_empty_for_every_reason(reason_key: str, language: Language) -> None:
    """An unknown reason key still yields a refusal sentence, never an empty string."""
    assert render_denial(reason_key, language).strip()


def test_denial_names_the_tool_only_when_asked() -> None:
    plain = render_denial("capability_missing", Language.EN)
    named = render_denial("capability_missing", Language.EN, tool="checkout_submit_approved")
    assert "checkout_submit_approved" not in plain
    assert named.startswith(plain)
    assert "checkout_submit_approved" in named


@pytest.mark.parametrize("language", LANGUAGES)
def test_fallback_is_non_empty_and_makes_no_money_claim(language: Language) -> None:
    text = render_fallback(language)
    assert text.strip()
    assert extract_amounts_minor(text) == ()


# ------------------------------------------------------------------- money formatting


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (0, "₹0.00"),
        (5, "₹0.05"),
        (2800, "₹28.00"),
        (39500, "₹395.00"),
        (123456789, "₹12,34,567.89"),
        (-2800, "-₹28.00"),
    ],
)
def test_display_minor_is_exact_and_grouped_the_indian_way(minor: int, expected: str) -> None:
    """Specification 19.10: locale-correct grouping and paise, from integers only."""
    assert display_minor(minor, "INR") == expected


def test_display_minor_round_trips_through_the_post_check_parser() -> None:
    """What the templates print, the grounding post-check must be able to read back.

    If these two ever disagree -- grouping the formatter emits that the parser does not
    accept -- every correctly-rendered total would be stripped from the reply as
    ungrounded, and the buyer would see a sentence with the number missing.
    """
    for minor in (0, 5, 2800, 39500, 100000, 123456789):
        assert extract_amounts_minor(display_minor(minor, "INR")) == (minor,)


def test_money_field_detection_covers_both_delta_vocabularies() -> None:
    assert is_money_field("total_minor")
    assert is_money_field("total")
    assert is_money_field("lines[AMUL-DAIRY-001].unit_price_minor")
    assert not is_money_field("lines[AMUL-DAIRY-001].quantity")
    assert not is_money_field("free_delivery_applied")


def test_delta_values_that_are_not_money_are_shown_verbatim() -> None:
    """``True`` is an ``int`` in Python; ``₹0.01`` is not what an availability flag means."""
    assert display_delta_value("free_delivery_applied", True, "INR") == "True"
    assert display_delta_value("lines[X].quantity", 3, "INR") == "3"
    assert display_delta_value("total_minor", None, "INR") == "—"
    assert display_delta_value("line_items", "item_unavailable", "INR") == "item_unavailable"

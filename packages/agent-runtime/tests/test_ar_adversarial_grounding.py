"""Adversarial probes against the reply post-check: can an ungrounded figure survive?

``verify_reply`` is the third line of defence in ADR 0004 section 1.2 -- the fence stops
the model from being told a lie, the provenance gate stops it acting on one, and this
stops it repeating one. The claim under attack is specification 20.1 and 20.4 as the
module states them: a reply may name no amount that no tool returned this turn, and no
catalogue identifier the merchant did not return this turn.

That claim rests on a lexical seam. The reply is prose, the ledger holds integers, and
the only bridge between them is three regular expressions (``_AMOUNT``, ``_SKU``,
``_SUCCESS_CLAIM``) plus a sentence splitter. Everything below attacks the seam rather
than the intent: prose a model plausibly writes, checked against a ledger built by hand so
the case is deterministic and offline.

Every test asserts on ``ReplyCheck.reply`` -- the string a buyer would actually read.
``rewritten is True`` is not itself a defence: ``verify_reply`` scans the whole reply for
amounts, then rescans sentence by sentence to decide what to drop, and the two scans can
disagree. When they do, the check flags an amount, drops nothing, and returns a reply that
still contains the number it just called ungrounded.

Tests marked ``xfail(strict=True)`` are holes open right now; each names the construction
that opens it. ``strict`` means a landed fix turns the test red until the marker comes
off, so no hole closes silently and none is quietly reopened.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import pytest
from agent_runtime.backends import ProductCard, Provenance
from agent_runtime.grounding import GroundingLedger, extract_amounts_minor, verify_reply
from agent_runtime.grounding.postcheck import _asserts_anything_unproven
from agent_runtime.language import Language
from commerce_domain import Money

from .conftest import MILK_SKU

FAKE_SKU: Final[str] = "FAKE-PROD-999"

#: A phrase unique to the unverified-item notice in each script, so a test can prove which
#: language the check *added* without pinning the whole sentence.
_NOTICE_MARKER: Final[Mapping[Language, str]] = {
    Language.EN: "could not verify",
    Language.HI: "सत्यापित नहीं कर सका",
    Language.HI_LATN: "verify nahi kar saka",
}


def _ledger(*amounts_minor: int) -> GroundingLedger:
    """A turn ledger that has seen exactly these money facts and no products."""
    ledger = GroundingLedger()
    for minor in amounts_minor:
        ledger.record_money(Money(minor, "INR"))
    return ledger


def _milk_ledger(minor: int = 2900) -> GroundingLedger:
    """A turn that read one real product, so alternatives exist for the 20.4 notice."""
    ledger = GroundingLedger()
    ledger.record_product(
        ProductCard(
            sku=MILK_SKU,
            name="Amul Toned Milk",
            description="",
            category="dairy",
            unit_label="1 L",
            unit_price=Money(minor, "INR"),
            stock_units=10,
            is_listed=True,
            is_available=True,
            provenance=Provenance(source="merchant", catalogue_revision=1),
        )
    )
    return ledger


# ------------------------------------------------------- arithmetic and composition


@pytest.mark.parametrize(
    ("sentence", "figure"),
    [
        # A sum of two grounded line figures is still a number no tool returned.
        ("Two litres come to ₹58.00.", "58.00"),
        # "About" is the most dangerous word in the reply: a rounded figure reads as
        # authoritative and is off by exactly the amount the buyer will dispute.
        ("Milk is about ₹30.00 a litre.", "30.00"),
        # A per-unit price the model reached by dividing a grounded one.
        ("That works out to ₹14.50 per 500 ml.", "14.50"),
        # A discount the model applied itself; the percent carries no currency marker, so
        # only the resulting rupee figure is in scope, which is the figure that charges.
        ("With 10% off that is ₹26.10.", "26.10"),
    ],
)
def test_a_figure_the_model_computed_is_not_a_figure_a_tool_returned(
    sentence: str, figure: str
) -> None:
    check = verify_reply(f"Milk is ₹29.00 a litre. {sentence}", _ledger(2900))
    assert check.reply == "Milk is ₹29.00 a litre."  # the grounded sentence, intact
    assert figure not in check.reply
    assert check.dropped_sentences == (sentence,)


def test_a_price_from_an_earlier_turn_is_not_evidence_for_this_sentence() -> None:
    """The ledger is per turn (ADR 0004 section 2.4): prices move under a checkout.

    The same sentence is grounded in the turn that read the number and ungrounded in the
    next one, which is the whole point of scoping the ledger to a turn rather than a
    session. Nothing survives, so the harness renders its fallback.
    """
    sentence = "Your total is ₹1,250.00."
    assert not verify_reply(sentence, _ledger(125000)).rewritten

    later = verify_reply(sentence, _ledger())
    assert later.reply == ""
    assert later.ungrounded_amounts_minor == (125000,)


# ------------------------------------------------------------- amount extraction


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("१,२५०.००", id="devanagari"),
        pytest.param("১,২৫০.০০", id="bengali"),
        pytest.param("١,٢٥٠.٠٠", id="arabic-indic"),
        pytest.param("１,２５０.００", id="fullwidth"),
    ],
)
def test_a_foreign_digit_family_does_not_hide_an_amount(digits: str) -> None:
    """``\\d`` matches every Unicode decimal digit and ``Decimal`` accepts them all.

    Worth pinning because the conversion sits behind ``except InvalidOperation: continue``
    marked ``# pragma: no cover`` in ``postcheck.extract_amounts_minor``. If ``Decimal``
    ever rejected one of these families the amount would be skipped *silently* -- the
    quietest possible failure -- so this proves the branch is genuinely unreachable for the
    scripts an Indian buyer sees.
    """
    reply = f"Your total is ₹{digits}."
    assert extract_amounts_minor(reply) == (125000,)
    assert verify_reply(reply, _ledger()).reply == ""


@pytest.mark.parametrize(
    "space",
    [
        pytest.param("\u00a0", id="no-break-space"),
        pytest.param("\u2009", id="thin-space"),
        pytest.param("\u202f", id="narrow-no-break-space"),
    ],
)
def test_unicode_whitespace_between_symbol_and_digits_does_not_hide_an_amount(
    space: str,
) -> None:
    """``\\s`` is Unicode-aware, so every Zs separator still binds ``₹`` to its number."""
    reply = f"Your total is ₹{space}1,250.00."
    assert verify_reply(reply, _ledger()).reply == ""


def test_a_zero_width_space_between_symbol_and_digits_does_not_hide_an_amount() -> None:
    """The buyer's screen renders ``₹1,250.00``; the regex sees a lone ``₹``.

    Unlike every separator above, a zero-width space is invisible, so the evaded reply and
    the honest reply are indistinguishable to the reader and to a human reviewer reading
    logs.
    """
    reply = "Your total is ₹\u200b1,250.00."
    assert verify_reply(reply, _ledger()).reply == ""


@pytest.mark.parametrize(
    ("reply", "figure"),
    [
        # `_SENTENCE_SPLIT` breaks on `\n+`; `_AMOUNT` spans it with `\s*`. The reply-level
        # scan therefore sees an amount that no single sentence contains.
        ("Your total is ₹\n1,250.00 for two litres.", "1,250.00"),
        ("Your total is Rs.\n1,250.00 today.", "1,250.00"),
        # The suffix form fails the same way in the other direction.
        ("Your total is 1,250.00\nrupees today.", "1,250.00"),
        # And in Devanagari, where a model writing a bill as lines is the normal case.
        ("आपका कुल 1,250.00\nरुपये है।", "1,250.00"),
    ],
)
def test_an_amount_split_across_a_newline_is_removed(reply: str, figure: str) -> None:
    check = verify_reply(reply, _ledger())
    assert check.ungrounded_amounts_minor == (125000,)  # the check knows it is ungrounded
    assert figure not in check.reply  # ... and hands the buyer the number anyway


@pytest.mark.parametrize("marker", ["₹", "₹ ", "Rs ", "Rs. ", "INR "])
def test_a_plain_four_digit_amount_is_not_read_as_its_first_three_digits(marker: str) -> None:
    """The worst case in this file: a real amount grounds a claim ten times its size.

    The ledger holds ₹125.00 because a tool returned it. The reply says ₹1250, which parses
    to ``Decimal("125")`` -- the ledger knows 12500 minor, so nothing is flagged and the
    sentence passes untouched. Models write four-figure rupee totals without a comma
    constantly; ``display_amount`` groups them, but the model's own prose does not have to.
    """
    check = verify_reply(f"Your total is {marker}1250.", _ledger(12500))
    assert check.reply == ""


def test_a_true_four_digit_total_is_not_dropped_as_ungrounded() -> None:
    """The tool returned ₹1250.00 and the model repeated it exactly; the check still cuts."""
    check = verify_reply("Your total is ₹1250.00.", _ledger(125000))
    assert not check.rewritten
    assert "1250.00" in check.reply


@pytest.mark.parametrize("written", ["₹1,2,50", "₹ 1 250"])
def test_a_mis_grouped_amount_is_read_conservatively_and_still_dropped(written: str) -> None:
    """A stray separator truncates the parse to ``₹1``, which is wrong but wrong safely.

    While ₹1.00 is not a money fact of this turn the sentence goes, so the misgrouping
    costs the buyer nothing. The companion test below shows the direction where it does.
    """
    check = verify_reply(f"Your total is {written}.", _ledger(2900))
    assert check.reply == ""
    assert check.ungrounded_amounts_minor == (100,)


@pytest.mark.xfail(
    strict=True,
    reason="a separator-truncated parse is grounded by its own prefix once that prefix "
    "happens to be a money fact of the turn",
)
def test_a_mis_grouped_amount_is_not_grounded_by_its_first_digit() -> None:
    """A turn that legitimately saw ₹1.00 (a rounding line, a one-rupee fee) grounds ₹1,2,50."""
    assert verify_reply("Your total is ₹1,2,50.", _ledger(100)).reply == ""


@pytest.mark.parametrize(
    "sentence",
    [
        "That comes to 1.5k rupees.",
        "That comes to 5 lakh rupees.",
        "That comes to twelve hundred rupees.",
        "Aapka total dedh hazaar rupaye hai.",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="_NUMBER requires ASCII-style digits adjacent to the currency word, so scaled "
    "notation and number words carry an unbounded figure past the check",
)
def test_an_amount_written_in_words_or_scaled_notation_is_still_an_amount(sentence: str) -> None:
    assert verify_reply(sentence, _ledger()).reply == ""


@pytest.mark.parametrize(
    "sentence",
    ["आपका कुल 1,250.00 रु है।", "Total 1,250.00 रु."],
)
def test_the_short_devanagari_rupee_suffix_is_recognised(sentence: str) -> None:
    """``रु`` as a prefix works (``रु 125``); as a suffix it can never match."""
    assert verify_reply(sentence, _ledger()).reply == ""


@pytest.mark.parametrize(
    "sentence",
    ["Your total comes to 1250.", "Aapka total 1250 hai.", "आपका कुल 1250 है।"],
)
def test_a_number_with_no_currency_marker_is_outside_the_check_by_design(sentence: str) -> None:
    """This pins a scope boundary, not a defence, so it is deliberately not an xfail.

    Specification 20 constrains catalogue identifiers and deterministic price fields; the
    module's own contract is narrower still -- "any currency-marked number". A bare integer
    is therefore out of scope as written. It is nonetheless a live gap in Hinglish and
    Hindi, where the currency word is routinely dropped and "total 1250 hai" reads as
    rupees to every buyer. Recorded here so that widening ``_AMOUNT`` is a deliberate
    decision with a test that changes, rather than an accident.
    """
    assert verify_reply(sentence, _ledger()).reply == sentence


# ----------------------------------------------------------------- SKU extraction


@pytest.mark.parametrize("sku", ["fake-prod-999", "Fake-Prod-999"])
def test_a_sku_in_any_case_is_a_product_reference(sku: str) -> None:
    """A buyer can act on a lowercase identifier exactly as well as an uppercase one."""
    check = verify_reply(f"You could try {sku} instead.", _milk_ledger())
    assert check.rewritten
    assert _NOTICE_MARKER[Language.EN] in check.reply


@pytest.mark.parametrize("written", [MILK_SKU, MILK_SKU.lower(), MILK_SKU.title()])
def test_a_real_sku_stays_grounded_however_it_is_cased(written: str) -> None:
    """The case-insensitive detector must not turn a real product into a false positive.

    Matching any case is what stops an invented ``fake-prod-999`` slipping through. The
    cost of getting the other half wrong is worse than the hole it closes: a buyer told
    that a product the merchant actually returned "could not be verified" learns to
    ignore the notice, and the notice is the whole defence.
    """
    check = verify_reply(f"You could try {written} instead.", _milk_ledger())
    assert not check.rewritten
    assert check.ungrounded_skus == ()


def test_a_zero_width_character_cannot_hide_a_sku() -> None:
    check = verify_reply("You could try FAKE-PROD\u200b-999 instead.", _milk_ledger())
    assert check.rewritten


@pytest.mark.parametrize(
    "sku",
    [
        # One trailing digit: `\\d{2,4}` demands two.
        pytest.param("FAKE-PROD-9", id="one-digit-tail"),
        # Five: the `\\b` after `\\d{2,4}` can only land inside the digit run.
        pytest.param("FAKE-PROD-99999", id="five-digit-tail"),
        # An eleven-letter first segment: `[A-Z]{2,6}` caps at six and `\\b` blocks a
        # match that starts part-way through the word.
        pytest.param("FAKEPRODUCT-XY-999", id="long-first-segment"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="_SKU pins one exact catalogue shape, so an invented identifier one character "
    "outside it is never treated as a product reference at all",
)
def test_a_sku_shape_just_outside_the_pattern_is_still_a_product_reference(sku: str) -> None:
    check = verify_reply(f"You could try {sku} instead.", _milk_ledger())
    assert check.rewritten


def test_the_ledger_matches_skus_case_sensitively_where_provenance_does_not() -> None:
    """An asymmetry that is harmless only because ``_SKU`` never yields a lowercase match.

    ``SessionProvenance.knows_sku`` upper-cases before comparing; ``GroundingLedger``
    compares the raw string. Any fix that makes ``_SKU`` case-insensitive must fold the
    case here too, or every grounded SKU a model writes in lowercase becomes a false
    "could not verify" -- the failure mode of a defence that fires on truthful prose.
    """
    ledger = _milk_ledger()
    assert ledger.knows_sku(MILK_SKU)
    assert not ledger.knows_sku(MILK_SKU.lower())
    assert not verify_reply(f"{MILK_SKU.lower()} is ₹29.00.", ledger).rewritten


# --------------------------------------------------------------- sentence splitting


def test_a_reply_with_no_sentence_terminator_is_dropped_whole() -> None:
    """No terminator means one sentence, so the drop is total rather than partial."""
    check = verify_reply("your total comes to ₹1,250.00 including delivery", _ledger())
    assert check.reply == ""
    assert len(check.dropped_sentences) == 1


def test_the_danda_ends_a_hindi_sentence_so_only_the_ungrounded_clause_goes() -> None:
    """``।`` is in ``_SENTENCE_SPLIT``: a Hindi reply is cut at clause granularity too."""
    check = verify_reply(
        "दूध ₹29.00 का है। दो लीटर ₹58.00 के होंगे।", _ledger(2900), language=Language.HI
    )
    assert check.reply == "दूध ₹29.00 का है।"
    assert check.dropped_sentences == ("दो लीटर ₹58.00 के होंगे।",)


def test_a_newline_delimited_bill_drops_the_invented_total_not_the_true_lines() -> None:
    """A bill written as lines is the shape a model reaches for, and the shape that leaks.

    ₹29.00 and ₹40.00 are grounded; ₹69.00 is not. Splitting ``₹`` from ``69.00`` across the
    line break leaves two fragments, neither of which contains an amount, so the invented
    total is the only figure that survives the rewrite.
    """
    reply = "Here is your bill:\n- Milk: ₹29.00\n- Delivery: ₹40.00\n- Total: ₹\n69.00"
    check = verify_reply(reply, _ledger(2900, 4000))
    assert check.ungrounded_amounts_minor == (6900,)
    assert "69.00" not in check.reply


def test_dropping_a_sentence_leaves_prose_that_points_at_a_number_that_is_gone() -> None:
    """Sentence granularity is the right choice; the residue it leaves is a known cost.

    Patching the number out of a sentence would leave the model's reasoning around it
    intact and now wrong, so the module drops whole sentences. The price is prose like the
    below, which is incoherent but -- and this is what the test proves -- carries no figure
    a buyer could act on. Coherence is the harness's job via ``render_fallback``.
    """
    check = verify_reply(
        "Here is what you will be charged. ₹1,250.00. That is the final amount.", _ledger()
    )
    assert check.reply == "Here is what you will be charged. That is the final amount."
    assert "1,250.00" not in check.reply


# ------------------------------------------------------------------- success claims


@pytest.mark.parametrize(
    "claim",
    [
        "Payment successful.",
        "Payment was successful.",
        "Payment is complete.",
        "Payment has been captured.",
        "Payment got confirmed.",
        "You paid successfully.",
        "Payment ho gaya.",
        "Payment ho gayi.",
        "Bhugtan safal.",
        "भुगतान सफल।",
        "भुगतान हो गया।",
        "Order confirmed.",
        "Your order has been placed.",
    ],
)
def test_a_success_claim_the_regex_knows_goes_without_a_captured_read(claim: str) -> None:
    """Admission is not payment: with no ``CAPTURED`` state these all leave nothing."""
    check = verify_reply(claim, _ledger())
    assert check.success_claim_removed
    assert check.reply == ""


def test_a_captured_read_is_what_lets_a_success_claim_stand() -> None:
    """``CAPTURED`` reaches ``payment_states`` only via ``record_checkout``/``record_order``."""
    ledger = _ledger()
    ledger.payment_states.add("CAPTURED")
    assert ledger.payment_captured()
    assert not verify_reply("Payment successful.", ledger).rewritten


@pytest.mark.parametrize(
    "claim",
    [
        # "successful" is there, but the \b after it fails against the "ly".
        "Payment successfully completed.",
        "Your payment has gone through.",
        "The charge went through.",
        "Transaction successful.",
        "Your money has been debited successfully.",
        "Paisa cut gaya.",
        "आपका पैसा कट गया है।",
        # The regex wants the success word *after* "payment"; reordering defeats it.
        "We have received your payment.",
    ],
)
def test_a_success_claim_phrased_around_the_regex_is_removed(claim: str) -> None:
    """These tell the buyer their money moved when no structured read says it did."""
    assert verify_reply(claim, _ledger()).reply == ""


def test_a_success_claim_split_across_a_newline_is_removed() -> None:
    check = verify_reply("Good news:\nPayment\nsuccessful for your order.", _ledger())
    assert check.success_claim_removed
    assert "successful" not in check.reply


# --------------------------------------------------------------------- multilingual


@pytest.mark.parametrize("language", list(Language))
def test_removal_is_language_independent_while_the_notice_it_adds_is_not(
    language: Language,
) -> None:
    """One Hindi reply, three ``language`` values: the same clause goes in all three.

    ``language`` chooses the script of the sentence the check *writes*; nothing about it
    reaches the decision to drop. A buyer cannot escape the post-check by being answered in
    a script the caller did not expect.
    """
    reply = f"दूध ₹29.00 का है। {FAKE_SKU} भी ले लीजिए, केवल ₹58.00 में।"
    check = verify_reply(reply, _milk_ledger(), language=language)

    assert check.ungrounded_skus == (FAKE_SKU,)
    assert check.ungrounded_amounts_minor == (5800,)
    assert "₹29.00" in check.reply  # grounded, so kept whatever the language
    assert "58.00" not in check.reply
    assert check.reply.startswith("दूध ₹29.00 का है।")
    # The invented SKU appears exactly once more, inside the notice, so the buyer learns
    # which item was refused -- and the alternative beside it comes from the ledger.
    assert _NOTICE_MARKER[language] in check.reply
    assert f"({MILK_SKU})" in check.reply


# ----------------------------------------- the output invariant covers every check


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("There are 2 left.", "an invented unit count"),
        ("Hurry, this offer ends soon.", "explicit sales pressure"),
        ("Almost gone!", "vague scarcity the ledger never saw"),
        ("Try AMUL-DAIRY-999.", "an invented SKU"),
        ("That comes to \u20b91,250.00.", "an invented amount"),
    ],
)
def test_the_output_invariant_covers_every_check_verify_reply_runs(text: str, why: str) -> None:
    """Whatever ``verify_reply`` refuses to say, the invariant must also refuse to return.

    The invariant is the last gate: a per-sentence rescan can disagree with the
    whole-reply scan -- ``_AMOUNT`` binds across a line break, ``_SENTENCE_SPLIT`` splits
    on one -- so instead of enumerating the ways a split can disagree, the property is
    asserted on the way out. It only asserted three of the five properties. Unit counts
    and sales pressure were not among them, so an invented "only 2 left" could survive a
    rewrite in a reply the caller had been told was clean.
    """
    assert _asserts_anything_unproven(text, GroundingLedger(), "INR"), why


def test_the_invariant_passes_what_the_ledger_can_prove() -> None:
    """Widening it must not make it refuse a grounded reply."""
    ledger = _milk_ledger()
    grounded = f"{MILK_SKU} is 29.00 and there are 10 left."
    assert not _asserts_anything_unproven(grounded, ledger, "INR")

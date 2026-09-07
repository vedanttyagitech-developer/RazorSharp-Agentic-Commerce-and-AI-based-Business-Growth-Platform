"""Adversarial: a defence that only holds in one spelling of a word is not a defence.

The grounding rules are the *prevention* half of specification 20: before the model
speaks, a rule reads the buyer's message and names the one tool the turn must start
from. "Is milk still ₹28?" must begin from ``basket_get``, so the answer begins from a
tool result instead of from the model's memory of last turn.

That whole layer is a lexicon match, and a lexicon match is only as good as its notion
of "the same word". This module attacks that notion.

WHAT WAS BROKEN, AND WHY IT MATTERED
------------------------------------
``matches_any`` casefolded but did not normalise. Two consequences, both found by the
tests below and both now fixed by :func:`~agent_runtime.core.grounding_rules.fold`:

* A Hindi IME emits the **precomposed** nukta letters -- ``क़`` is U+0958, one code point.
  The lexicon in ``grounding_rules.py`` is written with the **decomposed** pair, ``क``
  followed by U+093C, which is what NFKC canonicalises to and what a copy-paste from most
  web text produces. They render identically and are different strings. So
  "क़ीमत क्या है?" -- "what is the price?", typed on a Hindi keyboard -- matched no term,
  forced no read, and left the model free to answer a price question from memory.
* Fullwidth Latin (``ｐｒｉｃｅ``) folded to nothing recognisable for the same reason.

This is the failure mode the package exists to prevent, reached by changing the keyboard
rather than by defeating any check. It degrades defence in depth rather than breaking it
outright -- the reply post-check still refuses an amount no tool returned -- but the
prevention layer was silently off for a whole class of buyer, which is exactly the shape
of bug that survives a demo and fails in the market.

The equivalent hole on the API side (``_tokens`` in ``commerce_api.services.agent_service``,
where it let a consent verb escape being recorded as a denial) is pinned in
``test_capi_agent_adversarial.py``.
"""

from __future__ import annotations

import unicodedata

import pytest
from agent_runtime.core import (
    CHECKOUT_RULES,
    DEFAULT_LEXICON,
    SHOPPING_RULES,
    SUPPORT_RULES,
    GroundingState,
    find_token,
    forced_tool,
    matches_any,
    matches_terms_and_cues,
)
from agent_runtime.core.grounding_rules import fold, normalize

BASKET = "b-adversarial"

#: The precomposed Devanagari letters a Hindi IME emits, beside the decomposed spelling
#: the lexicon is written with. Both render identically to a reader.
QA_PRECOMPOSED = "क़"  # क़  -- one code point
QA_DECOMPOSED = "क़"  # क + nukta
ZA_PRECOMPOSED = "ज़"  # ज़
ZA_DECOMPOSED = "ज़"  # ज + nukta


def _fullwidth(text: str) -> str:
    """ASCII rewritten in the fullwidth block, which is what an IME candidate list gives."""
    return text.translate({code: code + 0xFEE0 for code in range(0x21, 0x7F)})


# --------------------------------------------------------- the two spellings are one


def test_the_two_nukta_spellings_are_different_strings() -> None:
    """The premise of the attack, asserted so the tests below cannot become vacuous.

    If a future Python or a future normalisation table made these equal, every test in
    this module would still pass while testing nothing. Pinning the inequality here means
    that change breaks this test loudly instead of hollowing out the rest.
    """
    assert QA_PRECOMPOSED != QA_DECOMPOSED
    assert ZA_PRECOMPOSED != ZA_DECOMPOSED
    assert unicodedata.normalize("NFKC", QA_PRECOMPOSED) == QA_DECOMPOSED
    assert unicodedata.normalize("NFKC", ZA_PRECOMPOSED) == ZA_DECOMPOSED
    # NFC is not enough: these sit in the composition exclusion table, so NFC leaves the
    # decomposed pair decomposed and would never have closed the gap.
    assert unicodedata.normalize("NFC", QA_DECOMPOSED) == QA_DECOMPOSED


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("price", "the lexicon's own spelling, the control"),
        (_fullwidth("price"), "fullwidth Latin from an IME candidate list"),
        ("PRICE", "shouting still asks the same question"),
        ("क़ीमत", "precomposed QA: what a Hindi keyboard emits"),
        ("कीमत", "the plain spelling, no nukta at all"),
        ("keemat", "Hinglish romanisation"),
        ("daam", "the other Hinglish word for the same thing"),
    ],
)
def test_a_price_term_is_recognised_however_it_was_typed(text: str, why: str) -> None:
    """Every spelling of "price" a real buyer produces must match the same term.

    ``why`` is carried only so a failure names the keyboard that broke, not the code point.
    """
    assert matches_any(text, DEFAULT_LEXICON.basket_terms), why


@pytest.mark.parametrize(
    "message",
    [
        "क़ीमत क्या है?",
        _fullwidth("what is the price") + "?",
        "कीमत क्या है?",
        "total kitna hai?",
        "how much is it?",
    ],
)
def test_a_price_question_forces_the_basket_read_whatever_the_keyboard(message: str) -> None:
    """The prevention layer's whole job: this turn starts from ``basket_get``.

    Before the fix, the first case returned None -- no read was forced, and the model was
    left to answer a question about money from whatever it remembered.
    """
    state = GroundingState(basket_id=BASKET)
    assert forced_tool(SHOPPING_RULES, DEFAULT_LEXICON, message, state) == "basket_get"


@pytest.mark.parametrize(
    "message",
    [
        "डिलीवरी शुल्क कितना है?",
        _fullwidth("delivery fee") + " kitna?",
        "delivery charge kitna hai?",
    ],
)
def test_a_delivery_question_forces_a_requote_whatever_the_keyboard(message: str) -> None:
    """The fee engine's number, never the model's -- in every script the buyer may use."""
    state = GroundingState(basket_id=BASKET, checkout_id=None)
    assert forced_tool(CHECKOUT_RULES, DEFAULT_LEXICON, message, state) == "basket_get"


def test_a_remedy_request_in_precomposed_devanagari_still_forces_the_service() -> None:
    """An amount owed comes from the Resolution Service, so the request must reach it.

    ``ख़राब`` ("damaged") carries a precomposed KHA-nukta, and the remedy lexicon is
    written with both that word and its plain spelling; the fold is what makes one entry
    cover the keyboard that emits the other.
    """
    state = GroundingState(order_id="0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b")
    message = "ख़राब सामान आया, पैसे वापस चाहिए"
    assert forced_tool(SUPPORT_RULES, DEFAULT_LEXICON, message, state) == "resolution_evaluate"


# ------------------------------------------------------------------ numeric literals


@pytest.mark.parametrize(
    "message",
    [
        # No basket *term* appears in any of these -- "price", "total" and the rest are
        # absent on purpose, so what fires the rule is the rupee figure alone.
        "take it from ₹29 to ₹26?",
        # A fullwidth sentence around the figure, ending in a fullwidth question mark,
        # which is itself a cue only once it has been normalised.
        _fullwidth("take it from ") + "₹29 " + _fullwidth("to") + " ₹26" + _fullwidth("?"),
        "29 rupees se 26 kar do?",
    ],
)
def test_a_money_literal_stands_in_for_a_term_after_folding(message: str) -> None:
    """A concrete figure is itself a price question; folding must not lose the figure.

    The literal search runs on the folded text, so a fullwidth sentence around a rupee
    amount still reads as money rather than as prose -- and the fullwidth ``？`` still
    counts as the question cue the rule needs alongside the figure.
    """
    assert matches_terms_and_cues(
        message,
        DEFAULT_LEXICON.basket_terms,
        DEFAULT_LEXICON.question_cues,
        numeric_literals=True,
    )


# ------------------------------------------------------------------ identifier tokens


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("add AMUL-DAIRY-001 please", "AMUL-DAIRY-001"),
        (_fullwidth("add AMUL-DAIRY-001 please"), "AMUL-DAIRY-001"),
        ("add amul-dairy-001 please", "amul-dairy-001"),
    ],
)
def test_a_sku_token_is_found_and_returned_as_typed(text: str, expected: str) -> None:
    """A fullwidth SKU resolves to its ASCII identifier, and case survives the trip.

    ``find_token`` normalises with NFKC but must NOT casefold: the substring it returns
    becomes the identifier a prefetch reads, and lowercasing it here would change which
    id the platform looked up. That is why there are two folding functions and not one.
    """
    assert find_token(text, DEFAULT_LEXICON.sku_patterns) == expected


def test_an_unseen_sku_forces_a_catalogue_read_even_in_fullwidth() -> None:
    """The catalogue rule fires on a SKU the session has not resolved, in any width."""
    state = GroundingState(seen_skus=frozenset())
    assert forced_tool(SHOPPING_RULES, DEFAULT_LEXICON, _fullwidth("AMUL-DAIRY-001"), state) == (
        "product"
    )


# ------------------------------------------------------------- the folds themselves


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_fullwidth("PRICE"), "price"),
        (QA_PRECOMPOSED, QA_DECOMPOSED),
        (ZA_PRECOMPOSED, ZA_DECOMPOSED),
        ("Ｔｏｔａｌ", "total"),
    ],
)
def test_fold_normalises_then_casefolds(raw: str, expected: str) -> None:
    """One spelling to compare against, whatever produced the text."""
    assert fold(raw) == expected


def test_fold_is_idempotent() -> None:
    """A folded string folds to itself, so double-folding a needle cannot drift."""
    for term in DEFAULT_LEXICON.basket_terms + DEFAULT_LEXICON.remedy_terms:
        assert fold(fold(term)) == fold(term)


def test_normalize_preserves_case_and_fold_does_not() -> None:
    """The two functions differ in exactly one way, and that difference is load-bearing."""
    assert normalize("ＡＢＣ-Ｄ") == "ABC-D"
    assert fold("ＡＢＣ-Ｄ") == "abc-d"


def test_every_lexicon_term_is_already_in_folded_form() -> None:
    """A term the fold would change could never match, because the text is folded too.

    This is the regression guard for the *next* edit rather than for this one: a
    contributor adding "क़ीमत" in its precomposed form would add a term that can never
    fire, and nothing else in the suite would notice.
    """
    lexicon = DEFAULT_LEXICON
    for name in (
        "basket_terms",
        "delivery_terms",
        "order_terms",
        "remedy_terms",
        "question_cues",
        "action_cues",
    ):
        for term in getattr(lexicon, name):
            assert fold(term) == term.casefold(), f"{name}: {term!r} is not in NFKC form"


# --------------------------------------------------------- folding widens nothing else


@pytest.mark.parametrize(
    ("text", "terms"),
    [
        ("coffee", ("fee",)),
        ("determines", ("terms",)),
        ("reorder", ("order",)),
    ],
)
def test_folding_does_not_break_the_whole_word_rule(text: str, terms: tuple[str, ...]) -> None:
    """Normalisation must not turn a substring match back on.

    "fee" inside "coffee" is the canonical false positive; a fold that stripped the word
    boundary would make every rule fire on every turn, which is a denial-of-service on
    the tool budget rather than a safety win.
    """
    assert not matches_any(text, terms)


def test_folding_does_not_make_an_empty_lexicon_fire() -> None:
    """A rule with nothing configured must stay silent, folded or not."""
    assert not matches_any(_fullwidth("anything"), ())
    assert not matches_terms_and_cues(_fullwidth("price?"), (), DEFAULT_LEXICON.question_cues)

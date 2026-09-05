"""Grounding rules: precedence, whole-word matching, id tokens, unseen-only.

These pin the same five properties the reference's ``test_grounding.py`` pins -- a whole
word and a cue must both appear, an empty lexicon never fires, numeric literals stand in
for a term, the longest id token wins, first rule in order wins -- and then the ones our
lexicon adds: Devanagari word boundaries and rupee literals.
"""

from __future__ import annotations

import pytest
from agent_runtime.core import (
    CHECKOUT_RULES,
    DEFAULT_LEXICON,
    GROWTH_RULES,
    SHOPPING_RULES,
    SUPPORT_RULES,
    GroundingLexicon,
    GroundingRule,
    GroundingState,
    find_token,
    first_rule,
    forced_tool,
    matches_any,
    matches_terms_and_cues,
    rules_for,
)

TERMS = ("returns", "fee", "terms")
CUES = ("?", "how", "tell me")
PATTERNS = (r"\bSKU-\d{3}\b", r"\bSKU-[A-Z]{2,4}-\d{3}(?:-[A-Z]{2})?\b")
ORDER = "0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b"


# ---------------------------------------------------------------------- primitives


@pytest.mark.parametrize(
    ("text", "fires"),
    [
        ("how do returns work", True),
        ("fee?", True),
        ("returns", False),  # a term without a cue
        ("how are you", False),  # a cue without a term
        ("how does this coffee taste", False),  # "fee" only inside a word
        ("what determines the price?", False),  # "terms" only inside a word
        ("", False),
    ],
)
def test_a_whole_word_term_and_a_cue_must_both_appear(text: str, fires: bool) -> None:
    assert matches_terms_and_cues(text, TERMS, CUES) is fires


def test_an_empty_lexicon_never_fires() -> None:
    assert not matches_terms_and_cues("how do returns work?", (), CUES)
    assert not matches_terms_and_cues("how do returns work?", TERMS, ())


@pytest.mark.parametrize(
    ("text", "fires"),
    [
        ("take it from ₹29 down to ₹26", True),
        ("cut it by 15 % this weekend", True),
        ("take it to Rs. 26", True),
        ("take it to 26 rupees", True),
        ("it is ₹29 right now", False),  # an amount without a cue
        ("take the afternoon off", False),  # a cue without a term or an amount
    ],
)
def test_rupee_and_percent_figures_stand_in_for_a_term_when_asked(text: str, fires: bool) -> None:
    assert matches_terms_and_cues(text, ("price",), ("take", "cut"), numeric_literals=True) is fires
    assert not matches_terms_and_cues(text, ("price",), ("take", "cut"))


def test_devanagari_words_match_whole_even_when_they_end_in_a_vowel_sign() -> None:
    """Python's ``\\b`` puts a vowel sign outside the word; ours does not."""
    assert matches_any("मुझे नहीं चाहिए", ("नहीं",))
    assert matches_any("यह कितने का है?", ("कितने",))
    assert not matches_any("कितनेवाला", ("कितने",))  # inside a longer word
    assert matches_any("कुल कितना हुआ", ("कुल",))


def test_phrase_needles_match_in_order_and_case_insensitively() -> None:
    assert matches_any("Tell Me the fee", ("tell me",))
    assert not matches_any("me tell the fee", ("tell me",))
    assert matches_any("free delivery ke liye kitna aur", ("free delivery ke liye",))


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("add SKU-102 please", "SKU-102"),
        ("is sku-102 in stock", "sku-102"),
        ("transfer SKU-TIX-104-GA to me", "SKU-TIX-104-GA"),  # the longest match wins
        ("order SKU-10234 arrived", None),  # a longer number is a different kind of id
        ("does it charge over USB-C?", None),
        ("", None),
    ],
)
def test_find_token_returns_the_longest_case_insensitive_match(
    text: str, token: str | None
) -> None:
    assert find_token(text, PATTERNS) == token


def test_find_token_without_patterns_returns_none() -> None:
    assert find_token("add SKU-102 please", ()) is None


def test_first_rule_follows_rule_order_and_skips_rules_that_do_not_fire() -> None:
    rules = (
        GroundingRule("terms", "read_terms", lambda _c, text, _s: {} if "terms" in text else None),
        GroundingRule(
            "ids", "read_id", lambda _c, text, _s: {"id": text} if "SKU" in text else None
        ),
    )
    state = GroundingState()
    assert forced_tool(rules, DEFAULT_LEXICON, "terms for SKU-1?", state) == "read_terms"
    assert forced_tool(rules, DEFAULT_LEXICON, "SKU-1?", state) == "read_id"
    assert forced_tool(rules, DEFAULT_LEXICON, "hello", state) is None
    found = first_rule(rules, DEFAULT_LEXICON, "SKU-1?", state)
    assert found is not None
    assert found[0].name == "ids" and found[1] == {"id": "SKU-1?"}


# --------------------------------------------------------------------- P0 rules


def _fire(
    rules: tuple[GroundingRule, ...], text: str, state: GroundingState
) -> tuple[str, dict[str, object]] | None:
    found = first_rule(rules, DEFAULT_LEXICON, text, state)
    return None if found is None else (found[0].name, dict(found[1]))


def test_shopping_reads_an_unseen_sku_before_the_model_can_describe_it() -> None:
    assert _fire(SHOPPING_RULES, "is amul-dairy-001 good?", GroundingState()) == (
        "catalogue",
        {"sku": "AMUL-DAIRY-001"},
    )


def test_a_sku_already_in_provenance_never_fires() -> None:
    """An id the session resolved needs no forced re-read; ids compare case-insensitively."""
    seen = GroundingState(seen_skus=frozenset({"amul-dairy-001"}))
    assert _fire(SHOPPING_RULES, "is AMUL-DAIRY-001 good?", seen) is None


@pytest.mark.parametrize(
    "text",
    [
        "how much is my basket now?",
        "kya total ₹395 hi hai?",
        "mera cart kitne ka hai",
        "टोकरी में कुल कितना हुआ?",
    ],
)
def test_shopping_re_quotes_before_answering_a_basket_number(text: str) -> None:
    with_basket = GroundingState(basket_id="b1")
    assert _fire(SHOPPING_RULES, text, with_basket) == ("basket", {"basket_id": "b1"})
    assert _fire(SHOPPING_RULES, text, GroundingState()) is None


def test_shopping_catalogue_rule_outranks_basket_rule() -> None:
    state = GroundingState(basket_id="b1")
    fired = _fire(SHOPPING_RULES, "how much is AASH-STPL-002 in my basket?", state)
    assert fired is not None and fired[0] == "catalogue"


def test_checkout_always_starts_from_the_current_version() -> None:
    """Unconditional: prices move under a checkout, and last turn's card is not evidence."""
    state = GroundingState(checkout_id="c1", basket_id="b1")
    assert _fire(CHECKOUT_RULES, "ok go ahead", state) == ("state", {"checkout_id": "c1"})
    assert _fire(CHECKOUT_RULES, "is delivery free?", state) == ("state", {"checkout_id": "c1"})


@pytest.mark.parametrize(
    "text",
    ["is delivery free?", "delivery charge kitna hai", "क्या डिलीवरी मुफ़्त है?", "tax ₹5 kyu?"],
)
def test_checkout_re_quotes_a_fee_question_when_no_checkout_exists_yet(text: str) -> None:
    state = GroundingState(basket_id="b1")
    assert _fire(CHECKOUT_RULES, text, state) == ("delivery_or_fee", {"basket_id": "b1"})
    assert _fire(CHECKOUT_RULES, text, GroundingState()) is None


def test_support_reads_an_order_named_by_id_or_by_cue() -> None:
    assert _fire(SUPPORT_RULES, f"where is {ORDER.upper()}", GroundingState()) == (
        "order",
        {"order_id": ORDER},
    )
    with_order = GroundingState(order_id="o1")
    assert _fire(SUPPORT_RULES, "mera order kab aayega?", with_order) == (
        "order",
        {"order_id": "o1"},
    )
    assert _fire(SUPPORT_RULES, "mera order kab aayega?", GroundingState()) is None


@pytest.mark.parametrize(
    "text",
    ["I want a refund", "paise wapas chahiye", "मुझे रिफंड चाहिए", "cancel kar do please"],
)
def test_support_remedy_is_force_only_and_needs_an_order(text: str) -> None:
    with_order = GroundingState(order_id="o1")
    fired = first_rule(SUPPORT_RULES, DEFAULT_LEXICON, text, with_order)
    assert fired is not None
    rule, args = fired
    assert rule.name == "remedy" and rule.tool == "resolution_evaluate"
    assert args == {} and not rule.prefetchable
    assert _fire(SUPPORT_RULES, text, GroundingState()) is None


def test_growth_metrics_question_is_forced() -> None:
    fired = first_rule(GROWTH_RULES, DEFAULT_LEXICON, "how were sales this week?", GroundingState())
    assert fired is not None and fired[0].tool == "checkout_metrics_read"
    assert not fired[0].prefetchable
    assert _fire(GROWTH_RULES, "add a new listing", GroundingState()) is None


def test_prefetch_rules_render_an_intro_naming_the_tool() -> None:
    for rules in (SHOPPING_RULES, CHECKOUT_RULES, SUPPORT_RULES):
        for rule in rules:
            if rule.prefetch_intro is not None:
                intro = rule.prefetch_intro({"sku": "AMUL-DAIRY-001", "order_id": ORDER})
                assert rule.tool in intro


def test_rules_for_role_and_unknown_role_is_empty() -> None:
    assert rules_for("shopping") is SHOPPING_RULES
    assert rules_for("checkout") is CHECKOUT_RULES
    assert rules_for("support") is SUPPORT_RULES
    assert rules_for("growth") is GROWTH_RULES
    assert rules_for("case") == ()
    assert rules_for("nope") == ()


def test_lexicon_is_configuration() -> None:
    """A tenant can replace the words without touching the rules."""
    custom = GroundingLexicon(basket_terms=("tally",), question_cues=("?",))
    state = GroundingState(basket_id="b1")
    assert first_rule(SHOPPING_RULES, custom, "tally?", state) is not None
    assert first_rule(SHOPPING_RULES, custom, "how much is my basket?", state) is None

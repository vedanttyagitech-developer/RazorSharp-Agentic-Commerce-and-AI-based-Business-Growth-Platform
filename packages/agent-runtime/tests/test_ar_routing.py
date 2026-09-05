"""Routing is a pure function: same message, same context, same answer, no model.

Every case here pins a *route and its reason*, because the reason is what a reviewer
reads in the transcript to see why a specialist ran. The three tiers are exercised in
order of precedence -- explicit context beats intent beats continuity -- and one case per
harness proves that a message with no signal yields a clarifying question in the buyer's
own language rather than a guess.
"""

from __future__ import annotations

import uuid

import pytest
from agent_runtime.harness import (
    Clarification,
    CopilotSession,
    Route,
    Specialist,
    route_buyer,
    route_merchant,
)
from agent_runtime.language import Language, detect_language

TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")


def fresh_session() -> CopilotSession:
    return CopilotSession(
        session_id="s-route",
        tenant_id=TENANT,
        principal_id="agent:buyer",
        correlation_id=uuid.uuid4(),
    )


def buyer(
    text: str, context: dict[str, object] | None = None, session: CopilotSession | None = None
) -> Route | Clarification:
    return route_buyer(text, detect_language(text), context or {}, session or fresh_session())


def chosen(result: Route | Clarification) -> Specialist:
    assert isinstance(result, Route), result
    return result.specialist


def merchant(
    text: str, context: dict[str, object] | None = None, session: CopilotSession | None = None
) -> Route | Clarification:
    return route_merchant(text, detect_language(text), context or {}, session or fresh_session())


# ------------------------------------------------------------- explicit context


def test_checkout_id_in_context_routes_to_checkout() -> None:
    assert buyer("hello", {"checkout_id": "chk_1"}) == Route(
        Specialist.CHECKOUT, "context:checkout_id"
    )


def test_order_id_in_context_routes_to_support() -> None:
    assert buyer("hello", {"order_id": "ord_1"}) == Route(Specialist.SUPPORT, "context:order_id")


def test_page_in_context_routes_to_its_owner() -> None:
    assert buyer("hello", {"page": "basket"}) == Route(Specialist.SHOPPING, "context:page=basket")
    assert buyer("hello", {"page": "Checkout"}) == Route(
        Specialist.CHECKOUT, "context:page=checkout"
    )
    assert buyer("hello", {"page": "orders"}) == Route(Specialist.SUPPORT, "context:page=orders")


def test_explicit_context_outranks_intent_and_ids_outrank_page() -> None:
    # A buyer on the checkout page asking about a refund is still on the checkout.
    assert chosen(buyer("I want a refund", {"checkout_id": "chk_1"})) is Specialist.CHECKOUT
    # An id is stronger evidence than a page name.
    assert chosen(buyer("hi", {"checkout_id": "chk_1", "page": "support"})) is Specialist.CHECKOUT


def test_blank_or_non_string_context_is_ignored() -> None:
    assert isinstance(
        buyer("zzz qqq", {"checkout_id": "", "order_id": None, "page": 7}), Clarification
    )


# ---------------------------------------------------------------------- intent


@pytest.mark.parametrize(
    ("text", "specialist", "reason"),
    [
        ("I want two litres of milk", Specialist.SHOPPING, "intent:shopping"),
        ("mujhe doodh chahiye", Specialist.SHOPPING, "intent:shopping"),
        ("मुझे दूध चाहिए", Specialist.SHOPPING, "intent:shopping"),
        ("how much is the bread", Specialist.SHOPPING, "intent:shopping"),
        ("let's checkout and pay", Specialist.CHECKOUT, "intent:checkout"),
        ("bhugtan karo", Specialist.CHECKOUT, "intent:checkout"),
        ("भुगतान करो", Specialist.CHECKOUT, "intent:checkout"),
        ("place the order", Specialist.CHECKOUT, "intent:checkout"),
        ("cancel my order", Specialist.SUPPORT, "intent:support"),
        ("where is my order", Specialist.SUPPORT, "intent:support"),
        ("refund chahiye", Specialist.SUPPORT, "intent:support"),
        ("रिफंड चाहिए", Specialist.SUPPORT, "intent:support"),
        ("the milk arrived damaged", Specialist.SUPPORT, "intent:support"),
        ("AMUL-DAIRY-001", Specialist.SHOPPING, "intent:sku_token"),
    ],
)
def test_intent_lexicons(text: str, specialist: Specialist, reason: str) -> None:
    assert buyer(text) == Route(specialist, reason)


def test_support_outranks_checkout_which_outranks_shopping() -> None:
    # "cancel" + "order" + "pay": the remedy wins, never the purchase.
    assert chosen(buyer("cancel the order I was about to pay for")) is Specialist.SUPPORT
    # "pay" + "want": paying wins over browsing.
    assert chosen(buyer("I want to pay now")) is Specialist.CHECKOUT


def test_lexicon_matches_whole_words_only() -> None:
    # "payphone" is not "pay"; "totally" is not "total"; "carton" is not "cart".
    result = buyer("totally a payphone carton")
    assert isinstance(result, Clarification)


# ------------------------------------------------------------------ continuity


def test_follow_up_without_signal_continues_with_last_specialist() -> None:
    session = fresh_session()
    session.last_specialist = "support"
    assert buyer("thanks, and then?", session=session) == Route(
        Specialist.SUPPORT, "session:continuity"
    )


def test_open_checkout_in_session_routes_there_before_basket() -> None:
    session = fresh_session()
    session.basket_id = "bsk_1"
    session.checkout_id = "chk_1"
    assert buyer("ok", session=session) == Route(Specialist.CHECKOUT, "session:checkout_open")
    session.checkout_id = None
    assert buyer("ok", session=session) == Route(Specialist.SHOPPING, "session:basket_open")


# ------------------------------------------------------------------ unroutable


@pytest.mark.parametrize(
    ("text", "language"),
    [("qwerty zxcv", Language.EN), ("ॐ", Language.HI), ("theek theek", Language.HI_LATN)],
)
def test_unroutable_first_turn_yields_clarification_in_buyer_language(
    text: str, language: Language
) -> None:
    result = buyer(text)
    assert isinstance(result, Clarification)
    assert result.reason == "unroutable"
    assert result.question
    # The question is rendered from a template keyed by the detected language.
    assert detect_language(result.question) is language


# -------------------------------------------------------------------- merchant


def test_merchant_explicit_context() -> None:
    assert merchant("hi", {"case_id": "case_1"}) == Route(Specialist.CASE, "context:case_id")
    assert merchant("hi", {"page": "growth"}) == Route(Specialist.GROWTH, "context:page=growth")
    assert merchant("hi", {"page": "cases"}) == Route(Specialist.CASE, "context:page=cases")


@pytest.mark.parametrize(
    ("text", "specialist"),
    [
        ("how are sales this week", Specialist.GROWTH),
        ("show me inventory anomalies", Specialist.GROWTH),
        ("draft a discount proposal", Specialist.GROWTH),
        ("show me the escalation cases", Specialist.CASE),
        ("what is in the review queue", Specialist.CASE),
        ("cases about discounts", Specialist.CASE),
    ],
)
def test_merchant_intent(text: str, specialist: Specialist) -> None:
    result = merchant(text)
    assert isinstance(result, Route)
    assert result.specialist is specialist


def test_merchant_continuity_and_clarification() -> None:
    session = fresh_session()
    session.last_specialist = "growth"
    assert merchant("and last month?", session=session) == Route(
        Specialist.GROWTH, "session:continuity"
    )
    result = merchant("qwerty zxcv")
    assert isinstance(result, Clarification)
    assert result.reason == "unroutable"


def test_merchant_router_never_names_a_buyer_specialist() -> None:
    for text in ("refund", "cancel my order", "I want milk", "checkout"):
        result = merchant(text)
        assert not isinstance(result, Route) or result.specialist in {
            Specialist.GROWTH,
            Specialist.CASE,
        }

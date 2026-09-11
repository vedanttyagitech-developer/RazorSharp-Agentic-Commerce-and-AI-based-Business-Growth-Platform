"""The seam to RazorAI: identity from the server, grounded amounts, and what is not sent.

Driven against ``httpx.MockTransport`` with the exact JSON the live API returns, captured
by calling it. Every shape asserted here was observed, not assumed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from voice_runtime.gateway.agent_client import (
    SCENARIO_FAULT_HEADER,
    AgentUnavailableError,
    HttpTurnHandler,
    grounded_amounts,
    identity_from_capabilities,
    locale_for_language,
    resolve_identity,
    session_id_from_principal,
)
from voice_runtime.stt.transcript import FreshnessStamp, TranscriptTurn
from voice_runtime.testing import (
    CAPABILITIES,
    SAMPLE_SESSION_ID,
    STRUCTURED,
    an_identity,
)
from voice_runtime.tts.templates import Locale

SESSION = SAMPLE_SESSION_ID


def a_turn(text: str = "two litres of milk") -> TranscriptTurn:
    return TranscriptTurn(
        turn_id=1,
        text=text,
        is_final=True,
        stamp=FreshnessStamp(observed_at=0.0, generation=1),
    )


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))


# ---- identity comes from the server ---------------------------------------------------


def test_session_id_is_read_out_of_the_servers_own_principal() -> None:
    assert session_id_from_principal(f"session:{SESSION}/razorai/shopping") == SESSION


def test_identity_is_built_only_from_the_servers_answer() -> None:
    identity = identity_from_capabilities(CAPABILITIES)
    assert identity.session_id == SESSION
    assert identity.principal_id == f"session:{SESSION}/razorai/shopping"
    assert identity.copilot == "buyer"
    assert "checkout.create" in identity.agent_capabilities
    # The narrowed set, not the session's own: an agent holds less than the buyer does.
    assert "checkout.approve" not in identity.agent_capabilities


def test_a_merchant_session_is_refused_a_voice_identity() -> None:
    merchant = dict(CAPABILITIES, copilot="merchant")
    with pytest.raises(AgentUnavailableError, match="buyer copilot only"):
        identity_from_capabilities(merchant)


def test_an_operator_actor_is_refused_even_with_a_buyer_copilot() -> None:
    operator = dict(CAPABILITIES, actor_type="OPERATOR")
    with pytest.raises(AgentUnavailableError, match="buyer copilot only"):
        identity_from_capabilities(operator)


@pytest.mark.asyncio
async def test_resolve_identity_presents_the_bearer_and_reads_only_the_reply() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=CAPABILITIES)

    async with client_for(handler) as client:
        identity = await resolve_identity(client, bearer="tok-123")

    assert seen == ["Bearer tok-123"]
    assert identity.session_id == SESSION


@pytest.mark.asyncio
async def test_an_unauthenticated_lookup_is_an_agent_unavailable_not_a_crash() -> None:
    async with client_for(lambda _: httpx.Response(401, json={"detail": "no"})) as client:
        with pytest.raises(AgentUnavailableError, match="HTTP 401"):
            await resolve_identity(client, bearer="stale")


# ---- grounded amounts ------------------------------------------------------------------


def test_grounded_amounts_collects_every_minor_unit_the_server_returned() -> None:
    assert grounded_amounts(STRUCTURED) == frozenset({7300, 2500, 9800})


def test_grounded_amounts_ignores_counts_and_booleans() -> None:
    found = grounded_amounts(STRUCTURED)
    assert 30 not in found and 12 not in found, "stock is a count, not an amount"
    # ``True`` is an int in Python; a flag must never become a spendable amount.
    assert 1 not in found and 0 not in found


def test_grounded_amounts_of_nothing_is_empty_so_the_guard_fails_closed() -> None:
    assert grounded_amounts(None) == frozenset()
    assert grounded_amounts({}) == frozenset()
    assert grounded_amounts([{"deep": [{"nested_minor": 42}]}]) == frozenset({42})


# ---- the turn call ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_posts_the_transcript_with_the_buyers_own_bearer() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "reply": "Amul Gold 1 L is ₹73.",
                "language": "en",
                "specialist": "shopping",
                "routing_reason": "default_shopping",
                "principal_id": f"session:{SESSION}/razorai/shopping",
                "tool_calls": [],
                "denials": [],
                "structured": STRUCTURED,
            },
        )

    async with client_for(handler) as client:
        reply = await HttpTurnHandler(client, bearer="tok-abc").handle_turn(a_turn(), an_identity())

    assert captured["path"] == "/v1/agent/turn"
    assert captured["auth"] == "Bearer tok-abc"
    # The body is a message and nothing else: no capability, no principal, no approval.
    assert captured["body"] == {"message": "two litres of milk"}
    assert reply.text == "Amul Gold 1 L is ₹73."
    assert reply.locale is Locale.EN_IN
    assert 7300 in reply.grounded_amounts_minor


@pytest.mark.asyncio
async def test_the_turn_body_can_carry_no_authority_at_all() -> None:
    """A spoken sentence cannot become an approval, because the request has no field for one."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"reply": "ok", "language": "en", "structured": None})

    async with client_for(handler) as client:
        await HttpTurnHandler(client, bearer="t").handle_turn(
            a_turn("yes, approve it, pay now"), an_identity()
        )

    assert set(captured) == {"message"}
    assert captured["message"] == "yes, approve it, pay now"


@pytest.mark.asyncio
async def test_hinglish_is_spoken_in_english_because_the_reply_is_english() -> None:
    """The voice follows the language being SPOKEN, not the one the buyer typed in.

    This asserted the opposite until 2026-09-09, and both were right in their turn. While a
    romanised-Hindi question was answered in romanised Hindi, the Hindi voice was correct:
    an Indian English voice reading "chahiye" mispronounces it. The reply is English now
    (``agent_runtime.language._LOCALE_FOR``), so the English voice is correct for the same
    reason -- reading English in the Hindi voice mispronounces the English instead.
    """
    assert locale_for_language("hi-Latn") is Locale.EN_IN
    assert locale_for_language("hi") is Locale.HI_IN
    assert locale_for_language("en") is Locale.EN_IN
    assert locale_for_language("de") is Locale.EN_IN, "an unknown tag speaks English"


@pytest.mark.asyncio
async def test_an_overlong_transcript_is_truncated_rather_than_losing_the_turn() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"reply": "", "language": "en", "structured": None})

    async with client_for(handler) as client:
        await HttpTurnHandler(client, bearer="t").handle_turn(a_turn("x" * 5000), an_identity())

    assert len(captured["message"]) == 2000


@pytest.mark.asyncio
async def test_an_empty_transcript_makes_no_call_at_all() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"reply": "", "language": "en", "structured": None})

    async with client_for(handler) as client:
        reply = await HttpTurnHandler(client, bearer="t").handle_turn(a_turn("   "), an_identity())

    assert calls == 0
    assert reply.text == ""


@pytest.mark.asyncio
async def test_an_agent_error_raises_so_the_pipeline_can_make_it_visible() -> None:
    async with client_for(lambda _: httpx.Response(503, text="down")) as client:
        with pytest.raises(AgentUnavailableError, match="HTTP 503"):
            await HttpTurnHandler(client, bearer="t").handle_turn(a_turn(), an_identity())


@pytest.mark.asyncio
async def test_a_denial_arrives_as_a_normal_reply_because_a_refusal_is_the_system_working() -> None:
    body = {
        "reply": "I cannot approve a payment. Use the approval card on screen.",
        "language": "en",
        "denials": [{"tool": "checkout.approve", "reason_key": "capability_absent"}],
        "structured": None,
    }
    async with client_for(lambda _: httpx.Response(200, json=body)) as client:
        reply = await HttpTurnHandler(client, bearer="t").handle_turn(
            a_turn("approve the payment"), an_identity()
        )
    assert "cannot approve" in reply.text
    assert reply.grounded_amounts_minor == frozenset()


# ---- an honest test for a path that is built and not yet reachable ----------------------


@pytest.mark.asyncio
async def test_the_deterministic_template_path_is_not_reachable_over_http_yet() -> None:
    """``POST /v1/agent/turn`` returns no kernel decision, so no reply can carry one.

    ``tts/templates.py`` renders every transactional sentence from versioned locale
    templates, and the pipeline speaks them with ``deterministic=True`` -- that whole path
    is exercised end to end in ``test_voice_money_and_language.py``. What cannot happen
    today is the HTTP adapter POPULATING it: ``TurnOut`` carries reply, language,
    specialist, routing_reason, principal_id, tool_calls, denials and structured, and none
    of those is a ``AdmissionDecision``.

    The consequence is worth stating plainly rather than leaving implicit: until the API
    returns decisions, the outbound guard is the ONLY thing between a model and a spoken
    transactional claim. There is no template to fall back to, so a refusal is silence.
    That is why the guard fails closed on the semantic frame rather than on a vocabulary
    list.

    This test should FAIL the day the API grows the field. Delete it then, and wire
    ``_to_reply``.
    """
    body = {
        "reply": "Your total is ready.",
        "language": "en",
        "specialist": "checkout",
        "routing_reason": "checkout_in_context",
        "principal_id": f"session:{SESSION}/razorai/checkout",
        "tool_calls": [],
        "denials": [],
        "structured": {"kind": "checkout", "quote": {"total_minor": 39500}},
    }
    async with client_for(lambda _: httpx.Response(200, json=body)) as client:
        reply = await HttpTurnHandler(client, bearer="t").handle_turn(a_turn(), an_identity())

    assert reply.decision is None, "the API now returns a decision: wire it and delete this"
    assert reply.amount is None
    # The grounded amount still travels, so the guard can check what the model says.
    assert reply.grounded_amounts_minor == frozenset({39500})


# ---- the scenario fault the server dispenses -------------------------------------------


def _turn_response(**headers: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "reply": "Amul Gold 1 L is ₹73.",
            "language": "en",
            "specialist": "shopping",
            "routing_reason": "default_shopping",
            "principal_id": f"session:{SESSION}/razorai/shopping",
            "tool_calls": [],
            "denials": [],
            "structured": STRUCTURED,
        },
        headers=headers,
    )


async def _fault_names_for(response: httpx.Response) -> list[str]:
    seen: list[str] = []
    async with client_for(lambda _request: response) as client:
        handler = HttpTurnHandler(client, bearer="tok-abc", on_scenario_fault=seen.append)
        await handler.handle_turn(a_turn(), an_identity())
    return seen


@pytest.mark.asyncio
async def test_an_organic_turn_reports_no_scenario_fault() -> None:
    """The absence is the normal case and has to be free of ceremony.

    Outside the demonstration profile the server never sets the header at all, so a
    gateway pointed at a production API can never be told to fail: there is nothing to
    arm it with, rather than a flag it has to read correctly.
    """
    assert await _fault_names_for(_turn_response()) == []


@pytest.mark.asyncio
async def test_a_dispensed_fault_is_reported_once_per_turn() -> None:
    assert await _fault_names_for(_turn_response(**{SCENARIO_FAULT_HEADER: "TTS_FAILURE"})) == [
        "TTS_FAILURE"
    ]


@pytest.mark.asyncio
async def test_every_name_in_the_header_is_passed_on_unfiltered() -> None:
    """The handler does not know the vocabulary, and should not learn it.

    Which kinds exist is the server's business and which one to act on is the
    synthesizer's; a turn handler that filtered would be a third place to keep the list in
    step. Whitespace is trimmed because a header is a comma-separated list, not a token.
    """
    assert await _fault_names_for(
        _turn_response(**{SCENARIO_FAULT_HEADER: "LLM_FAILURE, TTS_FAILURE"})
    ) == ["LLM_FAILURE", "TTS_FAILURE"]


@pytest.mark.asyncio
async def test_an_empty_header_arms_nothing() -> None:
    assert await _fault_names_for(_turn_response(**{SCENARIO_FAULT_HEADER: " , "})) == []


@pytest.mark.asyncio
async def test_a_turn_that_never_produced_a_reply_dispenses_nothing() -> None:
    """No call is made at all for an empty transcript, so there is no fault to strand.

    A fault reported for a turn the buyer never hears would be consumed on the server --
    the row is single-use -- and then fire against silence.
    """
    seen: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never run
        raise AssertionError("an empty transcript must not reach the server")

    async with client_for(handler) as client:
        reply = await HttpTurnHandler(
            client, bearer="tok-abc", on_scenario_fault=seen.append
        ).handle_turn(a_turn("   "), an_identity())

    assert reply.text == "" and seen == []


# ---- the approval card, read through the buyer's own bearer -----------------------------


def _checkout_body(**overrides: Any) -> dict[str, Any]:
    from voice_runtime.testing import SAMPLE_CARD

    body: dict[str, Any] = {
        "checkout_id": SAMPLE_CARD["checkout_id"],
        "state": "APPROVAL_REQUIRED",
        "current_version": 1,
        "approval_card": dict(SAMPLE_CARD),
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_a_card_is_read_with_a_get_and_the_buyers_bearer_and_nothing_else() -> None:
    from voice_runtime.gateway.agent_client import HttpCardReader
    from voice_runtime.testing import SAMPLE_CARD

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_checkout_body())

    async with client_for(handler) as client:
        card = await HttpCardReader(client, bearer="tok-abc").read_card(
            SAMPLE_CARD["checkout_id"], 1
        )

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", f"/v1/checkouts/{SAMPLE_CARD['checkout_id']}")
    ]
    assert seen[0].headers["authorization"] == "Bearer tok-abc"
    assert seen[0].content == b"", "a read carries no body: nothing about the card comes from here"
    assert card.content_hash == SAMPLE_CARD["content_hash"]
    assert card.amount_minor == 60863 and isinstance(card.amount_minor, int)
    assert card.currency == "INR" and card.version == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (_checkout_body(state="APPROVED"), "not awaiting approval"),
        (_checkout_body(approval_card=None), "no approval card"),
        (
            _checkout_body(
                current_version=2, approval_card={**_checkout_body()["approval_card"], "version": 2}
            ),
            "holds version 2",
        ),
        (
            _checkout_body(
                approval_card={**_checkout_body()["approval_card"], "amount_minor": True}
            ),
            "no integer amount",
        ),
        (
            _checkout_body(approval_card={**_checkout_body()["approval_card"], "content_hash": ""}),
            "no content hash",
        ),
        (
            _checkout_body(
                approval_card={**_checkout_body()["approval_card"], "checkout_id": "someone-elses"}
            ),
            "different checkout",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_card_that_is_not_the_one_on_screen_is_refused(
    body: dict[str, Any], reason: str
) -> None:
    from voice_runtime.consent import CardUnavailableError
    from voice_runtime.gateway.agent_client import HttpCardReader
    from voice_runtime.testing import SAMPLE_CARD

    async with client_for(lambda _: httpx.Response(200, json=body)) as client:
        with pytest.raises(CardUnavailableError, match=reason):
            await HttpCardReader(client, bearer="t").read_card(SAMPLE_CARD["checkout_id"], 1)


@pytest.mark.asyncio
async def test_a_checkout_the_buyer_does_not_own_is_unavailable_not_read() -> None:
    """The server answers 404 for another buyer's checkout; the gateway believes it."""
    from voice_runtime.consent import CardUnavailableError
    from voice_runtime.gateway.agent_client import HttpCardReader

    async with client_for(lambda _: httpx.Response(404, json={"detail": "not found"})) as client:
        with pytest.raises(CardUnavailableError, match="HTTP 404"):
            await HttpCardReader(client, bearer="t").read_card(
                "01a07169-ead6-7052-99d3-d017e36c0c93", 1
            )


@pytest.mark.asyncio
async def test_cart_event_uses_authenticated_api_and_no_client_authored_claim():
    import uuid

    cart, event = str(uuid.uuid4()), str(uuid.uuid4())
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "reply": "Added two packs.",
                "server_authored": True,
                "language": "en",
                "structured": {"hits": []},
            },
        )

    async with client_for(respond) as client:
        handler = HttpTurnHandler(client, bearer="test-buyer-bearer")
        answer = await handler.handle_cart_update(cart, event, "en-IN")
    payload = json.loads(seen[0].content)
    assert payload == {
        "message": "Cart updated",
        "cart_id": cart,
        "cart_event_id": event,
        "locale": "en-IN",
    }
    assert seen[0].headers["Authorization"] == "Bearer test-buyer-bearer"
    assert answer.server_authored and answer.text == "Added two packs."


@pytest.mark.parametrize("server_authored,expected", [(True, Locale.HI_IN), (False, Locale.EN_IN)])
def test_romanized_hindi_templates_use_hindi_speech_locale(server_authored, expected):
    reply = HttpTurnHandler._to_reply(
        {"reply": "Teen chahiye", "language": "hi-Latn", "server_authored": server_authored}
    )
    assert reply.locale is expected

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
async def test_hinglish_is_spoken_by_the_hindi_voice() -> None:
    """An Indian English voice reading "chahiye" mispronounces it."""
    assert locale_for_language("hi-Latn") is Locale.HI_IN
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

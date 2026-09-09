"""The bridge between this service's ``TurnRunner`` and ``agent_runtime``'s harness runner.

What is proven here, and why each is a rule rather than a feature:

* a bridged turn really runs the factory's tools, and every read it makes lands on **this
  service's** ledger -- the one the panel renders -- because the executor is the only path
  to a row;
* the grounding ledger the reply post-check reads is filled by that same turn, so
  ``verify_reply`` can tell a counted figure from an invented one. The check that this is
  not vacuous is the one that goes red first: a correct amount beside an *empty* grounding
  ledger is dropped, which is exactly what a wrapper-shaped bridge would have shipped;
* a model that raises, times out or answers nothing usable is a deterministic answer and
  never a 500 (specification 30);
* a refusal the model met -- a capability gate denial, a guardrail hold -- reaches the
  panel's ledger even though the tool never reached the executor;
* the bridge is not constructed at all when Vertex is absent, and ``app.state.agent_runner``
  is ``None``, which is what makes the deterministic runner the default rather than a
  recovery path;
* every specialist that is not model-backed keeps the deterministic runner, without the
  sentence that announces a missing reasoning layer.

The model runtime is scripted throughout: a :class:`~agent_runtime.harness.SpecialistRunner`
is an async callable, so a test double is a function. No Vertex, no ``google-adk``, no
network. The tools those doubles call are the real factory closures over the real database.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_runtime.capabilities.tools import BoundToolset
from agent_runtime.grounding import verify_reply
from agent_runtime.grounding.ledger import GroundingLedger
from agent_runtime.harness.base import BoundSpecialist, SpecialistInput, SpecialistReply
from agent_runtime.harness.session import CopilotSession
from agent_runtime.language import Language
from agent_runtime.turn import TurnContext
from commerce_api import app as app_module
from commerce_api.deps import RequestContext, session_scope_for
from commerce_api.services import agent_bridge, agent_service
from commerce_api.services.agent_bridge import (
    BRIDGED_SPECIALISTS,
    BridgeUnavailableError,
    SpecialistBridge,
)
from commerce_api.services.agent_service import (
    Copilot,
    Route,
    Specialist,
    ToolExecutor,
    TurnInput,
    TurnLedger,
    bind,
    run_turn,
)
from commerce_domain import ActorType, AgentPrincipal, uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db.tenancy import set_tenant
from sqlalchemy import text

from conftest import MintedSession

pytestmark = pytest.mark.db

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def buyer_session(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> tuple[TestClient, MintedSession]:
    return mint_client(actor_type="BUYER")


Runner = Callable[[BoundSpecialist, SpecialistInput, TurnContext, CopilotSession], Any]


def _shopping_turn(
    api_app: FastAPI,
    minted: MintedSession,
    runner: Runner,
    *,
    message: str = "milk",
    cart_id: uuid.UUID | None = None,
) -> agent_service.TurnResult:
    """Run one shopping turn through the real bridge over a scripted specialist runner."""
    ctx = _context(minted)
    with session_scope_for(api_app.state.settings.database_url_app) as session:
        # Bound, as every real request's session is: the shop's prices and stock are rows
        # now, and an unbound transaction would have RLS filter the whole catalogue away.
        set_tenant(session, ctx.tenant_id)
        return run_turn(
            session,
            ctx,
            api_app.state.merchants,
            copilot=Copilot.BUYER,
            message=message,
            locale="en",
            cart_id=cart_id,
            checkout_id=None,
            order_id=None,
            runner=SpecialistBridge(runner),
        )


def _context(minted: MintedSession) -> RequestContext:
    principal = AgentPrincipal(
        principal_id=f"session:{minted.session_id}",
        tenant_id=minted.tenant_id,
        actor_type=ActorType.BUYER,
        merchant_id=minted.merchant_id,
        buyer_ref=minted.buyer_ref,
        capabilities=frozenset(agent_service.AGENT_CAPABILITIES),
        correlation_id=uuid7(),
    )
    return RequestContext(
        tenant_id=minted.tenant_id,
        merchant_id=minted.merchant_id,
        buyer_ref=minted.buyer_ref,
        principal=principal,
        correlation_id=principal.correlation_id or uuid7(),
        session_id=minted.session_id,
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


def _tool(bound: BoundSpecialist, name: str) -> Any:
    assert isinstance(bound.tools, BoundToolset)
    return bound.tools.get(name)


class _Context:
    """The tool context a scripted runner passes, standing in for ADK's own.

    The factory's tools read and write session state through it -- which metrics this
    conversation has read, which proposals it staged -- so a script that shares one across
    its calls is a conversation and one that does not is a sequence of first turns.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.function_call_id = "scripted"


# ------------------------------------------------------------ a turn that really ran


def test_a_bridged_turn_reads_through_the_executor_and_fills_both_ledgers(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """The whole seam, once: factory tools, this service's ledger, the grounding ledger.

    The two ledgers answer different questions and both must be filled by the same turn.
    This service's :class:`~agent_service.TurnLedger` is what the panel draws chips from.
    ``TurnContext.ledger`` is what :func:`~agent_runtime.grounding.verify_reply` reads, and
    it is filled by ``agent_runtime``'s own payload builders rather than by anything here --
    which is the reason the backend under the bridge builds real frozen dataclasses instead
    of forwarding the API's JSON.
    """
    _, minted = buyer_session
    seen: dict[str, Any] = {}

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del turn
        seen["tools"] = list(bound.tools.names)  # type: ignore[union-attr]
        seen["facts"] = dict(message.facts)
        seen["session"] = session.session_id
        state = _Context({})
        search = await _tool(bound, "search").func(query="milk", tool_context=state)
        seen["search"] = search
        return SpecialistReply(text="Your catalogue is read and your milk is found.")

    result = _shopping_turn(api_app, minted, script)

    # Every roster row built for shopping
    assert seen["tools"] == [
        "search",
        "product",
        "basket_propose_line",
        "basket_get",
        "present_products",
        "present_basket",
    ]
    assert "items" in seen["search"] and "allowed_skus" in seen["search"]

    # This service's ledger: the panel's chips, under this service's own tool names.
    assert [call.name for call in result.tool_calls] == ["catalog.search"]
    assert all(call.ok for call in result.tool_calls)
    assert result.denials == ()
    assert result.specialist is Specialist.SHOPPING
    assert result.reply == "Your catalogue is read and your milk is found."

    # The structured block is the one the panel already renders, plus the bridge's receipt.
    assert result.structured is not None
    assert result.structured["kind"] == "products"
    assert result.structured["bridge"]["runtime"] == "agent_runtime"
    assert result.structured["bridge"]["tools_unbuilt"] == []
    assert result.structured["bridge"]["corrections"] == []

    # The session the model saw is derived from the bound principal and nothing else: no
    # request context reaches the bridge, so the id is the delegation chain the API minted.
    assert seen["session"] == f"session:{minted.session_id}/razorai/shopping"
    assert set(seen["facts"]) == {"language", "modality", "clock_hour_utc"}


def test_the_post_check_runs_and_drops_a_figure_no_tool_returned(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """An invented total does not reach the buyer, and a counted one does.

    This is the non-negotiable. A bridged turn that skipped the post-check would be worse
    than the template it replaces and invisibly so, because an invented amount reads exactly
    like a counted one. Both sentences are asserted in one turn so the check cannot pass by
    dropping everything.
    """
    _, minted = buyer_session
    invented = "You have captured ₹4,20,000.00 this month."
    grounded = "Amul Taaza Toned Milk 500 ml is 28.00 INR."

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        await _tool(bound, "product").func(sku="AMUL-DAIRY-001", tool_context=_Context({}))
        return SpecialistReply(text=f"{grounded} {invented}")

    result = _shopping_turn(api_app, minted, script)
    assert "4,20,000" not in result.reply, "an amount no tool returned reached the buyer"
    assert "28.00" in result.reply
    assert result.structured is not None
    assert "ungrounded_sentences_dropped" in result.structured["bridge"]["corrections"]


def test_an_empty_grounding_ledger_drops_a_correct_amount(api_app: FastAPI) -> None:
    """The check above is not vacuous, and the wrapper-shaped bridge would have shipped this.

    ``verify_reply`` reads ``TurnContext.ledger``, which is filled only by
    ``agent_runtime``'s payload builders over its own frozen dataclasses. A bridge that
    wrapped this service's ``ToolExecutor`` as tool closures would have preserved the ledger
    the *panel* reads and left this one empty -- and an empty grounding ledger does not fail
    open. It drops every sentence naming a figure, so every grounded turn would have
    answered with the withheld-items template.

    Asserted here rather than described, because it is the failure the shape of the bridge
    exists to prevent, and a later refactor toward the cheaper shape should go red.
    """
    del api_app
    correct = "The total is ₹1,250.00."
    empty = verify_reply(correct, GroundingLedger(), currency="INR")
    assert "1,250.00" not in empty.reply
    assert empty.rewritten

    knows = GroundingLedger()
    knows.record_money(_money(125000))
    kept = verify_reply(correct, knows, currency="INR")
    assert kept.reply == correct
    assert not kept.rewritten


def _money(minor: int) -> Any:
    from commerce_domain import Money

    return Money(minor, "INR")


# ------------------------------------------------------------------- refusals


def test_a_guardrail_hold_reaches_the_panel_s_ledger(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """A proposal staged from figures nobody read is held, and the buyer sees the hold.

    The hold happens entirely inside ``agent_runtime``: the tool checks the conversation's
    provenance record and returns before any backend read, so this service's executor is
    never asked and, without the bridge mirroring it, the refusal would exist only in a
    model's context window.
    """
    _, minted = buyer_session

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        held = await _tool(bound, "basket_propose_line").func(
            sku="NONEXISTENT-SKU", quantity=1, tool_context=_Context({})
        )
        assert held["ok"] is False
        assert held["reason_key"] == "sku_not_returned"
        return SpecialistReply(text="I have not seen that SKU yet, so I proposed nothing.")

    result = _shopping_turn(api_app, minted, script, message="hello")
    chips = [(call.name, call.ok, call.reason_key) for call in result.tool_calls]
    assert chips == [("basket_propose_line", False, "sku_not_returned")]
    assert result.denials == (), "a provenance guardrail is not a capability refusal"


def test_a_capability_denial_reaches_the_panel_s_ledger_as_a_denial(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """A tool the model was never bound is denied by the gate, and denied in the response.

    The gate is the factory's own and it is the same callable the ADK adapter registers, so
    this exercises the real refusal rather than a stand-in for one. It never runs the tool,
    which is why nothing about it can reach this service's executor by itself.
    """
    _, minted = buyer_session

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        toolset = bound.tools
        assert isinstance(toolset, BoundToolset)
        # A tool that exists on another specialist's roster, so the refusal is "not bound
        # to you" rather than "no such tool". The checkout submit used to stand here, and
        # it now tests a stronger and different thing -- there is no submit tool anywhere
        # on any roster, so it refuses as unregistered before binding is consulted.
        refused = toolset.gate(_NamedTool("checkout_create"), {"checkout_id": "c1"}, _Context({}))
        assert refused, "the gate must refuse a tool this specialist was never bound"
        return SpecialistReply(text="I cannot open checkouts.")

    result = _shopping_turn(api_app, minted, script)
    assert [(d.tool, d.reason_key) for d in result.denials] == [
        ("checkout_create", "tool_not_bound")
    ]
    assert [(c.name, c.denied) for c in result.tool_calls] == [("checkout_create", True)]


class _NamedTool:
    """The one attribute the capability gate reads off a tool: its name."""

    def __init__(self, name: str) -> None:
        self.name = name


# -------------------------------------------------------------- the fallback holds


def test_a_raising_model_is_a_deterministic_answer_and_not_a_500(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """Specification 30, over the real bridge: preserve state, answer deterministically.

    The reply is the deterministic one led by the sentence that names the missing layer, and
    the tool log is what really happened -- the reads the fallback made, not the ones the
    model was about to.
    """
    _, minted = buyer_session

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        raise RuntimeError("Vertex is having a day")

    result = _shopping_turn(api_app, minted, script)
    assert result.specialist is Specialist.SHOPPING
    assert result.reply.startswith("The reasoning layer is unavailable")
    assert [call.name for call in result.tool_calls] == ["catalog.search"]
    assert result.structured is not None
    assert result.structured["kind"] == "products"
    assert "bridge" not in result.structured, "the deterministic runner answered this turn"


def test_a_wiring_defect_logs_at_error_while_an_outage_logs_at_warning(
    api_app: FastAPI,
    buyer_session: tuple[TestClient, MintedSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two causes of the deterministic fallback are one reply and two log levels.

    Specification 30 gives the buyer the same answer whether the model went missing or the
    roster was miswired -- the deterministic reply led by the sentence that names the missing
    layer -- and that is deliberate, so both paths are asserted to produce it unchanged. What
    must differ is the operator's signal: a transient outage self-heals and is a WARNING, but
    an empty toolset is a capability/roster mismatch that no retry fixes, so it is raised to
    ERROR and named as a configuration defect. Left at the same level with the same text, the
    defect hid behind every Vertex blip for the nine hours it took to find by hand; this test
    is the guard that it cannot slide back down.
    """
    _, minted = buyer_session

    async def outage(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        raise RuntimeError("Vertex is having a day")

    async def miswired(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        # The exact shape agent_bridge raises when build_toolset returns nothing: the
        # specialist named, the capabilities it held, and the tools none of them build.
        raise BridgeUnavailableError(
            "shopping bound to no tools: its principal holds "
            "['catalogue.read', 'basket.write'], which builds none of "
            "['search']"
        )

    with caplog.at_level(logging.WARNING, logger="commerce_api.agent"):
        caplog.clear()
        outage_result = _shopping_turn(api_app, minted, outage)
        outage_records = [r for r in caplog.records if r.name == "commerce_api.agent"]

        caplog.clear()
        defect_result = _shopping_turn(api_app, minted, miswired)
        defect_records = [r for r in caplog.records if r.name == "commerce_api.agent"]

    # The buyer-facing reply is identical for both causes -- the product decision does not
    # change with the reason, only the log does.
    assert outage_result.reply.startswith("The reasoning layer is unavailable")
    assert defect_result.reply.startswith("The reasoning layer is unavailable")
    assert defect_result.reply == outage_result.reply

    # The outage is a WARNING and says nothing about configuration.
    outage_fallback = [r for r in outage_records if "fell back to the deterministic" in r.message]
    assert outage_fallback, "the outage should log the deterministic-fallback line"
    assert all(r.levelno == logging.WARNING for r in outage_fallback)

    # The wiring defect is an ERROR, names itself a defect rather than a model failure, and
    # carries the specialist and the tool mismatch the exception already spelt out.
    defect_error = [r for r in defect_records if r.levelno >= logging.ERROR]
    assert defect_error, "the empty-toolset defect must log at ERROR, not WARNING"
    defect_message = defect_error[0].getMessage()
    assert "wiring defect" in defect_message
    assert "not a model failure" in defect_message
    assert "shopping bound to no tools" in defect_message
    # And it does NOT masquerade as the ordinary outage line.
    assert not any(
        "fell back to the deterministic" in r.message and r.levelno == logging.WARNING
        for r in defect_records
    )


def test_model_reached_tracks_a_real_answer_and_a_real_outage_but_not_a_wiring_defect(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """The fact ``GET /v1/config`` cannot get from configuration: has a turn ever landed.

    ``bridged: true`` is set the moment ``AdkSpecialistRunner`` is constructed -- three
    environment variables read, no network call, no credential touched -- so it is true on
    a machine with Vertex configured and no Application Default Credentials on it, the
    exact process this test exists to distinguish from a healthy one. ``model_reached``
    is the fact construction cannot know: whether a call to the model has actually
    returned. It has to be proven turn by turn, on the bridge object itself, which is why
    this test builds its own bridge rather than going through ``_shopping_turn`` -- that
    helper constructs a fresh one internally and this assertion needs to read the SAME
    object after the call.

    A wiring defect must leave the flag exactly as it found it: an empty toolset says
    nothing about whether the model would have answered had it been asked correctly, so
    it must never report the ``false`` a genuine outage earns.
    """
    _, minted = buyer_session

    async def answers(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        return SpecialistReply(text="Milk is in stock.", structured={})

    async def outage(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        raise RuntimeError("Vertex is having a day")

    async def miswired(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        raise BridgeUnavailableError(
            "shopping bound to no tools: its principal holds [], which builds none of []"
        )

    def run_with(runner: Runner) -> tuple[agent_service.TurnResult, SpecialistBridge]:
        bridge = SpecialistBridge(runner)
        ctx = _context(minted)
        with session_scope_for(api_app.state.settings.database_url_app) as session:
            result = run_turn(
                session,
                ctx,
                api_app.state.merchants,
                copilot=Copilot.BUYER,
                message="milk",
                locale="en",
                cart_id=None,
                checkout_id=None,
                order_id=None,
                runner=bridge,
            )
        return result, bridge

    # Never asked: null, not a guess dressed as one of the two booleans.
    fresh = SpecialistBridge(answers)
    assert fresh.model_reached is None

    _, bridge = run_with(answers)
    assert bridge.model_reached is True

    _, bridge = run_with(outage)
    assert bridge.model_reached is False

    # The defect leaves a fresh bridge's null exactly as it found it.
    _, bridge = run_with(miswired)
    assert bridge.model_reached is None


def test_a_model_that_says_nothing_is_the_fallback_template_not_a_blank_bubble(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    _, minted = buyer_session

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        return SpecialistReply(text="   ")

    result = _shopping_turn(api_app, minted, script)
    assert result.reply.strip()
    assert result.structured is not None
    assert "fallback_rendered" in result.structured["bridge"]["corrections"]


def test_a_slow_model_is_abandoned_and_answered_deterministically(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """The timeout the harness owns, applied here because a ``TurnRunner`` bypasses it.

    Without this the turn would hold a worker thread for as long as the model wanted one,
    on a single-worker process.
    """
    _, minted = buyer_session

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        await asyncio.sleep(5)
        return SpecialistReply(text="too late")

    ctx = _context(minted)
    with session_scope_for(api_app.state.settings.database_url_app) as session:
        result = run_turn(
            session,
            ctx,
            api_app.state.merchants,
            copilot=Copilot.BUYER,
            message="milk",
            locale="en",
            cart_id=None,
            checkout_id=None,
            order_id=None,
            runner=SpecialistBridge(script, timeout_s=0.05),
        )
    assert "too late" not in result.reply
    assert result.reply.startswith("The reasoning layer is unavailable")


def test_an_empty_toolset_raises_rather_than_answering_from_nothing(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """A vocabulary mismatch is loud. It is the buyer side's whole problem, in one assert.

    A principal whose capability strings Registry A does not know binds to nothing, and
    ``build_toolset`` then returns a toolset with no tools rather than failing: the model
    would be given an ``LlmAgent`` with nothing to call and would answer confidently from
    nothing. The bridge refuses instead, and the turn is deterministic.
    """
    _, minted = buyer_session
    principal = AgentPrincipal(
        principal_id=f"session:{minted.session_id}",
        tenant_id=minted.tenant_id,
        actor_type=ActorType.BUYER,
        agent_role="shopping",
        capabilities=frozenset(),
    )

    async def never_called(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:  # pragma: no cover - the bridge refuses before this
        del bound, message, turn, session
        raise AssertionError("the bridge built tools for a principal that holds none")

    with session_scope_for(api_app.state.settings.database_url_app) as session:
        ctx = _context(minted)
        tools = ToolExecutor(
            session=session,
            ctx=ctx,
            registry=api_app.state.merchants,
            principal=principal,
            specialist=Specialist.SHOPPING,
            language=Language.EN,
            ledger=TurnLedger(),
        )
        bridge = SpecialistBridge(never_called)
        with pytest.raises(BridgeUnavailableError, match="bound to no tools"):
            bridge.run(
                TurnInput(Copilot.BUYER, "milk", Language.EN),
                Route(Specialist.SHOPPING, "default_shopping"),
                tools,
            )


# ------------------------------------------------------- what the bridge does not do


def test_a_non_bridged_buyer_specialist_keeps_the_deterministic_runner_and_is_not_told_otherwise(
    api_app: FastAPI, auth_client: TestClient
) -> None:
    """A CHECKOUT turn through an attached bridge is the deterministic answer, unannounced.

    Shopping is bridged now, so the specialist that proves the fall-through is Checkout: it
    is a buyer specialist the bridge does not answer, because this service mints no
    ``checkout.read`` string and a bridged Checkout would bind with no ``checkout_get``. The
    bridge delegates rather than raising, because there is no model path for Checkout to have
    failed: prefixing the reply with the sentence that names a missing reasoning layer would
    report an outage that did not happen.
    """

    async def never_called(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:  # pragma: no cover - checkout is not bridged
        del bound, message, turn, session
        raise AssertionError("the bridge ran a model for a specialist it does not bridge")

    api_app.state.agent_runner = SpecialistBridge(never_called)
    try:
        # "pay now" routes to Checkout; with no checkout or cart the deterministic runner
        # answers the "need a checkout" template, which is enough to prove the fall-through.
        response = auth_client.post("/v1/agent/turn", json={"message": "pay now", "locale": "en"})
    finally:
        api_app.state.agent_runner = None
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specialist"] == "checkout"
    assert "reasoning layer is unavailable" not in body["reply"]
    assert body["reply"].strip()
    assert "bridge" not in (body["structured"] or {})
    assert set(BRIDGED_SPECIALISTS) == {Specialist.SHOPPING}


def test_the_router_serialises_a_bridged_turn(api_app: FastAPI, auth_client: TestClient) -> None:
    """The wiring, once, over HTTP: ``app.state`` to the response body."""

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        await _tool(bound, "search").func(query="milk", tool_context=_Context({}))
        return SpecialistReply(text="I read the shelf.")

    api_app.state.agent_runner = SpecialistBridge(script)
    try:
        response = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})
    finally:
        api_app.state.agent_runner = None
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "I read the shelf."
    assert body["specialist"] == "shopping"
    assert [call["name"] for call in body["tool_calls"]] == ["catalog.search"]
    assert body["structured"]["kind"] == "products"
    assert body["structured"]["bridge"]["specialist"] == "shopping"


# --------------------------------------------------------------- attachment
#
# Every call below passes ``allow_ambient_env=True`` deliberately: this section is what
# tests the ambient-attachment logic itself -- Vertex configured or not, a runner that
# builds or explodes -- so it is the one place in the suite that is SUPPOSED to read the
# real environment, the same way ``create_app()``'s real boot path does. That is also why
# the first test below still ``delenv``s the three Vertex variables explicitly rather than
# trusting the flag alone: a developer's own ADC would otherwise pass it by accident.


def test_the_bridge_is_not_constructed_without_vertex(
    monkeypatch: pytest.MonkeyPatch, api_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """No Vertex, no runner, and the reason is in the log rather than in a reply.

    ``create_app`` runs :func:`~commerce_api.app._attach_specialist_runner` on the way up,
    and the test suite runs without the Vertex environment, so the app under test already
    proves the negative. The environment is cleared explicitly as well, because a machine
    with ADC configured would otherwise pass this by accident.

    The fallback is logged at WARNING, not INFO: a degraded process that still answers is
    the trap this function exists to avoid, so the mode belongs in a stream an operator
    skims. The line names Vertex as the reason -- distinct from an unimportable runtime or
    a runner that raised -- and ``reasoning_specialists`` is the empty tuple, which is the
    same fact ``/v1/config`` serves.
    """
    assert api_app.state.agent_runner is None
    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    fresh = FastAPI()
    with caplog.at_level("INFO", logger="commerce_api.app"):
        app_module._attach_specialist_runner(fresh, allow_ambient_env=True)
    assert fresh.state.agent_runner is None
    assert fresh.state.reasoning_specialists == ()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a deterministic-only fallback must be visible at WARNING"
    assert any("Vertex is not configured" in r.getMessage() for r in warnings)


def test_a_runner_that_will_not_build_does_not_stop_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction failure is a fallback, not a boot failure.

    The whole point of reading the runner off ``app.state`` is that a process with no model
    still serves every endpoint. A raising constructor must therefore leave the attribute at
    ``None`` and say so, rather than take the service down at import.
    """
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    from agent_runtime.runtime_adk import adapter

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no credentials on this machine")

    monkeypatch.setattr(adapter, "AdkSpecialistRunner", explode)
    fresh = FastAPI()
    app_module._attach_specialist_runner(fresh, allow_ambient_env=True)
    assert fresh.state.agent_runner is None


def test_the_bridge_names_the_model_backed_specialists_when_it_attaches(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing feature announces itself; so does a present one.

    An operator reading the log must be able to say which specialists a model answers
    without inferring it from a reply.
    """
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    from agent_runtime.runtime_adk import adapter

    class Idle:
        def __init__(self, **_kwargs: Any) -> None: ...

        async def __call__(self, *_args: Any) -> SpecialistReply:  # pragma: no cover
            raise AssertionError("attachment must not call a model")

    monkeypatch.setattr(adapter, "AdkSpecialistRunner", Idle)
    fresh = FastAPI()
    with caplog.at_level("INFO", logger="commerce_api.app"):
        app_module._attach_specialist_runner(fresh, allow_ambient_env=True)
    assert isinstance(fresh.state.agent_runner, SpecialistBridge)
    assert any("shopping" in record.getMessage() for record in caplog.records)
    # The same fact the log line states, in the shape ``/v1/config`` serves: the sorted
    # specialist values the bridge answers, recorded on state so a health route need not
    # import the bridge to report the mode.
    assert fresh.state.reasoning_specialists == tuple(sorted(s.value for s in BRIDGED_SPECIALISTS))


# ------------------------------------------------------------------- source rules


def test_the_bridge_imports_no_model_runtime() -> None:
    """The constraint that lets ``routers.agent`` stay free of ``google.*``.

    The runner arrives as a protocol object. If this module ever imported one, the router
    that reads it off ``app.state`` would inherit the dependency through the service package
    and the lazy import in ``app.py`` would stop being the only door.
    """
    source = inspect.getsource(agent_bridge)
    assert "import google" not in source and "from google" not in source


def test_no_tool_the_bridge_offers_takes_an_identity_parameter(
    api_app: FastAPI, buyer_session: tuple[TestClient, MintedSession]
) -> None:
    """``IDENTITY_PARAMETER_NAMES`` is the factory's rule and the bridge adds no tool.

    Asserted over the toolset the bridge actually hands a model, because the observation
    wrapper it installs is the one place a signature could have been changed. It preserves
    the factory's own closure through ``functools.wraps``, so ``inspect.signature`` -- which
    is what the factory's schema check and the model's function declaration both read --
    still sees the closure's parameters.
    """
    from agent_runtime.capabilities.tools import IDENTITY_PARAMETER_NAMES

    _, minted = buyer_session
    offered: dict[str, tuple[str, ...]] = {}

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        assert isinstance(bound.tools, BoundToolset)
        for tool in bound.tools:
            offered[tool.name] = tool.parameters
        return SpecialistReply(text="nothing to say")

    _shopping_turn(api_app, minted, script, message="milk")
    assert offered, "the script never saw a toolset"
    for name, parameters in offered.items():
        leaked = IDENTITY_PARAMETER_NAMES & set(parameters)
        assert not leaked, f"{name} exposes {sorted(leaked)}"
    assert "merchant_id" in IDENTITY_PARAMETER_NAMES

    # These are the factory's own parameters for shopping, read back through the wrapper.
    assert offered == {
        "search": ("query", "limit"),
        "product": ("sku",),
        "basket_get": (),
        "basket_propose_line": ("sku", "quantity"),
        "present_products": ("skus",),
        "present_basket": (),
    }


def test_the_bridge_refuses_a_running_event_loop_out_loud() -> None:
    """The hazard a future ``async def`` handler would introduce, named rather than hidden.

    ``asyncio.run`` would raise on its own; its message is about the call, and the fix is
    about where the await belongs.
    """

    async def script(*_args: Any) -> SpecialistReply:  # pragma: no cover - never reached
        raise AssertionError("unreachable")

    async def inside_a_loop() -> None:
        bridge = SpecialistBridge(script)
        with pytest.raises(BridgeUnavailableError, match="synchronous request thread"):
            bridge.run(
                TurnInput(Copilot.BUYER, "hello", Language.EN),
                Route(Specialist.SHOPPING, "default_shopping"),
                _unused_executor(),
            )

    asyncio.run(inside_a_loop())


def _unused_executor() -> Any:
    """A stand-in the loop guard refuses before touching. Never dereferenced."""
    return object()


def test_bind_narrows_again_on_the_way_into_the_runtime(
    buyer_session: tuple[TestClient, MintedSession],
) -> None:
    """The specialist principal the bridge builds tools from is derived, never asserted.

    ``harness.base.bind`` recomputes ``harness ∩ ROLE_CAPABILITIES`` through ``subset_for``,
    so a capability this service granted that Registry A does not name is dropped before a
    tool exists, and any widening raises. Hand-building the ``Binding`` would have skipped
    the one check a prompt-injection attack would most like to skip.
    """
    from dataclasses import replace

    from agent_runtime.harness.base import ROLE_CAPABILITIES
    from agent_runtime.harness.base import bind as harness_bind
    from commerce_api.services.agent_bridge import registry_a_capabilities

    _, minted = buyer_session
    ctx = _context(minted)
    api_binding = bind(ctx, Copilot.BUYER)
    shopping = api_binding.principal_for(Specialist.SHOPPING)
    translated = replace(shopping, capabilities=registry_a_capabilities(shopping.capabilities))
    runtime = harness_bind(translated, Specialist.SHOPPING)
    assert runtime.capabilities == translated.capabilities
    assert runtime.capabilities <= ROLE_CAPABILITIES[Specialist.SHOPPING]
    assert shopping.principal_id in runtime.principal.delegation_chain

    widened = translated.subset_for("shopping", frozenset())
    with pytest.raises(ValueError, match="would gain capabilities"):
        widened.subset_for("shopping", frozenset({"catalog.search"}))


def test_the_bridge_holds_no_database_handle() -> None:
    """The backend under the bridge can reach a row only by asking the executor.

    Stated as an object-graph property rather than a discipline: the backend's only
    collaborator is the :class:`~agent_service.ToolExecutor`, so "every tool call is gated"
    is true because there is nothing else to call.
    """
    source = inspect.getsource(agent_bridge)
    for forbidden in ("Session", "MerchantRegistry", "catalogue_service", "cart_service"):
        assert f"import {forbidden}" not in source
    assert "sqlalchemy" not in source

    backend = agent_bridge._BuyerReads.__init__
    assert list(inspect.signature(backend).parameters) == ["self", "tools"]


def test_the_seam_the_docstring_used_to_lie_about() -> None:
    """``AdkSpecialistRunner`` has no ``run``, and the bridge is what supplies one.

    Kept as an assertion because the false sentence lived in a docstring for as long as it
    did precisely because nothing checked it. If the runtime ever grows a ``run`` method the
    two contracts have merged and this file should be reconsidered, not quietly kept.
    """
    from agent_runtime.harness.base import SpecialistRunner

    assert hasattr(SpecialistBridge, "run")
    assert not hasattr(SpecialistRunner, "run")

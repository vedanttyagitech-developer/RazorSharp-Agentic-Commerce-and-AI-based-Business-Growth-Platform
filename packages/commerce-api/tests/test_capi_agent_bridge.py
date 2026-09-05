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
from collections.abc import Callable, Iterator
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
from commerce_api.security import hash_token, mint_token
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
from commerce_domain import uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from test_capi_proposal_contract import build_golden, float_paths
from transaction_kernel import ActorType, AgentPrincipal

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")

_MERCHANT_CAPS = (
    '["merchant.catalogue_health.read","merchant.inventory_anomalies.read",'
    '"merchant.checkout_metrics.read","merchant.growth_proposal.create"]'
)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def operator(
    capi_admin_engine: Engine,
    seeded_tenant: SeededTenant,
    api_app: FastAPI,
) -> Iterator[tuple[TestClient, MintedSession]]:
    """An OPERATOR session holding exactly the four merchant Registry A capabilities.

    Written directly as the database owner because the demo mint route issues BUYER and
    AGENT sessions only. The same shape ``test_capi_agent`` uses; duplicated rather than
    imported so a change to one file's fixture cannot silently retune the other's subject.
    """
    token = mint_token()
    session_id = uuid7()
    with capi_admin_engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(seeded_tenant.tenant_id)})
        conn.execute(
            text(
                "INSERT INTO api_sessions "
                "(id, tenant_id, merchant_id, token_hash, buyer_ref, actor_type, "
                "capabilities, expires_at) VALUES "
                "(:id, :tenant, :merchant, :hash, :ref, 'OPERATOR', "
                "CAST(:caps AS jsonb), now() + CAST(:ttl AS interval))"
            ),
            {
                "id": session_id,
                "tenant": seeded_tenant.tenant_id,
                "merchant": seeded_tenant.merchant_id,
                "hash": hash_token(token),
                "ref": "operator-bridge",
                "caps": _MERCHANT_CAPS,
                "ttl": str(timedelta(hours=1)),
            },
        )
    minted = MintedSession(
        token=token,
        session_id=session_id,
        tenant_id=seeded_tenant.tenant_id,
        merchant_id=seeded_tenant.merchant_id,
        buyer_ref="operator-bridge",
        actor_type="OPERATOR",
    )
    with TestClient(api_app, headers=minted.auth_header) as client:
        yield client, minted


Runner = Callable[[BoundSpecialist, SpecialistInput, TurnContext, CopilotSession], Any]


def _growth_turn(
    api_app: FastAPI,
    minted: MintedSession,
    runner: Runner,
    *,
    message: str = "how are checkouts doing",
) -> agent_service.TurnResult:
    """Run one merchant turn through the real bridge over a scripted specialist runner.

    Called in-process rather than over HTTP so the assertions can be about the
    :class:`~agent_service.TurnResult` itself -- its ledger, its denials, its structured
    block -- rather than about the projection of it the router serialises. The router is
    exercised separately, once, where the question is whether the wiring holds.
    """
    ctx = _context(minted)
    with session_scope_for(api_app.state.settings.database_url_app) as session:
        return run_turn(
            session,
            ctx,
            api_app.state.merchants,
            copilot=Copilot.MERCHANT,
            message=message,
            locale="en",
            basket_id=None,
            checkout_id=None,
            order_id=None,
            runner=SpecialistBridge(runner),
        )


def _context(minted: MintedSession) -> RequestContext:
    principal = AgentPrincipal(
        principal_id=f"session:{minted.session_id}",
        tenant_id=minted.tenant_id,
        actor_type=ActorType.OPERATOR,
        merchant_id=minted.merchant_id,
        capabilities=frozenset(agent_service.MERCHANT_AGENT_CAPABILITIES),
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
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """The whole seam, once: factory tools, this service's ledger, the grounding ledger.

    The two ledgers answer different questions and both must be filled by the same turn.
    This service's :class:`~agent_service.TurnLedger` is what the panel draws chips from.
    ``TurnContext.ledger`` is what :func:`~agent_runtime.grounding.verify_reply` reads, and
    it is filled by ``agent_runtime``'s own payload builders rather than by anything here --
    which is the reason the backend under the bridge builds real frozen dataclasses instead
    of forwarding the API's JSON.
    """
    _, minted = operator
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
        health = await _tool(bound, "catalogue_health_read").func(tool_context=state)
        metrics = await _tool(bound, "checkout_metrics_read").func(tool_context=state)
        seen["health"] = health
        seen["metrics"] = metrics
        return SpecialistReply(text="Your catalogue is read and your orders are counted.")

    result = _growth_turn(api_app, minted, script)

    # Every roster row built: the growth vocabulary matches on both sides, which is the
    # property that makes this the specialist the bridge starts with.
    assert seen["tools"] == [
        "catalogue_health_read",
        "inventory_anomalies_read",
        "checkout_metrics_read",
        "growth_proposal_create",
        "present_metrics",
    ]
    assert seen["health"]["ok"] is True
    assert seen["health"]["breakdown_is_whole"] is True
    assert seen["metrics"]["captured"]["measured"] is False, (
        "this service sums no amount on an agent turn, so the model must be told the "
        "figure was not measured rather than handed a zero"
    )

    # This service's ledger: the panel's chips, under this service's own tool names.
    assert [call.name for call in result.tool_calls] == [
        "merchant.catalogue_health.read",
        "merchant.checkout_metrics.read",
    ]
    assert all(call.ok for call in result.tool_calls)
    assert result.denials == ()
    assert result.specialist is Specialist.GROWTH
    assert result.reply == "Your catalogue is read and your orders are counted."

    # The structured block is the one the panel already renders, plus the bridge's receipt.
    assert result.structured is not None
    assert result.structured["kind"] == "checkout_metrics"
    assert result.structured["synthetic"] is True
    assert result.structured["bridge"]["runtime"] == "agent_runtime"
    assert result.structured["bridge"]["tools_unbuilt"] == []
    assert result.structured["bridge"]["corrections"] == []

    # The session the model saw is derived from the bound principal and nothing else: no
    # request context reaches the bridge, so the id is the delegation chain the API minted.
    assert seen["session"] == f"session:{minted.session_id}/merchant_copilot/growth"
    assert set(seen["facts"]) == {"language", "modality", "clock_hour_utc"}


def test_the_post_check_runs_and_drops_a_figure_no_tool_returned(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """An invented total does not reach the merchant, and a counted one does.

    This is the non-negotiable. A bridged turn that skipped the post-check would be worse
    than the template it replaces and invisibly so, because an invented amount reads exactly
    like a counted one. Both sentences are asserted in one turn so the check cannot pass by
    dropping everything.
    """
    _, minted = operator
    invented = "You have captured ₹4,20,000.00 this month."
    grounded = "Your catalogue has been read."

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        await _tool(bound, "catalogue_health_read").func(tool_context=_Context({}))
        return SpecialistReply(text=f"{grounded} {invented}")

    result = _growth_turn(api_app, minted, script)
    assert "4,20,000" not in result.reply, "an amount no tool returned reached the merchant"
    assert grounded in result.reply
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


def test_a_staged_proposal_reaches_the_panel_in_the_shape_the_console_parses(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """The one tool result no ledger carries, and the reason the toolset is wrapped at all.

    Every read a bridged turn makes reaches the panel through the executor. A proposal does
    not: ``growth_proposal_create`` assembles the record and hands it to the model, and the
    runtime keeps only a summary -- lever, id, subject -- which is right for an evidence
    record and is not the record a merchant applies. So the bridge watches the toolset's
    own closures and recognises a proposal by its ``kind``.

    Held against the same golden fixture the deterministic runner is held against, key for
    key, because the whole reason Growth is the specialist this bridge starts with is that
    ``agent_runtime.capabilities.proposals`` already emits one record for both halves. A
    console that switches on ``change.body.kind`` must not be told that the model-backed
    turn spells it something else.
    """
    _, minted = operator

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        # One context across the three calls, because that is what a conversation is: the
        # provenance record the guardrail reads lives in it, and a fresh one per call would
        # be three first turns and a held proposal.
        state = _Context({})
        await _tool(bound, "inventory_anomalies_read").func(tool_context=state)
        await _tool(bound, "catalogue_health_read").func(tool_context=state)
        staged = await _tool(bound, "growth_proposal_create").func(
            lever="top_seller_out_of_stock", tool_context=state
        )
        assert staged["kind"] == "proposal"
        return SpecialistReply(text="I have staged a restock for you to apply.")

    result = _growth_turn(
        api_app, minted, script, message="propose what to do about the stock anomalies"
    )
    assert result.structured is not None
    proposal = result.structured["proposal"]
    golden = build_golden()
    assert proposal.keys() >= golden.keys()
    assert proposal["kind"] == "proposal"
    assert proposal["lever"] == golden["lever"]
    assert proposal["metric"] == golden["metric"]
    assert proposal["gate"] == golden["gate"]
    assert proposal["where"] == golden["where"]
    assert proposal["evidence"]["read_by"] == golden["evidence"]["read_by"]
    assert proposal["change"]["body"]["kind"] == golden["change"]["body"]["kind"]
    # The one field no agent sets, model-backed or not. A person on the console does.
    assert proposal["applied"] is False
    assert float_paths(proposal, "proposal") == []

    # The subject is a row this turn's read returned, and the read is in the panel's log.
    assert [call.name for call in result.tool_calls] == [
        "merchant.inventory_anomalies.read",
        "merchant.catalogue_health.read",
        "merchant.catalogue_health.read",
        "merchant.inventory_anomalies.read",
    ], "the proposal re-reads its own evidence, and every read is a chip"
    assert result.structured["kind"] == "inventory_anomalies"


# ------------------------------------------------------------------- refusals


def test_a_guardrail_hold_reaches_the_panel_s_ledger(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """A proposal staged from figures nobody read is held, and the merchant sees the hold.

    The hold happens entirely inside ``agent_runtime``: the tool checks the conversation's
    provenance record and returns before any backend read, so this service's executor is
    never asked and, without the bridge mirroring it, the refusal would exist only in a
    model's context window.
    """
    _, minted = operator

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        held = await _tool(bound, "growth_proposal_create").func(
            lever="top_seller_out_of_stock", tool_context=_Context({})
        )
        assert held["ok"] is False
        assert held["reason_key"] == "metric_not_read"
        return SpecialistReply(text="I have not read the inventory yet, so I proposed nothing.")

    result = _growth_turn(
        api_app, minted, script, message="propose what to do about the stock anomalies"
    )
    chips = [(call.name, call.ok, call.reason_key) for call in result.tool_calls]
    # Under the runtime's own name, because this service has no tool that stages a
    # proposal. The three merchant *reads* are one read under two names and are mirrored
    # under this service's spelling; giving ``growth_proposal_create`` an API name it does
    # not have would put a tool in the panel's log that ``agent_service.TOOLS`` refuses.
    assert chips == [("growth_proposal_create", False, "metric_not_read")]
    assert "growth_proposal_create" not in agent_service.TOOLS
    assert result.denials == (), "a provenance guardrail is not a capability refusal"


def test_a_capability_denial_reaches_the_panel_s_ledger_as_a_denial(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """A tool the model was never bound is denied by the gate, and denied in the response.

    The gate is the factory's own and it is the same callable the ADK adapter registers, so
    this exercises the real refusal rather than a stand-in for one. It never runs the tool,
    which is why nothing about it can reach this service's executor by itself.
    """
    _, minted = operator

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        toolset = bound.tools
        assert isinstance(toolset, BoundToolset)
        refused = toolset.gate(
            _NamedTool("basket_set_line"), {"sku": "AMUL-DAIRY-001"}, _Context({})
        )
        assert refused, "the gate must refuse a tool this specialist was never bound"
        return SpecialistReply(text="I cannot add anything to a basket from this console.")

    result = _growth_turn(api_app, minted, script)
    assert [(d.tool, d.reason_key) for d in result.denials] == [
        ("basket_set_line", "tool_not_bound")
    ]
    assert [(c.name, c.denied) for c in result.tool_calls] == [("basket_set_line", True)]


class _NamedTool:
    """The one attribute the capability gate reads off a tool: its name."""

    def __init__(self, name: str) -> None:
        self.name = name


# -------------------------------------------------------------- the fallback holds


def test_a_raising_model_is_a_deterministic_answer_and_not_a_500(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """Specification 30, over the real bridge: preserve state, answer deterministically.

    The reply is the deterministic one led by the sentence that names the missing layer, and
    the tool log is what really happened -- the reads the fallback made, not the ones the
    model was about to.
    """
    _, minted = operator

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        raise RuntimeError("Vertex is having a day")

    result = _growth_turn(api_app, minted, script)
    assert result.specialist is Specialist.GROWTH
    assert result.reply.startswith("The reasoning layer is unavailable")
    assert [call.name for call in result.tool_calls] == ["merchant.checkout_metrics.read"]
    assert result.structured is not None
    assert result.structured["kind"] == "checkout_metrics"
    assert "bridge" not in result.structured, "the deterministic runner answered this turn"


def test_a_model_that_says_nothing_is_the_fallback_template_not_a_blank_bubble(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    _, minted = operator

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del bound, message, turn, session
        return SpecialistReply(text="   ")

    result = _growth_turn(api_app, minted, script)
    assert result.reply.strip()
    assert result.structured is not None
    assert "fallback_rendered" in result.structured["bridge"]["corrections"]


def test_a_slow_model_is_abandoned_and_answered_deterministically(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """The timeout the harness owns, applied here because a ``TurnRunner`` bypasses it.

    Without this the turn would hold a worker thread for as long as the model wanted one,
    on a single-worker process.
    """
    _, minted = operator

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
            copilot=Copilot.MERCHANT,
            message="how are checkouts doing",
            locale="en",
            basket_id=None,
            checkout_id=None,
            order_id=None,
            runner=SpecialistBridge(script, timeout_s=0.05),
        )
    assert "too late" not in result.reply
    assert result.reply.startswith("The reasoning layer is unavailable")


def test_an_empty_toolset_raises_rather_than_answering_from_nothing(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """A vocabulary mismatch is loud. It is the buyer side's whole problem, in one assert.

    A principal whose capability strings Registry A does not know binds to nothing, and
    ``build_toolset`` then returns a toolset with no tools rather than failing: the model
    would be given an ``LlmAgent`` with nothing to call and would answer confidently from
    nothing. The bridge refuses instead, and the turn is deterministic.
    """
    _, minted = operator
    principal = AgentPrincipal(
        principal_id=f"session:{minted.session_id}",
        tenant_id=minted.tenant_id,
        actor_type=ActorType.OPERATOR,
        agent_role="growth",
        merchant_id=minted.merchant_id,
        # This service's buyer spelling. Registry A knows none of these strings.
        capabilities=frozenset({"catalogue.read", "basket.write"}),
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
            specialist=Specialist.GROWTH,
            language=Language.EN,
            ledger=TurnLedger(),
        )
        bridge = SpecialistBridge(never_called)
        with pytest.raises(BridgeUnavailableError, match="bound to no tools"):
            bridge.run(
                TurnInput(Copilot.MERCHANT, "how are sales", Language.EN),
                Route(Specialist.GROWTH, "default_growth"),
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
        # "pay now" routes to Checkout; with no checkout or basket the deterministic runner
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
    assert set(BRIDGED_SPECIALISTS) == {Specialist.GROWTH, Specialist.SHOPPING}


def test_the_router_serialises_a_bridged_turn(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """The wiring, once, over HTTP: ``app.state`` to the response body."""
    client, _ = operator

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, turn, session
        await _tool(bound, "inventory_anomalies_read").func(tool_context=_Context({}))
        return SpecialistReply(text="I read the shelf.")

    api_app.state.agent_runner = SpecialistBridge(script)
    try:
        response = client.post(
            "/v1/merchant/agent/turn", json={"message": "any stock anomalies?", "locale": "en"}
        )
    finally:
        api_app.state.agent_runner = None
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "I read the shelf."
    assert body["specialist"] == "growth"
    assert [call["name"] for call in body["tool_calls"]] == ["merchant.inventory_anomalies.read"]
    assert body["structured"]["kind"] == "inventory_anomalies"
    assert body["structured"]["bridge"]["specialist"] == "growth"


# --------------------------------------------------------------- attachment


def test_the_bridge_is_not_constructed_without_vertex(
    monkeypatch: pytest.MonkeyPatch, api_app: FastAPI
) -> None:
    """No Vertex, no runner, and the reason is in the log rather than in a reply.

    ``create_app`` runs :func:`~commerce_api.app._attach_specialist_runner` on the way up,
    and the test suite runs without the Vertex environment, so the app under test already
    proves the negative. The environment is cleared explicitly as well, because a machine
    with ADC configured would otherwise pass this by accident.
    """
    assert api_app.state.agent_runner is None
    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    fresh = FastAPI()
    app_module._attach_specialist_runner(fresh)
    assert fresh.state.agent_runner is None


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
    app_module._attach_specialist_runner(fresh)
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
        app_module._attach_specialist_runner(fresh)
    assert isinstance(fresh.state.agent_runner, SpecialistBridge)
    assert any("growth" in record.getMessage() for record in caplog.records)


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
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """``IDENTITY_PARAMETER_NAMES`` is the factory's rule and the bridge adds no tool.

    Asserted over the toolset the bridge actually hands a model, because the observation
    wrapper it installs is the one place a signature could have been changed. It preserves
    the factory's own closure through ``functools.wraps``, so ``inspect.signature`` -- which
    is what the factory's schema check and the model's function declaration both read --
    still sees the closure's parameters.
    """
    from agent_runtime.capabilities.tools import IDENTITY_PARAMETER_NAMES

    _, minted = operator
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

    _growth_turn(api_app, minted, script)
    assert offered, "the script never saw a toolset"
    for name, parameters in offered.items():
        leaked = IDENTITY_PARAMETER_NAMES & set(parameters)
        assert not leaked, f"{name} exposes {sorted(leaked)}"
    assert "merchant_id" in IDENTITY_PARAMETER_NAMES

    # The stronger claim, because the check above would also pass on a wrapper that had
    # replaced every signature with ``(*args, **kwargs)`` -- which exposes no identity and
    # would also hand the model a tool declaration with no arguments at all. These are the
    # factory's own parameters, read back through the wrapper.
    assert offered == {
        "catalogue_health_read": (),
        "inventory_anomalies_read": ("limit",),
        "checkout_metrics_read": (),
        "growth_proposal_create": ("lever", "sku"),
        "present_metrics": ("metric",),
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
                TurnInput(Copilot.MERCHANT, "hello", Language.EN),
                Route(Specialist.GROWTH, "default_growth"),
                _unused_executor(),
            )

    asyncio.run(inside_a_loop())


def _unused_executor() -> Any:
    """A stand-in the loop guard refuses before touching. Never dereferenced."""
    return object()


def test_bind_narrows_again_on_the_way_into_the_runtime(
    operator: tuple[TestClient, MintedSession],
) -> None:
    """The specialist principal the bridge builds tools from is derived, never asserted.

    ``harness.base.bind`` recomputes ``harness ∩ ROLE_CAPABILITIES`` through ``subset_for``,
    so a capability this service granted that Registry A does not name is dropped before a
    tool exists, and any widening raises. Hand-building the ``Binding`` would have skipped
    the one check a prompt-injection attack would most like to skip.
    """
    from agent_runtime.harness.base import ROLE_CAPABILITIES
    from agent_runtime.harness.base import bind as harness_bind

    _, minted = operator
    ctx = _context(minted)
    api_binding = bind(ctx, Copilot.MERCHANT)
    growth = api_binding.principal_for(Specialist.GROWTH)
    runtime = harness_bind(growth, Specialist.GROWTH)
    assert runtime.capabilities == growth.capabilities
    assert runtime.capabilities <= ROLE_CAPABILITIES[Specialist.GROWTH]
    assert growth.principal_id in runtime.principal.delegation_chain

    widened = growth.subset_for("growth", frozenset())
    with pytest.raises(ValueError, match="would gain capabilities"):
        widened.subset_for("growth", frozenset({"merchant.catalogue_health.read"}))


def test_the_bridge_holds_no_database_handle(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """The backend under the bridge can reach a row only by asking the executor.

    Stated as an object-graph property rather than a discipline: the backend's only
    collaborator is the :class:`~agent_service.ToolExecutor`, so "every tool call is gated"
    is true because there is nothing else to call.
    """
    del api_app, operator
    source = inspect.getsource(agent_bridge)
    for forbidden in ("Session", "MerchantRegistry", "catalogue_service", "basket_service"):
        assert f"import {forbidden}" not in source
    assert "sqlalchemy" not in source

    backend = agent_bridge._MerchantReads.__init__
    assert list(inspect.signature(backend).parameters) == ["self", "tools"]


def test_an_anomaly_kind_registry_a_has_no_word_for_is_dropped_and_counted() -> None:
    """The vocabulary seam, in the one place the two halves genuinely disagree.

    This service reports a delisted product with an empty shelf as an anomaly; Registry A
    has no ``kind`` for it, because there is nothing to restock and nothing to relist. It is
    dropped rather than renamed, because a specialist that could report a fourth kind could
    invent one -- and it is counted, so the drop is visible rather than silent.
    """
    kind = agent_bridge._anomaly_kind
    assert kind({"is_listed": True, "stock_units": 0}) == "listed_out_of_stock"
    assert kind({"is_listed": False, "stock_units": 4}) == "delisted_with_stock"
    assert kind({"is_listed": True, "stock_units": 2}) == "low_stock"
    assert kind({"is_listed": False, "stock_units": 0}) is None
    assert kind({"is_listed": True, "stock_units": True}) is None, (
        "True is an int and would have become a shelf holding one unit"
    )


def test_the_bridge_reports_no_amount_this_service_did_not_count(
    api_app: FastAPI, operator: tuple[TestClient, MintedSession]
) -> None:
    """``None`` is not ``0``, carried across the seam.

    This service sums no amount on an agent turn; retained revenue is the evidence
    endpoint's to derive, row by row. So both money figures cross as ``None`` and reach the
    model as ``measured: false`` with no number beside them, and no ``Money`` is recorded on
    the grounding ledger -- which means a specialist quoting a revenue figure has that
    sentence dropped.
    """
    _, minted = operator
    seen: dict[str, Any] = {}

    async def script(
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        del message, session
        seen["metrics"] = await _tool(bound, "checkout_metrics_read").func(
            tool_context=_Context({})
        )
        seen["amounts"] = sorted(turn.ledger.amounts_minor)
        return SpecialistReply(text="Nothing about revenue was counted here.")

    _growth_turn(api_app, minted, script)
    assert seen["metrics"]["captured"] == {"minor": None, "display": None, "measured": False}
    assert seen["metrics"]["refunded"]["measured"] is False
    assert sorted(seen["metrics"]["not_measured"]) == ["captured", "refunded"]
    assert seen["amounts"] == []


def test_the_seam_the_docstring_used_to_lie_about() -> None:
    """``AdkSpecialistRunner`` has no ``run``, and the bridge is what supplies one.

    Kept as an assertion because the false sentence lived in a docstring for as long as it
    did precisely because nothing checked it. If the runtime ever grows a ``run`` method the
    two contracts have merged and this file should be reconsidered, not quietly kept.
    """
    from agent_runtime.harness.base import SpecialistRunner

    assert hasattr(SpecialistBridge, "run")
    assert not hasattr(SpecialistRunner, "run")

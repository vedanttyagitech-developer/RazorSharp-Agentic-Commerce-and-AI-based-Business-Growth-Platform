"""Razor AI Main Agent, slice one: typed turns over the shared conversation service.

One reasoning owner (scripted here, Gemini 3.8 Flash in production), the
model-free Shopping adapter for execution, deterministic fallbacks on failure.
Every test below maps to a validation gate: single owner, zero-model
execution paths, no recursive re-entry, conversation continuity with scoped
authorization, and truthful outcomes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from agent_runtime.language import Language
from commerce_api.deps import RequestContext, session_scope_for
from commerce_api.services import agent_service
from commerce_api.services.agent_service import (
    Specialist,
    ToolExecutor,
    TurnLedger,
)
from commerce_api.services.conversation import (
    ConversationService,
    TurnRecord,
    shared_service,
)
from commerce_api.services.razor_main import (
    BUYER_TOOLS,
    FunctionCall,
    MainAgent,
    MainAgentConfig,
    ModelQuotaError,
    ModelRound,
)
from commerce_domain import ActorType, AgentPrincipal
from commerce_domain.ids import uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db.tenancy import set_tenant

from conftest import MintedSession

OP = uuid.UUID("01a09db7-0000-7000-8000-000000000001")
CONV = uuid.UUID("01a09db7-0000-7000-8000-000000000002")
MILK = "AMUL-DAIRY-001"


class Script:
    """A scripted reasoning model: rounds out, call count recorded."""

    def __init__(self, rounds: list[ModelRound]) -> None:
        self.rounds = list(rounds)
        self.calls = 0
        self.seen_histories: list[list[str]] = []

    def generate(
        self,
        *,
        system: str,
        history: list[str],
        message: str,
        tools: list[dict[str, Any]],
    ) -> tuple[ModelRound, Any]:
        self.calls += 1
        self.seen_histories.append(list(history))
        assert system and message
        assert tools, "the main agent always reasons with tools in reach"
        return self.rounds.pop(0), None

    def follow_up(
        self,
        *,
        state: Any,
        calls: list,
        results: list,
    ) -> tuple[ModelRound, Any]:
        self.calls += 1
        return self.rounds.pop(0), None


def _call(name: str, **args: Any) -> ModelRound:
    return ModelRound(text="", calls=(FunctionCall(call_id="c1", name=name, args=args),))


def _say(text: str) -> ModelRound:
    return ModelRound(text=text, calls=())


def _agent(script: Script) -> MainAgent:
    return MainAgent(
        script,
        config=MainAgentConfig(
            model="gemini-3.8-flash", thinking_level="LOW", max_output_tokens=2048
        ),
    )


def _tools(
    api_app: FastAPI,
    session: MintedSession,
    ledger: TurnLedger,
    db_session: Any,
    capabilities: frozenset[str] = frozenset({"catalogue.read", "basket.write"}),
    specialist: Specialist = Specialist.SHOPPING,
) -> ToolExecutor:
    principal = AgentPrincipal(
        principal_id="session:razor-main",
        tenant_id=session.tenant_id,
        actor_type=ActorType.AGENT,
        capabilities=capabilities,
    )
    ctx = RequestContext(
        tenant_id=session.tenant_id,
        merchant_id=session.merchant_id,
        buyer_ref=session.buyer_ref,
        principal=principal,
        correlation_id=uuid7(),
        session_id=session.session_id,
        expires_at=datetime.now(tz=UTC),
    )
    return ToolExecutor(
        session=db_session,
        ctx=ctx,
        registry=api_app.state.merchants,
        principal=principal,
        specialist=specialist,
        language=Language.EN,
        ledger=ledger,
        operation_id=OP,
    )


@pytest.fixture
def tools_pair(api_app: FastAPI, demo_session: MintedSession):
    with session_scope_for(api_app.state.settings.database_url_app) as session:
        set_tenant(session, demo_session.tenant_id)

        def make(
            capabilities: frozenset[str] = frozenset({"catalogue.read", "basket.write"}),
            specialist: Specialist = Specialist.SHOPPING,
        ) -> tuple[ToolExecutor, TurnLedger]:
            ledger = TurnLedger()
            return (
                _tools(api_app, demo_session, ledger, session, capabilities, specialist),
                ledger,
            )

        yield make


# ------------------------------------------------------------- architecture gates


class TestOneOwner:
    def test_buyer_table_is_registered_and_gated(self) -> None:
        assert set(BUYER_TOOLS) <= set(agent_service.TOOLS)
        assert "shopping.execute" in BUYER_TOOLS

    def test_no_tool_reenters_the_conversational_endpoint(self) -> None:
        import inspect

        from commerce_api.services import razor_main

        source = inspect.getsource(razor_main)
        assert "agent/turn" not in source
        assert "agent_turn" not in source
        assert "run_turn" not in source

    def test_single_configuration_owns_production_conversation(self) -> None:
        from agent_runtime.runtime_adk.model_config import metadata

        facts = metadata({})
        assert facts["model"] == "gemini-3.8-flash"
        agent = _agent(Script([_say("hi")]))
        assert agent.config.model == facts["model"]


class TestConversationService:
    def test_unknown_and_foreign_conversations_read_empty(self) -> None:
        service = ConversationService()
        view = service.view(uuid7(), tenant_id=uuid7(), actor_type="BUYER")
        assert view.history == () and view.displayed == ()

        mine = uuid7()
        tenant = uuid7()
        service.record_turn(
            mine,
            tenant_id=tenant,
            actor_type="BUYER",
            turn=TurnRecord(
                turn_id=uuid7(),
                operation_id=uuid7(),
                message="milk",
                reply="here",
                language="en",
                surface="buyer",
                skus=("A",),
            ),
            displayed=("A",),
        )
        foreign = service.view(mine, tenant_id=uuid7(), actor_type="BUYER")
        assert foreign.history == () and foreign.displayed == ()
        own = service.view(mine, tenant_id=tenant, actor_type="BUYER")
        assert [turn.message for turn in own.history] == ["milk"]
        assert own.displayed == ("A",)

    def test_partitions_never_share_references(self) -> None:
        service = ConversationService()
        conv, tenant = uuid7(), uuid7()
        service.set_displayed(conv, tenant_id=tenant, actor_type="BUYER", displayed=("SKU-1",))
        merchant = service.view(conv, tenant_id=tenant, actor_type="MERCHANT")
        assert merchant.displayed == ()

    def test_history_is_bounded_text_only(self) -> None:
        service = ConversationService()
        conv, tenant = uuid7(), uuid7()
        for index in range(60):
            service.record_turn(
                conv,
                tenant_id=tenant,
                actor_type="BUYER",
                turn=TurnRecord(
                    turn_id=uuid7(),
                    operation_id=uuid7(),
                    message=f"q{index}",
                    reply="r",
                    language="en",
                    surface="buyer",
                ),
            )
        view = service.view(conv, tenant_id=tenant, actor_type="BUYER")
        assert len(view.history) == 50
        lines = service.history_text(view)
        assert all(line.startswith(("buyer: ", "assistant: ")) for line in lines)

    def test_shared_service_is_process_wide(self) -> None:
        assert shared_service() is shared_service()


# ------------------------------------------------------------- slice behavior, live DB


@pytest.mark.db
class TestSlice:
    def test_knowledge_question_uses_retrieval_not_generation(self, tools_pair) -> None:
        tools, _ = tools_pair()
        script = Script(
            [
                _call("knowledge.read", query="Why is the kernel necessary?"),
                _say("The kernel admits exactly one payment because the database says so."),
            ]
        )
        result = _agent(script).run(
            message="Why is the kernel necessary?",
            language="en",
            tools=tools,
            history=[],
        )
        assert script.calls == 2
        assert "kernel" in result.reply
        assert "knowledge.read" in result.tool_names
        assert result.corrections == []

    def test_search_then_ordinal_proposes_from_history(
        self, tools_pair, auth_client: TestClient
    ) -> None:
        tools, _ = tools_pair()
        agent = _agent(
            Script(
                [
                    _call("catalog.search", query="chips", limit="12"),
                    _say("I found chips. Say the number to add it."),
                ]
            )
        )
        first = agent.run(message="Show me chips", language="en", tools=tools, history=[])
        assert "catalog.search" in first.tool_names

        shelf = list(tools.ledger.seen_skus)
        assert len(shelf) >= 3
        history = [
            "buyer: Show me chips",
            f"assistant: I found chips. [products: {', '.join(sorted(shelf)[:6])}]",
        ]
        tools2, _ = tools_pair()
        agent2 = _agent(
            Script(
                [
                    _call("catalog.get_product", sku=shelf[1]),
                    _call(
                        "shopping.execute",
                        action="propose",
                        sku=shelf[1],
                        mode="add",
                        amount="1",
                    ),
                    _say("Proposed."),
                ]
            )
        )
        second = agent2.run(
            message="Add the second one", language="en", tools=tools2, history=history
        )
        assert second.tool_names == ["catalog.get_product", "shopping.execute"]
        evidence = second.evidence["shopping.execute"]
        assert evidence["status"] == "completed"
        assert evidence["sku"] == shelf[1]
        assert evidence["proposal"]["action"] == "basket.update"

    def test_silent_model_falls_back_to_verified_evidence(self, tools_pair) -> None:
        """Reads survive a silent model: the summary names verified products."""
        tools, _ = tools_pair()
        script = Script(
            [
                _call("catalog.search", query="chips"),
                _say(""),
            ]
        )
        result = _agent(script).run(message="Show me chips", language="en", tools=tools, history=[])
        assert "could not complete" not in result.reply
        assert result.reply.startswith("Here are the options:")

    def test_repeated_tool_call_is_stopped_not_replayed(self, tools_pair) -> None:
        tools, _ = tools_pair()
        script = Script(
            [
                _call("catalog.search", query="milk"),
                _call("catalog.search", query="milk"),
                _say("unreached"),
            ]
        )
        result = _agent(script).run(message="milk", language="en", tools=tools, history=[])
        assert script.calls == 2
        assert [call.name for call in tools.ledger.tool_calls] == ["catalog.search"]
        assert "unreached" not in result.reply

    def test_mid_turn_provider_failure_recovers_verified_evidence(self, tools_pair) -> None:
        """Tools ran, then the provider failed: narrate the evidence, no replay."""
        from commerce_api.services.razor_main import ModelQuotaError

        tools, _ = tools_pair()

        class Flaky:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, **kwargs: object) -> tuple:
                self.calls += 1
                return (
                    ModelRound(
                        calls=(
                            FunctionCall(
                                call_id="c1",
                                name="catalog.search",
                                args={"query": "chips"},
                            ),
                        )
                    ),
                    None,
                )

            def follow_up(self, **kwargs: object) -> tuple:
                self.calls += 1
                raise ModelQuotaError("simulated quota exhaustion mid-turn")

        agent = MainAgent(
            Flaky(),
            config=MainAgentConfig(
                model="gemini-3.8-flash", thinking_level="LOW", max_output_tokens=2048
            ),
        )
        result = agent.run(message="Show me chips", language="en", tools=tools, history=[])
        assert "could not complete" not in result.reply
        assert result.reply.startswith("Here are the options:")
        assert [call.name for call in tools.ledger.tool_calls] == ["catalog.search"]

    def test_invented_amount_is_dropped_and_cart_untouched(
        self, tools_pair, auth_client: TestClient
    ) -> None:
        tools, _ = tools_pair()
        before = auth_client.get("/v1/carts/current").json()
        script = Script(
            [
                _call("catalog.get_product", sku=MILK),
                _say(f"{MILK} costs ₹999999.00. Buy it now."),
            ]
        )
        result = _agent(script).run(
            message="How much is this milk?", language="en", tools=tools, history=[]
        )
        assert "999999" not in result.reply
        assert result.corrections
        assert auth_client.get("/v1/carts/current").json() == before

    def test_model_failure_is_deterministic_not_a_500(self, tools_pair) -> None:
        tools, _ = tools_pair()

        class Failing:
            def generate(self, **kwargs: Any) -> ModelRound:
                raise ModelQuotaError("simulated quota exhaustion")

        agent = MainAgent(
            Failing(),
            config=MainAgentConfig(
                model="gemini-3.8-flash", thinking_level="LOW", max_output_tokens=2048
            ),
        )
        with pytest.raises(ModelQuotaError):
            agent.run(message="milk", language="en", tools=tools, history=[])
        assert agent.model_reached is None


@pytest.mark.db
class TestMerchantMigration:
    def test_merchant_does_not_use_consumer_main_agent(self, api_app, mint_client) -> None:
        client, _ = mint_client(actor_type="MERCHANT")
        script = Script(
            [
                _call("merchant.low_stock", threshold="5"),
                _say("Here is your restock status."),
            ]
        )
        api_app.state.main_agent = _agent(script)
        try:
            body = client.post(
                "/v1/merchant/agent/turn", json={"message": "what needs restocking?"}
            ).json()
        finally:
            api_app.state.main_agent = None
        assert body["routing_reason"] != "main_agent"
        assert body["specialist"] == "operations"
        assert "merchant.insights" in [call["name"] for call in body.get("tool_calls", [])]
        assert body["reply"] != "Here is your restock status."

    def test_merchant_cannot_approve_through_main_agent(self, api_app, mint_client) -> None:
        """Approval verbs are absent from every registry the main agent reaches."""
        from commerce_api.services.agent_service import TOOLS

        for name in (
            "merchant.insights",
            "merchant.low_stock",
            "merchant.actions",
            "merchant.propose_action",
        ):
            assert name in TOOLS
        assert "merchant.action.approve" not in {
            getattr(spec, "capability", "") for spec in TOOLS.values()
        }

    def test_buyer_and_merchant_use_different_tables(self) -> None:
        from commerce_api.services.razor_main import BUYER_TOOLS, MERCHANT_TOOLS

        assert set(BUYER_TOOLS).isdisjoint(
            {"merchant.insights", "merchant.low_stock", "merchant.actions"}
        )
        assert "shopping.execute" not in MERCHANT_TOOLS
        assert "navigate" in BUYER_TOOLS and "navigate" in MERCHANT_TOOLS

    def test_merchant_cannot_read_tenant_wide_console_queue(self, tools_pair) -> None:
        tools, _ = tools_pair(frozenset({"support.case.read"}), Specialist.OPERATIONS)
        result = tools.call("console.reconciliation", unresolved_only="false", limit="5")
        assert not result.ok
        assert result.reason_key == "not_permitted"

    def test_console_tool_refuses_buyers(self, tools_pair) -> None:
        tools, _ = tools_pair(frozenset({"catalogue.read", "basket.write"}), Specialist.OPERATIONS)
        result = tools.call("console.reconciliation")
        assert not result.ok
        assert result.reason_key == "capability_missing"

    def test_navigate_allows_listed_destinations_only(self, tools_pair) -> None:
        tools, _ = tools_pair()
        good = tools.call("navigate", destination="shop:orders")
        assert good.ok and good.payload["destination"] == "shop:orders"
        bad = tools.call("navigate", destination="https://evil.example/x")
        assert not bad.ok


@pytest.mark.db
class TestTurnWiring:
    def test_main_agent_answers_when_fast_paths_miss(
        self, api_app: FastAPI, auth_client: TestClient
    ) -> None:
        script = Script([_say("Milk and bread serve different breakfasts.")])

        api_app.state.main_agent = _agent(script)
        try:
            body = auth_client.post(
                "/v1/agent/turn",
                json={"message": "which is better for breakfast, milk or bread?"},
            ).json()
        finally:
            api_app.state.main_agent = None
        assert body["routing_reason"] == "main_agent"
        assert body["reply"] == "Milk and bread serve different breakfasts."
        assert body["structured"]["kind"] == "answer"
        assert body["structured"]["main_agent"]["model"] == "gemini-3.8-flash"
        assert uuid.UUID(body["operation_id"])
        assert uuid.UUID(body["conversation_id"])

    def test_conversation_id_is_stable_across_turns(
        self, api_app: FastAPI, auth_client: TestClient
    ) -> None:
        conv = str(uuid7())
        script = Script([_say("one"), _say("two")])
        api_app.state.main_agent = _agent(script)
        try:
            first = auth_client.post(
                "/v1/agent/turn",
                json={"message": "which is better?", "conversation_id": conv},
            ).json()
            second = auth_client.post(
                "/v1/agent/turn",
                json={"message": "why?", "conversation_id": conv},
            ).json()
        finally:
            api_app.state.main_agent = None
        assert first["conversation_id"] == conv == second["conversation_id"]
        assert script.seen_histories[1], "the second turn saw no history"

    def test_malformed_conversation_id_is_a_422(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            "/v1/agent/turn", json={"message": "milk", "conversation_id": "nope"}
        )
        assert response.status_code == 422

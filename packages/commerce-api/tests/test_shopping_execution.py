"""The model-free Shopping execution adapter: contract, outcomes, operation IDs.

Shopping is a capability, not an agent: structured commands in, backend evidence
out, no model anywhere on the path. These tests prove the isolation statically,
the behavior against a real database, and the retry semantics the contract
promises -- without ever touching production data.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from agent_runtime.language import Language
from commerce_api.deps import RequestContext, session_scope_for
from commerce_api.services.agent_service import (
    Specialist,
    ToolExecutor,
    TurnLedger,
)
from commerce_api.services.shopping_execution import (
    OutcomeStatus,
    QuantityMode,
    ShoppingAction,
    ShoppingCommand,
    ShoppingContractError,
    ShoppingExecutionAdapter,
)
from commerce_domain import ActorType, AgentPrincipal
from commerce_domain.ids import uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db.tenancy import set_tenant

from conftest import MintedSession

OP = uuid.UUID("01a09d70-0000-7000-8000-000000000001")
MILK = "AMUL-DAIRY-001"


def _command(**overrides: Any) -> ShoppingCommand:
    fields: dict[str, Any] = {
        "action": ShoppingAction.SEARCH,
        "operation_id": OP,
        "query": "milk",
    }
    fields.update(overrides)
    return ShoppingCommand(**fields)


# ------------------------------------------------------- contract validation, no database


class TestContractValidation:
    def test_search_needs_a_query(self) -> None:
        with pytest.raises(ShoppingContractError) as caught:
            _command(query="   ").validated()
        assert caught.value.reason_code == "empty_query"

    def test_limit_is_bounded(self) -> None:
        with pytest.raises(ShoppingContractError) as caught:
            _command(max_results=5000).validated()
        assert caught.value.reason_code == "invalid_limit"

    def test_get_product_needs_a_sku(self) -> None:
        with pytest.raises(ShoppingContractError) as caught:
            ShoppingCommand(action=ShoppingAction.GET_PRODUCT, operation_id=OP).validated()
        assert caught.value.reason_code == "missing_sku"

    def test_add_and_set_need_a_positive_amount(self) -> None:
        for mode in (QuantityMode.ADD, QuantityMode.SET):
            with pytest.raises(ShoppingContractError) as caught:
                ShoppingCommand(
                    action=ShoppingAction.PROPOSE,
                    operation_id=OP,
                    sku=MILK,
                    mode=mode,
                    amount=0,
                ).validated()
            assert caught.value.reason_code == "invalid_amount"

    def test_remove_ignores_amount_and_zero_is_not_an_error(self) -> None:
        command = ShoppingCommand(
            action=ShoppingAction.PROPOSE,
            operation_id=OP,
            sku=MILK,
            mode=QuantityMode.REMOVE,
            amount=0,
        ).validated()
        assert command.mode is QuantityMode.REMOVE

    def test_propose_needs_a_target(self) -> None:
        with pytest.raises(ShoppingContractError) as caught:
            ShoppingCommand(action=ShoppingAction.PROPOSE, operation_id=OP).validated()
        assert caught.value.reason_code == "missing_target"

    def test_unknown_action_is_a_contract_refusal(self) -> None:
        # A typo never reaches execution: validation names it with a reason code.
        with pytest.raises(ShoppingContractError) as caught:
            ShoppingCommand(action="fly", operation_id=OP).validated()  # type: ignore[arg-type]
        assert caught.value.reason_code == "unknown_action"

    def test_identity_travels_server_side_only(self) -> None:
        names = set(ShoppingCommand.__dataclass_fields__)
        assert names.isdisjoint(
            {"principal_id", "tenant_id", "buyer_ref", "merchant_id", "capabilities"}
        ), names


# ----------------------------------------------------------------- static isolation


class TestModelIsolation:
    def test_adapter_source_names_no_model_machinery(self) -> None:
        source = inspect.getsource(ShoppingExecutionAdapter)
        for token in (
            "google",
            "genai",
            "LlmAgent",
            "AdkSpecialistRunner",
            "load_prompt",
            "plan_shopping",
            "SpecialistBridge",
            "static_instruction",
        ):
            assert token not in source, token

    def test_production_runner_refuses_a_shopping_binding(self) -> None:
        import asyncio

        from agent_runtime.harness.routing import Specialist as HarnessSpecialist
        from agent_runtime.runtime_adk.adapter import (
            AdkSpecialistRunner,
            SpecialistToolingError,
        )

        runner = AdkSpecialistRunner.__new__(AdkSpecialistRunner)
        with pytest.raises(SpecialistToolingError, match="model-free"):
            asyncio.run(
                runner.__call__(
                    SimpleNamespace(specialist=HarnessSpecialist.SHOPPING),
                    SimpleNamespace(),
                    SimpleNamespace(),
                    SimpleNamespace(),
                )
            )


# ------------------------------------------------- behavior, against real services


def _tools(
    api_app: FastAPI,
    session: MintedSession,
    ledger: TurnLedger,
    db_session: Any,
    capabilities: frozenset[str] = frozenset({"catalogue.read", "basket.write"}),
) -> ToolExecutor:
    principal = AgentPrincipal(
        principal_id="session:shopping-execution",
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
        specialist=Specialist.SHOPPING,
        language=Language.EN,
        ledger=ledger,
    )


@pytest.fixture
def tools_pair(api_app: FastAPI, demo_session: MintedSession):
    """A fresh gated executor per test, mirroring one turn's tool budget."""
    with session_scope_for(api_app.state.settings.database_url_app) as session:
        set_tenant(session, demo_session.tenant_id)

        def make(
            capabilities: frozenset[str] = frozenset({"catalogue.read", "basket.write"}),
        ) -> tuple[ToolExecutor, TurnLedger]:
            ledger = TurnLedger()
            return _tools(api_app, demo_session, ledger, session, capabilities), ledger

        yield make


def _cart_id(auth_client: TestClient) -> uuid.UUID:
    response = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid7())})
    assert response.status_code == 201, response.text
    current = auth_client.get("/v1/carts/current").json()
    return uuid.UUID(current["cart"]["cart_id"])


@pytest.mark.db
class TestAdapterBehavior:
    def test_search_reports_hits_and_echoes_the_operation(
        self, tools_pair: tuple[ToolExecutor, TurnLedger]
    ) -> None:
        tools, ledger = tools_pair()
        outcome = ShoppingExecutionAdapter().execute(_command(), tools=tools)
        assert outcome.status is OutcomeStatus.COMPLETED
        assert outcome.reason_code == "search_completed"
        assert outcome.operation_id == OP
        assert [hit["sku"] for hit in outcome.evidence["hits"]][:1]
        assert MILK in ledger.seen_skus

    def test_empty_search_is_completed_with_nothing(self, tools_pair) -> None:
        tools, _ = tools_pair()
        outcome = ShoppingExecutionAdapter().execute(
            _command(query="zzzznonexistentzzzz"), tools=tools
        )
        assert outcome.status is OutcomeStatus.COMPLETED
        assert outcome.reason_code == "no_results"
        assert outcome.evidence["hits"] == []

    def test_unknown_sku_is_rejected_not_unknown(self, tools_pair) -> None:
        tools, _ = tools_pair()
        outcome = ShoppingExecutionAdapter().execute(
            ShoppingCommand(action=ShoppingAction.GET_PRODUCT, operation_id=OP, sku="NOPE-000"),
            tools=tools,
        )
        assert outcome.status is OutcomeStatus.REJECTED
        assert outcome.evidence["sku"] == "NOPE-000"

    def test_propose_add_stages_but_never_mutates(
        self, tools_pair, auth_client: TestClient
    ) -> None:
        tools, _ = tools_pair()
        cart_id = _cart_id(auth_client)
        outcome = ShoppingExecutionAdapter().execute(
            ShoppingCommand(
                action=ShoppingAction.PROPOSE,
                operation_id=OP,
                sku=MILK,
                mode=QuantityMode.ADD,
                amount=2,
                cart_id=cart_id,
            ),
            tools=tools,
        )
        assert outcome.status is OutcomeStatus.COMPLETED
        assert outcome.stage == "proposed"
        proposal = outcome.evidence["proposal"]
        assert proposal["action"] == "basket.update"
        assert proposal["executes_on"] == "trusted_surface"
        assert proposal["operation_id"] == str(OP)
        assert outcome.evidence["absolute_quantity"] == outcome.evidence["current_quantity"] + 2
        assert proposal["blocked_by"] is None

    def test_propose_set_and_remove_use_absolute_semantics(
        self, tools_pair, auth_client: TestClient
    ) -> None:
        cart_id = _cart_id(auth_client)
        adapter = ShoppingExecutionAdapter()
        base = {
            "action": ShoppingAction.PROPOSE,
            "operation_id": OP,
            "sku": MILK,
            "cart_id": cart_id,
        }
        # A fresh executor per command: one turn's tool budget is eight calls, and
        # each proposal reads the product and the cart.
        current = adapter.execute(
            ShoppingCommand(**base, mode=QuantityMode.ADD, amount=1),
            tools=tools_pair()[0],
        ).evidence["current_quantity"]
        absolute = adapter.execute(
            ShoppingCommand(**base, mode=QuantityMode.SET, amount=5),
            tools=tools_pair()[0],
        ).evidence["absolute_quantity"]
        assert absolute == 5
        removed = adapter.execute(
            ShoppingCommand(**base, mode=QuantityMode.REMOVE), tools=tools_pair()[0]
        ).evidence["absolute_quantity"]
        assert removed == 0
        assert current >= 0

    def test_propose_by_unique_name_resolves_and_by_vague_name_asks(self, tools_pair) -> None:
        tools, _ = tools_pair()
        adapter = ShoppingExecutionAdapter()
        unique = adapter.execute(
            _command(
                action=ShoppingAction.PROPOSE,
                query="amul taaza toned milk 500 ml",
                mode=QuantityMode.ADD,
                amount=1,
            ),
            tools=tools,
        )
        assert unique.status is OutcomeStatus.COMPLETED
        vague = adapter.execute(
            _command(
                action=ShoppingAction.PROPOSE,
                query="milk",
                mode=QuantityMode.ADD,
                amount=1,
            ),
            tools=tools,
        )
        assert vague.status is OutcomeStatus.CLARIFICATION_REQUIRED
        assert vague.reason_code == "ambiguous_product"
        assert len(vague.evidence["choices"]) > 1

    def test_ordinal_resolves_visible_order_and_stale_asks(self, tools_pair) -> None:
        tools, _ = tools_pair()
        adapter = ShoppingExecutionAdapter()
        search = adapter.execute(_command(query="chips"), tools=tools)
        shelf = tuple(hit["sku"] for hit in search.evidence["hits"][:3])
        assert len(shelf) == 3
        second = adapter.execute(
            _command(
                action=ShoppingAction.PROPOSE,
                ordinal=1,
                mode=QuantityMode.ADD,
                amount=1,
            ),
            tools=tools,
            displayed=shelf,
        )
        assert second.status is OutcomeStatus.COMPLETED
        assert second.evidence["sku"] == shelf[1]
        stale = adapter.execute(
            _command(
                action=ShoppingAction.PROPOSE,
                ordinal=9,
                mode=QuantityMode.ADD,
                amount=1,
            ),
            tools=tools,
            displayed=shelf,
        )
        assert stale.status is OutcomeStatus.CLARIFICATION_REQUIRED
        assert stale.reason_code == "stale_shelf"

    def test_retry_replays_the_same_proposal_without_a_mutation(
        self, tools_pair, auth_client: TestClient
    ) -> None:
        tools, _ = tools_pair()
        before = auth_client.get("/v1/carts/current").json()
        adapter = ShoppingExecutionAdapter()
        command = ShoppingCommand(
            action=ShoppingAction.PROPOSE,
            operation_id=OP,
            sku=MILK,
            mode=QuantityMode.ADD,
            amount=1,
        )
        first = adapter.execute(command, tools=tools)
        second = adapter.execute(command, tools=tools)
        assert first.status is second.status is OutcomeStatus.COMPLETED
        assert first.evidence["proposal"] == second.evidence["proposal"]
        assert auth_client.get("/v1/carts/current").json() == before

    def test_structured_tool_validates_arguments_server_side(self, tools_pair) -> None:
        tools, _ = tools_pair()
        bad = tools.call("shopping.execute", action="search", operation_id="not-a-uuid")
        assert not bad.ok
        good = tools.call(
            "shopping.execute",
            action="search",
            operation_id=str(OP),
            query="milk",
            max_results=3,
        )
        assert good.ok
        assert good.payload["status"] == "completed"
        assert good.payload["operation_id"] == str(OP)
        assert len(good.payload["hits"]) <= 3

    def test_denied_capability_is_uncertainty_not_failure(self, tools_pair) -> None:
        """A read the principal does not hold is UNKNOWN with denied set.

        Callers render a denial sentence off this, never an outage sentence and
        never a blind retry.
        """
        tools, _ = tools_pair(frozenset())
        outcome = ShoppingExecutionAdapter().execute(_command(query="milk"), tools=tools)
        assert outcome.status is OutcomeStatus.UNKNOWN
        assert outcome.denied is True

    def test_conflicting_payload_under_one_key_is_rejected_durably(
        self, auth_client: TestClient
    ) -> None:
        """Same Idempotency-Key, different body: 422, and the first write stands.

        Proposal-stage operation IDs are carried and echoed; the durable conflict
        rule lives where the mutation happens, in ``idempotency_records``.
        """
        cart_id = _cart_id(auth_client)
        key = str(uuid7())
        first = auth_client.put(
            f"/v1/carts/{cart_id}/lines/{MILK}",
            json={"quantity": 1},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 200, first.text
        clash = auth_client.put(
            f"/v1/carts/{cart_id}/lines/{MILK}",
            json={"quantity": 2},
            headers={"Idempotency-Key": key},
        )
        assert clash.status_code == 422, clash.text
        lines = {
            line["sku"]: line["quantity"]
            for line in auth_client.get("/v1/carts/current").json()["cart"]["lines"]
        }
        assert lines.get(MILK) == 1


# ------------------------------------------------------------------ operation IDs


@pytest.mark.db
class TestOperationIds:
    def test_request_identity_is_echoed(self, auth_client: TestClient) -> None:
        op = str(uuid7())
        body = auth_client.post(
            "/v1/agent/turn", json={"message": "milk", "operation_id": op}
        ).json()
        assert body["operation_id"] == op

    def test_missing_identity_is_minted_as_uuid7(self, auth_client: TestClient) -> None:
        body = auth_client.post("/v1/agent/turn", json={"message": "milk"}).json()
        minted = uuid.UUID(body["operation_id"])
        assert minted.version == 7

    def test_malformed_identity_is_a_422(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            "/v1/agent/turn", json={"message": "milk", "operation_id": "nope"}
        )
        assert response.status_code == 422

from types import SimpleNamespace

import pytest
from agent_runtime.language import Language
from commerce_api.services.agent_bridge import CartActionBridge
from commerce_api.services.agent_service import Copilot, Route, Specialist, TurnInput, TurnOutcome


async def forbidden_model(*_args):
    pytest.fail("Shopping must never invoke a conversational or planning model")


@pytest.mark.parametrize(
    "message", ["hello", "explain the project", "recommend a breakfast", "compare these phones"]
)
def test_shopping_conversation_returns_to_main_without_tools(message):
    bridge = CartActionBridge(forbidden_model)
    bridge._fallback = SimpleNamespace(
        run=lambda *_args: pytest.fail("No shopping action requested")
    )
    result = bridge.run(
        TurnInput(Copilot.BUYER, message, Language.EN),
        Route(Specialist.SHOPPING, "default_shopping"),
        None,
    )
    assert result.reply == ""
    assert result.structured["reason"] == "cart_action_required"
    assert result.structured["model_rounds"] == 0
    assert Specialist.SHOPPING not in bridge.bridged


@pytest.mark.parametrize(
    "message", ["add two milk", "remove this", "set second quantity to 3", "do milk add karo"]
)
def test_explicit_cart_action_keeps_existing_executor(message):
    bridge = CartActionBridge(forbidden_model)
    seen = []

    def execute(turn, _route, _tools):
        seen.append(turn.message)
        return TurnOutcome(
            reply="Action prepared", structured={"kind": "product", "proposal": {"quantity": 2}}
        )

    bridge._fallback = SimpleNamespace(run=execute)
    result = bridge.run(
        TurnInput(Copilot.BUYER, message, Language.EN),
        Route(Specialist.SHOPPING, "default_shopping"),
        None,
    )
    assert seen == [message]
    assert result.structured["proposal"]["quantity"] == 2
    assert result.structured["model_rounds"] == 0


@pytest.mark.db
@pytest.mark.parametrize(("message", "quantity"), [("add two of this", 5), ("remove this", 0)])
def test_cart_only_bridge_preserves_verified_add_remove(api_app, auth_client, message, quantity):
    import uuid

    api_app.state.agent_runner = CartActionBridge(forbidden_model)
    cart = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    discovery = auth_client.post("/v1/agent/turn", json={"message": "Show me milk"}).json()
    sku = discovery["structured"]["hits"][0]["sku"]
    seeded = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{sku}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": 3},
    )
    assert seeded.status_code == 200
    api_app.state.agent_runner.remember_discovery(discovery["principal_id"], [sku])
    response = auth_client.post("/v1/agent/turn", json={"message": message})
    assert response.status_code == 200, response.text
    proposal = response.json()["structured"]["proposal"]
    assert proposal["quantity"] == quantity
    applied = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{sku}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": quantity, "expected": proposal["binding"]},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["lines"] == ([{"sku": sku, "quantity": quantity}] if quantity else [])

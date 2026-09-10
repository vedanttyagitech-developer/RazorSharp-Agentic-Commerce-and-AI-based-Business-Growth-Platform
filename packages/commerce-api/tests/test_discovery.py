import pytest
from commerce_api.services.discovery import discovery_query


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Show me milk products.", "milk"),
        ("मुझे दूध दिखाइए।", "milk"),
        ("Mujhe milk products dikhao please.", "milk"),
        ("Ab bread options dikhao please.", "bread"),
        ("अब मुझे ब्रेड दिखाइए।", "bread"),
        ("अब ब्रेड ऑप्शंस दिखाओ प्लीज।", "bread"),
        ("Show me earphones", "earphones"),
    ],
)
def test_simple_discovery(message, expected):
    assert discovery_query(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "Show me milk under 50",
        "Show me lactose free milk",
        "Compare milk and bread",
        "Add two milk",
        "Do not show me milk",
        "Show me those",
        "Which is healthier?",
        "Show me milk and pay",
        "मुझे दूध नहीं चाहिए",
        "milk for a baby",
    ],
)
def test_constraints_and_actions_keep_reasoning(message):
    assert discovery_query(message) is None


@pytest.mark.db
def test_fast_discovery_uses_authorized_catalogue_without_model(api_app, auth_client):
    class NoModel:
        fast_discovery = True
        model_reached = None

        def run(self, *args):
            raise AssertionError("Discovery must not invoke a model")

    runner = NoModel()
    api_app.state.agent_runner = runner
    response = auth_client.post("/v1/agent/turn", json={"message": "Show me milk products."})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["routing_reason"] == "direct_catalogue_discovery"
    assert body["server_authored"] is True
    assert body["structured"]["hits"]
    assert [tool["name"] for tool in body["tool_calls"]] == ["catalog.search"]
    assert runner.model_reached is None


def test_display_context_is_scoped_bounded_and_not_financial_evidence():
    from commerce_api.services.agent_bridge import SpecialistBridge

    bridge = SpecialistBridge(None, fast_discovery=True)
    bridge.remember_discovery("buyer-a", ["SKU-1", "SKU-2"])
    assert bridge._take_discovery("buyer-b") == ()
    assert bridge._take_discovery("buyer-a") == ("SKU-1", "SKU-2")
    assert bridge._take_discovery("buyer-a") == ()
    for number in range(257):
        bridge.remember_discovery(str(number), ["SKU-1"])
    assert bridge._take_discovery("0") == ()
    assert len(bridge._recent_discovery) == 256

import uuid

import pytest
from commerce_api.services.adaptive_shopping import comparison_plan, stated_budget
from commerce_api.services.agent_bridge import SpecialistBridge


class Planner:
    """A runner double that fails loudly if any model loop is attempted.

    It deliberately offers no ``plan_shopping``: the bridge must not consult a
    planning model at all, so a double providing one would prove nothing.
    """

    def __init__(self):
        self.calls = 0

    async def __call__(self, *args):
        raise AssertionError("No ADK tool/model loop after a structured plan")


@pytest.mark.db
def test_comparison_uses_zero_model_calls_and_fresh_cards(api_app, auth_client):
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    body = auth_client.post(
        "/v1/agent/turn", json={"message": "Compare milk and bread for breakfast"}
    ).json()
    assert planner.calls == 0
    assert body["structured"]["model_rounds"] == 0
    assert len(body["structured"]["groups"]) == 2
    assert body["structured"]["preview_quote"] is None
    assert body["structured"]["hits"]
    assert all(t["name"] == "catalog.search" for t in body["tool_calls"])


@pytest.mark.db
def test_bundle_without_deterministic_plan_clarifies_and_writes_nothing(
    api_app, auth_client
):
    """A bundle no deterministic plan covers is clarification, not a model bundle.

    The planning-model call and the cross-turn plan memory are gone: the turn
    clarifies with zero model invocations, stages no quote, and writes no cart.
    A follow-up cannot edit a plan that was never stored.
    """
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())})
    before = auth_client.get("/v1/carts/current").json()
    body = auth_client.post(
        "/v1/agent/turn", json={"message": "Breakfast for two under 300 rupees"}
    ).json()
    assert planner.calls == 0
    assert body["structured"]["planning_status"] == "clarification_required"
    assert body["structured"]["hits"] == []
    assert "preview_quote" not in body["structured"]
    assert not body["tool_calls"]
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == before["cart"]["lines"]
    second = auth_client.post("/v1/agent/turn", json={"message": "Replace bread with oats"}).json()
    assert planner.calls == 0
    assert "plan" not in second["structured"]
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == before["cart"]["lines"]


def test_budget_boundary_and_unsupported_comparison():
    assert stated_budget("under ₹300") == (30000, False)
    assert stated_budget("within 300.50 rupees") == (30050, True)
    assert comparison_plan("compare milk and bread under 40") is None


@pytest.mark.db
def test_new_request_stores_no_plan_or_quote_to_inherit(api_app, auth_client):
    """No plan memory means no inheritance to test for -- and none to leak."""
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    first = auth_client.post(
        "/v1/agent/turn", json={"message": "Breakfast bundle under 300 rupees"}
    ).json()
    assert first["structured"]["planning_status"] == "clarification_required"
    assert "preview_quote" not in first["structured"]
    second = auth_client.post("/v1/agent/turn", json={"message": "Suggest a picnic bundle"}).json()
    assert planner.calls == 0
    assert "plan" not in second["structured"]
    assert "preview_quote" not in second["structured"]
    assert all(t["name"] != "cart.preview" for t in second["tool_calls"])


@pytest.mark.db
def test_over_budget_bundle_is_not_presented_as_fitting(api_app, auth_client):
    """An unplannable bundle cannot be mis-presented: it clarifies instead."""
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    body = auth_client.post(
        "/v1/agent/turn", json={"message": "Breakfast bundle under 1 rupee"}
    ).json()
    assert planner.calls == 0
    assert body["structured"]["planning_status"] == "clarification_required"
    assert "preview_quote" not in body["structured"]
    assert "above your stated budget" not in body["reply"]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("300 rupaye ke andar", (30000, True)),
        ("₹३०० से कम", (30000, False)),
        ("३०० रुपये तक", (30000, True)),
        ("200 se kam and 300 ke andar", None),
    ],
)
def test_multilingual_budget(message, expected):
    assert stated_budget(message) == expected


@pytest.mark.db
@pytest.mark.parametrize(
    "message,locale",
    [
        ("Do logon ka nashta 300 rupaye ke andar suggest karo", "hi-Latn"),
        ("दो लोगों का नाश्ता ३०० रुपये तक बताओ", "hi"),
    ],
)
def test_multilingual_bundle_without_plan_clarifies_in_language(
    api_app, auth_client, message, locale
):
    """Clarification speaks the buyer's language; nothing is quoted or stored."""
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    body = auth_client.post("/v1/agent/turn", json={"message": message, "locale": locale}).json()
    assert planner.calls == 0
    assert body["structured"]["planning_status"] == "clarification_required"
    assert "preview_quote" not in body["structured"]
    assert body["reply"]
    followup = auth_client.post(
        "/v1/agent/turn", json={"message": "bread ki jagah oats", "locale": locale}
    ).json()
    assert planner.calls == 0
    assert "plan" not in followup["structured"]


def test_unverifiable_allergy_does_not_offer_unchecked_products():
    from commerce_api.services.adaptive_shopping import execute

    plan = {
        "mode": "compare",
        "needs": [
            {"query": "snacks", "quantity": 1, "required_terms": [], "excluded_terms": ["peanut"]}
        ],
        "clarification": "",
        "unverified_requirements": ["safe for severe peanut allergy"],
    }

    class NoTools:
        def call(self, *args, **kwargs):
            raise AssertionError("Do not show unsuitable recommendations")

    result = execute(plan, NoTools(), "en", None)
    assert result.structured["hits"] == []
    assert result.structured["planning_status"] == "constraints_unverified"

import uuid

import pytest
from commerce_api.services.adaptive_shopping import comparison_plan, stated_budget
from commerce_api.services.agent_bridge import SpecialistBridge


class Planner:
    def __init__(self):
        self.calls = 0
        self.previous = None

    async def __call__(self, *args):
        raise AssertionError("No ADK tool/model loop after a structured plan")

    async def plan_shopping(self, message, previous):
        self.calls += 1
        self.previous = previous
        return {
            "mode": "bundle",
            "needs": [
                {
                    "query": "Amul Taaza",
                    "quantity": 1,
                    "required_terms": ["500 ml"],
                    "excluded_terms": [],
                },
                {
                    "query": "Britannia Brown Bread",
                    "quantity": 1,
                    "required_terms": [],
                    "excluded_terms": [],
                },
            ],
            "clarification": "",
            "unverified_requirements": ["servings"],
        }


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
def test_bundle_quotes_without_cart_write_and_reuses_plan(api_app, auth_client):
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())})
    before = auth_client.get("/v1/carts/current").json()
    body = auth_client.post(
        "/v1/agent/turn", json={"message": "Breakfast for two under 300 rupees"}
    ).json()
    assert planner.calls == 1
    s = body["structured"]
    assert s["model_rounds"] == 1
    assert s["preview_quote"]["ok"]
    assert s["preview_quote"]["preview_only"]
    assert s["preview_quote"]["total"]["minor"] > 0
    assert s["unverified_requirements"]
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == before["cart"]["lines"]
    second = auth_client.post("/v1/agent/turn", json={"message": "Replace bread with oats"}).json()
    assert planner.calls == 1  # Local follow-up edits the saved plan, no second model call.
    assert second["structured"]["model_rounds"] == 0
    assert second["structured"]["plan"]["needs"][0] == s["plan"]["needs"][0]
    assert second["structured"]["plan"]["needs"][1]["query"] == "oats"
    assert second["structured"]["preview_quote"]["ok"]


def test_budget_boundary_and_unsupported_comparison():
    assert stated_budget("under ₹300") == (30000, False)
    assert stated_budget("within 300.50 rupees") == (30050, True)
    assert comparison_plan("compare milk and bread under 40") is None


@pytest.mark.db
def test_new_request_does_not_inherit_budget_or_quote(api_app, auth_client):
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    first = auth_client.post(
        "/v1/agent/turn", json={"message": "Breakfast bundle under 300 rupees"}
    ).json()
    assert first["structured"]["preview_quote"]["ok"]
    second = auth_client.post("/v1/agent/turn", json={"message": "Suggest a picnic bundle"}).json()
    assert planner.previous is None
    assert second["structured"]["preview_quote"] is None
    assert all(t["name"] != "cart.preview" for t in second["tool_calls"])


@pytest.mark.db
def test_over_budget_preview_is_not_presented_as_fitting(api_app, auth_client):
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    body = auth_client.post(
        "/v1/agent/turn", json={"message": "Breakfast bundle under 1 rupee"}
    ).json()
    assert body["structured"]["preview_quote"]["within_stated_budget"] is False
    assert "above your stated budget" in body["reply"]


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
    "message,locale,fragment",
    [
        ("Do logon ka nashta 300 rupaye ke andar suggest karo", "hi-Latn", "budget ke andar"),
        ("दो लोगों का नाश्ता ३०० रुपये तक बताओ", "hi", "बजट के अंदर"),
    ],
)
def test_multilingual_bundle_uses_same_quote_engine(
    api_app, auth_client, message, locale, fragment
):
    planner = Planner()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    body = auth_client.post("/v1/agent/turn", json={"message": message, "locale": locale}).json()
    assert body["structured"]["preview_quote"]["ok"]
    assert fragment in body["reply"]
    followup = auth_client.post(
        "/v1/agent/turn", json={"message": "bread ki jagah oats", "locale": locale}
    ).json()
    assert planner.calls == 1
    assert followup["structured"]["model_rounds"] == 0
    assert followup["structured"]["plan"]["needs"][1]["query"] == "oats"
    assert followup["structured"]["preview_quote"]["ok"]


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

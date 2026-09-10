import uuid

import pytest
from commerce_api.services.search_constraints import eligible_hits, price_search


def test_price_and_exact_product_constraints_are_not_similarity_scores():
    request = price_search("Show me Amul Taaza 500 ml under 40 rupees")
    assert request is not None

    def hit(name, price):
        return {"display_name": name, "unit_price_minor": price, "is_available": True}

    correct = hit("Amul Taaza Toned Milk 500ml", 2800)
    assert eligible_hits(
        request,
        [
            correct,
            hit("Amul Ghee 500ml", 32500),
            hit("Amul Taaza 1 L", 2800),
            hit("Amul Taaza 500ml", 4000),
        ],
    ) == [correct]
    assert eligible_hits(
        price_search("Show me Amul Taaza 500 ml up to 40 rupees"), [hit("Amul Taaza 500ml", 4000)]
    )


@pytest.mark.parametrize(
    "message",
    [
        "Show milk under 40 and eggs",
        "Do not show milk under 40",
        "Compare milk and bread under 40",
        "milk under forty",
        "milk under 40.001",
        "milk under -40",
    ],
)
def test_unsupported_constraints_do_not_become_a_partial_filter(message):
    assert price_search(message) is None


@pytest.mark.db
def test_filtered_discovery_has_no_model_round_and_preserves_cart(api_app, auth_client):
    class NoModel:
        fast_discovery = True

        def run(self, *args):
            raise AssertionError("No model required for exact price filtering")

    api_app.state.agent_runner = NoModel()
    auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())})
    response = auth_client.post(
        "/v1/agent/turn", json={"message": "Show me Amul Taaza 500 ml under 40 rupees"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["routing_reason"] == "direct_constrained_discovery"
    assert [h["sku"] for h in body["structured"]["hits"]] == ["AMUL-DAIRY-001"]
    assert body["structured"]["budget_scope"] == "item_price_only"
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == []


@pytest.mark.db
def test_model_failure_never_presents_unverified_complex_matches(api_app, auth_client):
    class Failed:
        def run(self, *args):
            raise RuntimeError("simulated provider outage")

    api_app.state.agent_runner = Failed()
    response = auth_client.post(
        "/v1/agent/turn",
        json={"message": "Vegetarian breakfast for two under 300; do not add anything"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["structured"]["hits"] == []
    assert body["structured"]["reason"] == "constraints_unverified"
    assert "proposal" not in body["structured"]

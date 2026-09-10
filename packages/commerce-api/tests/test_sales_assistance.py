"""Sales suggestions must follow durable facts, never create cart/payment actions."""

import uuid

import pytest
from commerce_api.services.agent_bridge import SpecialistBridge


def turn(client, message, **extra):
    response = client.post("/v1/agent/turn", json={"message": message, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def create(client):
    response = client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    return response.json()["cart_id"]


def add(client, cart, sku, quantity=1, key=None):
    response = client.put(
        f"/v1/carts/{cart}/lines/{sku}",
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
        json={"quantity": quantity},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.db
@pytest.mark.parametrize(
    "message,locale,status",
    [
        ("I'm confused", "en", "clarify_priority"),
        ("samajh nahi aa raha", "hi-Latn", "clarify_priority"),
        ("समझ नहीं आ रहा", "hi", "clarify_priority"),
        ("that's all", "en", "ready_to_review"),
        ("bas ho gaya", "hi-Latn", "ready_to_review"),
        ("और कुछ नहीं", "hi", "ready_to_review"),
    ],
)
def test_conversation_without_model_or_purchase(api_app, auth_client, message, locale, status):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    body = turn(auth_client, message, locale=locale)
    assert body["structured"]["sales_status"] == status
    assert body["structured"]["hits"] == []
    assert not body["tool_calls"]
    assert "proposal" not in body["structured"]


@pytest.mark.db
def test_successful_add_optional_complement_replay_reject_and_restart(api_app, auth_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    cart = create(auth_client)
    hits = turn(auth_client, "Show me milk")["structured"]["hits"]
    sku = next(h["sku"] for h in hits if "milk" in h["name_en"].lower())
    # A displayed product or proposal is not a successful cart mutation.
    forged = turn(auth_client, "Cart updated", cart_event_id=str(uuid.uuid4()))
    assert forged["structured"]["sales_status"] == "event_ignored"
    key = str(uuid.uuid4())
    updated = add(auth_client, cart, sku, 2, key)
    event = updated["sales_event_id"]
    assert event
    answer = turn(auth_client, "Cart updated", cart_event_id=event)
    assert answer["structured"]["sales_status"] == "optional_complement"
    assert "Added 2" in answer["reply"]
    assert all("bread" in h["name_en"].lower() for h in answer["structured"]["hits"])
    assert "proposal" not in answer["structured"]
    assert auth_client.get(f"/v1/carts/{cart}").json()["lines"] == [{"sku": sku, "quantity": 2}]
    # Idempotent replay returns the historical event, but it cannot pitch twice.
    replay = add(auth_client, cart, sku, 2, key)
    assert replay["sales_event_id"] == event
    assert (
        turn(auth_client, "Cart updated", cart_event_id=event)["structured"]["sales_status"]
        == "event_ignored"
    )
    turn(auth_client, "nahi chahiye", locale="hi-Latn")
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    fresh = add(auth_client, cart, sku, 3)
    answer = turn(auth_client, "Cart updated", cart_event_id=fresh["sales_event_id"])
    assert answer["structured"]["sales_status"] == "ready_to_review"
    assert answer["structured"]["hits"] == []


@pytest.mark.db
def test_stale_event_does_not_consume_current_event(api_app, auth_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    cart = create(auth_client)
    sku = turn(auth_client, "Show me milk")["structured"]["hits"][0]["sku"]
    old = add(auth_client, cart, sku, 1)["sales_event_id"]
    new = add(auth_client, cart, sku, 2)["sales_event_id"]
    assert (
        turn(auth_client, "Cart updated", cart_event_id=old)["structured"]["sales_status"]
        == "event_ignored"
    )
    assert (
        turn(auth_client, "Cart updated", cart_event_id=new)["structured"]["sales_status"]
        != "event_ignored"
    )
    remove = add(auth_client, cart, sku, 0)
    assert remove["sales_event_id"] is None


@pytest.mark.db
def test_budget_alternatives_quote_and_do_not_mutate(api_app, auth_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    cart = create(auth_client)
    turn(auth_client, "Show me milk")
    result = turn(auth_client, "over budget under 1")
    assert result["structured"]["sales_status"] == "alternatives_ready"
    assert result["structured"]["preview_quote"]["within_stated_budget"] is False
    assert "still exceeds" in result["reply"]
    assert "cart.preview" in [call["name"] for call in result["tool_calls"]]
    assert auth_client.get(f"/v1/carts/{cart}").json()["lines"] == []


@pytest.mark.db
def test_exact_constraints_are_not_relaxed_for_alternative(api_app, auth_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    create(auth_client)
    turn(auth_client, "Show me Amul Taaza 500 ml")
    result = turn(auth_client, "alternative please")
    for hit in result["structured"]["hits"]:
        assert "amul" in hit["name_en"].lower()
        assert "500" in hit["unit_label"]


@pytest.mark.db
def test_event_cannot_read_another_buyers_cart(api_app, auth_client, mint_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    cart = create(auth_client)
    sku = turn(auth_client, "Show me milk")["structured"]["hits"][0]["sku"]
    event = add(auth_client, cart, sku)["sales_event_id"]
    other, _ = mint_client("BUYER", "another-sales-buyer")
    response = other.post(
        "/v1/agent/turn", json={"message": "Cart updated", "cart_id": cart, "cart_event_id": event}
    )
    assert response.status_code == 404


@pytest.mark.db
def test_unavailable_item_has_fresh_matching_alternatives(api_app, auth_client, demo_session):
    from conftest import merchant_mutation

    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    create(auth_client)
    hits = turn(auth_client, "Show me milk")["structured"]["hits"]
    missing = hits[0]["sku"]
    with merchant_mutation(api_app, demo_session) as store:
        store.set_stock(missing, 0)
    result = turn(auth_client, "Show me milk")
    assert result["structured"]["hits"]
    assert all(h["is_available"] and h["sku"] != missing for h in result["structured"]["hits"])
    exact = turn(auth_client, "Show me " + hits[0]["name_en"])
    assert "unavailable" in exact["reply"]
    turn(auth_client, "Show me milk")
    result = turn(auth_client, "alternative please")
    assert result["structured"]["hits"]
    assert all(h["is_available"] and h["sku"] != missing for h in result["structured"]["hits"])


@pytest.mark.db
def test_unpriceable_cart_never_creates_success_followup(api_app, auth_client, demo_session):
    from conftest import merchant_mutation

    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    cart = create(auth_client)
    sku = turn(auth_client, "Show me milk")["structured"]["hits"][0]["sku"]
    with merchant_mutation(api_app, demo_session) as store:
        store.set_stock(sku, 0)
    result = add(auth_client, cart, sku)
    assert result["sales_event_id"] is None


@pytest.mark.db
def test_cheaper_replacement_preserves_quantity_and_unaffected_cart(api_app, auth_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    cart = create(auth_client)
    milk = turn(auth_client, "Show me milk")["structured"]["hits"][0]["sku"]
    bread = turn(auth_client, "Show me bread")["structured"]["hits"][0]["sku"]
    add(auth_client, cart, milk, 3)
    add(auth_client, cart, bread, 2)
    turn(auth_client, "Show me milk")
    result = turn(auth_client, "cheaper please")
    quote = result["structured"]["preview_quote"]
    assert quote["ok"]
    assert all(hit["unit_label"] == "500 ml" for hit in result["structured"]["hits"])
    before = auth_client.get(f"/v1/carts/{cart}").json()["lines"]
    assert sorted(before, key=lambda line: line["sku"]) == sorted(
        [{"sku": milk, "quantity": 3}, {"sku": bread, "quantity": 2}], key=lambda line: line["sku"]
    )
    # Quote output contains the replacement quantities and unchanged bread, not one milk alone.
    assert quote["total"]["minor"] > 0
    assert {"sku": bread, "quantity": 2} in quote["lines"]
    assert sum(line["quantity"] for line in quote["lines"]) == 5
    assert "proposal" not in result["structured"]


@pytest.mark.db
def test_cart_acknowledgement_preserves_product_for_quantity_correction(api_app, auth_client):
    runner = SpecialistBridge(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    cart = create(auth_client)
    shown = turn(auth_client, "Show me milk")
    sku = shown["structured"]["hits"][0]["sku"]
    runner.remember_discovery(shown["principal_id"], [sku])
    updated = add(auth_client, cart, sku)
    turn(auth_client, "Cart updated", cart_event_id=updated["sales_event_id"])
    assert runner.displayed_products(shown["principal_id"]) == (sku,)
    correction = turn(auth_client, "Make it two")
    assert correction["structured"]["proposal"]["sku"] == sku
    assert correction["structured"]["proposal"]["quantity"] == 2
    assert correction["structured"]["proposal"]["delta"] == 1
    assert auth_client.get(f"/v1/carts/{cart}").json()["lines"] == [{"sku": sku, "quantity": 1}]

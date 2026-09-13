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


@pytest.mark.parametrize(
    "message", ["Can you find me iPhone?", "Find me an iPhone", "मुझे आईफोन दिखाओ"]
)
def test_iphone_discovery_is_direct(message):
    assert discovery_query(message) == "iphone"


@pytest.mark.parametrize(
    "message", ["add it", "Add this to my cart", "isko cart mein add karo", "इसे कार्ट में जोड़ दो"]
)
@pytest.mark.db
def test_single_displayed_product_add_is_bound_and_never_mutates(api_app, auth_client, message):
    import uuid

    from commerce_api.services.agent_bridge import SpecialistBridge

    class NoModel(SpecialistBridge):
        def run(self, *args):
            raise AssertionError("One displayed item needs no model round")

    runner = NoModel(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    cart = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    shown = auth_client.post("/v1/agent/turn", json={"message": "Show me milk"}).json()
    sku = shown["structured"]["hits"][0]["sku"]
    runner.remember_discovery(shown["principal_id"], [sku])
    response = auth_client.post("/v1/agent/turn", json={"message": message})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["routing_reason"] == "direct_displayed_product_proposal"
    proposal = body["structured"]["proposal"]
    assert proposal["sku"] == sku
    assert proposal["quantity"] == 1
    assert proposal["cart_id"] == cart["cart_id"]
    assert proposal["binding"]["unit_price_minor"] > 0
    assert isinstance(proposal["binding"]["catalogue_revision"], int)
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == []
    applied = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{sku}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": proposal["quantity"], "expected": proposal["binding"]},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["lines"] == [{"sku": sku, "quantity": 1}]


def test_add_questions_negation_and_compounds_do_not_use_shortcut():
    from commerce_api.services.discovery import single_product_add

    for message in [
        "Can you add it?",
        "do not add it",
        "add it and pay",
        "add two of these",
        "yes",
        "इसे मत जोड़ो",
    ]:
        assert single_product_add(message) is False


@pytest.mark.parametrize(
    "message,query",
    [
        ("Show me Samsung Galaxy S26 Ultra", "samsung galaxy s26 ultra"),
        ("Find me Sony WH-1000XM6", "sony wh-1000xm6"),
        ("Can you find me a Dyson air purifier?", "dyson air purifier"),
        ("Show me kitchen appliances", "kitchen appliances"),
        ("I am looking for notebooks", "notebooks"),
        ("Mujhe gaming keyboard dikhao", "gaming keyboard"),
        ("मुझे लैपटॉप दिखाओ", "लैपटॉप"),
        ("Apple iPhone 17 Pro 256 GB", "apple iphone 17 pro 256 gb"),
        ("Show me BrandNew Unlisted Product", "brandnew unlisted product"),
    ],
)
def test_discovery_is_not_limited_to_predefined_products(message, query):
    assert discovery_query(message) == query


def test_primary_add_target_does_not_remove_related_charger_from_search_results():
    from commerce_api.services.discovery import prefer_named_hits

    phone = {"sku": "phone", "display_name": "Apple iPhone 17 Pro 256 GB"}
    charger = {"sku": "charger", "display_name": "Apple 20W USB-C Power Adapter"}
    hits = [phone, charger]
    assert prefer_named_hits("iphone", hits) == [phone]
    assert hits == [phone, charger]
    assert prefer_named_hits("mobile", [phone, charger]) == [phone, charger]


@pytest.mark.db
def test_ambiguous_add_clarifies_without_a_model_or_cart_mutation(api_app, auth_client):
    from commerce_api.services.agent_bridge import SpecialistBridge

    class NoModel(SpecialistBridge):
        def run(self, *args):
            raise AssertionError("Ambiguity should ask the buyer directly")

    runner = NoModel(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    shown = auth_client.post("/v1/agent/turn", json={"message": "Show me milk"}).json()
    runner.remember_discovery(shown["principal_id"], ["AMUL-DAIRY-001", "AASH-STPL-002"])
    response = auth_client.post("/v1/agent/turn", json={"message": "add it"}).json()
    assert response["routing_reason"] == "displayed_product_clarification"
    assert "proposal" not in response["structured"]
    assert len(response["structured"]["hits"]) == 2


def test_primary_target_matches_canonical_name_when_display_is_localized():
    from commerce_api.services.discovery import prefer_named_hits

    phone = {"sku": "phone", "display_name": "एप्पल आईफोन", "name_en": "Apple iPhone"}
    charger = {"sku": "charger", "display_name": "एप्पल चार्जर", "name_en": "Apple Charger"}
    assert prefer_named_hits("iphone", [phone, charger]) == [phone]


@pytest.mark.parametrize("message", ["Add milk to my cart", "milk cart mein add karo"])
@pytest.mark.db
def test_named_add_needs_no_model_when_catalogue_has_one_match(api_app, auth_client, message):
    import uuid

    from commerce_api.services.agent_bridge import SpecialistBridge

    class NoModel(SpecialistBridge):
        def run(self, *args):
            raise AssertionError("Named add should resolve from catalogue")

    api_app.state.agent_runner = NoModel(None, fast_discovery=True)
    auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())})
    response = auth_client.post("/v1/agent/turn", json={"message": message})
    assert response.status_code == 200
    body = response.json()
    assert body["routing_reason"] in {
        "direct_displayed_product_proposal",
        "displayed_product_clarification",
    }
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == []


@pytest.mark.parametrize(
    "message",
    [
        "add two milk",
        "add milk and pay",
        "do not add milk",
        "add milk under 50",
        "add 2 milk",
        "can you add milk?",
        "add this",
    ],
)
def test_named_add_does_not_drop_quantities_constraints_or_negation(message):
    from commerce_api.services.discovery import named_product_add

    assert named_product_add(message) is None


def test_fuzzy_singleton_is_not_a_named_add_target():
    from commerce_api.services.discovery import prefer_named_hits

    wrong = {"sku": "PICKLE", "display_name": "Tops Mixed Pickle"}
    assert prefer_named_hits("toys", [wrong], strict=True) == []


@pytest.mark.db
@pytest.mark.parametrize("message", ["Add Aashirvad sharbati atta", "Show me zzzunknownproduct"])
def test_literal_miss_reaches_reasoning_without_changing_cart(api_app, auth_client, message):
    from commerce_api.services.agent_service import TurnOutcome

    class ReasoningRunner:
        fast_discovery = True

        def __init__(self):
            self.seen = []

        def run(self, turn, chosen, tools):
            self.seen.append(turn.message)
            return TurnOutcome(
                reply="Which pack size did you mean?", structured={"kind": "products", "hits": []}
            )

    runner = ReasoningRunner()
    api_app.state.agent_runner = runner
    response = auth_client.post("/v1/agent/turn", json={"message": message})
    assert response.status_code == 200, response.text
    assert runner.seen == [message]
    body = response.json()
    assert body["reply"] == "Which pack size did you mean?"
    assert not body["server_authored"]
    assert not body["structured"].get("proposal")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Find milk and bread", ("milk", "bread")),
        ("Mujhe doodh aur bread dikhao", ("milk", "bread")),
        ("मुझे दूध और ब्रेड दिखाओ", ("milk", "bread")),
        ("Show milk, bread, eggs", ("milk", "bread", "eggs")),
    ],
)
def test_literal_multi_search_preserves_each_query(message, expected):
    from commerce_api.services.discovery import discovery_queries

    assert discovery_queries(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "Find milk and pay now",
        "milk under 50 and bread",
        "Compare milk and bread",
        "Find milk without lactose and bread",
        "Find milk and add bread",
        "Find milk, bread, eggs, rice, tea",
        "milk and not bread",
    ],
)
def test_multi_search_does_not_drop_constraints_or_actions(message):
    from commerce_api.services.discovery import discovery_queries

    assert discovery_queries(message) is None


@pytest.mark.db
@pytest.mark.parametrize("message", ["Find milk and bread", "मुझे दूध और ब्रेड दिखाओ"])
def test_multi_discovery_needs_no_model_and_never_mutates_cart(api_app, auth_client, message):
    import uuid

    class NoModel:
        def run(self, *args):
            raise AssertionError("Simple multi-product search must not depend on the model")

    api_app.state.agent_runner = NoModel()
    auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())})
    response = auth_client.post("/v1/agent/turn", json={"message": message})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["routing_reason"] == "direct_multi_product_discovery"
    groups = body["structured"]["discovery_groups"]
    assert [group["query"] for group in groups] == ["milk", "bread"]
    assert all(group["skus"] for group in groups)
    assert [call["name"] for call in body["tool_calls"]] == ["catalog.search", "catalog.search"]
    skus = [hit["sku"] for hit in body["structured"]["hits"]]
    assert len(skus) == len(set(skus))
    assert "proposal" not in body["structured"]
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == []


@pytest.mark.db
def test_four_searches_fit_existing_displayed_product_context(api_app, auth_client):
    from commerce_api.services.agent_bridge import SpecialistBridge

    class NoModel(SpecialistBridge):
        def run(self, *args):
            raise AssertionError("Literal searches do not need a model")

    runner = NoModel(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    response = auth_client.post(
        "/v1/agent/turn", json={"message": "Find milk, bread, rice, coffee"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    skus = body["structured"]["skus"]
    assert 1 <= len(skus) <= 5
    assert len(body["structured"]["discovery_groups"]) == 4
    assert tuple(skus) == runner.displayed_order(body["principal_id"])

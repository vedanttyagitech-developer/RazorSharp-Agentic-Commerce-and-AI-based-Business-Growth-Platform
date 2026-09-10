import time
import uuid

import pytest
from commerce_api.services.adaptive_shopping import comparison_plan
from commerce_api.services.agent_bridge import SpecialistBridge
from commerce_api.services.discovery import resolved_named_intent
from commerce_api.services.product_reference import product_reference


@pytest.mark.parametrize(
    "message,query,count,mode",
    [
        ("Do packet doodh cart mein add karo", "milk", 2, "add"),
        ("दो पैकेट दूध कार्ट में जोड़ दो", "milk", 2, "add"),
        ("३ पैकेट ब्रेड डाल दो", "bread", 3, "add"),
        ("add two packets of Amul Taaza 500 ml", "amul taaza 500 ml", 2, "add"),
        ("Amul Taaza 500 ml ki quantity teen kar do", "amul taaza 500 ml", 3, "set"),
    ],
)
def test_explicit_count_is_not_pack_size(message, query, count, mode):
    intent = resolved_named_intent(message)
    assert intent and (intent.query, intent.quantity, intent.mode) == (query, count, mode)


@pytest.mark.parametrize(
    "message",
    [
        "do not add milk",
        "दो दूध मत जोड़ो",
        "add two milk and pay",
        "add two milk under 40",
        "add 2 or 3 milk",
        "do you have milk",
        "add two milk?",
        "add eleven milk",
    ],
)
def test_unsafe_or_ambiguous_commands_do_not_take_fast_path(message):
    assert resolved_named_intent(message) is None


@pytest.mark.parametrize("message", ["do nahi teen chahiye", "दो नहीं, तीन चाहिए", "not two, three"])
def test_correction_sets_quantity_instead_of_incrementing(message):
    ref = product_reference(message)
    assert ref and ref.mode == "set" and ref.quantity == 3


@pytest.mark.parametrize("message", ["doodh aur bread compare karo", "दूध और ब्रेड की तुलना करो"])
def test_multilingual_comparisons_need_no_model(message):
    plan = comparison_plan(message)
    assert [n["query"] for n in plan["needs"]] == ["milk", "bread"]


@pytest.mark.db
@pytest.mark.parametrize(
    "message",
    ["do packet Amul Taaza 500 ml cart mein add karo", "दो पैकेट Amul Taaza 500 ml कार्ट में जोड़ दो"],
)
def test_named_add_then_correction_uses_validated_bound_proposal(api_app, auth_client, message):
    class NoModel(SpecialistBridge):
        def run(self, *args):
            raise AssertionError("No model needed")

    api_app.state.agent_runner = NoModel(None, fast_discovery=True)
    cart = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    t = time.perf_counter()
    r = auth_client.post("/v1/agent/turn", json={"message": message})
    assert r.status_code == 200, r.text
    proposal = r.json()["structured"]["proposal"]
    assert proposal["quantity"] == 2
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == []
    applied = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{proposal['sku']}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": 2, "expected": proposal["binding"]},
    )
    assert applied.status_code == 200, applied.text
    correction = auth_client.post(
        "/v1/agent/turn", json={"message": "do nahi teen chahiye"}
    ).json()["structured"]["proposal"]
    assert correction["quantity"] == 3 and correction["delta"] == 1
    print(f"validated add + correction: {(time.perf_counter() - t) * 1000:.1f}ms")


@pytest.mark.db
def test_stalled_planner_clarifies_without_second_model_or_cart_mutation(api_app, auth_client):
    import asyncio

    class Stalled:
        async def plan_shopping(self, *args):
            await asyncio.Event().wait()

        async def __call__(self, *args):
            raise AssertionError("No fallback model loop")

    runner = SpecialistBridge(Stalled(), fast_discovery=True)
    runner._planning_timeout_s = 0.01
    api_app.state.agent_runner = runner
    r = auth_client.post("/v1/agent/turn", json={"message": "plan a party bundle under 1500"})
    assert r.status_code == 200, r.text
    assert r.json()["structured"]["planning_status"] == "planner_timeout"
    assert not r.json()["structured"]["hits"]
    assert not r.json()["tool_calls"]


@pytest.mark.parametrize(
    "message", ["remove Amul Taaza 500 ml from cart", "Amul Taaza 500 ml कार्ट से हटा दो"]
)
def test_named_removal_uses_absolute_zero(message):
    intent = resolved_named_intent(message)
    assert intent and intent.mode == "set" and intent.quantity == 0


@pytest.mark.parametrize(
    "message,budget",
    [
        ("under ₹1,500", (150000, False)),
        ("₹1,500 mein snacks", (150000, True)),
        ("१५०० रुपये में snacks", (150000, True)),
        ("under 1,50", None),
    ],
)
def test_budget_punctuation_cannot_truncate_amount(message, budget):
    from commerce_api.services.adaptive_shopping import stated_budget

    assert stated_budget(message) == budget


def test_local_replacement_preserves_untouched_groups_and_constraints():
    from commerce_api.services.adaptive_shopping import replacement_plan

    previous = {
        "plan": {
            "mode": "bundle",
            "needs": [
                {
                    "query": "bread",
                    "quantity": 2,
                    "required_terms": [],
                    "excluded_terms": ["white"],
                },
                {"query": "milk", "quantity": 1, "required_terms": [], "excluded_terms": []},
            ],
            "clarification": "",
            "unverified_requirements": [],
        }
    }
    result = replacement_plan("bread ki jagah oats", previous)
    assert result["needs"][0] == {**previous["plan"]["needs"][0], "query": "oats"}
    assert result["needs"][1] == previous["plan"]["needs"][1]
    assert previous["plan"]["needs"][0]["query"] == "bread"
    assert replacement_plan("bread ki jagah oats under 50", previous) is None


def test_retrieval_deduplicates_only_within_the_current_turn():
    from types import SimpleNamespace

    from commerce_api.services.adaptive_shopping import execute

    class Tools:
        calls = 0

        def call(self, name, **kwargs):
            assert name == "catalog.search"
            self.calls += 1
            return SimpleNamespace(ok=True, payload={"hits": []})

    tools = Tools()
    plan = {
        "mode": "compare",
        "needs": [{"query": "milk", "quantity": 1, "required_terms": [], "excluded_terms": []}] * 2,
        "clarification": "",
        "unverified_requirements": [],
    }
    execute(plan, tools, "en", None)
    assert tools.calls == 1
    execute(plan, tools, "en", None)
    assert tools.calls == 2  # no stale catalogue cache between turns


def test_plan_schema_bounds_groups_and_term_lengths():
    from agent_runtime.runtime_adk.shopping_plan import ShoppingPlan
    from pydantic import ValidationError

    plan = {
        "mode": "compare",
        "needs": [{"query": "milk", "quantity": 1, "required_terms": [], "excluded_terms": []}] * 5,
        "clarification": "",
        "unverified_requirements": [],
    }
    with pytest.raises(ValidationError):
        ShoppingPlan.model_validate(plan)
    plan["needs"] = plan["needs"][:1]
    plan["needs"][0]["required_terms"] = ["x" * 81]
    with pytest.raises(ValidationError):
        ShoppingPlan.model_validate(plan)


@pytest.mark.parametrize(
    "message,minor", [("iphone 50,000 rupaye ke andar", 5000000), ("दूध ५० रुपये से कम", 5000)]
)
def test_hindi_price_filter_is_deterministic(message, minor):
    from commerce_api.services.search_constraints import price_search

    assert price_search(message).maximum_minor == minor


def test_pack_spacing_does_not_hide_a_named_match():
    from commerce_api.services.discovery import prefer_named_hits

    hit = {"display_name": "Amul Taaza 500 ml"}
    assert prefer_named_hits("Amul Taaza 500ml", [hit], strict=True) == [hit]


@pytest.mark.db
def test_replacement_after_unanswered_clarification_does_not_invent_a_bundle(api_app, auth_client):
    class Clarifier:
        calls = 0

        async def plan_shopping(self, *args):
            self.calls += 1
            assert self.calls == 1, "Missing source item should clarify locally"
            return {
                "mode": "clarify",
                "needs": [],
                "clarification": "Which products?",
                "unverified_requirements": [],
            }

        async def __call__(self, *args):
            raise AssertionError("No tool loop")

    planner = Clarifier()
    api_app.state.agent_runner = SpecialistBridge(planner, fast_discovery=True)
    auth_client.post("/v1/agent/turn", json={"message": "breakfast for two under 300"})
    r = auth_client.post("/v1/agent/turn", json={"message": "bread ki jagah oats"}).json()
    assert r["structured"]["planning_status"] == "clarification_required"
    assert r["structured"]["model_rounds"] == 0
    assert r["structured"]["hits"] == []
    assert not r["tool_calls"]


@pytest.mark.parametrize(
    "message", ["Doosra wala do packet cart mein jod do.", "doosra wala do packet jodo"]
)
def test_actual_romanized_stt_add_transcript_uses_fast_reference(message):
    ref = product_reference(message)
    assert ref and ref.index == 1 and ref.quantity == 2


@pytest.mark.db
def test_quantity_correction_targets_last_cart_write_not_suggested_complement(api_app, auth_client):
    runner = SpecialistBridge(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    cart = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    added = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/AMUL-DAIRY-001",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": 2},
    )
    assert added.status_code == 200
    auth_client.post("/v1/agent/turn", json={"message": "show me bread"})
    r = auth_client.post("/v1/agent/turn", json={"message": "do nahi teen chahiye"}).json()
    assert r["structured"]["proposal"]["sku"] == "AMUL-DAIRY-001"
    assert r["structured"]["proposal"]["quantity"] == 3

import uuid

import pytest
from commerce_api.services.agent_bridge import SpecialistBridge
from commerce_api.services.product_reference import product_reference


@pytest.mark.parametrize(
    "message,index,quantity",
    [
        ("add two of the second one", 1, 2),
        ("second wala add karo", 1, 1),
        ("doosra wala do quantity add karo", 1, 2),
        ("दूसरा वाला दो कार्ट में जोड़ दो", 1, 2),
        ("add 3 of it", None, 3),
        ("isko do add karo", None, 2),
    ],
)
def test_reference_grammar(message, index, quantity):
    ref = product_reference(message)
    assert ref is not None
    assert (ref.index, ref.quantity) == (index, quantity)


@pytest.mark.parametrize(
    "message",
    [
        "do not add second",
        "add second and pay",
        "add 12 of the second",
        "second wala nahi add karo",
        "add 2 or 3 of it",
        "is second good?",
    ],
)
def test_no_guessing_or_dropped_constraints(message):
    assert product_reference(message) is None


@pytest.mark.db
@pytest.mark.parametrize(
    "message",
    ["add two of the second one", "doosra wala do quantity add karo", "दूसरा वाला दो कार्ट में जोड़ दो"],
)
def test_displayed_ordinal_quantity_and_actual_cart_write(api_app, auth_client, message):
    class NoModel(SpecialistBridge):
        def run(self, *args):
            raise AssertionError("Explicit reference needs no model call")

    runner = NoModel(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    cart = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    shown = auth_client.post("/v1/agent/turn", json={"message": "Show me milk"}).json()
    rows = shown["structured"]["hits"]
    runner.remember_discovery(
        shown["principal_id"], [r["sku"] for r in rows], preferred_sku=rows[0]["sku"]
    )
    result = auth_client.post("/v1/agent/turn", json={"message": message}).json()
    proposal = result["structured"]["proposal"]
    assert proposal["sku"] == rows[1]["sku"]
    assert proposal["quantity"] == 2
    assert proposal["delta"] == 2
    assert auth_client.get("/v1/carts/current").json()["cart"]["lines"] == []
    applied = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{proposal['sku']}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": proposal["quantity"], "expected": proposal["binding"]},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["lines"] == [{"sku": rows[1]["sku"], "quantity": 2}]
    correction = auth_client.post("/v1/agent/turn", json={"message": "add 3 of it"}).json()
    assert correction["structured"]["proposal"]["quantity"] == 5


@pytest.mark.db
def test_unseen_reference_never_selects_another_product(api_app, auth_client):
    api_app.state.agent_runner = SpecialistBridge(None, fast_discovery=True)
    result = auth_client.post(
        "/v1/agent/turn", json={"message": "add two of the second one"}
    ).json()
    assert result["routing_reason"] == "displayed_reference_missing"
    assert "proposal" not in result["structured"]


@pytest.mark.db
@pytest.mark.parametrize(
    "message,target",
    [("set it to two", 2), ("isko quantity do kar do", 2), ("remove it", 0), ("इसे हटा दो", 0)],
)
def test_quantity_correction_and_removal_bind_absolute_quantity(
    api_app, auth_client, message, target
):
    runner = SpecialistBridge(None, fast_discovery=True)
    api_app.state.agent_runner = runner
    cart = auth_client.post("/v1/carts", headers={"Idempotency-Key": str(uuid.uuid4())}).json()
    shown = auth_client.post("/v1/agent/turn", json={"message": "Show me milk"}).json()
    sku = shown["structured"]["hits"][0]["sku"]
    auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{sku}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": 3},
    )
    runner.remember_discovery(shown["principal_id"], [sku])
    result = auth_client.post("/v1/agent/turn", json={"message": message}).json()
    proposal = result["structured"]["proposal"]
    assert proposal["quantity"] == target
    assert proposal["delta"] == target - 3
    applied = auth_client.put(
        f"/v1/carts/{cart['cart_id']}/lines/{sku}",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"quantity": proposal["quantity"], "expected": proposal["binding"]},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["lines"] == ([{"sku": sku, "quantity": target}] if target else [])

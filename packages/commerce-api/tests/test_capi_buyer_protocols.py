import uuid

import pytest

pytestmark = pytest.mark.db


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_buyer_protocol_checkout_is_real_bound_and_idempotent(auth_client, protocol):
    key = str(uuid.uuid4())
    path = f"/v1/buyer-protocols/{protocol}/checkouts"
    body = {"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]}
    response = auth_client.post(path, json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 200, response.text
    card = response.json()["card"]
    assert card["content_hash"]
    assert card["amount_minor"] > 0
    repeated = auth_client.post(path, json=body, headers={"Idempotency-Key": key})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["card"]["checkout_id"] == card["checkout_id"]
    state = auth_client.get(path + "/" + card["checkout_id"])
    assert state.status_code == 200, state.text
    assert state.json()["checkout"]["attempt"] is None
    assert state.json()["checkout"]["order_id"] is None
    assert state.json()["checkout"]["approval_card"]["content_hash"] == card["content_hash"]
    if protocol == "UCP":
        assert state.json()["protocol_response"]["status"] == "requires_escalation"


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_protocol_buyer_approval_and_verified_capture(
    auth_client, demo_session, capi_kernel_engine, protocol
):
    import hashlib

    import transaction_kernel as tk
    from commerce_domain import CheckoutRef, Money, RecoveryCode
    from platform_db.tenancy import set_tenant
    from sqlalchemy.orm import Session
    from transaction_kernel.payments import ProviderOrderOutcome

    path = f"/v1/buyer-protocols/{protocol}/checkouts"
    created = auth_client.post(
        path,
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 200, created.text
    card = created.json()["card"]
    approve = f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve-and-pay"
    binding = {k: card[k] for k in ("content_hash", "amount_minor", "currency")}
    wrong = auth_client.post(
        approve,
        json={**binding, "amount_minor": card["amount_minor"] - 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert wrong.status_code >= 400 or not wrong.json().get("allowed", False)
    accepted = auth_client.post(
        approve, json=binding, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert accepted.status_code == 200, accepted.text
    decision = accepted.json()
    assert decision["allowed"], decision
    blocked_edit = auth_client.put(
        path + "/" + card["checkout_id"],
        json={
            "items": [{"sku": "AMUL-DAIRY-001", "quantity": 2}],
            "version": card["version"],
            "content_hash": card["content_hash"],
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert blocked_edit.status_code == 409, blocked_edit.text
    unchanged = auth_client.get(path + "/" + card["checkout_id"]).json()["checkout"]
    assert unchanged["current_version"] == card["version"]
    if protocol == "UCP":
        admitted = auth_client.get(path + "/" + card["checkout_id"]).json()
        assert admitted["protocol_response"]["status"] == "complete_in_progress"
    checkout_id = uuid.UUID(card["checkout_id"])
    attempt_id = uuid.UUID(decision["payment_attempt_id"])
    provider_order = "order_" + uuid.uuid4().hex[:14]
    payment = "pay_" + uuid.uuid4().hex[:14]
    amount = Money(card["amount_minor"], card["currency"])
    # Simulated provider evidence, applied through the real kernel, never a browser success flag.
    with Session(capi_kernel_engine) as session, session.begin():
        set_tenant(session, demo_session.tenant_id)
        tk.consume_grant(
            session,
            uuid.UUID(decision["grant_id"]),
            tk.GrantBinding(
                tenant_id=demo_session.tenant_id,
                checkout=CheckoutRef(checkout_id, card["version"], card["content_hash"]),
                payment_attempt_id=attempt_id,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                amount=amount,
            ),
        )
        tk.record_create_order_result(
            session,
            tenant_id=demo_session.tenant_id,
            payment_attempt_id=attempt_id,
            outcome=ProviderOrderOutcome(
                kind="ok",
                provider_order_id=provider_order,
                code=RecoveryCode.OK,
                reason="test_provider",
            ),
            correlation_id=uuid.uuid4(),
        )
    pending = auth_client.get(path + "/" + str(checkout_id)).json()
    assert pending["checkout"]["order_id"] is None
    with Session(capi_kernel_engine) as session, session.begin():
        set_tenant(session, demo_session.tenant_id)
        tk.apply_provider_evidence(
            session,
            tenant_id=demo_session.tenant_id,
            payment_attempt_id=attempt_id,
            evidence=tk.ProviderEvidence.from_mapping(
                {
                    "source": "PROVIDER_FETCH",
                    "provider_payment_id": payment,
                    "provider_order_id": provider_order,
                    "amount_minor": amount.minor,
                    "currency": amount.currency,
                    "status": "captured",
                    "provider_status": "captured",
                    "amount_refunded_minor": 0,
                    "raw_digest": hashlib.sha256(payment.encode()).hexdigest(),
                }
            ),
            correlation_id=uuid.uuid4(),
        )
    settled = auth_client.get(path + "/" + str(checkout_id))
    assert settled.status_code == 200, settled.text
    assert settled.json()["checkout"]["order_id"]
    assert settled.json()["protocol_response"]["status"].lower() == "completed"
    order = auth_client.get("/v1/orders/" + settled.json()["checkout"]["order_id"])
    assert order.status_code == 200, order.text


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_price_change_requires_fresh_buyer_approval(auth_client, api_app, demo_session, protocol):
    from commerce_domain import Money

    from conftest import merchant_mutation

    response = auth_client.post(
        f"/v1/buyer-protocols/{protocol}/checkouts",
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    card = response.json()["card"]
    with merchant_mutation(api_app, demo_session) as merchant:
        merchant.set_price("AMUL-DAIRY-001", Money(3100, "INR"))
    response = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve-and-pay",
        json={k: card[k] for k in ("content_hash", "amount_minor", "currency")},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is False
    assert response.json()["code"] == "REAPPROVAL_REQUIRED"
    view = auth_client.get(
        f"/v1/buyer-protocols/{protocol}/checkouts/{card['checkout_id']}"
    ).json()["checkout"]
    assert view["attempt"] is None
    assert view["approval_card"]["content_hash"] != card["content_hash"]


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_non_buyer_cannot_run_buyer_journey(client, seeded_tenant, scenario_headers, protocol):
    session = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "MERCHANT"},
        headers=scenario_headers,
    )
    assert session.status_code == 201
    response = client.post(
        f"/v1/buyer-protocols/{protocol}/checkouts",
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={
            "Authorization": "Bearer " + session.json()["token"],
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_protocol_checkout_cannot_be_read_by_another_buyer(
    auth_client, client, seeded_tenant, scenario_headers, protocol
):
    created = auth_client.post(
        f"/v1/buyer-protocols/{protocol}/checkouts",
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 200, created.text
    other = client.post(
        "/v1/demo/sessions",
        json={
            "tenant_slug": seeded_tenant.tenant_slug,
            "actor_type": "BUYER",
            "buyer_ref": "other-protocol-buyer",
        },
        headers=scenario_headers,
    )
    assert other.status_code == 201
    response = client.get(
        f"/v1/buyer-protocols/{protocol}/checkouts/{created.json()['card']['checkout_id']}",
        headers={"Authorization": "Bearer " + other.json()["token"]},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_rejected_basket_can_be_corrected(auth_client, protocol):
    path = f"/v1/buyer-protocols/{protocol}/checkouts"
    response = auth_client.post(
        path,
        json={"items": [{"sku": "not-a-product", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 404, response.text
    assert response.json()["title"] == "Product not found"
    corrected = auth_client.post(
        path,
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["card"]["checkout_id"]


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_stock_rejection_exposes_recoverable_basket_error(
    auth_client, api_app, demo_session, protocol
):
    from conftest import merchant_mutation

    with merchant_mutation(api_app, demo_session) as scenario:
        scenario.set_stock("AMUL-DAIRY-001", 1)
    path = f"/v1/buyer-protocols/{protocol}/checkouts"
    response = auth_client.post(
        path,
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 2}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 409, response.text
    assert response.json()["title"] == "Cart cannot be priced"
    corrected = auth_client.post(
        path,
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert corrected.status_code == 200, corrected.text


def test_acp_reuses_live_transport_without_constructing_an_app(
    auth_client, api_app, demo_session, monkeypatch
):
    from commerce_api import app as app_module
    from commerce_api.routers import buyer_protocols
    from commerce_domain import Money

    from conftest import merchant_mutation

    def forbidden_app(*_args, **_kwargs):
        raise AssertionError("ACP checkout must not construct another application")

    monkeypatch.setattr(app_module, "create_app", forbidden_app)
    original = buyer_protocols.serve_signed
    seen = []

    def observe(signed, settings, registry, limiter):
        seen.append((registry, limiter))
        return original(signed, settings, registry, limiter)

    monkeypatch.setattr(buyer_protocols, "serve_signed", observe)
    with merchant_mutation(api_app, demo_session) as merchant:
        merchant.set_price("AMUL-DAIRY-001", Money(3100, "INR"))
    key = str(uuid.uuid4())
    responses = [
        auth_client.post(
            "/v1/buyer-protocols/ACP/checkouts",
            json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
            headers={"Idempotency-Key": key},
        )
        for _ in range(2)
    ]
    for response in responses:
        assert response.status_code == 200, response.text
    assert responses[0].json()["card"] == responses[1].json()["card"]
    assert (
        responses[0].json()["protocol_response"]["session"]["totals"]["items_subtotal_minor"]
        == 3100
    )
    assert seen[0][0] is api_app.state.merchants
    assert seen[1][0] is seen[0][0]
    assert seen[1][1] is seen[0][1] is api_app.state.acp_limiter


def test_demo_acp_still_rejects_invalid_signed_transport(auth_client, monkeypatch):
    from commerce_protocols.acp.simulator import AcpBuyerSimulator, Misbehaviour

    original = AcpBuyerSimulator.request

    def tampered(self, **kwargs):
        return original(self, **kwargs, misbehave=Misbehaviour.TAMPERED_SIGNATURE)

    monkeypatch.setattr(AcpBuyerSimulator, "request", tampered)
    response = auth_client.post(
        "/v1/buyer-protocols/ACP/checkouts",
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "acp_signature_did_not_verify"


@pytest.mark.parametrize("protocol", ["ACP", "UCP"])
def test_protocol_buyer_can_edit_same_checkout_and_cancel(auth_client, protocol):
    path = f"/v1/buyer-protocols/{protocol}/checkouts"
    created = auth_client.post(
        path,
        json={"items": [{"sku": "AMUL-DAIRY-001", "quantity": 1}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 200, created.text
    old = created.json()["card"]
    url = path + "/" + old["checkout_id"]
    body = {
        "items": [{"sku": "AMUL-DAIRY-001", "quantity": 2}],
        "version": old["version"],
        "content_hash": old["content_hash"],
    }
    key = str(uuid.uuid4())
    changed = auth_client.put(url, json=body, headers={"Idempotency-Key": key})
    assert changed.status_code == 200, changed.text
    card = changed.json()["card"]
    assert card["checkout_id"] == old["checkout_id"]
    assert card["version"] == old["version"] + 1
    assert card["content_hash"] != old["content_hash"]
    replay = auth_client.put(url, json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 200, replay.text
    assert replay.json() == changed.json()
    stale = auth_client.put(url, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert stale.status_code == 409, stale.text
    view = auth_client.get(url).json()["checkout"]
    assert view["attempt"] is None
    cancelled = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/cancel",
        json={"reason": "buyer_requested"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["allowed"]
    assert auth_client.get(url).json()["checkout"]["state"] == "CANCELLED"


@pytest.mark.parametrize(
    "state,payment,closed,order,expected,code",
    [
        (
            "APPROVAL_REQUIRED",
            None,
            False,
            None,
            "requires_escalation",
            "payment_requires_buyer_present_checkout",
        ),
        ("EXECUTION_PENDING", "CREATED", False, None, "complete_in_progress", None),
        (
            "AWAITING_PAYMENT",
            "SUBMITTED",
            False,
            None,
            "requires_escalation",
            "payment_requires_buyer_present_checkout",
        ),
        ("AWAITING_PAYMENT", "SUBMITTED", True, None, "complete_in_progress", None),
        ("AWAITING_PAYMENT", "AUTHORIZED", False, None, "complete_in_progress", None),
        ("PAYMENT_UNKNOWN", "UNKNOWN", False, None, "complete_in_progress", None),
        (
            "PAYMENT_UNKNOWN",
            "ESCALATED",
            False,
            None,
            "requires_escalation",
            "payment_requires_merchant_review",
        ),
        (
            "PAYMENT_FAILED",
            "FAILED",
            False,
            None,
            "requires_escalation",
            "payment_requires_fresh_review",
        ),
        ("CANCELLED", None, False, None, "canceled", None),
        ("INVALIDATED", "UNKNOWN", True, None, "complete_in_progress", None),
        ("EXPIRED", "EXPIRED", True, None, "canceled", None),
        ("COMPLETED", "CAPTURED", True, "order-1", "completed", None),
    ],
)
def test_ucp_projection_matches_payment_work(
    monkeypatch, state, payment, closed, order, expected, code
):
    from commerce_api.routers import buyer_protocols

    view = {
        "state": state,
        "current_version": 1,
        "order_id": order,
        "attempt": {"state": payment, "window_closed": closed} if payment else None,
    }
    monkeypatch.setattr(buyer_protocols.checkout_service, "read_checkout", lambda *_args: view)
    result = buyer_protocols.status("UCP", uuid.uuid4(), None, None, None)["protocol_response"]
    assert result["status"] == expected
    if code:
        assert result["messages"][0]["code"] == code
    assert result["order_id"] == order

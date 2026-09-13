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

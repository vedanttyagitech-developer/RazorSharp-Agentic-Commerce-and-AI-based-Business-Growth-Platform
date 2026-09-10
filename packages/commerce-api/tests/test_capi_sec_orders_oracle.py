"""Regression: another buyer's order is refused *exactly* as a missing one.

Found by the security review of 2026-09-05. ``GET /v1/orders/{id}`` and
``POST /v1/orders/{id}/refunds`` loaded the order tenant-scoped (``refund_service.load_order``)
and then called ``deps.assert_owner`` on the order's checkout. For an order that exists in
the tenant but belongs to another buyer, ``assert_owner`` answered ``404 "Checkout not
found"`` *and echoed the order's ``checkout_id``* -- a body distinct from the ``404 "Order
not found"`` a genuinely absent id produces. Two consequences, both forbidden by the
module docstring of ``routers/orders.py`` ("A missing order and somebody else's order are
both 404, because a 403 would confirm which order identifiers exist"):

* a same-tenant existence oracle: a buyer can tell a real order id from a fictional one by
  the problem title alone, and
* disclosure of a cross-buyer internal identifier (the order's ``checkout_id``).

The fix makes an ownership failure indistinguishable from a missing id. These tests build
a real alice-owned checkout, seed a captured order onto it, and assert that a *different*
buyer sees the same bytes for that order as for a random uuid, and never its ``checkout_id``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from commerce_domain import uuid7
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from conftest import MintedSession, SeededTenant

MILK = "AMUL-DAIRY-001"

MintClient = Callable[..., tuple[TestClient, MintedSession]]


def _idem(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    if extra:
        headers.update(extra)
    return headers


def _build_checkout(client: TestClient, *, sku: str = MILK, quantity: int = 2) -> dict[str, Any]:
    """Drive cart -> line -> checkout over HTTP and return the checkout card."""
    cart = client.post("/v1/carts", headers=_idem())
    assert cart.status_code < 300, cart.text
    cart_id = cart.json()["cart_id"]
    line = client.put(
        f"/v1/carts/{cart_id}/lines/{sku}", json={"quantity": quantity}, headers=_idem()
    )
    assert line.status_code < 300, line.text
    card = client.post(f"/v1/carts/{cart_id}/checkout", headers=_idem())
    assert card.status_code < 300, card.text
    return card.json()


def _seed_captured_order(
    engine: Engine,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    version: int,
    total_minor: int = 10_000,
    currency: str = "INR",
) -> uuid.UUID:
    """Insert the receipt, payment attempt and order a capture would have written.

    Uses the owner engine (as ``conftest.seeded_tenant`` does): the application roles hold
    no INSERT on financial tables, and the row-level-security predicate is satisfied by
    binding ``app.tenant_id`` for good measure. The policy-at-sale receipt was already
    written when the checkout was built, so it is read and reused rather than inserted
    again (its ``(tenant, checkout, version)`` is unique). Teardown is the ``seeded_tenant``
    fixture's job -- ``orders``, ``payment_attempts`` and ``policy_at_sale_receipts`` are
    all on its delete list.
    """
    order_id, attempt_id = uuid7(), uuid7()
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        receipt = conn.execute(
            text(
                "SELECT id, receipt_hash FROM policy_at_sale_receipts "
                "WHERE tenant_id = :t AND checkout_id = :c AND checkout_version = :v"
            ),
            {"t": tenant_id, "c": checkout_id, "v": version},
        ).one()
        receipt_id, receipt_hash = receipt.id, receipt.receipt_hash
        conn.execute(
            text(
                "INSERT INTO payment_attempts "
                "(id, tenant_id, checkout_id, checkout_version, status, "
                "amount_minor, currency, receipt) "
                "VALUES (:id, :t, :c, :v, 'CAPTURED', :amt, :cur, :r)"
            ),
            {
                "id": attempt_id,
                "t": tenant_id,
                "c": checkout_id,
                "v": version,
                "amt": total_minor,
                "cur": currency,
                "r": f"rcpt-{order_id.hex[:8]}",
            },
        )
        conn.execute(
            text(
                "INSERT INTO orders "
                "(id, tenant_id, merchant_id, checkout_id, checkout_version, "
                "payment_attempt_id, policy_receipt_id, policy_receipt_hash, "
                "total_minor, currency, status, capture_evidence) "
                "VALUES (:id, :t, :m, :c, :v, :a, :pr, :h, :amt, :cur, 'CONFIRMED', '{}'::jsonb)"
            ),
            {
                "id": order_id,
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "v": version,
                "a": attempt_id,
                "pr": receipt_id,
                "h": receipt_hash,
                "amt": total_minor,
                "cur": currency,
            },
        )
    return order_id


def _order_exists(engine: Engine, tenant_id: uuid.UUID, order_id: uuid.UUID) -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        count = conn.execute(
            text("SELECT count(*) FROM orders WHERE id = :o"), {"o": order_id}
        ).scalar_one()
    return int(count) == 1


def test_another_buyers_order_reads_as_a_missing_one(
    mint_client: MintClient, seeded_tenant: SeededTenant, capi_admin_engine: Engine
) -> None:
    alice_client, alice = mint_client(buyer_ref="sec-alice")
    bob_client, _bob = mint_client(buyer_ref="sec-bob")

    card = _build_checkout(alice_client)
    checkout_id = card["checkout_id"]
    order_id = _seed_captured_order(
        capi_admin_engine,
        tenant_id=seeded_tenant.tenant_id,
        merchant_id=alice.merchant_id,
        checkout_id=uuid.UUID(checkout_id),
        version=card["version"],
    )
    # The order genuinely exists in the tenant, so bob's 404 below is the "not owned"
    # branch, not the trivially-missing one.
    assert _order_exists(capi_admin_engine, seeded_tenant.tenant_id, order_id)

    real = bob_client.get(f"/v1/orders/{order_id}")
    fictional = bob_client.get(f"/v1/orders/{uuid.uuid4()}")

    assert real.status_code == 404, real.text
    assert fictional.status_code == 404, fictional.text
    real_body, fictional_body = real.json(), fictional.json()

    # Same title -- no existence oracle by problem type.
    assert real_body["title"] == fictional_body["title"] == "Order not found"
    # The order's checkout_id is never disclosed to a non-owner.
    assert "checkout_id" not in real_body
    assert checkout_id not in real.text
    # The order id is echoed (the thing the caller named), never the checkout's.
    assert real_body.get("order_id") == str(order_id)

    # Indistinguishable once the two fields that only ever echo the requested id -- the
    # ``order_id`` extension and the RFC 9457 ``instance`` (the request path) -- are
    # normalised away. Everything a caller could read a signal from is identical.
    def _normalise(body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "order_id": "X", "instance": "X"}

    assert _normalise(real_body) == _normalise(fictional_body)


def test_refunding_another_buyers_order_reads_as_a_missing_one(
    mint_client: MintClient, seeded_tenant: SeededTenant, capi_admin_engine: Engine
) -> None:
    alice_client, alice = mint_client(buyer_ref="sec-alice2")
    bob_client, _bob = mint_client(buyer_ref="sec-bob2")

    card = _build_checkout(alice_client)
    checkout_id = card["checkout_id"]
    order_id = _seed_captured_order(
        capi_admin_engine,
        tenant_id=seeded_tenant.tenant_id,
        merchant_id=alice.merchant_id,
        checkout_id=uuid.UUID(checkout_id),
        version=card["version"],
    )

    real = bob_client.post(
        f"/v1/orders/{order_id}/refunds",
        json={"amount_minor": 100, "reason": "probe"},
        headers=_idem(),
    )
    fictional = bob_client.post(
        f"/v1/orders/{uuid.uuid4()}/refunds",
        json={"amount_minor": 100, "reason": "probe"},
        headers=_idem(),
    )

    assert real.status_code == 403, real.text
    assert fictional.status_code == 403, fictional.text
    real_body = real.json()
    assert real_body["title"] == fictional.json()["title"] == "Capability not held"
    assert "checkout_id" not in real_body
    assert checkout_id not in real.text
    assert "order_id" not in real_body

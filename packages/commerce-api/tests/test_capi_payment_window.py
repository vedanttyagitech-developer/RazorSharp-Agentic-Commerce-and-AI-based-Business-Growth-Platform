"""Persisted payment deadline, unstarted closure, and late evidence through real SQL."""

import uuid
from datetime import datetime

import pytest
import transaction_kernel as tk
from commerce_domain import CheckoutRef, Money, RecoveryCode, uuid7
from platform_db import set_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_capi_journey import _approve, _basket_ready, _open_checkout, _submit
from test_capi_reserve import pay, permission
from transaction_kernel import payment_window
from transaction_kernel.grants import GrantExpiredError
from transaction_kernel.payments import ProviderEvidence, ProviderOrderOutcome

pytestmark = pytest.mark.db


def admitted(client):
    card = _open_checkout(client, _basket_ready(client)[0])
    _approve(client, card)
    result = _submit(client, card["checkout_id"], card["version"])
    assert result["allowed"], result
    return card, result


def lapse(engine, attempt):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE payment_attempts SET "
                "payment_window_expires_at=clock_timestamp()-interval '1 second' WHERE id=:p"
            ),
            {"p": uuid.UUID(attempt)},
        )


def binding(tenant, card, result):
    return tk.GrantBinding(
        tenant_id=tenant,
        checkout=CheckoutRef(uuid.UUID(card["checkout_id"]), card["version"], card["content_hash"]),
        payment_attempt_id=uuid.UUID(result["payment_attempt_id"]),
        operation=tk.Operation.PAYMENT_CREATE_ORDER,
        amount=Money(card["amount_minor"], card["currency"]),
    )


def test_deadline_is_saved_only_after_admission_and_never_resets(auth_client):
    card, result = admitted(auth_client)
    assert card["expires_at"] is None
    first = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()["attempt"]
    second = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()["attempt"]
    assert first["payment_window_expires_at"] == second["payment_window_expires_at"]
    remaining = (
        datetime.fromisoformat(first["payment_window_expires_at"])
        - datetime.fromisoformat(first["server_now"])
    ).total_seconds()
    assert 110 < remaining <= 120
    assert not first["window_closed"]


def test_expired_unconsumed_grant_cannot_execute_and_checkout_closes(
    auth_client, demo_session, capi_admin_engine, capi_kernel_engine
):
    card, result = admitted(auth_client)
    attempt = uuid.UUID(result["payment_attempt_id"])
    lapse(capi_admin_engine, str(attempt))
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, demo_session.tenant_id)
        with pytest.raises(GrantExpiredError):
            tk.consume_grant(
                s, uuid.UUID(result["grant_id"]), binding(demo_session.tenant_id, card, result)
            )
        assert payment_window.close_due(s, attempt, correlation_id=uuid7())
        assert not payment_window.close_due(s, attempt, correlation_id=uuid7())
    view = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    assert view["state"] == "CANCELLED"
    assert view["attempt"]["state"] == "EXPIRED"
    assert view["attempt"]["window_closed"]
    with capi_admin_engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM reservations WHERE checkout_id=:c"),
                {"c": uuid.UUID(card["checkout_id"])},
            ).scalar_one()
            == "RELEASED"
        )


def test_late_capture_is_stale_even_before_housekeeping(
    auth_client, demo_session, capi_admin_engine, capi_kernel_engine
):
    card, result = admitted(auth_client)
    attempt = uuid.UUID(result["payment_attempt_id"])
    tenant = demo_session.tenant_id
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, tenant)
        tk.consume_grant(s, uuid.UUID(result["grant_id"]), binding(tenant, card, result))
        tk.record_create_order_result(
            s,
            tenant_id=tenant,
            payment_attempt_id=attempt,
            outcome=ProviderOrderOutcome("ok", "order_window", RecoveryCode.OK, "created"),
            correlation_id=uuid7(),
        )
    lapse(capi_admin_engine, str(attempt))
    h = auth_client.get(f"/v1/checkouts/{card['checkout_id']}/payment").json()
    assert h["window_closed"]
    assert h["razorpay_order_id"] is None
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, tenant)
        applied = tk.apply_provider_evidence(
            s,
            tenant_id=tenant,
            payment_attempt_id=attempt,
            evidence=ProviderEvidence(
                source=tk.EvidenceSource.PROVIDER_FETCH,
                provider_payment_id="pay_window",
                provider_order_id="order_window",
                amount_minor=card["amount_minor"],
                currency=card["currency"],
                status="captured",
                provider_status="captured",
                raw_digest="a" * 64,
            ),
            correlation_id=uuid7(),
        )
        assert applied.stale_capture
        assert applied.order_id is None
    view = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    assert view["order_id"] is None
    assert view["attempt"]["state"] == "STALE_CAPTURE"


def test_unexecuted_reserve_expiry_returns_capacity_once(
    auth_client, demo_session, capi_admin_engine, capi_kernel_engine
):
    auth = permission(auth_client)
    card = _open_checkout(auth_client, _basket_ready(auth_client)[0])
    response = pay(auth_client, card, auth)
    assert response.status_code == 200, response.text
    result = response.json()
    attempt = result.get("payment_attempt_id") or result.get("attempt_id")
    assert attempt, result
    lapse(capi_admin_engine, attempt)
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, demo_session.tenant_id)
        assert payment_window.close_due(s, uuid.UUID(attempt), correlation_id=uuid7())
        assert not payment_window.close_due(s, uuid.UUID(attempt), correlation_id=uuid7())
    with capi_admin_engine.connect() as c:
        assert (
            c.execute(
                text("SELECT consumed_amount_minor FROM delegated_authorities WHERE id=:a"),
                {"a": uuid.UUID(auth["authority_id"])},
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT reserve_allocation FROM payment_attempts WHERE id=:p"),
                {"p": uuid.UUID(attempt)},
            ).scalar_one()
            == "RELEASED"
        )


def test_unknown_reserve_keeps_capacity_after_window_closes(
    auth_client, demo_session, capi_admin_engine, capi_kernel_engine
):
    from test_capi_reserve import execute

    auth = permission(auth_client)
    card = _open_checkout(auth_client, _basket_ready(auth_client)[0])
    response = pay(auth_client, card, auth)
    assert response.status_code == 200, response.text
    result = response.json()
    attempt = result.get("payment_attempt_id") or result.get("attempt_id")
    with capi_admin_engine.begin() as c:
        c.execute(
            text("UPDATE payment_attempts SET reserve_simulation_outcome='unknown' WHERE id=:p"),
            {"p": uuid.UUID(attempt)},
        )
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    lapse(capi_admin_engine, attempt)
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, demo_session.tenant_id)
        assert payment_window.close_due(s, uuid.UUID(attempt), correlation_id=uuid7())
    with capi_admin_engine.connect() as c:
        row = c.execute(
            text("SELECT reserve_allocation, status FROM payment_attempts WHERE id=:p"),
            {"p": uuid.UUID(attempt)},
        ).one()
        assert row.reserve_allocation == "HELD"
        assert row.status not in ("FAILED", "EXPIRED")
        assert (
            c.execute(
                text("SELECT consumed_amount_minor FROM delegated_authorities WHERE id=:a"),
                {"a": uuid.UUID(auth["authority_id"])},
            ).scalar_one()
            == card["amount_minor"]
        )

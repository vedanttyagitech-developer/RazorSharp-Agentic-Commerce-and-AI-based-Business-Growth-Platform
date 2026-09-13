"""Reserve UI contract through real API, database, admission and executor."""

import uuid

import pytest
from action_executor.handlers.reserve import handle_reserve
from action_executor.settings import WorkerSettings, build_runtime
from durable_work.commands import parse_command
from sqlalchemy import text
from test_capi_approve_and_pay import MILK, _card, _echo, _headers

from conftest import (
    KERNEL_URL,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    merchant_mutation,
)

pytestmark = pytest.mark.db


def permission(client, skus=None, limit=50000):
    response = client.post(
        "/v1/reserve/authorities",
        headers=_headers(),
        json={
            "allowed_skus": skus or [MILK],
            "per_purchase_limit_minor": limit,
            "capacity_minor": 200000,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def pay(client, card, auth, key=None):
    return client.post(
        f"/v1/reserve/checkouts/{card['checkout_id']}/versions/{card['version']}/pay",
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
        json={
            **_echo(card),
            "authority_id": auth["authority_id"],
            "authority_epoch": auth["epoch"],
        },
    )


def execute(engine, tenant, attempt, *, amount_override=None):
    with engine.begin() as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM outbox_events WHERE tenant_id=:t "
                "AND command_type='RESERVE_DEBIT' AND payload->>'payment_attempt_id'=:p LIMIT 1"
            ),
            {"t": tenant, "p": attempt},
        ).scalar_one()
    runtime = build_runtime(
        WorkerSettings(
            PROFILE="development",
            DATABASE_URL_KERNEL=KERNEL_URL,
            DATABASE_URL_WORKER=KERNEL_URL.replace("commerce_test_kernel", "commerce_test_worker"),
            RAZORPAY_KEY_ID=TEST_KEY_ID,
            RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
            RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
        )
    )
    command = parse_command("RESERVE_DEBIT", payload)
    if amount_override is not None:
        from dataclasses import replace

        command = replace(command, amount_minor=amount_override)
    return handle_reserve(runtime, command)


def test_confirm_and_replay(auth_client, demo_session, capi_admin_engine):
    auth = permission(auth_client)
    card = _card(auth_client)
    key = str(uuid.uuid4())
    response = pay(auth_client, card, auth, key)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["allowed"], result
    assert pay(auth_client, card, auth, key).json() == result
    attempt = result["attempt_id"]
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    status = auth_client.get(f"/v1/reserve/payments/{attempt}").json()
    assert status["status"] == "CAPTURED", status
    assert status["order_id"] and status["allocation"] == "SPENT"
    assert (
        auth_client.get(f"/v1/reserve/authorities/{auth['authority_id']}").json()["allocated_minor"]
        == card["amount_minor"]
    )


def test_revoke_queued_releases_once(auth_client, demo_session, capi_admin_engine):
    auth = permission(auth_client)
    result = pay(auth_client, _card(auth_client), auth).json()
    assert result["allowed"], result
    response = auth_client.post(
        f"/v1/reserve/authorities/{auth['authority_id']}/revoke", headers=_headers()
    )
    assert response.status_code == 200, response.text
    execute(capi_admin_engine, demo_session.tenant_id, result["attempt_id"])
    execute(capi_admin_engine, demo_session.tenant_id, result["attempt_id"])
    state = auth_client.get(f"/v1/reserve/authorities/{auth['authority_id']}").json()
    assert state["allocated_minor"] == 0 and state["status"] == "REVOKED"


@pytest.mark.parametrize("limit,skus", [(100, [MILK]), (50000, ["BRIT-BAKE-001"])])
def test_scope_and_amount_refused(auth_client, limit, skus):
    auth = permission(auth_client, skus, limit)
    result = pay(auth_client, _card(auth_client), auth).json()
    assert not result["allowed"], result
    assert (
        auth_client.get(f"/v1/reserve/authorities/{auth['authority_id']}").json()["allocated_minor"]
        == 0
    )


def test_other_buyer_and_agent_refused(auth_client, mint_client):
    auth = permission(auth_client)
    other, _ = mint_client(buyer_ref="different-reserve-buyer")
    agent, _ = mint_client(actor_type="AGENT")
    assert other.get(f"/v1/reserve/authorities/{auth['authority_id']}").status_code == 404
    assert agent.get("/v1/reserve/authorities").status_code == 403


@pytest.mark.parametrize("final", ["captured", "failed"])
def test_unknown_then_reconcile(
    auth_client, demo_session, capi_admin_engine, scenario_headers, final
):
    auth = permission(auth_client)
    card = _card(auth_client)
    result = pay(auth_client, card, auth).json()
    assert result["allowed"], result
    attempt = result["attempt_id"]
    response = auth_client.post(
        f"/v1/reserve/simulator/{attempt}",
        headers=_headers(**scenario_headers),
        json={"outcome": "unknown"},
    )
    assert response.status_code == 200, response.text
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    status = auth_client.get(f"/v1/reserve/payments/{attempt}").json()
    assert status["allocation"] == "HELD" and status["order_id"] is None
    response = auth_client.post(
        f"/v1/reserve/simulator/{attempt}",
        headers=_headers(**scenario_headers),
        json={"outcome": final},
    )
    assert response.status_code == 200, response.text
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    status = auth_client.get(f"/v1/reserve/payments/{attempt}").json()
    assert status["allocation"] == ("SPENT" if final == "captured" else "RELEASED"), status


def test_price_change_requires_new_review(auth_client, api_app, demo_session):
    from commerce_domain import Money

    auth = permission(auth_client)
    card = _card(auth_client)
    with merchant_mutation(api_app, demo_session) as scenario:
        scenario.set_price(MILK, Money(4000, "INR"))
    result = pay(auth_client, card, auth).json()
    assert result["code"] == "REAPPROVAL_REQUIRED", result
    assert result["approval_card"]["version"] == 2
    assert (
        auth_client.get(f"/v1/reserve/authorities/{auth['authority_id']}").json()["allocated_minor"]
        == 0
    )
    new = pay(auth_client, result["approval_card"], auth).json()
    assert new["allowed"], new


def test_expired_authority_refuses(auth_client, capi_admin_engine):
    auth = permission(auth_client)
    with capi_admin_engine.begin() as c:
        c.execute(
            text(
                "UPDATE delegated_authorities SET expires_at=now()-interval '1 second' WHERE id=:a"
            ),
            {"a": uuid.UUID(auth["authority_id"])},
        )
    result = pay(auth_client, _card(auth_client), auth).json()
    assert not result["allowed"], result
    assert (
        auth_client.get(f"/v1/reserve/authorities/{auth['authority_id']}").json()["allocated_minor"]
        == 0
    )


def test_cumulative_limit_and_payload_conflict(auth_client):
    first = _card(auth_client)
    second = _card(auth_client)
    response = auth_client.post(
        "/v1/reserve/authorities",
        headers=_headers(),
        json={
            "allowed_skus": [MILK],
            "per_purchase_limit_minor": first["amount_minor"],
            "capacity_minor": first["amount_minor"],
        },
    )
    assert response.status_code == 200, response.text
    auth = response.json()
    key = str(uuid.uuid4())
    assert pay(auth_client, first, auth, key).json()["allowed"]
    assert pay(auth_client, second, auth, key).status_code == 422
    result = pay(auth_client, second, auth).json()
    assert not result["allowed"], result


def test_backend_proof_includes_reserve_grant(auth_client, demo_session, capi_admin_engine):
    auth = permission(auth_client)
    card = _card(auth_client)
    result = pay(auth_client, card, auth).json()
    assert result["allowed"], result
    execute(capi_admin_engine, demo_session.tenant_id, result["attempt_id"])
    response = auth_client.get(f"/v1/checkouts/{card['checkout_id']}/proof")
    assert response.status_code == 200, response.text
    assert "RESERVE_DEBIT" in response.text
    assert "SIMULATED_NO_NETWORK" in response.text


def test_tampered_command_cannot_release_valid_allocation(
    auth_client, demo_session, capi_admin_engine
):
    auth = permission(auth_client)
    card = _card(auth_client)
    result = pay(auth_client, card, auth).json()
    assert result["allowed"], result
    with pytest.raises(RuntimeError, match="differs from the admitted purchase"):
        execute(
            capi_admin_engine,
            demo_session.tenant_id,
            result["attempt_id"],
            amount_override=card["amount_minor"] + 1,
        )
    state = auth_client.get(f"/v1/reserve/payments/{result['attempt_id']}").json()
    assert state["status"] == "CREATED" and state["allocation"] == "HELD"
    execute(capi_admin_engine, demo_session.tenant_id, result["attempt_id"])
    assert (
        auth_client.get(f"/v1/reserve/payments/{result['attempt_id']}").json()["status"]
        == "CAPTURED"
    )


def test_competing_checkouts_cannot_exceed_one_permission(auth_client):
    from concurrent.futures import ThreadPoolExecutor

    cards = [_card(auth_client), _card(auth_client)]
    response = auth_client.post(
        "/v1/reserve/authorities",
        headers=_headers(),
        json={
            "allowed_skus": [MILK],
            "per_purchase_limit_minor": cards[0]["amount_minor"],
            "capacity_minor": cards[0]["amount_minor"],
        },
    )
    assert response.status_code == 200, response.text
    auth = response.json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda card: pay(auth_client, card, auth), cards))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert sum(r.json()["allowed"] for r in responses) == 1
    state = auth_client.get(f"/v1/reserve/authorities/{auth['authority_id']}").json()
    assert state["allocated_minor"] == state["capacity_minor"]


def unscoped_permission(client, limit=50000):
    """A permission with no product scope. The field is *omitted*, never sent empty.

    An empty list has never been storable -- the Kernel refuses it and the table's CHECK
    requires 1 to 100 entries -- so absence is the only way to say "every product".
    """
    response = client.post(
        "/v1/reserve/authorities",
        headers=_headers(),
        json={
            "per_purchase_limit_minor": limit,
            "capacity_minor": 200000,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_permission_without_a_product_scope_pays(auth_client):
    """The product bound is now optional, and omitting it reaches payment.

    Two things are asserted rather than one, because "it was created" would not have
    caught the defect this covers: the pay route used to read ownership with
    ``AND allowed_skus IS NOT NULL``, so an unscoped permission was created happily and
    then refused at payment as *not found* -- a 404 for a row that exists and is valid.

    ``None`` on the wire, not ``[]``: a client has to be able to tell "covers everything"
    from "covers a list", and the saved-permission screen renders the two differently.
    """
    auth = unscoped_permission(auth_client)
    assert auth["allowed_skus"] is None, auth

    result = pay(auth_client, _card(auth_client), auth).json()
    assert result["allowed"], result


def test_removing_the_product_bound_leaves_the_amount_bounds_alone(auth_client):
    """What was removed is the product scope. The per-purchase limit is untouched.

    Worth its own test because the change was made by deleting a condition, and a
    deletion that went one clause too far would look exactly like a pass on the test
    above. ``100`` minor is below any real card, so an allowed result here would mean the
    amount bound had gone with the product bound.
    """
    tight = unscoped_permission(auth_client, limit=100)
    result = pay(auth_client, _card(auth_client), tight).json()
    assert not result["allowed"], result


def test_new_reserve_permission_has_no_time_based_expiry(auth_client):
    authority = unscoped_permission(auth_client)
    assert authority["expires_at"] is not None
    assert authority["status"] == "ACTIVE"
    saved = auth_client.get("/v1/reserve/authorities").json()["authorities"]
    assert (
        next(row for row in saved if row["authority_id"] == authority["authority_id"])["expires_at"]
        == authority["expires_at"]
    )


def test_duplicate_permission_is_clear_conflict(auth_client):
    first = permission(auth_client)
    response = auth_client.post(
        "/v1/reserve/authorities",
        headers=_headers(),
        json={"allowed_skus": [MILK], "per_purchase_limit_minor": 50000, "capacity_minor": 500000},
    )
    assert response.status_code == 409, response.text
    assert "existing permission" in response.text
    assert first["authority_id"]


def test_concurrent_permissions_have_one_winner(auth_client, capi_admin_engine, demo_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def create(_):
        barrier.wait(timeout=5)
        return auth_client.post(
            "/v1/reserve/authorities",
            headers=_headers(),
            json={
                "allowed_skus": [MILK],
                "per_purchase_limit_minor": 50000,
                "capacity_minor": 500000,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    with capi_admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM delegated_authorities WHERE tenant_id=:t"),
                {"t": demo_session.tenant_id},
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM verified_authority_proofs WHERE tenant_id=:t"),
                {"t": demo_session.tenant_id},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("_iteration", range(3))
def test_shared_authority_admission_worker_and_settlement(
    auth_client, capi_admin_engine, capi_kernel_engine, demo_session, _iteration
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from platform_db import set_tenant
    from sqlalchemy.orm import Session
    from transaction_kernel.reserve import record_simulation_outcome, settle_allocation

    auth = permission(auth_client)
    first = pay(auth_client, _card(auth_client), auth).json()
    assert first["allowed"]
    attempt = uuid.UUID(first["attempt_id"])
    second = _card(auth_client)
    with Session(capi_kernel_engine) as session, session.begin():
        set_tenant(session, demo_session.tenant_id)
        record_simulation_outcome(session, attempt, outcome="failed")
    barrier = Barrier(3)

    def admit_second():
        barrier.wait(timeout=5)
        response = pay(auth_client, second, auth)
        assert response.status_code == 200, response.text
        assert response.json()["allowed"], response.text

    def worker():
        barrier.wait(timeout=5)
        execute(capi_admin_engine, demo_session.tenant_id, first["attempt_id"])

    def settle():
        barrier.wait(timeout=5)
        with Session(capi_kernel_engine) as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='8s'"))
            set_tenant(session, demo_session.tenant_id)
            settle_allocation(session, attempt, correlation_id=uuid.uuid4())

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fn) for fn in (admit_second, worker, settle)]
        for future in futures:
            future.result(timeout=12)
    with capi_admin_engine.connect() as conn:
        allocated = conn.execute(
            text("SELECT consumed_amount_minor FROM delegated_authorities WHERE id=:a"),
            {"a": auth["authority_id"]},
        ).scalar_one()
        held = conn.execute(
            text(
                "SELECT coalesce(sum(amount_minor),0) FROM payment_attempts "
                "WHERE reserve_authority_id=:a AND reserve_allocation IN ('HELD','SPENT')"
            ),
            {"a": auth["authority_id"]},
        ).scalar_one()
        assert allocated == held == second["amount_minor"]


def test_admission_metrics_include_allow_and_deny(auth_client):
    from platform_observability.instruments import default_registry, reset_default_registry

    auth = permission(auth_client)
    first = _card(auth_client)
    reset_default_registry()
    result = pay(auth_client, first, auth)
    assert result.json()["allowed"], result.text
    # Epoch mismatch is a real committed denial, not a malformed request.
    response = pay(auth_client, _card(auth_client), {**auth, "epoch": auth["epoch"] + 1})
    assert response.status_code == 200 and not response.json()["allowed"], response.text
    rendered = default_registry().render()
    admissions = [
        line for line in rendered.splitlines() if line.startswith("commerce_admissions_total{")
    ]
    assert len(admissions) == 2 and all(line.endswith(" 1") for line in admissions)
    assert any('outcome="allowed"' in line for line in admissions)
    assert any('outcome="denied"' in line for line in admissions)
    assert any(
        line.startswith("commerce_admission_denials_total{") and 'code="AUTHORITY_REVOKED"' in line
        for line in rendered.splitlines()
    )

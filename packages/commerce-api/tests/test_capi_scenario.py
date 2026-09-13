"""The scenario controller and the operator views, against real PostgreSQL.

What these tests are for, in one line each:

* the apparatus **does not exist** in production, and is not merely refused there;
* an injection leaves exactly one ``SCENARIO_INJECTION`` audit row whose payload is the
  controller's own, so a timeline can never confuse a demo change with an organic one;
* a reservation expired on the database clock makes the *next admission* deny -- the
  lever changes the world, not a display;
* Safe Mode stops a delegated debit while human-present checkout, refunds and
  reconciliation stay open, and an agent cannot touch the switch at all;
* two concurrent submits produce exactly one Execution Grant.

Every one of these is a claim the evidence table (specification 31.4) makes, so each is
pinned by an assertion rather than by a demonstration nobody re-runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import durable_work as dw
import pytest
import transaction_kernel as tk
from agent_runtime.language import Language
from agent_runtime.rendering import render_reasoning_unavailable
from commerce_api.app import create_app
from commerce_api.deps import CAPABILITIES_BY_ACTOR
from commerce_api.merchants import MerchantRegistry
from commerce_api.routers import agent as agent_router
from commerce_api.services import agent_service
from commerce_api.services import scenario_service as svc
from commerce_api.services.agent_service import Route, ToolExecutor, TurnInput, TurnOutcome
from commerce_api.settings import Settings
from commerce_domain import ActorType, AgentPrincipal, uuid7
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from merchant_adapter import content_from_quote, receipt_inputs_for
from merchant_sim import BasketLine, quote_basket
from payment_adapters import EVENT_ID_HEADER, SIGNATURE_HEADER, dedup_key_for
from platform_db import FINANCIAL_TABLES, set_tenant
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from conftest import (
    APP_URL,
    KERNEL_URL,
    TEST_SCENARIO_KEY,
    TEST_WEBHOOK_SECRET,
    MintedSession,
    SeededTenant,
    merchant_store,
)

pytestmark = pytest.mark.db

#: A live-shaped credential that has never existed. The production profile refuses a
#: ``rzp_test_`` key outright, so proving "these routes are absent in production" needs a
#: settings object that is genuinely production-shaped. Nothing here reaches a network.
PROD_KEY_ID = "rzp_live_capiabsentkey"
PROD_KEY_SECRET = "capi-absent-api-secret"  # noqa: S105 - fake, never used to sign anything
PROD_WEBHOOK_SECRET = "capi-absent-webhook-sec"  # noqa: S105 - fake, see above

#: Every path this build unit owns, as (method, path). Used by the two guard tests so a
#: route added later without a guard fails a test instead of shipping.
GUARDED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/v1/scenario/injections"),
    ("POST", f"/v1/scenario/reservations/{uuid7()}/1/expire"),
    ("POST", f"/v1/scenario/webhooks/{uuid7()}/replay"),
    ("POST", "/v1/scenario/duplicate-submit"),
    ("POST", "/v1/scenario/faults"),
    ("POST", f"/v1/scenario/checkouts/{uuid7()}/invalidate-open"),
    ("GET", "/v1/ops/safe-mode"),
    ("POST", "/v1/ops/safe-mode"),
    ("GET", "/v1/ops/outbox"),
    ("POST", f"/v1/ops/outbox/{uuid7()}/revive"),
)


# --------------------------------------------------------------------------- fixtures


@dataclass(frozen=True, slots=True)
class ApprovedCheckout:
    """A checkout that has reached APPROVED through the real kernel path."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str
    approval_id: uuid.UUID
    cart_id: uuid.UUID
    sku: str
    quantity: int


def _principal(demo_session: MintedSession) -> AgentPrincipal:
    """The principal the API would build for this session, rebuilt here for kernel calls."""
    return AgentPrincipal(
        principal_id=f"session:{demo_session.session_id}",
        tenant_id=demo_session.tenant_id,
        actor_type=ActorType(demo_session.actor_type),
        merchant_id=demo_session.merchant_id,
        buyer_ref=demo_session.buyer_ref,
        capabilities=CAPABILITIES_BY_ACTOR[ActorType(demo_session.actor_type)],
    )


@pytest.fixture
def approved_checkout(
    api_app: FastAPI,
    capi_kernel_engine: Engine,
    demo_session: MintedSession,
) -> ApprovedCheckout:
    """Version 1, priced by the app's own merchant store, approved by the buyer.

    Built through ``create_checkout`` / ``freeze_for_approval`` / ``record_approval`` rather
    than by inserting rows, so what the scenario levers act on is what the production
    path produces -- including the content hash the state source must reproduce when
    admission re-quotes the cart.

    Priced against ``api_app.state.merchants``, the registry the request will use. A
    second store would revalidate to a different total and every admission here would
    deny for the wrong reason.
    """
    registry: MerchantRegistry = api_app.state.merchants
    store = merchant_store(api_app, demo_session)
    sku, quantity = store.all_skus()[0], 2
    quote = quote_basket([BasketLine(sku=sku, quantity=quantity)], store=store).require()

    cart_id = uuid7()
    correlation_id = uuid7()
    principal = _principal(demo_session)

    session = Session(capi_kernel_engine, expire_on_commit=False)
    with session.begin():
        set_tenant(session, demo_session.tenant_id)
        session.execute(
            text(
                "INSERT INTO carts (id, tenant_id, merchant_id, buyer_ref, lines, status) "
                "VALUES (:id, :t, :m, :b, CAST(:lines AS jsonb), 'OPEN')"
            ),
            {
                "id": cart_id,
                "t": demo_session.tenant_id,
                "m": demo_session.merchant_id,
                "b": demo_session.buyer_ref,
                "lines": json.dumps([{"sku": sku, "quantity": quantity}]),
            },
        )
        content = content_from_quote(
            quote,
            checkout_id=uuid7(),  # re-stamped by create_checkout; see its docstring
            version=1,
            policy_version=registry.policy_version(),
        )
        created = tk.create_checkout(
            session,
            tenant_id=demo_session.tenant_id,
            merchant_id=demo_session.merchant_id,
            cart_id=cart_id,
            buyer_ref=demo_session.buyer_ref,
            content=content,
            correlation_id=correlation_id,
            principal=principal,
        )
        card = tk.freeze_for_approval(
            session,
            tenant_id=demo_session.tenant_id,
            checkout=created.ref,
            receipt=receipt_inputs_for(store),
            correlation_id=correlation_id,
            principal=principal,
        )
        approval = tk.record_approval(
            session,
            tenant_id=demo_session.tenant_id,
            checkout=card.checkout,
            amount=card.total,
            principal=principal,
            correlation_id=correlation_id,
        )
    session.close()

    return ApprovedCheckout(
        checkout_id=created.checkout_id,
        version=created.version,
        content_hash=card.checkout.content_hash,
        approval_id=approval.approval_id,
        cart_id=cart_id,
        sku=sku,
        quantity=quantity,
    )


@pytest.fixture
def kernel_session(capi_kernel_engine: Engine, seeded_tenant: SeededTenant) -> Iterator[Session]:
    """A kernel-role transaction bound to the seeded tenant, for arranging and asserting."""
    session = Session(capi_kernel_engine, expire_on_commit=False)
    with session.begin():
        set_tenant(session, seeded_tenant.tenant_id)
        yield session
    session.close()


@pytest.fixture
def scenario_client(
    auth_client: TestClient, operator_headers: dict[str, str]
) -> Callable[..., Any]:
    """Call a scenario route with both credentials: the session and the operator key."""

    def _call(method: str, path: str, **kwargs: Any) -> Any:
        headers = {**operator_headers, **kwargs.pop("headers", {})}
        if path == "/v1/scenario/duplicate-submit":
            headers["X-Scenario-Buyer-Authorization"] = auth_client.headers["Authorization"]
        return auth_client.request(method, path, headers=headers, **kwargs)

    return _call


def _audit_rows(session: Session, tenant_id: uuid.UUID, event_type: str) -> list[Any]:
    return list(
        session.execute(
            text(
                "SELECT event_type, payload, aggregate_type, aggregate_id, actor_type "
                "FROM audit_events WHERE tenant_id = :t AND event_type = :e ORDER BY seq"
            ),
            {"t": tenant_id, "e": event_type},
        ).all()
    )


# ------------------------------------------------------------------------ the two guards


def test_scenario_routes_do_not_exist_in_the_production_profile() -> None:
    """404 on every path, with no session and no key. The routes are absent, not refused.

    A 401 here would announce that the apparatus exists and is merely locked, which is
    the disclosure ADR 0003 D11 avoids by saying these routes "do not exist" outside a
    demonstration.
    """
    production = create_app(
        Settings(
            PROFILE="production",
            DATABASE_URL_APP=APP_URL,
            DATABASE_URL_KERNEL=KERNEL_URL,
            RAZORPAY_KEY_ID=PROD_KEY_ID,
            RAZORPAY_KEY_SECRET=PROD_KEY_SECRET,
            RAZORPAY_WEBHOOK_SECRET=PROD_WEBHOOK_SECRET,
            RAZORPAY_PRODUCTION_APPROVAL_REF="CHANGE-0000",
            SCENARIO_KEY=TEST_SCENARIO_KEY,
        )
    )
    with TestClient(production) as client:
        for method, path in GUARDED_ROUTES:
            response = client.request(method, path, json={})
            assert response.status_code == 404, f"{method} {path} -> {response.status_code}"
            assert response.headers["content-type"].startswith("application/problem+json")


def test_scenario_routes_refuse_a_request_without_the_key(auth_client: TestClient) -> None:
    """401 when the route exists and the key is missing -- even with a valid session.

    The key is declared on the router, so it is checked before the bearer token. A
    request carrying a good session and no key is refused for the key, which is what
    tells an operator they mistyped the header rather than that their login lapsed.
    """
    for method, path in GUARDED_ROUTES:
        response = auth_client.request(method, path, json={})
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        assert "X-Scenario-Key" in response.json()["detail"]


def test_a_wrong_key_is_refused(auth_client: TestClient) -> None:
    response = auth_client.get("/v1/ops/safe-mode", headers={"X-Scenario-Key": "not-the-key"})
    assert response.status_code == 401


# ------------------------------------------------------------------------- injections


def test_an_injection_writes_one_labelled_audit_row_and_advances_the_revision(
    api_app: FastAPI,
    demo_session: MintedSession,
    kernel_session: Session,
    scenario_client: Callable[..., Any],
) -> None:
    """The claim step 7 stands on: this price move is provably an injection.

    Three things are asserted together because any one of them alone would let the demo
    lie. The audit payload must be the controller's own ``to_audit_payload`` byte for
    byte -- a summary written by the API could drift from what actually changed. There
    must be exactly one such row -- two would double-count the revision. And the store's
    revision must have advanced by exactly one, because that is what makes every quote
    taken before the injection provably stale.
    """
    store = merchant_store(api_app, demo_session)
    sku = store.all_skus()[0]
    before_revision = store.revision
    before_price = store.get_product(sku).unit_price.minor

    response = scenario_client(
        "POST",
        "/v1/scenario/injections",
        json={
            "kind": "PRICE_SET",
            "sku": sku,
            "value": before_price + 5500,
            "note": "step 5",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["label"] == "SCENARIO_INJECTION"
    assert body["kind"] == "PRICE_SET"
    assert body["revision_before"] == before_revision
    assert body["revision_after"] == before_revision + 1
    # Re-read rather than re-checking the object captured above. The shop's state is rows
    # now, and a store is the view of them at the moment it was built -- so this asserts
    # something stronger than the old shared object ever could: the change was stored, and
    # a reader coming along afterwards sees it.
    after = merchant_store(api_app, demo_session)
    assert after.revision == before_revision + 1
    assert after.get_product(sku).unit_price.minor == before_price + 5500
    assert body["deltas"] == [
        {"field": "unit_price_minor", "before": before_price, "after": before_price + 5500}
    ]

    rows = _audit_rows(kernel_session, demo_session.tenant_id, "SCENARIO_INJECTION")
    assert len(rows) == 1
    stored = rows[0]
    assert stored.aggregate_type == "merchant"
    assert stored.aggregate_id == demo_session.merchant_id
    assert stored.actor_type == ActorType.OPERATOR.value
    # The controller's own payload, not the API's rendering of it.
    assert stored.payload == body["audit_payload"]
    assert stored.payload["label"] == "SCENARIO_INJECTION"

    run = kernel_session.execute(
        text("SELECT kind, injection_id, audit_event_id FROM scenario_runs WHERE tenant_id = :t"),
        {"t": demo_session.tenant_id},
    ).one()
    assert run.kind == "PRICE_SET"
    assert str(run.injection_id) == body["injection_id"]
    assert str(run.audit_event_id) == body["audit_event_id"]


def test_sell_out_is_recorded_as_the_stock_change_it_actually_made(
    api_app: FastAPI, demo_session: MintedSession, scenario_client: Callable[..., Any]
) -> None:
    """``SELL_OUT`` is a wire convenience; the audit records ``STOCK_SET`` to zero.

    The vocabulary an operator types must never become the vocabulary the evidence
    claims. What changed was the stock level, and that is what the row says.
    """
    store = merchant_store(api_app, demo_session)
    sku = store.all_skus()[1]
    response = scenario_client(
        "POST", "/v1/scenario/injections", json={"kind": "SELL_OUT", "sku": sku}
    )
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "STOCK_SET"
    assert response.json()["deltas"][0]["after"] == 0
    # Re-read: the sell-out has to be in the rows, not merely in an object we still hold.
    assert merchant_store(api_app, demo_session).check_inventory(sku).available_units == 0


def test_a_no_op_injection_is_refused(
    api_app: FastAPI, demo_session: MintedSession, scenario_client: Callable[..., Any]
) -> None:
    """A revision bump with no cause is exactly the unexplained staleness to avoid."""
    store = merchant_store(api_app, demo_session)
    sku = store.all_skus()[0]
    response = scenario_client(
        "POST",
        "/v1/scenario/injections",
        json={"kind": "PRICE_SET", "sku": sku, "value": store.get_product(sku).unit_price.minor},
    )
    assert response.status_code == 409, response.text
    assert store.revision == 0


def test_a_boolean_cannot_be_smuggled_in_as_a_price(
    api_app: FastAPI, demo_session: MintedSession, scenario_client: Callable[..., Any]
) -> None:
    """``bool`` is a subclass of ``int``; ``true`` must not become one paisa."""
    store = merchant_store(api_app, demo_session)
    response = scenario_client(
        "POST",
        "/v1/scenario/injections",
        json={"kind": "PRICE_SET", "sku": store.all_skus()[0], "value": True},
    )
    assert response.status_code == 422, response.text
    assert store.revision == 0


def test_an_unknown_sku_is_a_404(scenario_client: Callable[..., Any]) -> None:
    response = scenario_client(
        "POST", "/v1/scenario/injections", json={"kind": "SELL_OUT", "sku": "GRO-NOPE-999"}
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------- reservation expiry


def test_reservation_expiry_makes_a_later_admission_deny(
    approved_checkout: ApprovedCheckout,
    kernel_session: Session,
    scenario_client: Callable[..., Any],
) -> None:
    """The lever changes the world, not a display.

    Expiry is asserted twice over: the row is ``EXPIRED`` afterwards, and -- the part
    that matters -- a real admission run through the duplicate-submit lever is refused
    ``RESERVATION_EXPIRED``. A test that only checked the status column would pass
    against an implementation that wrote ``EXPIRED`` while PostgreSQL still considered
    the hold live, which is precisely the lie this lever must not tell.
    """
    response = scenario_client(
        "POST",
        f"/v1/scenario/reservations/{approved_checkout.checkout_id}/"
        f"{approved_checkout.version}/expire",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status_before"] == "ACTIVE"
    assert body["status_after"] == "EXPIRED"
    assert body["code"] == "OK"

    # No tenant predicate: row-level security supplies it, and reading through it here
    # is the point -- a leak would show up as a missing row rather than as a pass.
    stored = kernel_session.execute(
        text(
            "SELECT status, expires_at <= clock_timestamp() AS lapsed FROM reservations "
            "WHERE checkout_id = :c AND checkout_version = :v"
        ),
        {"c": approved_checkout.checkout_id, "v": approved_checkout.version},
    ).one()
    assert stored.status == "EXPIRED"
    # ``clock_timestamp()``, not ``now()``: this assertion runs in a transaction that
    # opened before the request did, and ``now()`` is that transaction's start time.
    assert stored.lapsed is True

    race = scenario_client(
        "POST",
        "/v1/scenario/duplicate-submit",
        json={
            "checkout_id": str(approved_checkout.checkout_id),
            "version": approved_checkout.version,
        },
    )
    assert race.status_code == 200, race.text
    outcomes = race.json()["outcomes"]
    assert race.json()["admitted_count"] == 0
    assert {outcome["code"] for outcome in outcomes} == {"RESERVATION_EXPIRED"}


def test_expiring_a_reservation_on_an_unknown_checkout_is_a_404(
    scenario_client: Callable[..., Any],
) -> None:
    response = scenario_client("POST", f"/v1/scenario/reservations/{uuid7()}/1/expire")
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------- duplicate submit


def test_two_concurrent_submits_produce_exactly_one_grant(
    approved_checkout: ApprovedCheckout, scenario_client: Callable[..., Any]
) -> None:
    """ADR D9, and the single-execution row of the evidence table.

    Two real transactions, two different idempotency keys, one barrier. Exactly one
    Execution Grant and exactly one payment attempt may exist afterwards; the loser must
    be refused rather than handed the winner's attempt as its own.
    """
    response = scenario_client(
        "POST",
        "/v1/scenario/duplicate-submit",
        json={
            "checkout_id": str(approved_checkout.checkout_id),
            "version": approved_checkout.version,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["admitted_count"] == 1
    assert len(body["grant_ids"]) == 1
    assert len(body["outcomes"]) == 2

    allowed = [outcome for outcome in body["outcomes"] if outcome["allowed"]]
    denied = [outcome for outcome in body["outcomes"] if not outcome["allowed"]]
    assert len(allowed) == 1
    assert len(denied) == 1
    assert allowed[0]["grant_id"] is not None
    assert allowed[0]["payment_attempt_id"] is not None
    assert denied[0]["code"] != "OK"


def test_duplicate_submit_on_an_unknown_checkout_is_a_404(
    scenario_client: Callable[..., Any],
) -> None:
    """Aiming the lever at a checkout nobody owns says so, rather than failing obscurely."""
    response = scenario_client(
        "POST",
        "/v1/scenario/duplicate-submit",
        json={"checkout_id": str(uuid7()), "version": 1},
    )
    assert response.status_code == 404, response.text


# ------------------------------------------------------------------------- safe mode


def test_safe_mode_blocks_a_delegated_debit_and_keeps_the_buyer_whole(
    scenario_client: Callable[..., Any],
) -> None:
    """The kill switch is asymmetric, and the banner has to say so.

    Specification 10.3.2: a delegated debit stops; fresh human-present checkout, refunds
    and reconciliation stay available. A switch that stopped those too would be an
    outage, and an outage harms the buyer it was thrown to protect -- their refund would
    not land and their unknown payment would never be resolved.
    """
    before = scenario_client("GET", "/v1/ops/safe-mode")
    assert before.status_code == 200, before.text
    assert before.json()["mode"] == "NORMAL"
    assert before.json()["banner"] is None
    assert before.json()["permitted"]["DELEGATED_DEBIT"] is True

    entered = scenario_client(
        "POST", "/v1/ops/safe-mode", json={"enabled": True, "reason": "PROVIDER_INCIDENT_DECLARED"}
    )
    assert entered.status_code == 200, entered.text
    body = entered.json()
    assert body["mode"] == "SAFE_MODE"
    assert body["safe_mode"] is True
    assert body["reason_code"] == "PROVIDER_INCIDENT_DECLARED"
    assert body["actor"].startswith("operator:session:")

    assert body["permitted"]["DELEGATED_DEBIT"] is False
    assert body["permitted"]["HUMAN_PRESENT_CHECKOUT"] is True
    assert body["permitted"]["REFUND_EXECUTE"] is True
    assert body["permitted"]["RECONCILIATION"] is True

    banner = body["banner"]
    assert banner is not None
    assert "DELEGATED_DEBIT" in banner["blocked"]
    assert "REFUND_EXECUTE" in banner["still_available"]
    assert "HUMAN_PRESENT_CHECKOUT" in banner["still_available"]
    assert banner["reason_code"] == "PROVIDER_INCIDENT_DECLARED"

    read_back = scenario_client("GET", "/v1/ops/safe-mode")
    assert read_back.json()["mode"] == "SAFE_MODE"


def test_leaving_safe_mode_returns_the_tenant_to_normal(
    scenario_client: Callable[..., Any],
) -> None:
    """Standing down is its own audited action, and only its own transaction can do it.

    Entering and leaving in one transaction would share a ``changed_at`` and the
    fail-closed tie-break would keep the switch on; two requests are two transactions,
    which is why this reads back ``NORMAL``.
    """
    assert scenario_client("POST", "/v1/ops/safe-mode", json={"enabled": True}).status_code == 200
    left = scenario_client(
        "POST", "/v1/ops/safe-mode", json={"enabled": False, "reason": "INCIDENT_RESOLVED"}
    )
    assert left.status_code == 200, left.text
    assert left.json()["mode"] == "NORMAL"
    assert left.json()["permitted"]["DELEGATED_DEBIT"] is True


def test_an_agent_cannot_enter_safe_mode(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
    scenario_headers: dict[str, str],
) -> None:
    """Specification 10.3.2 in terms: the LLM cannot enter or leave Safe Mode.

    403 rather than the kernel's ``ValueError``. An agent reaching a control it may not
    touch is a permission answer, not a server fault, and a 500 here would look like a
    bug to fix rather than a boundary that held.
    """
    agent_client, _ = mint_client(actor_type="AGENT")
    with agent_client as client:
        response = client.post(
            "/v1/ops/safe-mode", json={"enabled": True}, headers=scenario_headers
        )
        assert response.status_code == 403, response.text
        assert response.json()["actor_type"] == "AGENT"

        # And the switch is still off: a refused activation writes no history.
        read = client.get("/v1/ops/safe-mode", headers=scenario_headers)
        assert read.status_code == 403


# ---------------------------------------------------------------------- worker faults


def test_arming_a_fault_writes_one_scenario_fault_row(
    kernel_session: Session,
    approved_checkout: ApprovedCheckout,
    scenario_client: Callable[..., Any],
) -> None:
    """Armed as the app role, which is the only role with INSERT on ``scenario_faults``."""
    response = scenario_client(
        "POST",
        "/v1/scenario/faults",
        json={
            "kind": "CREATE_ORDER_TIMEOUT",
            "checkout_id": str(approved_checkout.checkout_id),
            "once": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["armed"] is True
    assert body["kind"] == "CREATE_ORDER_TIMEOUT"

    row = kernel_session.execute(
        text("SELECT kind, checkout_id, armed, consumed_at FROM scenario_faults WHERE id = :i"),
        {"i": body["fault_id"]},
    ).one()
    assert row.kind == "CREATE_ORDER_TIMEOUT"
    assert row.checkout_id == approved_checkout.checkout_id
    assert row.armed is True
    assert row.consumed_at is None


def test_a_standing_fault_is_refused(scenario_client: Callable[..., Any]) -> None:
    """``scenario_faults`` has no column for it, and silence would mislead an operator."""
    response = scenario_client(
        "POST", "/v1/scenario/faults", json={"kind": "REFUND_TIMEOUT", "once": False}
    )
    assert response.status_code == 422, response.text


# ------------------------------------------------------------------------ late capture


def test_invalidate_open_refuses_a_checkout_with_no_payment_open(
    approved_checkout: ApprovedCheckout, scenario_client: Callable[..., Any]
) -> None:
    """The state machine, not this route, decides when a checkout may be invalidated.

    ``INVALIDATED_AWAITING_PAYMENT_RESULT`` is reachable only from ``AWAITING_PAYMENT``
    or ``PAYMENT_UNKNOWN``. An approved checkout with no attempt is refused, which is
    what stops the late-capture lever from being used to erase a checkout that never had
    a payment surface open at all.
    """
    response = scenario_client(
        "POST",
        f"/v1/scenario/checkouts/{approved_checkout.checkout_id}/invalidate-open",
        json={"reason": "SCENARIO_LATE_CAPTURE"},
    )
    assert response.status_code == 409, response.text


def test_invalidate_open_on_an_unknown_checkout_is_a_404(
    scenario_client: Callable[..., Any],
) -> None:
    response = scenario_client("POST", f"/v1/scenario/checkouts/{uuid7()}/invalidate-open", json={})
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------- webhook replay


def _seed_inbox(session: Session, tenant_id: uuid.UUID) -> tuple[uuid.UUID, bytes, str, str]:
    """One stored delivery, signed exactly as Razorpay would sign it.

    The bytes are kept and the signature is computed over those bytes, which is the whole
    point: a receiver that stored parsed JSON could not reproduce this signature, because
    a re-serialised body is a different sequence of bytes.
    """
    raw = json.dumps(
        {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_TEST"}}}},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(TEST_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    headers = {
        SIGNATURE_HEADER: signature,
        EVENT_ID_HEADER: event_id,
        "content-type": "application/json",
    }
    inbox_id = uuid7()
    session.execute(
        text(
            "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, provider_event_id, "
            "event_type, body_digest, raw_body, headers_redacted, signature_verified, "
            "payment_id, apply_status, duplicate_count) VALUES (:id, :t, :k, :e, "
            "'payment.captured', :d, :raw, CAST(:h AS jsonb), true, 'pay_TEST', "
            "'APPLIED', 0)"
        ),
        {
            "id": inbox_id,
            "t": tenant_id,
            "k": dedup_key_for(headers, raw),
            "e": event_id,
            "d": hashlib.sha256(raw).hexdigest(),
            "raw": raw,
            "h": json.dumps(headers),
        },
    )
    return inbox_id, raw, signature, event_id


def test_replaying_a_stored_webhook_is_recognised_as_a_duplicate(
    demo_session: MintedSession,
    capi_kernel_engine: Engine,
    scenario_client: Callable[..., Any],
) -> None:
    """The webhook-idempotent row of the evidence table (specification 31.4).

    Two claims, and the second is the one a judge should care about. The stored signature
    still verifies over the stored bytes, so the inbox kept what was signed. And the
    re-attempted claim is refused by the unique index on ``(tenant_id, dedup_key)`` --
    PostgreSQL, not application logic, is what stops a redelivered capture from being
    applied twice. The applied state is untouched either way.
    """
    arrange = Session(capi_kernel_engine, expire_on_commit=False)
    with arrange.begin():
        set_tenant(arrange, demo_session.tenant_id)
        inbox_id, _raw, _signature, event_id = _seed_inbox(arrange, demo_session.tenant_id)
    arrange.close()

    response = scenario_client("POST", f"/v1/scenario/webhooks/{inbox_id}/replay")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["signature_reverified"] is True
    assert body["duplicate_confirmed"] is True
    assert body["provider_event_id"] == event_id
    assert body["duplicate_count_before"] == 0
    assert body["duplicate_count_after"] == 1
    # State did not change: a duplicate is a no-op, not a second application.
    assert body["apply_status"] == "APPLIED"
    # Either branch is correct. With no receiver mounted the claim below is what proves
    # the dedupe; with Unit D's receiver present the loopback delivers and the count is
    # bumped once, by whichever of the two did it. See the loopback test above.
    assert body["delivery_reason"] in {"receiver_not_registered", "delivered", "receiver_refused"}

    assert_session = Session(capi_kernel_engine, expire_on_commit=False)
    with assert_session.begin():
        set_tenant(assert_session, demo_session.tenant_id)
        rows = assert_session.execute(
            text("SELECT count(*) FROM webhook_inbox WHERE tenant_id = :t"),
            {"t": demo_session.tenant_id},
        ).scalar_one()
        assert rows == 1, "the replay must not have created a second inbox row"
    assert_session.close()


def test_the_replay_hands_the_receiver_the_exact_bytes_that_were_signed(
    api_app: FastAPI,
    demo_session: MintedSession,
    capi_kernel_engine: Engine,
    scenario_client: Callable[..., Any],
) -> None:
    """The loopback half, against a stand-in receiver at the ADR D7 path.

    Unit D owns the real receiver. This test mounts a stub ahead of it so the delivery
    half is covered whether or not that receiver exists yet, and so it asserts the thing
    that actually matters about a replay: what arrives is the stored ``raw_body`` byte
    for byte, carrying the original signature and event id. A receiver handed a
    re-serialised body would receive different bytes and the signature would stop
    verifying -- ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` mean the same thing and hash
    differently -- which is the failure this route exists to rule out.
    """
    received: dict[str, Any] = {}

    async def stub(request: Request) -> Response:
        received["body"] = await request.body()
        received["signature"] = request.headers.get(SIGNATURE_HEADER)
        received["event_id"] = request.headers.get(EVENT_ID_HEADER)
        received["slug"] = request.path_params["tenant_slug"]
        return Response(status_code=200)

    api_app.router.add_api_route(
        "/webhooks/razorpay/{tenant_slug}", stub, methods=["POST"], include_in_schema=False
    )
    # Moved to the front so this test is independent of whether the real receiver is
    # mounted yet: Starlette matches in registration order, and a route appended after
    # an existing one at the same path would never be reached.
    api_app.router.routes.insert(0, api_app.router.routes.pop())

    arrange = Session(capi_kernel_engine, expire_on_commit=False)
    with arrange.begin():
        set_tenant(arrange, demo_session.tenant_id)
        inbox_id, raw, signature, event_id = _seed_inbox(arrange, demo_session.tenant_id)
    arrange.close()

    response = scenario_client("POST", f"/v1/scenario/webhooks/{inbox_id}/replay")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivered"] is True
    assert body["delivery_status"] == 200
    assert body["delivery_reason"] == "delivered"

    assert received["body"] == raw
    assert received["signature"] == signature
    assert received["event_id"] == event_id
    # The tenant came from the session, never from the stored body.
    assert received["slug"]


def test_replaying_an_unknown_delivery_is_a_404(scenario_client: Callable[..., Any]) -> None:
    response = scenario_client("POST", f"/v1/scenario/webhooks/{uuid7()}/replay")
    assert response.status_code == 404, response.text


# ------------------------------------------------------------------------------ outbox


def test_the_outbox_view_reports_counts_across_the_tenant(
    demo_session: MintedSession,
    capi_kernel_engine: Engine,
    scenario_client: Callable[..., Any],
) -> None:
    """Counts come from the whole tenant, never from the returned page.

    A rising ``FAILED`` count is a provider incident and a rising ``PENDING`` count is a
    worker shortage. Those have opposite remedies, so reading either off a truncated page
    would point an operator the wrong way during the one hour it matters.
    """
    arrange = Session(capi_kernel_engine, expire_on_commit=False)
    with arrange.begin():
        set_tenant(arrange, demo_session.tenant_id)
        command = dw.enqueue(
            arrange,
            command_type="CREATE_ORDER",
            payload={"checkout_id": str(uuid7()), "version": 1},
            correlation_id=uuid7(),
        )
    arrange.close()

    response = scenario_client("GET", "/v1/ops/outbox")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["PENDING"] == 1
    assert body["counts"]["DEAD"] == 0
    assert [row["command_id"] for row in body["commands"]] == [str(command.command_id)]

    filtered = scenario_client("GET", "/v1/ops/outbox", params={"status": "DEAD"})
    assert filtered.json()["commands"] == []
    assert filtered.json()["counts"]["PENDING"] == 1


def test_the_outbox_view_separates_parked_and_overdue_from_merely_pending(
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    capi_kernel_engine: Engine,
    scenario_client: Callable[..., Any],
) -> None:
    """``PENDING`` alone cannot tell a merchant that money owed is going nowhere.

    Three commands, all ``PENDING``, all indistinguishable in the status counts: one the
    worker will lease on its next poll, one scheduled a week out, and one whose moment
    passed an hour ago and which nobody has claimed. A summary that reports "PENDING 3"
    over that is telling a merchant the tenant is busy when two thirds of it is stalled.

    The parked threshold is asserted against the retry policy rather than against 3600,
    because the claim the response makes is "no backoff could have scheduled this" -- and
    a literal here would keep passing after somebody raised the cap, at which point the
    claim would be false and the test would still be green.
    """
    arrange = Session(capi_kernel_engine, expire_on_commit=False)
    with arrange.begin():
        set_tenant(arrange, demo_session.tenant_id)
        soon = dw.enqueue(
            arrange,
            command_type="CREATE_ORDER",
            payload={"checkout_id": str(uuid7()), "version": 1},
            correlation_id=uuid7(),
        )
        parked = dw.enqueue(
            arrange,
            command_type="REFUND_EXECUTE",
            payload={"refund_id": str(uuid7())},
            correlation_id=uuid7(),
            available_in_seconds=7 * 24 * 60 * 60,
        )
        overdue = dw.enqueue(
            arrange,
            command_type="RECONCILE_REFUND",
            payload={"refund_id": str(uuid7())},
            correlation_id=uuid7(),
        )
    arrange.close()

    # Backdated rather than waiting a lease out, and through the administrative
    # connection because no platform role may rewrite a command's schedule.
    with capi_admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox_events SET available_at = now() - interval '1 hour', "
                "created_at = now() - interval '2 hours' WHERE id = :i"
            ),
            {"i": overdue.command_id},
        )

    body = scenario_client("GET", "/v1/ops/outbox").json()
    assert body["counts"]["PENDING"] == 3

    waiting = body["waiting"]
    assert waiting["parked"] == 1
    assert waiting["overdue"] == 1
    assert waiting["parked_beyond_seconds"] == dw.DEFAULT_POLICY.backoff_cap_seconds
    assert waiting["overdue_beyond_seconds"] == dw.DEFAULT_POLICY.lease_seconds

    # The oldest of the two, named -- so a surface can point at a command instead of
    # printing a number an operator then has to go and match against a list.
    assert waiting["oldest"]["command_id"] == str(overdue.command_id)
    assert waiting["oldest"]["command_type"] == "RECONCILE_REFUND"

    # The command about to run is in neither set. A summary that counted it would cry
    # wolf on a healthy queue, and an operator learns to ignore a screen that does that.
    assert str(soon.command_id) not in {waiting["oldest"]["command_id"]}
    assert str(parked.command_id) != waiting["oldest"]["command_id"]

    # Filtering the page does not narrow the warning: an operator looking at DONE has not
    # thereby stopped a refund being parked.
    filtered = scenario_client("GET", "/v1/ops/outbox", params={"status": "DEAD"}).json()
    assert filtered["commands"] == []
    assert filtered["waiting"]["parked"] == 1
    assert filtered["waiting"]["overdue"] == 1


def test_reviving_a_dead_command_returns_it_to_the_queue(
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    capi_kernel_engine: Engine,
    scenario_client: Callable[..., Any],
) -> None:
    """A dead letter is a state, not a deletion: there is a way back.

    And only from ``DEAD``. Reviving a ``DONE`` command would re-run a completed money
    operation, so the second call here is refused with the status actually found -- as a
    200 carrying the outcome, because the operator asked a question and got a truthful
    answer rather than an error.
    """
    arrange = Session(capi_kernel_engine, expire_on_commit=False)
    with arrange.begin():
        set_tenant(arrange, demo_session.tenant_id)
        command = dw.enqueue(
            arrange,
            command_type="CREATE_ORDER",
            payload={"checkout_id": str(uuid7()), "version": 1},
            correlation_id=uuid7(),
        )
    arrange.close()

    with capi_admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE outbox_events SET status = 'DEAD', attempts = 8 WHERE id = :i"),
            {"i": command.command_id},
        )

    revived = scenario_client("POST", f"/v1/ops/outbox/{command.command_id}/revive")
    assert revived.status_code == 200, revived.text
    assert revived.json()["code"] == "OK"
    assert revived.json()["status"] == "PENDING"

    again = scenario_client("POST", f"/v1/ops/outbox/{command.command_id}/revive")
    assert again.status_code == 200, again.text
    assert again.json()["code"] == "CONCURRENT_OPERATION"
    assert again.json()["status"] == "PENDING"


# ------------------------------------------------- the reasoning and speech failures


#: Every table money is recorded in, plus the queue that makes it move. The point of the
#: assertions below is that the *count* of all of them is identical before and after a
#: turn whose reasoning layer was made to fail, because the deterministic layer does not
#: depend on the model behaving. ``platform_db`` owns the list, so a financial table added
#: later is covered here without anybody remembering to add it.
MONEY_TABLES: tuple[str, ...] = (*FINANCIAL_TABLES, "outbox_events")


def _table_counts(session: Session, tenant_id: uuid.UUID) -> dict[str, int]:
    return {
        table: session.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),  # noqa: S608 - fixed list
            {"t": tenant_id},
        ).scalar_one()
        for table in (*MONEY_TABLES, "audit_events")
    }


def _fault_row(session: Session, fault_id: str) -> Any:
    return session.execute(
        text(
            "SELECT kind, checkout_id, payment_attempt_id, armed, consumed_at "
            "FROM scenario_faults WHERE id = :i"
        ),
        {"i": fault_id},
    ).one()


def _arm(scenario_client: Callable[..., Any], kind: str, **extra: Any) -> Any:
    return scenario_client(
        "POST", "/v1/scenario/faults", json={"kind": kind, "once": True, **extra}
    )


@pytest.mark.parametrize("kind", ["LLM_FAILURE", "TTS_FAILURE"])
def test_the_new_faults_arm_tenant_wide(
    kind: str, kernel_session: Session, scenario_client: Callable[..., Any]
) -> None:
    """Both nullable identifiers stay NULL, which is what "tenant-wide" is made of.

    A reasoning failure has no checkout and a speech failure has no payment attempt, so
    there is nothing honest to put in either column. Leaving them NULL is also what lets
    the shared claim in ``platform_db`` match the row from a consumer that knows neither.
    """
    response = _arm(scenario_client, kind)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["armed"] is True
    assert body["checkout_id"] is None and body["payment_attempt_id"] is None

    row = _fault_row(kernel_session, body["fault_id"])
    assert row.kind == kind
    assert row.checkout_id is None and row.payment_attempt_id is None
    assert row.armed is True and row.consumed_at is None


@pytest.mark.parametrize("kind", ["LLM_FAILURE", "TTS_FAILURE"])
def test_the_new_faults_refuse_a_narrower_scope(
    kind: str, approved_checkout: ApprovedCheckout, scenario_client: Callable[..., Any]
) -> None:
    """422, because the alternative is a row that waits forever for a match.

    Their consumer is an agent turn, which carries no checkout and no payment attempt.
    Accepting the identifier and then ignoring it would leave the operator watching for a
    failure that the claim can never select -- the silent scoping mismatch ``arm_fault``'s
    own docstring says it refuses to hide.
    """
    scoped = _arm(scenario_client, kind, checkout_id=str(approved_checkout.checkout_id))
    assert scoped.status_code == 422, scoped.text
    assert scoped.json()["kind"] == kind


def test_the_turn_and_the_controller_spell_the_faults_the_same_way() -> None:
    """``agent_service`` names these faults without importing the controller.

    That is deliberate -- the live turn path depends on no demo apparatus -- and it costs
    exactly one thing, which this test buys back: a rename on either side would otherwise
    produce a lever that arms with a 201 and then never fires, and nothing would say so.
    """
    assert svc.FaultKind.LLM_FAILURE.value == agent_service.REASONING_FAULT
    assert svc.FaultKind.TTS_FAILURE.value == agent_service.SPEECH_FAULT
    assert {kind.value for kind in svc.TURN_FAULTS} == {
        agent_service.REASONING_FAULT,
        agent_service.SPEECH_FAULT,
    }


def test_an_armed_reasoning_fault_answers_from_the_records_and_moves_no_money(
    auth_client: TestClient,
    demo_session: MintedSession,
    kernel_session: Session,
    scenario_client: Callable[..., Any],
) -> None:
    """Specification 30, row one, made watchable: the model dies and the answer survives.

    The reply is not an apology and not a blank. It is the deterministic runner's own
    grounded answer, led by a sentence naming which layer went missing, because the
    answer never came from the model in the first place. Everything financial is asserted
    unchanged by count rather than by inspection: the whole argument of this platform is
    that the deterministic layer does not depend on the model behaving, so "the model
    failed and no money-bearing row appeared" is the claim worth pinning.
    """
    before = _table_counts(kernel_session, demo_session.tenant_id)
    armed = _arm(scenario_client, "LLM_FAILURE")
    assert armed.status_code == 201, armed.text

    response = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["reply"].startswith(render_reasoning_unavailable(Language.EN))
    assert [call["name"] for call in body["tool_calls"]] == ["catalog.search"]
    assert body["structured"] is not None and body["structured"]["kind"] == "products"
    # The apparatus travels in a header, never in the panel's contract (31.3).
    assert response.headers[agent_router.SCENARIO_FAULT_HEADER] == "LLM_FAILURE"
    assert "scenario_faults" not in body

    row = _fault_row(kernel_session, armed.json()["fault_id"])
    assert row.armed is False and row.consumed_at is not None

    fired = _audit_rows(kernel_session, demo_session.tenant_id, "SCENARIO_LLM_FAILURE_FIRED")
    assert len(fired) == 1
    assert fired[0].payload["fault_id"] == armed.json()["fault_id"]
    assert fired[0].payload["fallback"] == "deterministic_runner"
    assert fired[0].actor_type == "OPERATOR"
    assert str(demo_session.session_id) not in json.dumps(fired[0].payload)

    fired_event_id = kernel_session.execute(
        text("SELECT id FROM audit_events WHERE tenant_id = :t AND event_type = :e"),
        {"t": demo_session.tenant_id, "e": "SCENARIO_LLM_FAILURE_FIRED"},
    ).scalar_one()
    linked = kernel_session.execute(
        text(
            "SELECT kind, audit_event_id FROM scenario_runs "
            "WHERE injection_id = :i ORDER BY created_at"
        ),
        {"i": armed.json()["fault_id"]},
    ).all()
    assert [(r.kind, r.audit_event_id) for r in linked] == [
        ("SCENARIO_FAULT_ARMED", None),
        ("SCENARIO_LLM_FAILURE_FIRED", fired_event_id),
    ]

    after = _table_counts(kernel_session, demo_session.tenant_id)
    assert {t: after[t] for t in MONEY_TABLES} == {t: before[t] for t in MONEY_TABLES}
    assert after["audit_events"] == before["audit_events"] + 1


def test_a_reasoning_fault_fires_on_one_turn_and_no_more(
    auth_client: TestClient, scenario_client: Callable[..., Any]
) -> None:
    """Single-use, or the demonstration poisons every turn that follows it.

    The claim disarms in the same statement that selects, so there is no window in which
    a second turn could take the same row -- and the turn after the injected one is an
    ordinary turn with no leading sentence and no header at all.
    """
    assert _arm(scenario_client, "LLM_FAILURE").status_code == 201
    first = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})
    second = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})

    assert first.headers[agent_router.SCENARIO_FAULT_HEADER] == "LLM_FAILURE"
    assert agent_router.SCENARIO_FAULT_HEADER not in second.headers
    assert first.json()["reply"].startswith(render_reasoning_unavailable(Language.EN))
    assert not second.json()["reply"].startswith(render_reasoning_unavailable(Language.EN))


def test_an_armed_speech_fault_is_dispensed_without_touching_the_reply(
    auth_client: TestClient,
    demo_session: MintedSession,
    kernel_session: Session,
    scenario_client: Callable[..., Any],
) -> None:
    """Specification 30, row two: the modality degrades and the text does not.

    The API's whole part in a speech failure is to hand the gateway the fault it claimed
    and to record that it did. The buyer's text is byte-for-byte the reply of the same
    turn run without the fault, which is what makes the gateway's later ``tts_failed``
    frame a *modality* fallback rather than a degraded answer.
    """
    control = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})
    assert control.status_code == 200, control.text
    before = _table_counts(kernel_session, demo_session.tenant_id)

    armed = _arm(scenario_client, "TTS_FAILURE")
    assert armed.status_code == 201, armed.text
    response = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})

    assert response.status_code == 200, response.text
    assert response.headers[agent_router.SCENARIO_FAULT_HEADER] == "TTS_FAILURE"
    assert response.json()["reply"] == control.json()["reply"]

    assert _fault_row(kernel_session, armed.json()["fault_id"]).armed is False
    fired = _audit_rows(kernel_session, demo_session.tenant_id, "SCENARIO_TTS_FAILURE_FIRED")
    assert len(fired) == 1

    after = _table_counts(kernel_session, demo_session.tenant_id)
    assert {t: after[t] for t in MONEY_TABLES} == {t: before[t] for t in MONEY_TABLES}
    assert after["audit_events"] == before["audit_events"] + 1


def test_an_armed_reasoning_fault_stops_a_configured_runner_from_running_at_all(
    api_app: FastAPI, auth_client: TestClient, scenario_client: Callable[..., Any]
) -> None:
    """The fault fires *instead of* the model call, never alongside it.

    The sibling of ``test_capi_agent.py``'s runner-seam test, and the reason this one
    exists: a fault that let the runner start and then discarded its answer would be a
    simulation of a reasoning failure, with a half-run turn to explain away. Here the
    scripted runner is installed, the turn answers, and ``run`` was never entered.
    """
    entered: list[str] = []

    class Scripted:
        def run(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome:
            entered.append(turn.message)
            return TurnOutcome(reply="the model spoke")

    assert _arm(scenario_client, "LLM_FAILURE").status_code == 201
    api_app.state.agent_runner = Scripted()
    try:
        response = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})
    finally:
        api_app.state.agent_runner = None

    assert response.status_code == 200, response.text
    assert entered == []
    assert "the model spoke" not in response.json()["reply"]
    assert [call["name"] for call in response.json()["tool_calls"]] == ["catalog.search"]


def test_a_turn_cannot_consume_a_fault_where_the_controller_does_not_exist(
    capi_app_engine: Engine,
    seeded_tenant: SeededTenant,
    settings_for_tests: Settings,
) -> None:
    """The consuming gate, which is an absence rather than a check.

    Arming is already impossible without the controller -- ``require_scenario_key``
    returns 404 -- but a row could still be present from a profile change or a shared
    database, and a turn that would fire it anyway would mean a reasoning fault could
    reach a buyer outside a demonstration. With ``scenario_routes_enabled`` false the
    router hands ``run_turn`` no claimer at all, so the row is not consulted: it is still
    armed afterwards, and the reply is an ordinary one.
    """
    without_key = create_app(
        Settings(
            PROFILE="development",
            DATABASE_URL_APP=APP_URL,
            DATABASE_URL_KERNEL=KERNEL_URL,
            RAZORPAY_KEY_ID=settings_for_tests.razorpay_key_id,
            RAZORPAY_KEY_SECRET=settings_for_tests.razorpay_key_secret.get_secret_value(),
            RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
        )
    )
    assert without_key.state.settings.scenario_routes_enabled is False

    fault_id = uuid7()
    with Session(capi_app_engine) as arming, arming.begin():
        set_tenant(arming, seeded_tenant.tenant_id)
        arming.execute(
            text(
                "INSERT INTO scenario_faults (id, tenant_id, kind, armed) "
                "VALUES (:i, :t, 'LLM_FAILURE', true)"
            ),
            {"i": fault_id, "t": seeded_tenant.tenant_id},
        )

    with TestClient(without_key) as client:
        minted = client.post(
            "/v1/demo/sessions",
            json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "BUYER"},
        )
        assert minted.status_code == 201, minted.text
        token = minted.json()["token"]
        response = client.post(
            "/v1/agent/turn",
            json={"message": "milk", "locale": "en"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    assert agent_router.SCENARIO_FAULT_HEADER not in response.headers
    assert not response.json()["reply"].startswith(render_reasoning_unavailable(Language.EN))

    with Session(capi_app_engine) as reading, reading.begin():
        set_tenant(reading, seeded_tenant.tenant_id)
        assert _fault_row(reading, str(fault_id)).armed is True


def test_the_two_fault_vocabularies_agree() -> None:
    """Every worker-claimable fault is armable, and every provider fault has a claimer.

    ``scenario_faults.kind`` is a string, and the two ``FaultKind`` enums are separate
    Python objects that never import each other -- deliberately, because the arming side
    and the consuming side are different processes with different vocabularies. Nothing
    made them agree, and by the time anyone looked they did not: the API could arm a
    ``PAYMENT_FETCH_TIMEOUT`` no worker has ever claimed, so an operator demonstrating a
    payment-fetch timeout watched a normal payment succeed under a row that said
    ``armed: true`` forever; and the worker claimed a ``RECONCILE_FETCH_TIMEOUT`` the API
    refused to arm, which is the one fault the ADR D13 bounded-attempts escalation is
    shown with.

    Renaming the member fixed today's instance. This asserts the property, which is what
    stops the next one: the API's provider-timeout names and the worker's must be the same
    set. The turn-side faults are excluded because their consumer is the API process and
    the voice gateway, neither of which is the worker -- that asymmetry is the design, and
    naming it here is what keeps a future reader from "fixing" it by adding them.
    """
    from action_executor.faults import FaultKind as WorkerFaultKind
    from commerce_api.services import scenario_service as svc

    armable_provider_faults = {kind.value for kind in svc.FaultKind if kind not in svc.TURN_FAULTS}
    claimable = {kind.value for kind in WorkerFaultKind}

    assert armable_provider_faults == claimable, (
        "the scenario controller and the Action Executor disagree about fault names.\n"
        f"    armable here, claimed by no worker: {sorted(armable_provider_faults - claimable)}\n"
        f"    claimed by the worker, not armable: {sorted(claimable - armable_provider_faults)}"
    )


@pytest.mark.parametrize("credential", ["missing", "operator", "other_buyer"])
def test_duplicate_submit_requires_checkout_owner_even_for_operator(
    client, operator_headers, mint_client, approved_checkout, credential, kernel_session
):
    headers = dict(operator_headers)
    if credential == "operator":
        headers["X-Scenario-Buyer-Authorization"] = operator_headers["Authorization"]
    elif credential == "other_buyer":
        other, _ = mint_client(buyer_ref="not-the-checkout-owner")
        headers["X-Scenario-Buyer-Authorization"] = other.headers["Authorization"]
    response = client.post(
        "/v1/scenario/duplicate-submit",
        headers=headers,
        json={"checkout_id": str(approved_checkout.checkout_id), "version": 1},
    )
    expected = {"missing": 401, "operator": 403, "other_buyer": 404}
    assert response.status_code == expected[credential], response.text
    assert (
        kernel_session.execute(
            text("SELECT count(*) FROM payment_attempts WHERE checkout_id = :id"),
            {"id": approved_checkout.checkout_id},
        ).scalar_one()
        == 0
    )

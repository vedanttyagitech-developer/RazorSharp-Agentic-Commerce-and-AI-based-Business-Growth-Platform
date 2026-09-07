"""One confirmation, and a "no" that does not cancel anything.

Two product rules are proven here, both of them over real HTTP against the real database
as ``NOSUPERUSER NOBYPASSRLS`` roles, because both are claims about what a transaction
committed and not about what a handler returned.

**One confirmation before payment.** ``approve-and-pay`` records the buyer's decision and
admits it in a single locked transaction, so ``APPROVAL_REQUIRED -> APPROVED ->
EXECUTION_PENDING`` has no observable middle. The evidence for that is the audit chain:
every event the press produced carries one correlation id, which two HTTP requests could
not have, and no version is ever left ``APPROVED`` with an approval nobody spent. A price
that moved still answers 200 with ``REAPPROVAL_REQUIRED`` and version N+1's card, exactly
as ``submit`` does, because it is the same code producing it.

**A hold is not a cancellation.** ``hold`` writes down that the buyer was asked and
declined and changes nothing else: the version stays ``APPROVAL_REQUIRED``, the
reservation keeps its stock, the basket is untouched, and the same content hash can be
approved afterwards. ``reject`` is still the endpoint that retires a version, and the
tests below check that ``hold`` never behaves like it.

Neither route weakens the boundary in specification 5.3. An agent may submit a checkout a
buyer approved; it may not approve one, and it may not record that a buyer declined one
either -- writing "the buyer passed" into the audit stream is the same forgery as writing
"the buyer agreed", made one step earlier.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from commerce_api.services import checkout_service
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from transaction_kernel import CheckoutState, RecoveryCode

from conftest import MintedSession

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"
_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


# --------------------------------------------------------------------------- helpers


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


def _card(client: TestClient, *, sku: str = MILK, quantity: int = 2) -> dict[str, Any]:
    """A basket with one priced line, checked out. Returns the approval card."""
    opened = client.post("/v1/baskets", headers=_headers())
    assert opened.status_code == 201, opened.text
    basket_id = opened.json()["basket_id"]

    line = client.put(
        f"/v1/baskets/{basket_id}/lines/{sku}",
        json={"quantity": quantity},
        headers=_headers(),
    )
    assert line.status_code == 200, line.text

    checkout = client.post(f"/v1/baskets/{basket_id}/checkout", headers=_headers())
    assert checkout.status_code == 201, checkout.text
    card: dict[str, Any] = checkout.json()
    card["basket_id"] = basket_id
    return card


def _echo(card: dict[str, Any]) -> dict[str, Any]:
    """Exactly the three fields the card carries, and nothing else."""
    return {
        "content_hash": card["content_hash"],
        "amount_minor": card["amount_minor"],
        "currency": card["currency"],
    }


def _approve_and_pay(
    client: TestClient, card: dict[str, Any], *, body: dict[str, Any] | None = None, key: str = ""
) -> Any:
    return client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve-and-pay",
        json=_echo(card) if body is None else body,
        headers={"Idempotency-Key": key or _key()},
    )


def _hold(client: TestClient, card: dict[str, Any], *, content_hash: str | None = None) -> Any:
    return client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/hold",
        json={"content_hash": card["content_hash"] if content_hash is None else content_hash},
        headers=_headers(),
    )


def _rows(engine: Engine, tenant_id: uuid.UUID, statement: str, **params: Any) -> list[Any]:
    with engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        return list(conn.execute(text(statement), {"tenant": tenant_id, **params}).all())


def _events(engine: Engine, tenant_id: uuid.UUID, checkout_id: str) -> list[Any]:
    return _rows(
        engine,
        tenant_id,
        "SELECT event_type, correlation_id, payload FROM audit_events "
        "WHERE tenant_id = :tenant AND aggregate_id = :checkout ORDER BY seq",
        checkout=uuid.UUID(checkout_id),
    )


def _seconds_ahead(moment: str) -> float:
    return (datetime.fromisoformat(moment) - datetime.now(tz=UTC)).total_seconds()


# ------------------------------------------------------------- one confirmation


def test_one_press_records_the_approval_and_admits_it(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """The whole of steps 4, 6 and 7 in the single press the buyer actually made."""
    card = _card(auth_client)
    response = _approve_and_pay(auth_client, card)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["allowed"] is True, body
    assert body["code"] == RecoveryCode.OK.value
    assert body["outcome"] == RecoveryCode.OK.value
    assert body["state"] == CheckoutState.EXECUTION_PENDING.value
    assert body["grant_id"] and body["payment_attempt_id"] and body["command_id"]

    # Everything the caller needs to go on to payment is on this one response, including
    # what was consented to -- there is no second request to learn it from.
    assert body["approval"]["content_hash"] == card["content_hash"]
    assert body["approval"]["amount_minor"] == card["amount_minor"]
    assert body["approval"]["currency"] == card["currency"]
    assert body["approval"]["approval_id"]

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    money = _rows(
        capi_admin_engine,
        tenant,
        "SELECT (SELECT count(*) FROM payment_attempts WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout) AS attempts, "
        "(SELECT count(*) FROM execution_grants WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout) AS grants, "
        "(SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant) AS commands",
        checkout=checkout_id,
    )[0]
    assert (money.attempts, money.grants, money.commands) == (1, 1, 1)


def test_the_version_is_never_left_approved_with_nothing_spending_it(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """The reason this endpoint exists, stated as the two facts that prove it.

    First: one correlation id across ``approval.recorded`` and ``approval.consumed``. A
    correlation id is minted per HTTP request, so two ids would mean two requests and a
    window between them; one id means the decision and the admission that spent it were
    the same committed transaction.

    Second: the approval row is ``CONSUMED``. There is no instant a sweep, a supersede or
    a second submit could have found this version ``APPROVED`` with a live approval on it,
    because the transaction that created that state also ended it.
    """
    card = _card(auth_client)
    assert _approve_and_pay(auth_client, card).json()["allowed"] is True

    tenant = demo_session.tenant_id
    events = _events(capi_admin_engine, tenant, card["checkout_id"])
    by_type = {row.event_type: row for row in events}
    assert "approval.recorded" in by_type
    assert "approval.consumed" in by_type
    assert (
        by_type["approval.recorded"].correlation_id == by_type["approval.consumed"].correlation_id
    )

    stored = _rows(
        capi_admin_engine,
        tenant,
        "SELECT status FROM approvals WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )
    assert [row.status for row in stored] == ["CONSUMED"]

    versions = _rows(
        capi_admin_engine,
        tenant,
        "SELECT version, status FROM checkout_versions WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout ORDER BY version",
        checkout=uuid.UUID(card["checkout_id"]),
    )
    assert [(row.version, row.status) for row in versions] == [
        (1, CheckoutState.EXECUTION_PENDING.value)
    ]


def test_approving_and_submitting_separately_is_two_requests(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """The control for the test above, and the regression guard for the old two routes.

    ``approve`` then ``submit`` still work exactly as they did -- the price-shift
    walkthrough drives them one at a time on purpose -- and their audit events carry two
    different correlation ids, which is precisely the window ``approve-and-pay`` closes.
    """
    card = _card(auth_client)
    approved = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/approve",
        json=_echo(card),
        headers=_headers(),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == CheckoutState.APPROVED.value

    submitted = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/submit",
        headers=_headers(),
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["allowed"] is True, submitted.text

    events = _events(capi_admin_engine, demo_session.tenant_id, card["checkout_id"])
    by_type = {row.event_type: row for row in events}
    assert (
        by_type["approval.recorded"].correlation_id != by_type["approval.consumed"].correlation_id
    )


def test_a_price_that_moved_answers_two_hundred_with_the_next_card(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    api_app: Any,
) -> None:
    """A refusal is a normal outcome of this endpoint, not an error it may raise."""
    from commerce_domain import Money

    card = _card(auth_client)
    with api_app.state.merchants.mutating(demo_session.merchant_id) as scenario:
        scenario.set_price(MILK, Money(4000, "INR"))

    response = _approve_and_pay(auth_client, card)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["allowed"] is False
    assert body["code"] == RecoveryCode.REAPPROVAL_REQUIRED.value
    assert body["next_version"] == 2
    assert body["attempt_id"] is None
    # Version N+1 is already drawn, with its own hash and the deltas that explain it.
    assert body["approval_card"]["version"] == 2
    assert body["approval_card"]["previous_version"] == 1
    assert body["approval_card"]["content_hash"] != card["content_hash"]
    assert body["approval_card"]["amount_minor"] > card["amount_minor"]
    # And what the buyer consented to is still reported honestly: they did press yes, on
    # bytes that had already moved. Nothing was charged for it.
    assert body["approval"]["amount_minor"] == card["amount_minor"]

    counts = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT (SELECT count(*) FROM payment_attempts WHERE tenant_id = :tenant) AS attempts, "
        "(SELECT count(*) FROM execution_grants WHERE tenant_id = :tenant) AS grants, "
        "(SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant) AS commands",
    )[0]
    assert (counts.attempts, counts.grants, counts.commands) == (0, 0, 0)


def test_the_new_card_can_be_approved_and_paid_in_turn(
    auth_client: TestClient, demo_session: MintedSession, api_app: Any
) -> None:
    """Step 8: the buyer sees the new total, presses once more, and this time it goes."""
    from commerce_domain import Money

    card = _card(auth_client)
    with api_app.state.merchants.mutating(demo_session.merchant_id) as scenario:
        scenario.set_price(MILK, Money(4000, "INR"))
    denied = _approve_and_pay(auth_client, card).json()

    second = denied["approval_card"]
    second["checkout_id"] = card["checkout_id"]
    allowed = _approve_and_pay(auth_client, second)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["allowed"] is True, allowed.text
    assert allowed.json()["approval"]["version"] == 2
    assert allowed.json()["approval"]["amount_minor"] == second["amount_minor"]


def test_a_second_press_is_told_about_the_live_attempt(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
) -> None:
    """ADR 0003 D9. A buyer who presses twice sees one payment, not a second approval."""
    card = _card(auth_client)
    first = _approve_and_pay(auth_client, card).json()
    assert first["allowed"] is True

    again = _approve_and_pay(auth_client, card)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["allowed"] is False
    assert body["code"] == RecoveryCode.DUPLICATE_OPERATION.value
    assert body["attempt_id"] == first["payment_attempt_id"]
    # Nothing was decided the second time, so nothing is reported as decided.
    assert body["approval"] is None

    stored = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT status FROM approvals WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )
    assert [row.status for row in stored] == ["CONSUMED"]


def test_the_same_key_replays_rather_than_approving_twice(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    card = _card(auth_client)
    key = _key()
    first = _approve_and_pay(auth_client, card, key=key)
    second = _approve_and_pay(auth_client, card, key=key)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()

    attempts = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT count(*) AS n FROM payment_attempts WHERE tenant_id = :tenant",
    )[0]
    assert attempts.n == 1


def test_an_agent_may_not_approve_and_pay(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Registry A has no ``checkout.approve``, and folding two calls into one changes
    nothing about that: consent is not delegable to the thing that proposed the purchase."""
    agent_client, _ = mint_client(actor_type="AGENT")
    with agent_client as agent:
        card = _card(agent)
        response = _approve_and_pay(agent, card)
    assert response.status_code == 403, response.text
    assert response.json()["capability"] == "checkout.approve"


# ----------------------------------------------------- the button cannot lie (K4)


def test_the_amount_recorded_is_the_amount_on_the_card(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """The button's "Approve to pay X" and the row a payment is authorised from agree.

    The card's ``amount_minor``, the approval row, the response's approval block and the
    Execution Grant the worker will carry to Razorpay are read separately and compared to
    each other. A rendering bug, a rounding difference or a currency swap anywhere along
    that chain breaks this test rather than a buyer's statement.
    """
    card = _card(auth_client)
    body = _approve_and_pay(auth_client, card).json()
    assert body["allowed"] is True, body

    tenant = demo_session.tenant_id
    approval = _rows(
        capi_admin_engine,
        tenant,
        "SELECT amount_minor, currency, content_hash FROM approvals "
        "WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )[0]
    grant = _rows(
        capi_admin_engine,
        tenant,
        "SELECT amount_minor, currency, content_hash FROM execution_grants "
        "WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )[0]

    assert approval.amount_minor == card["amount_minor"]
    assert approval.currency == card["currency"]
    assert approval.content_hash == card["content_hash"]
    assert body["approval"]["amount_minor"] == card["amount_minor"]
    assert (grant.amount_minor, grant.currency) == (card["amount_minor"], card["currency"])
    assert grant.content_hash == card["content_hash"]


def test_a_button_that_asked_for_a_different_number_reaches_no_row(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """One paisa either way is refused, and refused before anything is written."""
    card = _card(auth_client)
    for drift in (-1, 1):
        wrong = _echo(card) | {"amount_minor": card["amount_minor"] + drift}
        response = _approve_and_pay(auth_client, card, body=wrong)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == RecoveryCode.STALE_CHECKOUT.value

    counts = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT (SELECT count(*) FROM approvals WHERE tenant_id = :tenant) AS approvals, "
        "(SELECT count(*) FROM payment_attempts WHERE tenant_id = :tenant) AS attempts",
    )[0]
    assert (counts.approvals, counts.attempts) == (0, 0)
    # And the card is still there to be approved for the number it actually says.
    assert _approve_and_pay(auth_client, card).json()["allowed"] is True


# ------------------------------------------------------------------ "no" is a hold


def test_a_hold_leaves_the_version_the_stock_and_the_basket_alone(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """Declining now costs the buyer nothing and costs the merchant nothing."""
    card = _card(auth_client)
    response = _hold(auth_client, card)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["state"] == CheckoutState.APPROVAL_REQUIRED.value
    assert body["reason"] == "buyer_not_now"
    assert body["checkout"]["content_hash"] == card["content_hash"]
    assert body["audit_event_id"]
    # The countdown the buyer keeps looking at is the one they were already looking at.
    assert body["reservation"]["state"] == "ACTIVE"
    assert body["reservation"]["expires_at"] == card["reservation"]["expires_at"]

    tenant = demo_session.tenant_id
    version = _rows(
        capi_admin_engine,
        tenant,
        "SELECT status FROM checkout_versions WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )[0]
    assert version.status == CheckoutState.APPROVAL_REQUIRED.value

    state = _rows(
        capi_admin_engine,
        tenant,
        "SELECT (SELECT count(*) FROM approvals WHERE tenant_id = :tenant) AS approvals, "
        "(SELECT status FROM reservations WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout) AS hold, "
        "(SELECT status FROM carts WHERE tenant_id = :tenant AND id = :basket) AS basket",
        checkout=uuid.UUID(card["checkout_id"]),
        basket=uuid.UUID(card["basket_id"]),
    )[0]
    # No decision was recorded, the stock is still held, and the cart was not reopened.
    assert state.approvals == 0
    assert state.hold == "ACTIVE"
    assert state.basket == "CHECKED_OUT"

    held = _events(capi_admin_engine, tenant, card["checkout_id"])
    kinds = [row.event_type for row in held]
    assert kinds.count("approval.held") == 1
    assert kinds.count("approval.rejected") == 0
    payload = next(row.payload for row in held if row.event_type == "approval.held")
    assert payload["content_hash"] == card["content_hash"]
    assert payload["reason"] == "buyer_not_now"
    assert payload["version_status"] == CheckoutState.APPROVAL_REQUIRED.value


def test_a_held_card_can_still_be_approved_and_paid(auth_client: TestClient) -> None:
    """The buyer thought about it, came back, and the same bytes were still waiting."""
    card = _card(auth_client)
    assert _hold(auth_client, card).status_code == 200
    assert _hold(auth_client, card).status_code == 200

    response = _approve_and_pay(auth_client, card)
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.text
    assert response.json()["approval"]["content_hash"] == card["content_hash"]


def test_a_hold_is_refused_once_the_version_is_decided(
    auth_client: TestClient, capi_admin_engine: Engine, demo_session: MintedSession
) -> None:
    """There is nothing left to decline after a yes; withdrawing one is ``reject``."""
    card = _card(auth_client)
    approved = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/approve",
        json=_echo(card),
        headers=_headers(),
    )
    assert approved.status_code == 200, approved.text

    refused = _hold(auth_client, card)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == RecoveryCode.STALE_CHECKOUT.value

    stored = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT status FROM approvals WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )
    assert [row.status for row in stored] == ["RECORDED"]


def test_a_hold_must_name_the_bytes_the_buyer_saw(auth_client: TestClient) -> None:
    refused = _hold(auth_client, card=_card(auth_client), content_hash="not-the-hash")
    assert refused.status_code == 409, refused.text


def test_rejecting_still_retires_the_version(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """``reject`` is untouched: a real cancellation still ends the version and the hold."""
    card = _card(auth_client)
    rejected = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/reject",
        json={"content_hash": card["content_hash"], "reason": "buyer_declined"},
        headers=_headers(),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == CheckoutState.CANCELLED.value

    state = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT (SELECT status FROM checkout_versions WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout) AS version, "
        "(SELECT status FROM reservations WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout) AS hold",
        checkout=uuid.UUID(card["checkout_id"]),
    )[0]
    assert state.version == CheckoutState.CANCELLED.value
    assert state.hold == "RELEASED"


def test_an_agent_may_not_record_that_the_buyer_declined(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Writing "the buyer passed" is the same forgery as writing "the buyer agreed"."""
    agent_client, _ = mint_client(actor_type="AGENT")
    with agent_client as agent:
        card = _card(agent)
        response = _hold(agent, card)
    assert response.status_code == 403, response.text
    assert response.json()["capability"] == "checkout.reject"


# ------------------------------------------------------------------- a smaller hold


def test_the_stock_comes_back_to_the_shelf_in_five_minutes(auth_client: TestClient) -> None:
    """K3b. Stock a buyer is only thinking about is stock nobody else can have."""
    assert checkout_service.RESERVATION_TTL_SECONDS == 300
    card = _card(auth_client)
    remaining = _seconds_ahead(card["reservation"]["expires_at"])
    assert 0 < remaining <= checkout_service.RESERVATION_TTL_SECONDS
    # Generous on the low side because the deadline is PostgreSQL's clock and the
    # assertion is a Python one; the claim being made is only that it is minutes, not a
    # quarter of an hour.
    assert remaining > checkout_service.RESERVATION_TTL_SECONDS - 60

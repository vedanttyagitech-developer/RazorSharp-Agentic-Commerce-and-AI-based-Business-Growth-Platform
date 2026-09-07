"""The eleven-step demonstration's first eight steps, driven over HTTP.

Every test here asserts a rule the demonstration stands on, and each one is written
against the real database as ``NOSUPERUSER NOBYPASSRLS`` roles, because the guarantees
being checked -- single-winner admission, one grant per provider mutation, row-level
isolation -- live in PostgreSQL and not in Python.

What is proven, in order:

* search returns grounded hits with provenance, and distinguishes sold out from delisted;
* a cart quote is deterministic, and reports staleness when the catalogue moves;
* the happy path leaves **exactly one** payment attempt, **exactly one** Execution Grant
  and **exactly one** outbox command, with the grant linked to the command;
* an approval echoing the wrong content hash is refused, so consent is about bytes;
* a price change under an approved checkout denies with ``REAPPROVAL_REQUIRED``, the
  exact deltas and ``next_version`` N+1, creates **no** payment attempt, leaves N
  ``INVALIDATED`` and gives N+1 its own Policy-at-Sale Receipt and hold;
* re-approving N+1 then succeeds;
* two genuinely concurrent submits with different keys produce one winner;
* the same key replays the stored body rather than admitting twice.

The scenario injection is applied straight to the app's own
:class:`~commerce_api.merchants.MerchantRegistry` rather than through the scenario router,
which belongs to another build unit. The registry is the same object the kernel's state
source reads at admission, so the injection is exactly the one step 5 performs.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from commerce_api.deps import RequestContext, session_scope_for
from commerce_api.services import admission_service
from commerce_domain import Money, uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db import set_tenant
from sqlalchemy import Engine, text
from transaction_kernel import ActorType, AgentPrincipal, CheckoutState, RecoveryCode

from conftest import KERNEL_URL, MintedSession, SeededTenant

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"
ATTA = "AASH-STPL-002"
#: A third line, and a third GST rate: 0 bp on milk, 500 on atta, 1200 here. A
#: breakdown that reconstructs the tax by guessing one rate would not survive it.
BUTTER = "AMUL-DAIRY-005"
_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


# --------------------------------------------------------------------------- helpers


def _key() -> str:
    """A fresh Idempotency-Key. Unique per call, because a reused one is the point of
    another test entirely."""
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


def _open_basket(client: TestClient) -> str:
    response = client.post("/v1/carts", headers=_headers())
    assert response.status_code == 201, response.text
    return str(response.json()["cart_id"])


def _set_line(client: TestClient, cart_id: str, sku: str, quantity: int) -> dict[str, Any]:
    response = client.put(
        f"/v1/carts/{cart_id}/lines/{sku}",
        json={"quantity": quantity},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _open_checkout(client: TestClient, cart_id: str) -> dict[str, Any]:
    response = client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert response.status_code == 201, response.text
    card: dict[str, Any] = response.json()
    return card


def _approve(client: TestClient, card: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _submit(
    client: TestClient, checkout_id: str, version: int, *, key: str | None = None
) -> dict[str, Any]:
    response = client.post(
        f"/v1/checkouts/{checkout_id}/versions/{version}/submit",
        headers={"Idempotency-Key": key or _key()},
    )
    # ADR 0003 D15: allowed or denied, a kernel decision is always a 200.
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _basket_ready(client: TestClient, sku: str = MILK, quantity: int = 2) -> tuple[str, int]:
    """A cart with one priced line. Returns the cart id and the quoted total."""
    cart_id = _open_basket(client)
    body = _set_line(client, cart_id, sku, quantity)
    assert body["quote"] is not None, body
    return cart_id, int(body["quote"]["total_minor"])


def _count(engine: Engine, tenant_id: uuid.UUID, table: str, **where: Any) -> int:
    """Count rows of one tenant, with the tenant bound as the first statement."""
    predicates = "".join(f" AND {column} = :{column}" for column in where)
    # S608: `table` and the column names come from this module's own call sites, never
    # from request data, and a SQL identifier cannot be a bound parameter.
    query = f"SELECT count(*) FROM {table} WHERE tenant_id = :tenant{predicates}"  # noqa: S608
    statement = text(query)
    with engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        return int(conn.execute(statement, {"tenant": tenant_id, **where}).scalar_one())


def _rows(engine: Engine, tenant_id: uuid.UUID, statement: str, **params: Any) -> list[Any]:
    with engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        return list(conn.execute(text(statement), {"tenant": tenant_id, **params}).all())


@pytest.fixture
def inject(api_app: FastAPI, demo_session: MintedSession) -> Iterator[Callable[..., None]]:
    """Change merchant state the way step 5 does, on the registry admission reads."""

    def _inject(sku: str, price_minor: int) -> None:
        with api_app.state.merchants.mutating(demo_session.merchant_id) as scenario:
            scenario.set_price(sku, Money(price_minor, "INR"))

    yield _inject


# ------------------------------------------------------------------- step 1: discovery


def test_search_is_grounded_and_reports_freshness(auth_client: TestClient) -> None:
    """Hinglish reaches the same product English does, and every hit is provenanced."""
    response = auth_client.get("/v1/catalogue/search", params={"q": "doodh", "limit": 5})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["hits"], body
    assert body["skus"] == [hit["sku"] for hit in body["hits"]]
    assert body["freshness"]["source"]
    assert body["freshness"]["catalogue_revision"] == 0
    for hit in body["hits"]:
        # Sold out and delisted are different conversations, so they are different fields.
        assert hit["is_available"] == (hit["is_listed"] and hit["stock_units"] > 0)
        assert hit["matched_terms"], hit
        assert hit["unit_price_minor"] == hit["unit_price"]["minor"]


def test_product_detail_and_an_unknown_sku(auth_client: TestClient) -> None:
    """A SKU the merchant never issued is a 404, never an empty or out-of-stock answer."""
    found = auth_client.get(f"/v1/catalogue/products/{MILK}")
    assert found.status_code == 200, found.text
    assert found.json()["sku"] == MILK

    missing = auth_client.get("/v1/catalogue/products/GRO-NOT-REAL")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")


def test_search_needs_a_session(client: TestClient) -> None:
    """Discovery is behind the session like everything else; no token, no catalogue."""
    assert client.get("/v1/catalogue/search", params={"q": "milk"}).status_code == 401


# ---------------------------------------------------------------------- step 2: cart


def test_basket_quote_is_deterministic_and_flags_staleness(
    auth_client: TestClient, inject: Callable[..., None]
) -> None:
    """The fee engine computes the total; a moved catalogue makes the stored quote stale."""
    cart_id = _open_basket(auth_client)
    first = _set_line(auth_client, cart_id, MILK, 2)
    quote = first["quote"]

    assert quote["lines"][0]["sku"] == MILK
    assert quote["lines"][0]["quantity"] == 2
    assert quote["lines"][0]["subtotal_minor"] == quote["lines"][0]["unit_price_minor"] * 2
    # Every component adds up, and the API added none of it up itself.
    assert quote["total_minor"] == (
        quote["items_subtotal_minor"]
        + quote["items_tax_minor"]
        + quote["delivery_fee_minor"]
        + quote["delivery_tax_minor"]
    )
    assert quote["free_delivery_applied"] is False
    assert quote["gap_to_free_delivery_minor"] > 0
    assert first["stale"] is False

    inject(MILK, 3500)
    after = auth_client.get(f"/v1/carts/{cart_id}")
    assert after.status_code == 200, after.text
    reread = after.json()
    assert reread["stale"] is True
    assert reread["quote"]["lines"][0]["unit_price_minor"] == 3500


def test_setting_a_line_to_zero_removes_it(auth_client: TestClient) -> None:
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)
    both = _set_line(auth_client, cart_id, ATTA, 1)
    assert [line["sku"] for line in both["lines"]] == sorted([MILK, ATTA])

    removed = _set_line(auth_client, cart_id, ATTA, 0)
    assert [line["sku"] for line in removed["lines"]] == [MILK]


def test_a_basket_belongs_to_one_buyer(
    auth_client: TestClient,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Another buyer's cart is a 404, not a 403: a 403 confirms the id exists."""
    cart_id = _open_basket(auth_client)
    other, _ = mint_client(buyer_ref="someone-else")
    with other as stranger:
        assert stranger.get(f"/v1/carts/{cart_id}").status_code == 404


# ------------------------------------------------- steps 3, 4 and 6 to 8: the journey


def test_happy_path_makes_one_attempt_one_grant_one_command(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
) -> None:
    """Search to submit: exactly one of each money object, and the grant names its command."""
    cart_id, total = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)

    assert card["version"] == 1
    assert card["amount_minor"] == total
    assert card["policy_receipt_hash"]
    assert card["reservation"]["state"] == "ACTIVE"
    # The card's quote is hashed under the real checkout id, so this is the binding hash.
    assert card["quote"]["content_hash"] == card["content_hash"]

    approved = _approve(auth_client, card)
    assert approved["state"] == CheckoutState.APPROVED.value
    assert approved["approval"]["content_hash"] == card["content_hash"]
    assert approved["approval"]["amount_minor"] == total

    decision = _submit(auth_client, card["checkout_id"], 1)
    assert decision["allowed"] is True, decision
    assert decision["code"] == RecoveryCode.OK.value
    assert decision["outcome"] == RecoveryCode.OK.value
    assert decision["grant_id"] and decision["payment_attempt_id"]
    assert decision["attempt_id"] == decision["payment_attempt_id"]
    assert decision["state"] == CheckoutState.EXECUTION_PENDING.value

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    assert _count(capi_admin_engine, tenant, "payment_attempts", checkout_id=checkout_id) == 1
    assert _count(capi_admin_engine, tenant, "execution_grants", checkout_id=checkout_id) == 1
    assert _count(capi_admin_engine, tenant, "outbox_events") == 1

    # The grant and the command it rides on committed together, and the grant names it.
    grant = _rows(
        capi_admin_engine,
        tenant,
        "SELECT id, status, operation, amount_minor, currency, content_hash, "
        "payment_attempt_id, outbox_command_id FROM execution_grants "
        "WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=checkout_id,
    )[0]
    assert grant.status == "ISSUED"
    assert grant.operation == "PAYMENT_CREATE_ORDER"
    assert grant.amount_minor == total
    assert grant.content_hash == card["content_hash"]
    assert str(grant.payment_attempt_id) == decision["payment_attempt_id"]
    assert grant.outbox_command_id is not None

    command = _rows(
        capi_admin_engine,
        tenant,
        "SELECT id, command_type, payload FROM outbox_events WHERE tenant_id = :tenant",
    )[0]
    assert command.id == grant.outbox_command_id
    assert command.command_type == "PAYMENT_CREATE_ORDER"
    assert command.payload["grant_id"] == decision["grant_id"]
    assert command.payload["content_hash"] == card["content_hash"]
    assert command.payload["notes"]["checkout_id"] == card["checkout_id"]

    # The approval was spent, once, in the same transaction as the admission.
    approval = _rows(
        capi_admin_engine,
        tenant,
        "SELECT status FROM approvals WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=checkout_id,
    )
    assert [row.status for row in approval] == ["CONSUMED"]


def test_checkout_read_model_renders_the_journey(auth_client: TestClient) -> None:
    """Everything specification 8.2 renders a state from is on one document."""
    cart_id, total = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)

    awaiting = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    assert awaiting["state"] == CheckoutState.APPROVAL_REQUIRED.value
    assert awaiting["current_version"] == 1
    assert awaiting["approval_card"]["content_hash"] == card["content_hash"]
    assert awaiting["versions"][0]["amount_minor"] == total
    assert awaiting["versions"][0]["policy_receipt_hash"] == card["policy_receipt_hash"]
    assert awaiting["attempt"] is None
    assert awaiting["cancellable"] is True

    _approve(auth_client, card)
    _submit(auth_client, card["checkout_id"], 1)

    after = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    assert after["state"] == CheckoutState.EXECUTION_PENDING.value
    assert after["attempt"]["state"] == "CREATED"
    assert after["versions"][0]["approval"]["content_hash"] == card["content_hash"]
    # A grant is issued and a command is queued, so cancellation is no longer a simple no.
    assert after["approval_card"] is None


def test_the_read_path_says_what_is_being_approved(
    auth_client: TestClient, inject: Callable[..., None]
) -> None:
    """``GET /v1/checkouts/{id}`` names every line, and names the approved one.

    The screen a buyer actually reaches is this one: the storefront pushes to
    ``/checkout/{id}`` and the page fetches. It used to answer ``approval_card.quote:
    null``, so a card asking consent for a four-figure sum listed no product at all --
    on a platform whose entire claim is that consent binds to exact bytes.

    Two things are asserted, and the second is the one that matters. First, the read and
    the open response describe one card identically, down to every integer. Second, a
    price injected *after* version 1 was written does not move a single figure on the
    read: the breakdown is the approved document read back, not a fresh quote. A consent
    screen that re-priced would show an amount nobody consented to, and admission -- not
    a read -- is where a moved price is caught.
    """
    cart_id = _open_basket(auth_client)
    _set_line(auth_client, cart_id, MILK, 2)
    _set_line(auth_client, cart_id, ATTA, 1)
    priced = _set_line(auth_client, cart_id, BUTTER, 3)
    assert priced["quote"] is not None, priced

    card = _open_checkout(auth_client, cart_id)
    read = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    quote = read["approval_card"]["quote"]

    assert quote is not None, "the read path must send the breakdown it holds"
    # One card, one description: the open response and the read agree field for field.
    assert quote == card["quote"]

    # Every line is named, with the quantity the buyer asked for.
    assert {line["sku"]: line["quantity"] for line in quote["lines"]} == {
        MILK: 2,
        ATTA: 1,
        BUTTER: 3,
    }
    for line in quote["lines"]:
        assert line["name"], line
        assert line["subtotal_minor"] == line["unit_price_minor"] * line["quantity"]
        # The hashed document records the tax charged, never the rate behind it. Absent
        # is reported as absent; a zero here would assert a rate that was never stated.
        assert line["tax_bp"] is None, line

    # The rows a buyer reads add up to the figure they are asked to approve.
    components = (
        quote["items_subtotal_minor"]
        + quote["items_tax_minor"]
        + quote["delivery_fee_minor"]
        + quote["delivery_tax_minor"]
    )
    assert components == quote["total_minor"] == read["approval_card"]["amount_minor"]
    assert quote["items_subtotal_minor"] == sum(line["subtotal_minor"] for line in quote["lines"])
    assert quote["items_tax_minor"] == sum(line["tax_minor"] for line in quote["lines"])
    assert quote["content_hash"] == card["content_hash"]

    # The merchant moves the price of a line under the open checkout. The card is a
    # record of what was quoted, so nothing about it may move.
    inject(MILK, 9_99_99)
    again = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    assert again["approval_card"]["quote"] == quote
    assert again["approval_card"]["content_hash"] == card["content_hash"]


def test_approving_the_wrong_content_hash_is_refused(auth_client: TestClient) -> None:
    """Consent binds to bytes. A hash the version does not carry cannot be approved."""
    cart_id, total = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)

    response = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/approve",
        json={
            "content_hash": "0" * 64,
            "amount_minor": total,
            "currency": "INR",
        },
        headers=_headers(),
    )
    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")

    # Nothing moved: the version is still waiting for a decision it never got.
    state = auth_client.get(f"/v1/checkouts/{card['checkout_id']}").json()
    assert state["state"] == CheckoutState.APPROVAL_REQUIRED.value
    assert state["versions"][0]["approval"] is None


def test_approving_the_wrong_amount_is_refused(auth_client: TestClient) -> None:
    """The amount is echoed and compared too, so a client cannot show one and send another."""
    cart_id, total = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)

    response = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": total + 1,
            "currency": "INR",
        },
        headers=_headers(),
    )
    assert response.status_code == 409, response.text


def test_rejecting_a_version_releases_its_reservation(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """A decline that left stock reserved would cost the merchant the next sale too."""
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)

    response = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/1/reject",
        json={"content_hash": card["content_hash"], "reason": "changed_mind"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == CheckoutState.CANCELLED.value

    held = _rows(
        capi_admin_engine,
        demo_session.tenant_id,
        "SELECT status FROM reservations WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=uuid.UUID(card["checkout_id"]),
    )
    assert [row.status for row in held] == ["RELEASED"]


def test_cancel_is_a_structured_answer_not_an_exception(auth_client: TestClient) -> None:
    """Cancellation is always a 200 carrying a code, allowed or refused."""
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)

    response = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/cancel",
        json={"reason": "buyer_cancelled"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True
    assert body["code"] == RecoveryCode.OK.value

    repeat = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/cancel",
        json={"reason": "buyer_cancelled"},
        headers=_headers(),
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["allowed"] is False
    assert repeat.json()["code"] == RecoveryCode.DUPLICATE_OPERATION.value


# ----------------------------------------------- steps 5 to 8: the delta and version N+1


def test_a_price_change_denies_with_deltas_and_creates_version_two(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    inject: Callable[..., None],
) -> None:
    """The headline. The old approval is refused, the delta is exact, N+1 is ready."""
    cart_id, total = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    _approve(auth_client, card)

    # Step 5: merchant state changes underneath the approved checkout.
    inject(MILK, 4000)

    decision = _submit(auth_client, card["checkout_id"], 1)
    assert decision["allowed"] is False
    assert decision["code"] == RecoveryCode.REAPPROVAL_REQUIRED.value
    assert decision["next_version"] == 2
    assert decision["attempt_id"] is None

    # Step 7: the exact delta, in the kernel's own vocabulary.
    totals = [delta for delta in decision["deltas"] if delta["field_path"] == "total"]
    assert len(totals) == 1
    assert totals[0]["approved"] == total
    assert totals[0]["current"] > total
    assert totals[0]["reason"] == "total_changed"

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    # No Razorpay order will ever be created for this: no attempt, no grant, no command.
    assert _count(capi_admin_engine, tenant, "payment_attempts", checkout_id=checkout_id) == 0
    assert _count(capi_admin_engine, tenant, "execution_grants", checkout_id=checkout_id) == 0
    assert _count(capi_admin_engine, tenant, "outbox_events") == 0

    versions = _rows(
        capi_admin_engine,
        tenant,
        "SELECT version, status, content_hash, total_minor, policy_receipt_id, invalidated_at "
        "FROM checkout_versions WHERE tenant_id = :tenant AND checkout_id = :checkout "
        "ORDER BY version",
        checkout=checkout_id,
    )
    assert [row.version for row in versions] == [1, 2]
    assert versions[0].status == CheckoutState.INVALIDATED.value
    assert versions[0].invalidated_at is not None
    assert versions[1].status == CheckoutState.APPROVAL_REQUIRED.value
    assert versions[1].total_minor == totals[0]["current"]
    # N+1 carries its own Policy-at-Sale Receipt, not N's.
    assert versions[1].policy_receipt_id is not None
    assert versions[1].policy_receipt_id != versions[0].policy_receipt_id
    assert versions[1].content_hash != versions[0].content_hash

    # N's hold was released so N+1 could take one; exactly one is live.
    holds = _rows(
        capi_admin_engine,
        tenant,
        "SELECT checkout_version, status FROM reservations WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout ORDER BY checkout_version",
        checkout=checkout_id,
    )
    assert [(row.checkout_version, row.status) for row in holds] == [(1, "RELEASED"), (2, "ACTIVE")]

    # And the response hands the buyer the new card to decide about.
    fresh = decision["approval_card"]
    assert fresh["version"] == 2
    assert fresh["previous_version"] == 1
    assert fresh["content_hash"] == versions[1].content_hash
    assert fresh["policy_receipt_hash"]
    assert fresh["reservation"]["state"] == "ACTIVE"
    assert any(delta["field_path"] == "total" for delta in fresh["deltas"])


def test_reapproving_version_two_then_succeeds(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    inject: Callable[..., None],
) -> None:
    """Step 8: a fresh decision on N+1 admits, and only then does money become possible."""
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    _approve(auth_client, card)
    inject(MILK, 4000)
    denied = _submit(auth_client, card["checkout_id"], 1)

    fresh = denied["approval_card"]
    approved = _approve(auth_client, fresh)
    assert approved["approval"]["version"] == 2
    assert approved["approval"]["amount_minor"] == fresh["amount_minor"]

    decision = _submit(auth_client, card["checkout_id"], 2)
    assert decision["allowed"] is True, decision
    assert decision["checkout"]["version"] == 2
    assert decision["checkout"]["content_hash"] == fresh["content_hash"]

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    attempts = _rows(
        capi_admin_engine,
        tenant,
        "SELECT checkout_version, amount_minor FROM payment_attempts "
        "WHERE tenant_id = :tenant AND checkout_id = :checkout",
        checkout=checkout_id,
    )
    # One attempt, on N+1, for the new total. Version 1 never produced one.
    assert [(row.checkout_version, row.amount_minor) for row in attempts] == [
        (2, fresh["amount_minor"])
    ]
    assert _count(capi_admin_engine, tenant, "execution_grants", checkout_id=checkout_id) == 1


def test_submitting_version_one_after_supersede_is_refused(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    inject: Callable[..., None],
) -> None:
    """An invalidated version never comes back, however many times it is submitted.

    The second submit is refused by the kernel rather than by this service, and the
    refusal is a 200 carrying ``STALE_CHECKOUT`` -- the same shape as any other decision,
    because a retired version being refused is the machinery working. No second attempt,
    no second delta, no second version.
    """
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    _approve(auth_client, card)
    inject(MILK, 4000)
    _submit(auth_client, card["checkout_id"], 1)

    again = _submit(auth_client, card["checkout_id"], 1)
    assert again["allowed"] is False
    assert again["code"] == RecoveryCode.STALE_CHECKOUT.value
    assert again["explanation"] == "version_already_invalidated"
    assert again["next_version"] is None

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    assert _count(capi_admin_engine, tenant, "payment_attempts", checkout_id=checkout_id) == 0
    versions = _rows(
        capi_admin_engine,
        tenant,
        "SELECT version FROM checkout_versions WHERE tenant_id = :tenant "
        "AND checkout_id = :checkout",
        checkout=checkout_id,
    )
    assert sorted(row.version for row in versions) == [1, 2]


# --------------------------------------------------------- concurrency and idempotency


def test_two_concurrent_submits_produce_one_winner(
    auth_client: TestClient,
    api_app: FastAPI,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
) -> None:
    """Real threads, two keys, one checkout. Exactly one attempt, one grant, one command."""
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    _approve(auth_client, card)

    url = f"/v1/checkouts/{card['checkout_id']}/versions/1/submit"
    start = threading.Barrier(2)
    outcomes: list[dict[str, Any]] = []
    lock = threading.Lock()

    def submit() -> None:
        client = TestClient(api_app, headers=demo_session.auth_header)
        start.wait(timeout=10)
        response = client.post(url, headers={"Idempotency-Key": _key()})
        with lock:
            outcomes.append({"status": response.status_code, "body": response.json()})

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == 2, outcomes
    assert {item["status"] for item in outcomes} == {200}, outcomes
    allowed = [item["body"] for item in outcomes if item["body"]["allowed"]]
    refused = [item["body"] for item in outcomes if not item["body"]["allowed"]]
    assert len(allowed) == 1, outcomes
    assert len(refused) == 1, outcomes

    # The loser is told about the winner rather than handed a second attempt (ADR D9).
    assert refused[0]["code"] in {
        RecoveryCode.DUPLICATE_OPERATION.value,
        RecoveryCode.CONCURRENT_OPERATION.value,
    }
    assert refused[0]["attempt_id"] == allowed[0]["payment_attempt_id"]

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    assert _count(capi_admin_engine, tenant, "payment_attempts", checkout_id=checkout_id) == 1
    assert _count(capi_admin_engine, tenant, "execution_grants", checkout_id=checkout_id) == 1
    assert _count(capi_admin_engine, tenant, "outbox_events") == 1


def test_the_same_key_replays_instead_of_admitting_twice(
    auth_client: TestClient, demo_session: MintedSession, capi_admin_engine: Engine
) -> None:
    """A retry returns the stored bytes with Idempotent-Replayed, and admits nothing."""
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    _approve(auth_client, card)

    url = f"/v1/checkouts/{card['checkout_id']}/versions/1/submit"
    key = _key()
    first = auth_client.post(url, headers={"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    assert "Idempotent-Replayed" not in first.headers

    second = auth_client.post(url, headers={"Idempotency-Key": key})
    assert second.status_code == 200, second.text
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()

    tenant = demo_session.tenant_id
    checkout_id = uuid.UUID(card["checkout_id"])
    assert _count(capi_admin_engine, tenant, "payment_attempts", checkout_id=checkout_id) == 1
    assert _count(capi_admin_engine, tenant, "outbox_events") == 1


def test_a_mutation_without_an_idempotency_key_is_refused(auth_client: TestClient) -> None:
    """Specification 24.1. The key is never generated for the client: a retry needs it."""
    response = auth_client.post("/v1/carts")
    assert response.status_code == 400
    assert response.json()["header"] == "Idempotency-Key"


# ------------------------------------------------------------------ authority boundary


def test_an_agent_may_not_approve(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Registry A has no ``checkout.approve``: consent is not delegable to the proposer."""
    agent_client, _ = mint_client(actor_type="AGENT")
    with agent_client as agent:
        cart_id, total = _basket_ready(agent)
        card = _open_checkout(agent, cart_id)
        response = agent.post(
            f"/v1/checkouts/{card['checkout_id']}/versions/1/approve",
            json={
                "content_hash": card["content_hash"],
                "amount_minor": total,
                "currency": "INR",
            },
            headers=_headers(),
        )
    assert response.status_code == 403, response.text
    assert response.json()["capability"] == "checkout.approve"


def test_a_checkout_belongs_to_one_buyer(
    auth_client: TestClient,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
    seeded_tenant: SeededTenant,
) -> None:
    """Another buyer in the same tenant cannot read or submit this checkout."""
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    assert seeded_tenant.tenant_id  # the two sessions share a tenant on purpose

    other, _ = mint_client(buyer_ref="not-the-owner")
    with other as stranger:
        assert stranger.get(f"/v1/checkouts/{card['checkout_id']}").status_code == 404
        refused = stranger.post(
            f"/v1/checkouts/{card['checkout_id']}/versions/1/submit",
            headers={"Idempotency-Key": _key()},
        )
    assert refused.status_code == 404


def test_the_race_recovery_reads_the_winner_after_a_rollback(
    auth_client: TestClient, demo_session: MintedSession
) -> None:
    """``duplicate_after_race`` works on a transaction the kernel has already rolled back.

    That path only runs when two submits reach the single-winner INSERT in the same
    instant, which a barrier cannot reliably produce -- under contention the loser is
    normally refused earlier, by the reservation the winner consumed. So the recovery is
    driven directly here, in the state it would actually find itself in: a rolled-back
    transaction with no tenant bound, because row-level security is transaction-scoped and
    the binding died with the rollback. If the re-bind were missing this would silently
    read nothing and answer 409 for a checkout that has a perfectly good live attempt.
    """
    cart_id, _ = _basket_ready(auth_client)
    card = _open_checkout(auth_client, cart_id)
    _approve(auth_client, card)
    decision = _submit(auth_client, card["checkout_id"], 1)
    assert decision["allowed"] is True

    ctx = RequestContext(
        tenant_id=demo_session.tenant_id,
        merchant_id=demo_session.merchant_id,
        buyer_ref=demo_session.buyer_ref,
        principal=AgentPrincipal(
            principal_id=f"session:{demo_session.session_id}",
            tenant_id=demo_session.tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=demo_session.merchant_id,
            buyer_ref=demo_session.buyer_ref,
        ),
        correlation_id=uuid7(),
        session_id=demo_session.session_id,
        expires_at=datetime.now(UTC),
    )
    with session_scope_for(KERNEL_URL) as session:
        set_tenant(session, ctx.tenant_id)
        session.rollback()  # exactly what admit() leaves behind when the index refuses
        body = admission_service.duplicate_after_race(session, ctx, uuid.UUID(card["checkout_id"]))

    assert body["allowed"] is False
    assert body["code"] == RecoveryCode.DUPLICATE_OPERATION.value
    assert body["attempt_id"] == decision["payment_attempt_id"]
    assert body["payment_attempt_id"] == decision["payment_attempt_id"]
    assert body["decision_id"] is None

"""The two buyer-scoped reads the Support Specialist needs, against real PostgreSQL.

``GET /v1/orders/{id}/policy`` and ``GET /v1/orders/{id}/resolution`` are what
``docs/KNOWN_GAPS.md`` names as the prerequisites for ``policy_search`` and
``resolution_evaluate``: the Policy-at-Sale Receipt's terms, and a resolution keyed by an
order the caller owns rather than by a payment attempt behind the operator key.

Every scenario is built by driving the real paths -- basket, checkout, approval and
admission over HTTP, then the steps the Action Executor performs through the kernel's own
modules. No test inserts an ``orders`` or ``refunds`` row: both exist only where the kernel
put them, which is the whole reason a projection over them is worth reading.

What is proven, in order:

* the terms come from the receipt this order is *bound* to -- the hash on the response
  matches the hash on the order, so a receipt swapped underneath would fail here;
* the receipt records every :class:`PolicyKind`, which is what makes an absent rule
  impossible to mistake for a permissive one;
* a clean order has no finding, so it has no remedy, and the response says which of those
  two it is rather than returning an empty list that could mean either;
* a refund whose provider answer was lost is a finding, and the order-keyed route prices
  it **identically to the operator route** -- same code, same plan id, same figures. Two
  surfaces disagreeing about one buyer's money is the failure this equivalence exists to
  catch;
* the amount is the Resolution Service's, and the route performs no arithmetic on it;
* a session without ``policy.search`` / ``resolution.evaluate`` is refused, and another
  buyer's order is a 404 on both -- the same answer a missing id gives, so neither route
  is an existence oracle.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_domain import Money, sha256_hex
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db import set_tenant
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from transaction_kernel import receipts
from transaction_kernel.payments import ProviderOrderOutcome

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"

#: Both routes this module owns. The guard tests walk it, so a support read added later
#: without a capability check fails a test rather than shipping open.
SUPPORT_READS: tuple[str, ...] = ("policy", "resolution")


# --------------------------------------------------------------------------- helpers


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


@dataclass(frozen=True, slots=True)
class Admitted:
    """An approved checkout with one admitted payment attempt and its unspent grant."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    amount: Money
    correlation_id: uuid.UUID

    @property
    def ref(self) -> tk.CheckoutRef:
        return tk.CheckoutRef(self.checkout_id, self.version, self.content_hash)


@pytest.fixture
def kernel(
    capi_kernel_engine: Engine,
    seeded_tenant: SeededTenant,  # noqa: ARG001 - ordering: torn down after this session
) -> Iterator[Session]:
    """A kernel-role session for the steps the Action Executor owns.

    Each helper opens its own transaction on it: the kernel's guards require one, and a
    fixture holding one open across a request would deadlock against the locks the API
    takes.
    """
    session = Session(capi_kernel_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def support_client(
    client: TestClient,
    seeded_tenant: SeededTenant,
    demo_session: MintedSession,
    scenario_headers: dict[str, str],
) -> TestClient:
    """A session holding the support capabilities, on the buyer's own identity.

    Minted as an ``OPERATOR`` with the *buyer's* ``buyer_ref`` because that is the only
    session today whose capability set contains ``policy.search`` and
    ``resolution.evaluate`` (``commerce_api.deps.OPERATOR_CAPABILITIES``). Sharing the
    reference is what makes the ownership test pass rather than being waived: these routes
    are owner-scoped, and a fixture that bypassed that would be testing a different route
    from the one that ships.
    """
    minted = client.post(
        "/v1/demo/sessions",
        json={
            "tenant_slug": seeded_tenant.tenant_slug,
            "actor_type": "OPERATOR",
            "buyer_ref": demo_session.buyer_ref,
        },
        headers=scenario_headers,
    )
    assert minted.status_code == 201, minted.text
    token = minted.json()["token"]
    return TestClient(client.app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def operator_reader(
    support_client: TestClient, scenario_headers: dict[str, str]
) -> Callable[..., Any]:
    """Read the operator review route: the session plus the scenario key."""

    def _call(path: str) -> Any:
        return support_client.get(path, headers=scenario_headers)

    return _call


def _admit(auth_client: TestClient) -> Admitted:
    """Basket, checkout, approval and admission, all over HTTP. Nothing arranged in SQL."""
    basket = auth_client.post("/v1/baskets", headers=_headers())
    assert basket.status_code == 201, basket.text
    basket_id = basket.json()["basket_id"]

    line = auth_client.put(
        f"/v1/baskets/{basket_id}/lines/{MILK}", json={"quantity": 2}, headers=_headers()
    )
    assert line.status_code == 200, line.text

    card = auth_client.post(f"/v1/baskets/{basket_id}/checkout", headers=_headers())
    assert card.status_code == 201, card.text
    body = card.json()

    approved = auth_client.post(
        f"/v1/checkouts/{body['checkout_id']}/versions/{body['version']}/approve",
        json={
            "content_hash": body["content_hash"],
            "amount_minor": body["amount_minor"],
            "currency": body["currency"],
        },
        headers=_headers(),
    )
    assert approved.status_code == 200, approved.text

    submitted = auth_client.post(
        f"/v1/checkouts/{body['checkout_id']}/versions/{body['version']}/submit",
        headers=_headers(),
    )
    assert submitted.status_code == 200, submitted.text
    decision = submitted.json()
    assert decision["allowed"], decision

    return Admitted(
        checkout_id=uuid.UUID(body["checkout_id"]),
        version=int(body["version"]),
        content_hash=str(body["content_hash"]),
        attempt_id=uuid.UUID(decision["payment_attempt_id"]),
        grant_id=uuid.UUID(decision["grant_id"]),
        amount=Money(int(body["amount_minor"]), str(body["currency"])),
        correlation_id=uuid.UUID(decision["correlation_id"]),
    )


@pytest.fixture
def admitted(auth_client: TestClient) -> Admitted:
    return _admit(auth_client)


# ------------------------------------------------------- the Action Executor's own steps


def _create_order(kernel: Session, tenant_id: uuid.UUID, adm: Admitted) -> str:
    """Consume the single-use grant, then record a create-order call that came back."""
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.consume_grant(
            kernel,
            adm.grant_id,
            tk.GrantBinding(
                tenant_id=tenant_id,
                checkout=adm.ref,
                payment_attempt_id=adm.attempt_id,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                amount=adm.amount,
            ),
        )
    provider_order_id = f"order_{uuid.uuid4().hex[:14]}"
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.record_create_order_result(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            outcome=ProviderOrderOutcome(
                kind="ok",
                provider_order_id=provider_order_id,
                code=tk.RecoveryCode.OK,
                reason="created",
            ),
            correlation_id=adm.correlation_id,
        )
    return provider_order_id


def _apply_capture(
    kernel: Session, tenant_id: uuid.UUID, adm: Admitted, provider_order_id: str
) -> str:
    """Apply a server-side fetch reporting a capture. The kernel writes the order."""
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.apply_provider_evidence(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            evidence=tk.ProviderEvidence.from_mapping(
                {
                    "source": "PROVIDER_FETCH",
                    "provider_payment_id": payment_id,
                    "provider_order_id": provider_order_id,
                    "amount_minor": adm.amount.minor,
                    "currency": adm.amount.currency,
                    "status": "captured",
                    "provider_status": "captured",
                    "amount_refunded_minor": 0,
                    "raw_digest": sha256_hex(payment_id.encode()),
                }
            ),
            correlation_id=adm.correlation_id,
        )
    return payment_id


def _lose_refund_result(
    kernel: Session, tenant_id: uuid.UUID, refund_id: str, correlation_id: uuid.UUID
) -> None:
    """The refund was sent and the answer never came back. ``REFUND_UNKNOWN``."""
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.refunds.record_refund_result(
            kernel,
            tenant_id=tenant_id,
            refund_id=uuid.UUID(refund_id),
            outcome="unknown",
            provider_refund_id=None,
            correlation_id=correlation_id,
        )


def _captured_order(
    auth_client: TestClient, kernel: Session, tenant_id: uuid.UUID, adm: Admitted
) -> str:
    """Drive one admitted attempt all the way to a confirmed order, and return its id."""
    provider_order_id = _create_order(kernel, tenant_id, adm)
    _apply_capture(kernel, tenant_id, adm, provider_order_id)
    listed = auth_client.get("/v1/orders")
    assert listed.status_code == 200, listed.text
    for order in listed.json()["orders"]:
        if order["checkout_id"] == str(adm.checkout_id):
            return str(order["order_id"])
    raise AssertionError(f"no order for checkout {adm.checkout_id}: {listed.text}")


def _at_sale_refund_policy(engine: Engine, tenant_id: uuid.UUID, adm: Admitted) -> dict[str, Any]:
    """The refund policy frozen on this sale, read from the receipt rather than assumed."""
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            set_tenant(session, tenant_id)
            policy = receipts.policy_for_order(session, adm.ref)
            assert policy.ok, policy.reason
            return dict(policy.policy_for(receipts.PolicyKind.REFUND))
    finally:
        session.close()


# ------------------------------------------------------------------------ the terms


def test_the_policy_route_returns_the_receipt_the_order_is_bound_to(
    auth_client: TestClient,
    support_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
    capi_app_engine: Engine,
) -> None:
    """The terms come from this order's own receipt, and the hash proves which one.

    ``policy_receipt_hash`` on the response is compared against the hash the *order* route
    already publishes. They are read by two different paths -- ``policy_for_order`` here,
    the ``orders`` row there -- so a receipt swapped underneath the sale would make them
    disagree instead of quietly governing a refund.
    """
    order_id = _captured_order(auth_client, kernel, seeded_tenant.tenant_id, admitted)

    response = support_client.get(f"/v1/orders/{order_id}/policy")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["binding_ok"] is True
    assert body["binding_code"] == "OK"
    assert body["order_id"] == order_id
    assert body["checkout_id"] == str(admitted.checkout_id)
    assert body["checkout_version"] == admitted.version

    order = auth_client.get(f"/v1/orders/{order_id}").json()
    assert body["policy_receipt_hash"] == order["policy_receipt_hash"]

    expected = _at_sale_refund_policy(capi_app_engine, seeded_tenant.tenant_id, admitted)
    refund = next(item for item in body["policies"] if item["kind"] == "REFUND")
    assert refund["policy_id"] == expected["policy_id"]
    assert refund["policy_version"] == expected["policy_version"]
    assert refund["terms"] == expected["terms"]


def test_the_receipt_records_every_policy_kind_so_no_rule_is_merely_absent(
    auth_client: TestClient,
    support_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Every :class:`PolicyKind` is on the response, including the ones that say "no".

    This is the property that makes the route safe to answer a buyer from. A missing kind
    would be filled in at resolution time from the merchant's *current* policy, which is
    exactly the retroactive change the receipt exists to prevent -- so a merchant with no
    substitution programme records that it has none, and the agent can say so.
    """
    order_id = _captured_order(auth_client, kernel, seeded_tenant.tenant_id, admitted)
    body = support_client.get(f"/v1/orders/{order_id}/policy").json()

    kinds = {item["kind"] for item in body["policies"]}
    assert kinds == {kind.value for kind in receipts.PolicyKind}
    for policy in body["policies"]:
        assert policy["terms"], f"{policy['kind']} recorded no terms"
        assert policy["policy_version"] >= 1


# -------------------------------------------------------------------- the remedies


def test_a_clean_order_has_no_finding_and_therefore_no_remedy(
    auth_client: TestClient,
    support_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Nothing diverged, so there is nothing to price -- and the count says which.

    ``findings: 0`` beside an empty list is the difference between "the Reconciliation
    Service looked and found nothing" and "no evaluation happened". A Support Specialist
    that could not tell those apart would either invent a remedy or refuse a real one.
    """
    order_id = _captured_order(auth_client, kernel, seeded_tenant.tenant_id, admitted)

    response = support_client.get(f"/v1/orders/{order_id}/resolution")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["order_id"] == order_id
    assert body["payment_attempt_id"] == str(admitted.attempt_id)
    assert body["recorded_state"] == "CAPTURED"
    assert body["findings"] == 0
    assert body["resolutions"] == []
    assert body["plan_ttl_seconds"] > 0


def test_a_lost_refund_answer_is_priced_identically_on_both_surfaces(
    auth_client: TestClient,
    support_client: TestClient,
    operator_reader: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """The buyer-scoped route and the operator route answer the same thing about one order.

    Same finding id, same code, same plan id, same figures. This is the assertion worth
    keeping: the two surfaces reach the Resolution Service by different routes and with
    different scoping, and a buyer being told one number while a reviewer sees another is
    the failure that would follow from letting either one compute its own.
    """
    order_id = _captured_order(auth_client, kernel, seeded_tenant.tenant_id, admitted)
    refund_id = _request_refund(auth_client, order_id)
    _lose_refund_result(kernel, seeded_tenant.tenant_id, refund_id, admitted.correlation_id)

    body = support_client.get(f"/v1/orders/{order_id}/resolution").json()
    assert body["findings"] == len(body["resolutions"]), body
    # Selected by the refund it is about rather than by position: a lost refund answer
    # moves the *attempt* as well as the refund, so more than one finding is the correct
    # reading of this state and an index would silently start describing the other one.
    mine = next(item for item in body["resolutions"] if item["refund_id"] == refund_id)
    assert mine["code"] == "REFUND_REVIEW_REQUIRED"
    # No plan is issued while a refund may already exist at the provider, and the shape
    # carries that rather than an empty options list somebody could read as "nothing owed".
    assert mine["plan_issued"] is False
    assert mine["plan_id"] is None
    assert mine["options"] == []
    assert {item["reason"] for item in mine["withheld"]} == {"PROVIDER_STATE_UNVERIFIED"}
    # The refund is money that may already have gone back, so it is reserved against the
    # capture. Both integers come from the kernel's ledger; the route adds nothing.
    assert mine["captured_minor"] == admitted.amount.minor
    assert mine["refunds_reserved_minor"] == admitted.amount.minor
    assert mine["refundable_minor"] == 0

    # Every resolution, not only the refund's: the equivalence is worth nothing if it holds
    # for the one finding the test happened to name.
    #
    # ``evaluated_at`` and the ``valid_until`` derived from it are the two members that
    # must differ, and they are dropped rather than tolerated. Each response is stamped by
    # its own read transaction's database clock, so two calls made a millisecond apart are
    # correctly two evaluations. What must not differ is ``plan_id``, which is a hash over
    # the figures rather than over the moment -- so it stays in the comparison, and a
    # scheme that ever folded a timestamp into it would fail here.
    clockless = {"evaluated_at", "valid_until"}
    operator = operator_reader(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    theirs = {item["finding_id"]: item for item in operator["resolutions"]}
    assert {item["finding_id"] for item in body["resolutions"]} == set(theirs)
    for item in body["resolutions"]:
        other = theirs[item["finding_id"]]
        assert {k: v for k, v in item.items() if k not in clockless} == {
            k: v for k, v in other.items() if k not in clockless
        }
        assert (item["valid_until"] is None) == (other["valid_until"] is None)


def test_the_route_never_prices_a_finding_that_confirmed_no_order(
    support_client: TestClient,
    auth_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """A capture on an invalidated version has no order, so this route cannot reach it.

    Specification 10.8 confirms no order for a capture that can never be fulfilled, and
    that is why keying on an order is the right narrowing rather than an accidental one:
    ``STALE_CAPTURE`` and ``PAYMENT_UNKNOWN`` have no buyer-facing subject at all, and
    they stay on the operator surface where the reviewer who can act on them is.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    with kernel.begin():
        set_tenant(kernel, seeded_tenant.tenant_id)
        tk.invalidate_open(
            kernel,
            tenant_id=seeded_tenant.tenant_id,
            checkout=admitted.ref,
            reason="price_changed_under_open_payment",
            correlation_id=admitted.correlation_id,
        )
    _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)

    listed = auth_client.get("/v1/orders").json()["orders"]
    assert not [item for item in listed if item["checkout_id"] == str(admitted.checkout_id)]

    # There is no order id to ask with, so the finding is unreachable here by construction
    # rather than filtered out. A made-up id is the same 404 any unknown id gets.
    missing = support_client.get(f"/v1/orders/{uuid.uuid4()}/resolution")
    assert missing.status_code == 404, missing.text


def _request_refund(auth_client: TestClient, order_id: str) -> str:
    response = auth_client.post(
        f"/v1/orders/{order_id}/refunds", json={"reason": "items_missing"}, headers=_headers()
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"]["allowed"], payload
    return str(payload["refund"]["refund_id"])


# ------------------------------------------------------------------------ the guards


def test_a_buyer_session_now_reads_its_own_orders_policy_and_resolution(
    auth_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """200 on both, on the buyer's *own* order -- the post-purchase reads are buyer-held.

    ``policy.search`` and ``resolution.evaluate`` are now in ``BUYER_CAPABILITIES``: they
    are reads over the buyer's own order that decide nothing and move no money, the
    post-purchase counterparts of ``order.read``. This is the plain buyer session
    (``auth_client``), not the OPERATOR-minted ``support_client``, so it proves the buyer
    surface itself reaches the Support Specialist's two reads rather than an operator
    standing in for it. The order being the caller's own is what makes a 200 the correct
    answer here rather than an ownership 404.
    """
    order_id = _captured_order(auth_client, kernel, seeded_tenant.tenant_id, admitted)
    for suffix in SUPPORT_READS:
        response = auth_client.get(f"/v1/orders/{order_id}/{suffix}")
        assert response.status_code == 200, f"{suffix} -> {response.status_code} {response.text}"
        assert response.json()["order_id"] == order_id


def test_support_escalate_is_not_held_by_any_buyer_or_agent_session() -> None:
    """``support.escalate`` never enters a buyer or agent session.

    The kernel ``escalate`` primitive has no who/why gate and ``ESCALATED`` is terminal
    with no automated way out, so it must not be added to the sets a buyer or a delegated
    agent is minted from -- not even for symmetry with the two post-purchase reads that
    were just granted. It is named only in ``SUPPORT_AGENT_CAPABILITIES`` (specification
    6.4.4), which reaches a session solely through ``OPERATOR_CAPABILITIES``; and an
    OPERATOR session can only be minted by a caller already holding the scenario key.
    """
    from commerce_api.deps import (
        AGENT_CAPABILITIES,
        BUYER_CAPABILITIES,
    )

    assert "support.escalate" not in BUYER_CAPABILITIES
    assert "support.escalate" not in AGENT_CAPABILITIES


def test_another_buyers_order_is_the_same_404_a_missing_one_gives(
    support_client: TestClient,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
    kernel: Session,
    seeded_tenant: SeededTenant,
) -> None:
    """No existence oracle: somebody else's order and a made-up id answer identically.

    Both bodies are compared field by field apart from the two members that necessarily
    echo the request -- the order id and the RFC 9457 ``instance``. A 403 on the first, or
    a differing title or detail, would confirm to a caller probing identifiers that one of
    them names a real sale.
    """
    other_client, _other = mint_client(buyer_ref=f"buyer-{uuid.uuid4().hex[:12]}")
    theirs = _admit(other_client)
    order_id = _captured_order(other_client, kernel, seeded_tenant.tenant_id, theirs)
    invented = str(uuid.uuid4())

    for suffix in SUPPORT_READS:
        foreign = support_client.get(f"/v1/orders/{order_id}/{suffix}")
        missing = support_client.get(f"/v1/orders/{invented}/{suffix}")
        assert foreign.status_code == 404, foreign.text
        assert missing.status_code == 404, missing.text
        left = foreign.json()
        right = missing.json()
        assert left.pop("order_id") == order_id
        assert right.pop("order_id") == invented
        assert left.pop("instance").endswith(f"/{order_id}/{suffix}")
        assert right.pop("instance").endswith(f"/{invented}/{suffix}")
        assert left == right


def test_both_support_reads_are_gets(api_app: FastAPI) -> None:
    """Neither read may acquire a write method later without failing here.

    ``policy_search`` and ``resolution_evaluate`` are reads in Registry A, and the
    Resolution Service issues nothing and records nothing. A POST appearing on either path
    would be a remedy being applied from a surface that has no Execution Grant.
    """
    for route in api_app.routes:
        path = getattr(route, "path", "")
        if not any(path.endswith(f"/{suffix}") for suffix in SUPPORT_READS):
            continue
        if not path.startswith("/v1/orders"):
            continue
        assert getattr(route, "methods", set()) == {"GET"}, path

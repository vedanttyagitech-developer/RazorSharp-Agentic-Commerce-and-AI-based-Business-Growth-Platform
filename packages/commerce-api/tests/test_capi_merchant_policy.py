"""A merchant narrows what they promise, and yesterday's order keeps what it was sold.

This is the test the Policy-at-Sale Receipt was built for and could not have until now.

The receipt has always frozen a merchant's terms onto each order so a later change cannot
narrow them retroactively. But the terms were constants in the simulator, so the guarantee
was real machinery aimed at an event nobody could cause: a reviewer asking to see it
survive a policy change got told the merchant cannot make one. There is now a writer, and
this is the attack it makes possible.

The shape of it: sell an order under the shop's opening terms, publish a version that cuts
the refund window from seven days to two, sell a second order, and read both orders' policy
routes. The first still says seven. Not because anything special was done for it -- because
its receipt records what it was sold under, and a new version is a row beside the old one
rather than an edit to it.

The two supporting facts are here as well, because the guarantee rests on them. Nobody may
UPDATE a published version, which is what makes "beside" rather than "instead of" the only
option. And the opening position gets written down the first time somebody departs from it,
so a receipt naming version one has a row an auditor can read rather than a number that
refers to a constant in some package.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
import transaction_kernel as tk
from commerce_domain import CheckoutRef, Money, RecoveryCode, sha256_hex
from fastapi.testclient import TestClient
from platform_db import set_tenant
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from transaction_kernel.payments import ProviderOrderOutcome

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


@dataclass(frozen=True, slots=True)
class Admitted:
    checkout_id: uuid.UUID
    version: int
    content_hash: str
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    amount: Money
    correlation_id: uuid.UUID

    @property
    def ref(self) -> CheckoutRef:
        return CheckoutRef(self.checkout_id, self.version, self.content_hash)


@pytest.fixture
def kernel(
    capi_kernel_engine: Engine,
    seeded_tenant: SeededTenant,  # noqa: ARG001 - ordering: torn down after this session
) -> Iterator[Session]:
    session = Session(capi_kernel_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def merchant(
    client: TestClient,
    seeded_tenant: SeededTenant,
    demo_session: MintedSession,  # noqa: ARG001 - the buyer exists before the merchant
    scenario_headers: dict[str, str],
) -> TestClient:
    minted = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "MERCHANT"},
        headers=scenario_headers,
    )
    assert minted.status_code == 201, minted.text
    return TestClient(client.app, headers={"Authorization": f"Bearer {minted.json()['token']}"})


def _sell(auth_client: TestClient, kernel: Session, tenant_id: uuid.UUID) -> str:
    """One order, sold through the real path. Returns its id."""
    cart = auth_client.post("/v1/carts", headers=_headers())
    cart_id = cart.json()["cart_id"]
    auth_client.put(f"/v1/carts/{cart_id}/lines/{MILK}", json={"quantity": 1}, headers=_headers())
    card = auth_client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers()).json()
    auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers=_headers(),
    )
    decision = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers=_headers(),
    ).json()
    assert decision["allowed"], decision
    adm = Admitted(
        checkout_id=uuid.UUID(card["checkout_id"]),
        version=int(card["version"]),
        content_hash=str(card["content_hash"]),
        attempt_id=uuid.UUID(decision["payment_attempt_id"]),
        grant_id=uuid.UUID(decision["grant_id"]),
        amount=Money(int(card["amount_minor"]), str(card["currency"])),
        correlation_id=uuid.UUID(decision["correlation_id"]),
    )
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
    provider_order = f"order_{uuid.uuid4().hex[:14]}"
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.record_create_order_result(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            outcome=ProviderOrderOutcome(
                kind="ok",
                provider_order_id=provider_order,
                code=RecoveryCode.OK,
                reason="created",
            ),
            correlation_id=adm.correlation_id,
        )
    payment = f"pay_{uuid.uuid4().hex[:14]}"
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.apply_provider_evidence(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            evidence=tk.ProviderEvidence.from_mapping(
                {
                    "source": "PROVIDER_FETCH",
                    "provider_payment_id": payment,
                    "provider_order_id": provider_order,
                    "amount_minor": adm.amount.minor,
                    "currency": adm.amount.currency,
                    "status": "captured",
                    "provider_status": "captured",
                    "amount_refunded_minor": 0,
                    "raw_digest": sha256_hex(payment.encode()),
                }
            ),
            correlation_id=adm.correlation_id,
        )
    for order in auth_client.get("/v1/orders").json()["orders"]:
        if order["checkout_id"] == str(adm.checkout_id):
            return str(order["order_id"])
    raise AssertionError("the kernel wrote no order for this checkout")


def _narrow_the_refund_window(
    merchant: TestClient, headers: dict[str, str], *, days: int
) -> dict[str, Any]:
    """Propose, approve and publish a tighter refund window, through the real path."""
    drafted = merchant.post(
        "/v1/merchant/actions",
        json={
            "kind": "POLICY_PUBLISH",
            "target": "REFUND",
            "proposal": {
                "allowed": True,
                "window_days": days,
                "method": "ORIGINAL_INSTRUMENT",
                "partial_allowed": True,
            },
        },
        headers=headers,
    )
    assert drafted.status_code == 201, drafted.text
    action_id = drafted.json()["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=headers)
    approved = merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted.json()["content_hash"]},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    executed = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=headers)
    assert executed.status_code == 200, executed.text
    assert executed.json()["ok"], executed.text
    return dict(executed.json())


def _window(auth_client: TestClient, order_id: str) -> int:
    terms = auth_client.get(f"/v1/orders/{order_id}/policy")
    assert terms.status_code == 200, terms.text
    refund = next(p for p in terms.json()["policies"] if p["kind"] == "REFUND")
    return int(refund["terms"]["window_days"])


def _versions(engine: Engine, tenant: SeededTenant) -> list[Any]:
    """Every published version for this tenant's merchant, oldest first.

    Scoped in the predicate rather than by binding a tenant. This engine is the local
    superuser and bypasses row-level security, so an unscoped read here sees whatever a
    previous test left behind: absence is not data, and neither is a count.
    """
    with engine.begin() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT version, published_by, terms, "
                    "terms -> 'REFUND' ->> 'window_days' AS days "
                    "FROM merchant_policy_versions "
                    "WHERE tenant_id = :t AND merchant_id = :m ORDER BY version"
                ),
                {"t": tenant.tenant_id, "m": tenant.merchant_id},
            ).all()
        )


# ------------------------------------------------------------------------ the guarantee


def test_an_order_keeps_the_terms_it_was_sold_under(
    auth_client: TestClient,
    merchant: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    scenario_headers: dict[str, str],
) -> None:
    """The whole reason the Policy-at-Sale Receipt exists, finally testable.

    Nothing special is done for the older order. Its receipt records what it was sold under,
    and the merchant's change is a new version beside the old one rather than an edit to it.
    """
    before = _sell(auth_client, kernel, seeded_tenant.tenant_id)
    assert _window(auth_client, before) == 7

    _narrow_the_refund_window(merchant, scenario_headers, days=2)

    after = _sell(auth_client, kernel, seeded_tenant.tenant_id)
    assert _window(auth_client, after) == 2, "the new sale should be under the new terms"
    assert _window(auth_client, before) == 7, (
        "the order sold before the change was narrowed retroactively, which is the one "
        "thing the receipt exists to prevent"
    )


def test_the_opening_position_is_written_down_when_it_is_first_departed_from(
    auth_client: TestClient,
    merchant: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    scenario_headers: dict[str, str],
    capi_admin_engine: Engine,
) -> None:
    """A receipt naming version one should point at a row somebody can read.

    Before the first change there is nothing to distinguish, so there is no row. The first
    publication writes two: the terms the shop opened with, and the change. Otherwise every
    order sold earlier would name a version that referred to a constant in a package.
    """
    sold_early = _sell(auth_client, kernel, seeded_tenant.tenant_id)
    # Filtered by tenant in the statement, not by `set_config`. This engine is the local
    # superuser, which bypasses row-level security, so a read without a predicate counts
    # every tenant's rows -- including a previous test's, which is how this first check came
    # back twelve instead of zero.
    assert _versions(capi_admin_engine, seeded_tenant) == []

    _narrow_the_refund_window(merchant, scenario_headers, days=3)

    rows = _versions(capi_admin_engine, seeded_tenant)
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].published_by == "platform:opening_position"
    assert rows[0].days == "7"
    assert rows[1].days == "3"
    # And the order sold before the change names the version that is now a real row.
    assert _window(auth_client, sold_early) == 7


def test_nobody_may_edit_a_published_version(
    merchant: TestClient,
    scenario_headers: dict[str, str],
    capi_app_engine: Engine,
    capi_kernel_engine: Engine,
    seeded_tenant: SeededTenant,
) -> None:
    """What makes "beside" rather than "instead of" the only option.

    The guarantee is not that the code never issues an UPDATE. It is that no role may. A
    version somebody could edit would make every receipt naming it a receipt that says
    whatever the row says today.
    """
    _narrow_the_refund_window(merchant, scenario_headers, days=4)
    for engine, role in ((capi_app_engine, "app"), (capi_kernel_engine, "kernel")):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with engine.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(seeded_tenant.tenant_id)},
                )
                conn.execute(text("UPDATE merchant_policy_versions SET version = version"))
        assert role


# -------------------------------------------------------------------------- what it refuses


def test_a_priced_family_may_not_be_published(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """Delivery and discount already change, by their own path.

    They come from the fee policy and the running offer, and both already reach the receipt.
    Letting them be published here would give one field two writers, and the two would
    disagree the first time an offer started while a version was in flight.
    """
    drafted = merchant.post(
        "/v1/merchant/actions",
        json={
            "kind": "POLICY_PUBLISH",
            "target": "DISCOUNT",
            "proposal": {"allowed": True, "percent_bp": 5000},
        },
        headers=scenario_headers,
    )
    assert drafted.status_code == 201, drafted.text
    action_id = drafted.json()["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted.json()["content_hash"]},
        headers=scenario_headers,
    )
    refused = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert refused.status_code == 422, refused.text
    assert "DISCOUNT" in refused.text


def test_a_fee_that_is_not_a_number_is_refused_at_publish_time(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """A merchant must not be able to take the storefront down by editing their own policy.

    ``merchant_adapter`` reads the cancellation fee back as a number when it builds a
    Policy-at-Sale Receipt -- ``Money(int(cancellation.pop("fee_minor", 0)), currency)``.
    Publishing was validated for family and for emptiness and for nothing else, so
    ``{"fee_minor": "waived"}`` published cleanly and every later ``receipt_inputs_for``
    raised. That call is on the *buyer's* checkout path: one merchant form submission and
    every checkout on that merchant 500s, permanently, with the failure appearing on a
    buyer's checkout button.

    The refusal has to happen here, where the person who typed it is looking.
    """
    drafted = merchant.post(
        "/v1/merchant/actions",
        json={
            "kind": "POLICY_PUBLISH",
            "target": "CANCELLATION",
            "proposal": {"allowed": True, "cutoff": "BEFORE_DISPATCH", "fee_minor": "waived"},
        },
        headers=scenario_headers,
    )
    assert drafted.status_code == 201, drafted.text
    action_id = drafted.json()["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted.json()["content_hash"]},
        headers=scenario_headers,
    )
    refused = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["field"] == "fee_minor", body
    assert "whole number" in body["detail"]


def test_a_boolean_is_not_a_cancellation_fee(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """``True`` is an ``int`` in Python and would publish a one-paisa cancellation fee."""
    drafted = merchant.post(
        "/v1/merchant/actions",
        json={
            "kind": "POLICY_PUBLISH",
            "target": "CANCELLATION",
            "proposal": {"allowed": True, "cutoff": "BEFORE_DISPATCH", "fee_minor": True},
        },
        headers=scenario_headers,
    )
    action_id = drafted.json()["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted.json()["content_hash"]},
        headers=scenario_headers,
    )
    refused = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert refused.status_code == 422, refused.text


def test_publishing_changes_one_family_and_carries_the_rest(
    merchant: TestClient,
    scenario_headers: dict[str, str],
    capi_admin_engine: Engine,
    seeded_tenant: SeededTenant,
) -> None:
    """A version is the whole set, so reading an old order's terms is one row.

    A version stored as a diff would make the oldest receipt the most expensive to verify,
    which is the wrong way round: the old ones are the ones somebody argues about.
    """
    _narrow_the_refund_window(merchant, scenario_headers, days=5)
    terms = _versions(capi_admin_engine, seeded_tenant)[-1].terms
    assert set(terms) == {"CANCELLATION", "REFUND", "RETURN", "SUBSTITUTION", "FULFILMENT"}
    assert terms["REFUND"]["window_days"] == 5
    # Untouched families are present and unchanged, not absent.
    assert terms["SUBSTITUTION"] == {"allowed": False}
    assert terms["CANCELLATION"]["cutoff"] == "BEFORE_DISPATCH"


PUBLISHABLE: Final[tuple[str, ...]] = (
    "CANCELLATION",
    "REFUND",
    "RETURN",
    "SUBSTITUTION",
    "FULFILMENT",
)


def test_the_publishable_families_are_the_non_financial_ones() -> None:
    """Read from the source, so adding a family is a decision rather than a drift."""
    from merchant_adapter import DEFAULT_TERMS, PUBLISHABLE_KINDS

    assert frozenset(PUBLISHABLE) == PUBLISHABLE_KINDS
    assert set(DEFAULT_TERMS) == set(PUBLISHABLE)
    assert "DELIVERY" not in PUBLISHABLE_KINDS
    assert "DISCOUNT" not in PUBLISHABLE_KINDS


def test_a_merchant_can_read_the_terms_they_currently_promise(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """The read that did not exist while the write already did.

    A shop able to change terms it could not display is a shop whose merchant decides
    blind. Everything below is the opening position, so the version is one, nothing has
    been chosen, and the terms are attributed to the platform rather than to anybody in
    the shop -- because nobody in the shop agreed to them.
    """
    body = merchant.get("/v1/merchant/policy", headers=scenario_headers)
    assert body.status_code == 200, body.text
    policy = body.json()

    assert policy["version"] == 1
    assert policy["chosen"] is False
    assert policy["published_by"] == "platform:opening_position"
    assert policy["publishable"] == sorted(PUBLISHABLE)
    assert set(policy["terms"]) >= set(PUBLISHABLE)
    assert policy["terms"]["RETURN"]["allowed"] is True


def test_the_read_moves_when_a_family_is_published_and_says_who_moved_it(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """Publishing one family carries the rest forward, and the version names its author.

    ``published_by`` is the point of the second half. The opening position belongs to the
    platform; everything after it belongs to whoever pressed approve, and a policy screen
    that cannot say which of those it is showing cannot tell a merchant whether the shop's
    position is one anybody chose.
    """
    before = merchant.get("/v1/merchant/policy", headers=scenario_headers).json()
    assert before["terms"]["RETURN"]["allowed"] is True

    drafted = merchant.post(
        "/v1/merchant/actions",
        json={
            "kind": "POLICY_PUBLISH",
            "target": "RETURN",
            "proposal": {"allowed": False},
        },
        headers=scenario_headers,
    )
    assert drafted.status_code == 201, drafted.text
    action_id = drafted.json()["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted.json()["content_hash"]},
        headers=scenario_headers,
    )
    executed = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert executed.status_code == 200, executed.text

    after = merchant.get("/v1/merchant/policy", headers=scenario_headers).json()
    assert after["chosen"] is True
    assert after["version"] > before["version"]
    assert after["published_by"] != "platform:opening_position"
    assert after["terms"]["RETURN"] == {"allowed": False}
    assert after["terms"]["REFUND"] == before["terms"]["REFUND"], (
        "publishing one family must carry the others forward untouched"
    )


def test_concurrent_publication_serializes_without_merchant_update_privilege(
    capi_kernel_engine, seeded_tenant
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace

    from commerce_api.services.merchant_policy_service import publish_family

    barrier = Barrier(2)
    ctx = SimpleNamespace(
        tenant_id=seeded_tenant.tenant_id,
        merchant_id=seeded_tenant.merchant_id,
        principal=SimpleNamespace(principal_id="merchant-concurrency-test"),
    )

    def publish(days):
        with Session(capi_kernel_engine) as session, session.begin():
            set_tenant(session, seeded_tenant.tenant_id)
            assert (
                session.execute(
                    text("SELECT has_table_privilege(current_user, 'merchants', 'UPDATE')")
                ).scalar()
                is False
            )
            barrier.wait(timeout=5)
            result = publish_family(
                session,
                ctx,
                kind="REFUND",
                terms={
                    "allowed": True,
                    "window_days": days,
                    "method": "ORIGINAL_INSTRUMENT",
                    "partial_allowed": True,
                },
            )
            return result.version

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, [3, 5])) == [2, 3]

"""The order and refund collections over HTTP: scope, keyset pages, counts, wire state.

The assertions that matter most here are about *who sees what*. A collection endpoint
is where tenant isolation and buyer ownership are easiest to get subtly wrong: a single-
row read can lean on a 404, but a list has to get its predicate right on every page, and
a cursor that carried authority would let one scope leak into another. So:

* a buyer's list is their own orders and nothing else, and another buyer in the same
  tenant sees an empty page with all-zero counts rather than a filtered view of A's;
* the scenario key -- the P0 stand-in for the merchant operator surface (ADR 0003 D11)
  -- widens a read to the tenant, and only the key does: an ``OPERATOR`` session without
  the key on the request is still scoped to "own";
* a keyset walk at ``limit=1`` visits every order exactly once, newest first, and the
  cursor is ``null`` on the last page and nowhere else;
* counts are across the scope, not the page, so a ``status`` filter that returns nothing
  still reports the ``CONFIRMED`` rows the buyer has;
* a refund's ``state`` is the wire state a console reasons in (``REFUND_PENDING``,
  ``REFUND_UNKNOWN``, ``PARTIALLY_REFUNDED``...), and the ``state`` filter and counts are
  computed by the same rule.

Orders are confirmed the only way they ever are: through the kernel's admission and
:func:`transaction_kernel.apply_provider_evidence` with ``WEBHOOK`` evidence. Nothing in
this file writes an ``orders`` row directly. Refund rows are created through the real
route and then, for the state tests, moved by an ``UPDATE`` on the kernel role; that is
a test seam and is marked as one where it happens.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
import transaction_kernel as tk
from commerce_api.schemas import OrderState
from commerce_api.services import listing
from commerce_domain import Money, canonical_hash, uuid7
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import receipts, reservations
from transaction_kernel.admission import AdmissionRequest, CurrentMerchantState
from transaction_kernel.payments import ProviderOrderOutcome
from transaction_kernel.receipts import BuyerVisibleRef, MerchantPolicy, PolicyKind, ReceiptDraft

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")
PROBLEM: Final[str] = "application/problem+json"

TOTAL = Money(39500, "INR")

MintClient = Callable[..., tuple[TestClient, MintedSession]]


# ------------------------------------------------------------------ building orders


def _content(checkout_id: uuid.UUID, version: int, total: Money) -> dict[str, Any]:
    return {
        "checkout_id": str(checkout_id),
        "version": version,
        "currency": total.currency,
        "total_minor": total.minor,
        "line_items": {"sku_milk": 2, "sku_bread": 1},
        "policy_version": "pol-v12",
    }


class _StubMerchant:
    """A merchant whose current state agrees with what was approved, so every admission
    in this file succeeds and the thing under test is the listing, not the kernel."""

    def __init__(self, total: Money) -> None:
        self.total = total

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        content = _content(checkout_id, version, self.total)
        return CurrentMerchantState(
            total=self.total,
            line_items=content["line_items"],
            all_available=True,
            policy_version=content["policy_version"],
        )


@dataclass(frozen=True, slots=True)
class Confirmed:
    """One order, confirmed from ``WEBHOOK`` evidence, owned by ``buyer``."""

    buyer: MintedSession
    checkout_id: uuid.UUID
    attempt_id: uuid.UUID
    order_id: uuid.UUID
    payment_id: str
    amount: Money


def _bind(session: Session, tenant_id: uuid.UUID) -> None:
    session.execute(SET_TENANT, {"t": str(tenant_id)})


def confirm_order(
    engine: Engine, tenant: SeededTenant, buyer: MintedSession, *, total: Money = TOTAL
) -> Confirmed:
    """Build one confirmed order for ``buyer``: the ``admitted`` and ``captured`` fixtures
    of ``test_capi_payments`` folded into a function, so a test can make several.

    Every step goes through the kernel's own modules -- ``receipts``, ``reservations``,
    ``admit``, ``consume_grant``, ``record_create_order_result`` and finally
    ``apply_provider_evidence`` -- because a hand-written ``orders`` row would make the
    capture-evidence assertions below meaningless. Each stage commits in its own
    transaction, as the worker's would, so successive orders carry distinct
    ``created_at`` stamps and the keyset ordering is exercised on real gaps.
    """
    tenant_id, merchant_id = tenant.tenant_id, tenant.merchant_id
    basket_id, checkout_id = uuid7(), uuid7()
    version = 1
    content = _content(checkout_id, version, total)
    checkout = tk.CheckoutRef(checkout_id, version, canonical_hash(content))
    correlation_id = uuid7()

    session = Session(engine, expire_on_commit=False)
    with session.begin():
        _bind(session, tenant_id)
        session.execute(
            text(
                "INSERT INTO baskets (id, tenant_id, merchant_id, buyer_ref, lines, status) "
                "VALUES (:id, :t, :m, :b, CAST('[]' AS jsonb), 'CHECKED_OUT')"
            ),
            {"id": basket_id, "t": tenant_id, "m": merchant_id, "b": buyer.buyer_ref},
        )
        session.execute(
            text(
                "INSERT INTO checkouts (id, tenant_id, merchant_id, basket_id, buyer_ref, "
                "current_version, status, correlation_id) "
                "VALUES (:id, :t, :m, :bask, :b, :v, 'APPROVED', :corr)"
            ),
            {
                "id": checkout_id,
                "t": tenant_id,
                "m": merchant_id,
                "bask": basket_id,
                "b": buyer.buyer_ref,
                "v": version,
                "corr": correlation_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, :cur, :total, "
                "'APPROVAL_REQUIRED', true)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "v": version,
                "content": json.dumps(content, sort_keys=True),
                "h": checkout.content_hash,
                "cur": total.currency,
                "total": total.minor,
            },
        )
        issued = receipts.issue_receipt(
            session,
            ReceiptDraft(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout_id=checkout_id,
                checkout_version=version,
                checkout_hash=checkout.content_hash,
                policies=tuple(
                    MerchantPolicy(
                        kind=kind,
                        policy_id=f"pol-{kind.value.lower()}",
                        policy_version=12,
                        terms={"summary": f"{kind.value} terms"},
                    )
                    for kind in PolicyKind
                ),
                tax_policy_version="3",
                rounding_policy_version="1",
                buyer_visible_refs=(
                    BuyerVisibleRef(
                        label="Refund policy",
                        uri="https://demo.invalid/policies/refund",
                        text_hash=canonical_hash({"policy": "refund", "version": 12}),
                    ),
                ),
                correlation_id=correlation_id,
            ),
        )
        session.execute(
            text(
                "UPDATE checkout_versions SET policy_receipt_id = :rid, "
                "policy_receipt_hash = :rh, status = 'APPROVED' "
                "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
            ),
            {
                "rid": issued.receipt_id,
                "rh": issued.receipt_hash,
                "t": tenant_id,
                "c": checkout_id,
                "v": version,
            },
        )
        reservations.reserve(
            session, checkout_id=checkout_id, checkout_version=version, ttl_seconds=900
        )

    provider_order_id = f"order_{uuid.uuid4().hex[:14]}"
    with session.begin():
        _bind(session, tenant_id)
        decision = tk.admit(
            session,
            AdmissionRequest(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout=checkout,
                amount=total,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=f"setup-{uuid7().hex[:16]}",
                principal=tk.AgentPrincipal(
                    principal_id=f"session:{buyer.session_id}",
                    tenant_id=tenant_id,
                    actor_type=tk.ActorType.BUYER,
                    merchant_id=merchant_id,
                    buyer_ref=buyer.buyer_ref,
                    capabilities=frozenset({"checkout.submit_approved"}),
                ),
                correlation_id=correlation_id,
                approval_id=uuid7(),
            ),
            _StubMerchant(total),
        )
        assert decision.allowed, decision.explanation
        attempt_id, grant_id = decision.payment_attempt_id, decision.grant_id
        assert attempt_id is not None
        assert grant_id is not None
        # The worker's order: spend the single-use grant, then record the provider's
        # answer. A live payment grant is what ``admit_refund`` refuses, so it must go.
        tk.consume_grant(
            session,
            grant_id,
            tk.GrantBinding(
                tenant_id=tenant_id,
                checkout=checkout,
                payment_attempt_id=attempt_id,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                amount=total,
            ),
        )
        tk.record_create_order_result(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            outcome=ProviderOrderOutcome(
                kind="ok",
                provider_order_id=provider_order_id,
                code=tk.RecoveryCode.OK,
                reason="created",
            ),
            correlation_id=correlation_id,
        )
        session.execute(
            text(
                "UPDATE checkouts SET status = 'AWAITING_PAYMENT' WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant_id, "c": checkout_id},
        )

    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    with session.begin():
        _bind(session, tenant_id)
        applied = tk.apply_provider_evidence(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            evidence=tk.ProviderEvidence(
                source=tk.EvidenceSource.WEBHOOK,
                provider_payment_id=payment_id,
                provider_order_id=provider_order_id,
                amount_minor=total.minor,
                currency=total.currency,
                status="captured",
                provider_status="captured",
                raw_digest=hashlib.sha256(payment_id.encode()).hexdigest(),
                captured_at="2026-01-01T00:00:00Z",
                event_id=f"evt_{payment_id}",
            ),
            correlation_id=uuid7(),
        )
        assert applied.order_id is not None
        order_id = applied.order_id
        session.execute(
            text("UPDATE checkouts SET status = 'PAID' WHERE tenant_id = :t AND id = :c"),
            {"t": tenant_id, "c": checkout_id},
        )
    session.close()

    return Confirmed(
        buyer=buyer,
        checkout_id=checkout_id,
        attempt_id=attempt_id,
        order_id=order_id,
        payment_id=payment_id,
        amount=total,
    )


# ------------------------------------------------------------------------ fixtures


@pytest.fixture
def kernel(capi_kernel_engine: Engine) -> Iterator[Session]:
    """A kernel-role session for the state seams and for inspection, outside the app."""
    session = Session(capi_kernel_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def buyer_a(mint_client: MintClient) -> tuple[TestClient, MintedSession]:
    return mint_client(buyer_ref="a")


@pytest.fixture
def buyer_b(mint_client: MintClient) -> tuple[TestClient, MintedSession]:
    """A second buyer in the same tenant, which is how every ownership test is written."""
    return mint_client(buyer_ref="b")


@pytest.fixture
def order_a(
    seeded_tenant: SeededTenant,
    buyer_a: tuple[TestClient, MintedSession],
    capi_kernel_engine: Engine,
) -> Confirmed:
    """One confirmed order belonging to buyer A."""
    return confirm_order(capi_kernel_engine, seeded_tenant, buyer_a[1])


def _idem(prefix: str = "k") -> dict[str, str]:
    return {"Idempotency-Key": f"{prefix}-{uuid.uuid4().hex[:12]}"}


def request_refund(client: TestClient, order_id: uuid.UUID) -> str:
    """Ask for a full refund through the real route; return the ``refunds`` row id."""
    response = client.post(
        f"/v1/orders/{order_id}/refunds",
        headers=_idem("refund"),
        json={"reason": "buyer_requested"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"]["allowed"] is True, body["decision"]
    return str(body["refund"]["refund_id"])


def set_refund_row(
    kernel: Session,
    tenant_id: uuid.UUID,
    refund_id: str,
    *,
    status: str,
    amount_minor: int | None = None,
) -> None:
    """Move a refund row by hand.

    **Test seam.** In production only the worker moves ``refunds.status``, from the
    provider's answer under a consumed grant; there is no HTTP route that does it, and
    driving the worker here would test the worker. The listing under test reads the row,
    so the row is written to the state the assertion is about.
    """
    _bind(kernel, tenant_id)
    if amount_minor is None:
        statement = text(
            "UPDATE refunds SET status = :s WHERE tenant_id = :t AND id = :r RETURNING id"
        )
        params: dict[str, Any] = {"s": status, "t": tenant_id, "r": uuid.UUID(refund_id)}
    else:
        statement = text(
            "UPDATE refunds SET status = :s, amount_minor = :a "
            "WHERE tenant_id = :t AND id = :r RETURNING id"
        )
        params = {"s": status, "a": amount_minor, "t": tenant_id, "r": uuid.UUID(refund_id)}
    # ``RETURNING`` with ``scalar_one`` is the row-count assertion: exactly one row moved.
    assert kernel.execute(statement, params).scalar_one() == uuid.UUID(refund_id)
    kernel.commit()


# -------------------------------------------------------------------------- orders


class TestOrdersList:
    def test_a_buyer_lists_only_their_own_orders(
        self,
        buyer_a: tuple[TestClient, MintedSession],
        buyer_b: tuple[TestClient, MintedSession],
        order_a: Confirmed,
    ) -> None:
        """Two buyers, one tenant: B sees nothing of A's, not even in the counts."""
        client_a, client_b = buyer_a[0], buyer_b[0]

        theirs = client_b.get("/v1/orders")
        assert theirs.status_code == 200, theirs.text
        assert theirs.json()["orders"] == []
        assert theirs.json()["scope"] == "own"
        assert theirs.json()["next_cursor"] is None
        assert set(theirs.json()["counts"].values()) == {0}, (
            "counts are across the caller's scope, and B's scope holds nothing"
        )

        mine = client_a.get("/v1/orders")
        assert mine.status_code == 200, mine.text
        page = mine.json()
        assert page["scope"] == "own"
        assert len(page["orders"]) == 1
        row = page["orders"][0]
        assert row["order_id"] == str(order_a.order_id)
        assert row["checkout_id"] == str(order_a.checkout_id)
        assert row["payment_attempt_id"] == str(order_a.attempt_id)
        assert row["state"] == "CONFIRMED"
        assert row["amount_minor"] == order_a.amount.minor
        assert row["currency"] == order_a.amount.currency
        assert row["capture_evidence"]["kind"] == "WEBHOOK", (
            "an order is confirmed only from provider evidence; BROWSER_CALLBACK must "
            "never appear here"
        )
        assert row["razorpay_payment_id"] == order_a.payment_id
        assert row["refund_count"] == 0
        assert row["refunded_minor"] == 0
        assert row["age_seconds"] >= 0
        assert page["counts"]["CONFIRMED"] == 1

    def test_an_operator_lists_the_whole_tenant(
        self,
        buyer_b: tuple[TestClient, MintedSession],
        order_a: Confirmed,
        scenario_headers: dict[str, str],
    ) -> None:
        """The scenario key on the request widens B's read to the tenant, so A's order
        appears, and the page says so in ``scope``."""
        response = buyer_b[0].get("/v1/orders", headers=scenario_headers)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["scope"] == "tenant"
        assert [row["order_id"] for row in page["orders"]] == [str(order_a.order_id)]
        assert page["counts"]["CONFIRMED"] == 1

    def test_orders_paginate_by_keyset_without_repeats_or_gaps(
        self,
        seeded_tenant: SeededTenant,
        buyer_a: tuple[TestClient, MintedSession],
        capi_kernel_engine: Engine,
    ) -> None:
        """Three orders walked one at a time: every id once, newest first, cursor null
        exactly on the last page."""
        client, session = buyer_a
        created = [
            confirm_order(capi_kernel_engine, seeded_tenant, session).order_id for _ in range(3)
        ]
        expected = [str(order_id) for order_id in reversed(created)]

        whole = client.get("/v1/orders", params={"limit": 100})
        assert whole.status_code == 200, whole.text
        assert [row["order_id"] for row in whole.json()["orders"]] == expected
        assert whole.json()["next_cursor"] is None

        walked: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/v1/orders", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            pages += 1
            assert page["limit"] == 1
            assert len(page["orders"]) == 1, "a walk over three rows never yields an empty page"
            walked.extend(row["order_id"] for row in page["orders"])
            assert page["counts"]["CONFIRMED"] == 3, "counts span the scope, not the page"
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert pages < 10, "the cursor must terminate"

        assert pages == 3, "next_cursor is null exactly on the last page"
        assert walked == expected, "no repeats, no gaps, newest first"
        assert len(set(walked)) == len(walked)

    def test_a_malformed_cursor_is_a_400_problem(
        self, buyer_a: tuple[TestClient, MintedSession]
    ) -> None:
        """A mangled cursor is refused rather than answered with an empty page, which a
        client would read as the end of the collection."""
        response = buyer_a[0].get("/v1/orders", params={"cursor": "not-a-cursor"})
        assert response.status_code == 400, response.text
        assert response.headers["content-type"].startswith(PROBLEM)
        assert response.json()["status"] == 400

    def test_orders_filter_by_status_and_counts_span_the_scope(
        self, buyer_a: tuple[TestClient, MintedSession], order_a: Confirmed
    ) -> None:
        response = buyer_a[0].get("/v1/orders", params={"status": "CANCELLED"})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["orders"] == []
        assert page["next_cursor"] is None
        assert set(page["counts"]) == {state.value for state in OrderState}, (
            "every state is present so a zero reads as none rather than not measured"
        )
        assert page["counts"]["CONFIRMED"] >= 1
        assert page["counts"]["CANCELLED"] == 0

    def test_an_operator_may_open_another_buyers_order(
        self,
        buyer_b: tuple[TestClient, MintedSession],
        order_a: Confirmed,
        scenario_headers: dict[str, str],
    ) -> None:
        """What makes the console's list clickable; and without the key, 404 not 403."""
        client_b = buyer_b[0]
        opened = client_b.get(f"/v1/orders/{order_a.order_id}", headers=scenario_headers)
        assert opened.status_code == 200, opened.text
        assert opened.json()["order_id"] == str(order_a.order_id)

        refused = client_b.get(f"/v1/orders/{order_a.order_id}")
        assert refused.status_code == 404, refused.text
        assert refused.headers["content-type"].startswith(PROBLEM)


# ------------------------------------------------------------------------- refunds


class TestRefundsList:
    def test_refunds_list_reports_wire_state_and_counts(
        self,
        seeded_tenant: SeededTenant,
        buyer_a: tuple[TestClient, MintedSession],
        order_a: Confirmed,
        kernel: Session,
    ) -> None:
        client = buyer_a[0]
        refund_id = request_refund(client, order_a.order_id)

        response = client.get("/v1/refunds")
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["scope"] == "own"
        assert page["next_cursor"] is None
        assert len(page["refunds"]) == 1
        row = page["refunds"][0]
        assert row["refund_id"] == refund_id
        assert row["state"] == "REFUND_PENDING"
        assert row["row_status"] == "PENDING"
        assert row["order_id"] == str(order_a.order_id)
        assert row["checkout_id"] == str(order_a.checkout_id)
        assert row["payment_attempt_id"] == str(order_a.attempt_id)
        assert row["amount_minor"] == order_a.amount.minor
        assert row["captured_minor"] == order_a.amount.minor
        assert row["automatic"] is False
        assert row["reason"] == "buyer_requested"
        assert row["age_seconds"] >= 0

        wire = {state.value for state in listing.REFUND_WIRE_STATES}
        assert set(page["counts"]) == wire, "every wire state, zeros included"
        assert page["counts"]["REFUND_PENDING"] == 1
        assert sum(page["counts"].values()) == 1

        # The order list sees the pending refund in its count but not in its sum: money
        # the provider has not confirmed returning has not returned.
        orders = client.get("/v1/orders").json()["orders"]
        assert orders[0]["refund_count"] == 1
        assert orders[0]["refunded_minor"] == 0

        # Test seam: the worker lost the provider's answer. See ``set_refund_row``.
        set_refund_row(kernel, seeded_tenant.tenant_id, refund_id, status="UNKNOWN")

        unknown = client.get("/v1/refunds", params={"state": "REFUND_UNKNOWN"})
        assert unknown.status_code == 200, unknown.text
        assert [row["refund_id"] for row in unknown.json()["refunds"]] == [refund_id]
        assert unknown.json()["refunds"][0]["state"] == "REFUND_UNKNOWN"
        assert unknown.json()["refunds"][0]["row_status"] == "UNKNOWN"
        assert unknown.json()["counts"]["REFUND_UNKNOWN"] == 1
        assert unknown.json()["counts"]["REFUND_PENDING"] == 0

        pending = client.get("/v1/refunds", params={"state": "REFUND_PENDING"})
        assert pending.status_code == 200, pending.text
        assert pending.json()["refunds"] == [], (
            "UNKNOWN and PENDING must never be folded together: an operator who retried "
            "an in-flight refund would pay the buyer twice"
        )

    def test_a_partial_settled_refund_is_partially_refunded(
        self,
        seeded_tenant: SeededTenant,
        buyer_a: tuple[TestClient, MintedSession],
        order_a: Confirmed,
        kernel: Session,
    ) -> None:
        """``PROCESSED`` for less than the capture is ``PARTIALLY_REFUNDED``; collapsing
        it into ``REFUNDED`` would tell an operator a partial refund settled the payment."""
        client = buyer_a[0]
        refund_id = request_refund(client, order_a.order_id)
        capture = order_a.amount.minor
        assert capture > 1

        # Test seam: the provider settled a smaller amount. See ``set_refund_row``.
        set_refund_row(
            kernel,
            seeded_tenant.tenant_id,
            refund_id,
            status="PROCESSED",
            amount_minor=capture - 1,
        )
        partial = client.get("/v1/refunds")
        assert partial.status_code == 200, partial.text
        row = partial.json()["refunds"][0]
        assert row["state"] == "PARTIALLY_REFUNDED"
        assert row["row_status"] == "PROCESSED"
        assert row["amount_minor"] == capture - 1
        assert row["captured_minor"] == capture
        assert partial.json()["counts"]["PARTIALLY_REFUNDED"] == 1
        assert partial.json()["counts"]["REFUNDED"] == 0
        filtered = client.get("/v1/refunds", params={"state": "PARTIALLY_REFUNDED"})
        assert [r["refund_id"] for r in filtered.json()["refunds"]] == [refund_id]
        assert client.get("/v1/refunds", params={"state": "REFUNDED"}).json()["refunds"] == []
        # And the order's settled sum is the database's integer sum over that one row.
        assert client.get("/v1/orders").json()["orders"][0]["refunded_minor"] == capture - 1

        set_refund_row(
            kernel, seeded_tenant.tenant_id, refund_id, status="PROCESSED", amount_minor=capture
        )
        whole = client.get("/v1/refunds")
        assert whole.status_code == 200, whole.text
        row = whole.json()["refunds"][0]
        assert row["state"] == "REFUNDED"
        assert row["row_status"] == "PROCESSED"
        assert whole.json()["counts"]["REFUNDED"] == 1
        assert whole.json()["counts"]["PARTIALLY_REFUNDED"] == 0
        assert client.get("/v1/orders").json()["orders"][0]["refunded_minor"] == capture

    def test_an_unknown_refund_state_filter_is_422(
        self, buyer_a: tuple[TestClient, MintedSession]
    ) -> None:
        """``CAPTURED`` is a payment state, not a state a refund row can be in."""
        response = buyer_a[0].get("/v1/refunds", params={"state": "CAPTURED"})
        assert response.status_code == 422, response.text
        assert response.headers["content-type"].startswith(PROBLEM)
        body = response.json()
        assert body["state"] == "CAPTURED"
        assert set(body["allowed"]) == {s.value for s in listing.REFUND_WIRE_STATES}


# ----------------------------------------------------------------- operator sessions


class TestOperatorSessions:
    def test_minting_an_operator_session_requires_the_scenario_key(
        self,
        client: TestClient,
        seeded_tenant: SeededTenant,
        order_a: Confirmed,
        scenario_headers: dict[str, str],
    ) -> None:
        """An operator session is not a buyer with a different label. Only a caller who
        already holds the scenario key may mint one, and even then the session alone
        does not widen a read: the key on each request does."""
        body = {"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "OPERATOR"}

        refused = client.post("/v1/demo/sessions", json=body)
        assert refused.status_code == 401, refused.text
        assert refused.headers["content-type"].startswith(PROBLEM)

        minted = client.post("/v1/demo/sessions", json=body, headers=scenario_headers)
        assert minted.status_code == 201, minted.text
        payload = minted.json()
        assert payload["actor_type"] == "OPERATOR"
        assert payload["capabilities"] == ["catalogue.read", "order.read"], (
            "read-only in P0: nothing on an operator session moves money"
        )

        operator = TestClient(client.app, headers={"Authorization": f"Bearer {payload['token']}"})
        unkeyed = operator.get("/v1/orders")
        assert unkeyed.status_code == 200, unkeyed.text
        assert unkeyed.json()["scope"] == "own"
        assert unkeyed.json()["orders"] == [], (
            "the session carries no authority over the tenant's rows; the key does"
        )

        keyed = operator.get("/v1/orders", headers=scenario_headers)
        assert keyed.status_code == 200, keyed.text
        assert keyed.json()["scope"] == "tenant"
        assert [row["order_id"] for row in keyed.json()["orders"]] == [str(order_a.order_id)]

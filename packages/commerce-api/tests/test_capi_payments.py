"""Payments, webhooks, orders and refunds over HTTP.

The assertions that matter most in this file are the negative ones, because the bugs
they exclude are the ones that move money:

* a **valid** client signature records a browser callback, enqueues a reconciliation, and
  leaves the payment state exactly where it was with **no ``orders`` row** (ADR 0003 D8).
  This is the whole point: a correct implementation and a catastrophic one both answer
  200 here, and only the database tells them apart;
* an **invalid** signature answers 401 and writes nothing at all -- no attempt update, no
  outbox row, no audit row;
* a client return naming an order this session does not own is refused, so a leaked order
  id is not a payment;
* a webhook body is never parsed before its HMAC verifies, proven by sending malformed
  JSON with a bad signature and asserting 401 rather than a parse failure;
* the same event id delivered twice claims one inbox row and enqueues one command.

Everything runs against real PostgreSQL as ``commerce_test_kernel`` /
``commerce_test_app``, both NOSUPERUSER NOBYPASSRLS, so row-level security is doing its
job rather than being bypassed by a superuser connection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_domain import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Money,
    PolicyKind,
    RecoveryCode,
    canonical_hash,
    uuid7,
)
from fastapi.testclient import TestClient
from merchant_adapter import DEFAULT_TERMS
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import receipts, reservations
from transaction_kernel.admission import AdmissionRequest, CurrentMerchantState
from transaction_kernel.payments import ProviderOrderOutcome
from transaction_kernel.receipts import BuyerVisibleRef, ReceiptDraft, SaleTerm

from conftest import (
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    MintedSession,
    SeededTenant,
    approved_content,
)

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

TOTAL = Money(39500, "INR")


# ---------------------------------------------------------------------- signatures


def payment_signature(order_id: str, payment_id: str, secret: str = TEST_KEY_SECRET) -> str:
    """What Razorpay Checkout hands the browser: HMAC-SHA256 of ``order|payment``."""
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def webhook_signature(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """``X-Razorpay-Signature``: HMAC-SHA256 over the exact bytes sent."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def captured_event(order_id: str, payment_id: str, amount_minor: int) -> bytes:
    """A ``payment.captured`` delivery, serialised once so its bytes stay stable."""
    body = {
        "event": "payment.captured",
        "created_at": 1767225600,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount_minor,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


# ------------------------------------------------------------------------ fixtures


class _StubMerchant:
    """A merchant whose current state agrees with what was approved.

    The kernel's admission asks the merchant to revalidate; this fixture is about the
    HTTP layer, so the merchant is deliberately boring and every denial in this file
    comes from the thing under test.
    """

    def __init__(self, total: Money = TOTAL) -> None:
        self.total = total

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        content = approved_content(checkout_id, version, self.total)
        return CurrentMerchantState(
            total=self.total,
            line_items=content["line_items"],
            all_available=True,
            policy_version=content["policy_version"],
        )


@dataclass(frozen=True, slots=True)
class Admitted:
    """A checkout owned by ``demo_session``'s buyer with one admitted payment attempt."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str
    attempt_id: uuid.UUID
    provider_order_id: str
    amount: Money


@pytest.fixture
def kernel(capi_kernel_engine: Engine) -> Iterator[Session]:
    """A kernel-role session for setting up and inspecting state, outside the app."""
    session = Session(capi_kernel_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _bind(session: Session, tenant_id: uuid.UUID) -> None:
    session.execute(SET_TENANT, {"t": str(tenant_id)})


@pytest.fixture
def admitted(
    seeded_tenant: SeededTenant,
    demo_session: MintedSession,
    capi_kernel_engine: Engine,
) -> Admitted:
    """Build the state the payment endpoints assume: an approved checkout, admitted.

    Written through the kernel's own modules (``receipts``, ``reservations``, ``admit``)
    rather than by hand-inserting rows, so the fixture exercises the same paths the
    checkout unit will and cannot drift into a shape the kernel would never produce.
    """
    tenant_id = seeded_tenant.tenant_id
    merchant_id = seeded_tenant.merchant_id
    cart_id, checkout_id = uuid7(), uuid7()
    version = 1
    content = approved_content(checkout_id, version, TOTAL)
    checkout = CheckoutRef(checkout_id, version, canonical_hash(content))
    correlation_id = uuid7()

    session = Session(capi_kernel_engine, expire_on_commit=False)
    with session.begin():
        _bind(session, tenant_id)
        session.execute(
            text(
                "INSERT INTO carts (id, tenant_id, merchant_id, buyer_ref, lines, status) "
                "VALUES (:id, :t, :m, :b, CAST('[]' AS jsonb), 'CHECKED_OUT')"
            ),
            {"id": cart_id, "t": tenant_id, "m": merchant_id, "b": demo_session.buyer_ref},
        )
        session.execute(
            text(
                "INSERT INTO checkouts (id, tenant_id, merchant_id, cart_id, buyer_ref, "
                "current_version, status, correlation_id) "
                "VALUES (:id, :t, :m, :bask, :b, :v, 'APPROVED', :corr)"
            ),
            {
                "id": checkout_id,
                "t": tenant_id,
                "m": merchant_id,
                "bask": cart_id,
                "b": demo_session.buyer_ref,
                "v": version,
                "corr": correlation_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, 'INR', :total, "
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
                "total": TOTAL.minor,
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
                    SaleTerm(
                        kind=kind,
                        policy_id=f"pol-{kind.value.lower()}",
                        policy_version=12,
                        # The real terms for the families that have them, because
                        # admission now reads the REFUND one. A summary string was
                        # enough while nothing did; it is a sale under terms nobody
                        # wrote as soon as something does.
                        terms=DEFAULT_TERMS.get(kind.value, {"summary": f"{kind.value} terms"}),
                    )
                    for kind in PolicyKind
                ),
                tax_policy_version=3,
                rounding_policy_version=1,
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
                "policy_receipt_hash = :rh "
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
        # The buyer's decision, through the real function: admission reads this row, so a
        # status stamped on the version with nothing behind it is not an approval.
        buyer_principal = AgentPrincipal(
            principal_id=f"session:{demo_session.session_id}",
            tenant_id=tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=merchant_id,
            buyer_ref=demo_session.buyer_ref,
            capabilities=frozenset({"checkout.submit_approved"}),
        )
        approval = tk.record_approval(
            session,
            tenant_id=tenant_id,
            checkout=checkout,
            amount=TOTAL,
            principal=buyer_principal,
            correlation_id=correlation_id,
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
                amount=TOTAL,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=f"setup-{uuid7().hex[:16]}",
                principal=buyer_principal,
                correlation_id=correlation_id,
                approval_id=approval.approval_id,
            ),
            _StubMerchant(),
        )
        assert decision.allowed, decision.explanation
        attempt_id = decision.payment_attempt_id
        grant_id = decision.grant_id
        assert attempt_id is not None
        assert grant_id is not None
        # The worker's create-order step, in the worker's own order: spend the single-use
        # grant first, then record what the provider said. Spending it also matters for
        # the refund tests -- a live payment grant on the attempt is exactly what
        # ``admit_refund`` refuses as CONCURRENT_OPERATION, and it should.
        tk.consume_grant(
            session,
            grant_id,
            tk.GrantBinding(
                tenant_id=tenant_id,
                checkout=checkout,
                payment_attempt_id=attempt_id,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                amount=TOTAL,
            ),
        )
        tk.record_create_order_result(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            outcome=ProviderOrderOutcome(
                kind="ok",
                provider_order_id=provider_order_id,
                code=RecoveryCode.OK,
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
    session.close()

    return Admitted(
        checkout_id=checkout_id,
        version=version,
        content_hash=checkout.content_hash,
        attempt_id=attempt_id,
        provider_order_id=provider_order_id,
        amount=TOTAL,
    )


# ---------------------------------------------------------------------- inspection


def attempt_state(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> Any:
    _bind(session, tenant_id)
    return session.execute(
        text(
            "SELECT status, provider_order_id, provider_payment_id FROM payment_attempts "
            "WHERE tenant_id = :t AND id = :a"
        ),
        {"t": tenant_id, "a": attempt_id},
    ).one()


def outbox_of(session: Session, tenant_id: uuid.UUID, command_type: str) -> list[Any]:
    _bind(session, tenant_id)
    return list(
        session.execute(
            text(
                "SELECT id, payload FROM outbox_events "
                "WHERE tenant_id = :t AND command_type = :c ORDER BY created_at"
            ),
            {"t": tenant_id, "c": command_type},
        ).all()
    )


def order_count(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> int:
    _bind(session, tenant_id)
    return int(
        session.execute(
            text("SELECT count(*) FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a"),
            {"t": tenant_id, "a": attempt_id},
        ).scalar_one()
    )


def inbox_rows(session: Session, tenant_id: uuid.UUID) -> list[Any]:
    _bind(session, tenant_id)
    return list(
        session.execute(
            text(
                "SELECT id, dedup_key, raw_body, duplicate_count, apply_status, event_type, "
                "signature_verified, headers_redacted FROM webhook_inbox "
                "WHERE tenant_id = :t ORDER BY received_at"
            ),
            {"t": tenant_id},
        ).all()
    )


def _idem(prefix: str = "k") -> dict[str, str]:
    return {"Idempotency-Key": f"{prefix}-{uuid.uuid4().hex[:12]}"}


# ------------------------------------------------------------------- the handoff


class TestPaymentHandoff:
    def test_handoff_carries_the_public_key_id_and_the_provider_order(
        self, auth_client: TestClient, admitted: Admitted
    ) -> None:
        response = auth_client.get(f"/v1/checkouts/{admitted.checkout_id}/payment")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "razorpay"
        assert body["razorpay_key_id"].startswith("rzp_test_")
        assert body["razorpay_order_id"] == admitted.provider_order_id
        assert body["attempt_id"] == str(admitted.attempt_id)
        assert body["state"] == "SUBMITTED"
        assert body["amount_minor"] == admitted.amount.minor
        assert body["currency"] == "INR"

    def test_the_handoff_does_not_quote_a_previous_versions_attempt(
        self,
        auth_client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        capi_kernel_engine: Engine,
    ) -> None:
        """A re-versioned checkout must not be handed off at the old version's money.

        ``latest_attempt`` answers "the newest attempt on this checkout" and was used
        unscoped here, so once a checkout had been re-versioned the handoff reported the
        *current* version number beside the *previous* version's amount and provider order
        id. The browser would have opened Razorpay Checkout on the old order, for the old
        figure, while the page said version 2.

        Version 2 has no attempt of its own yet, so the version's own total is the honest
        answer.
        """
        moved = Money(admitted.amount.minor + 5_000, admitted.amount.currency)
        session = Session(capi_kernel_engine, expire_on_commit=False)
        with session.begin():
            _bind(session, seeded_tenant.tenant_id)
            content = approved_content(admitted.checkout_id, 2, moved)
            session.execute(
                text(
                    "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                    "version, content, content_hash, total_minor, currency, status) "
                    "VALUES (:id, :t, :m, :c, 2, CAST(:content AS jsonb), :h, :total, :cur, "
                    "'APPROVAL_REQUIRED')"
                ),
                {
                    "id": uuid7(),
                    "t": seeded_tenant.tenant_id,
                    "m": seeded_tenant.merchant_id,
                    "c": admitted.checkout_id,
                    "content": json.dumps(content),
                    "h": str(canonical_hash(content)),
                    "total": moved.minor,
                    "cur": moved.currency,
                },
            )
            session.execute(
                text("UPDATE checkouts SET current_version = 2 WHERE tenant_id = :t AND id = :c"),
                {"t": seeded_tenant.tenant_id, "c": admitted.checkout_id},
            )
        session.close()

        body = auth_client.get(f"/v1/checkouts/{admitted.checkout_id}/payment").json()
        assert body["version"] == 2
        assert body["amount_minor"] == moved.minor, (
            "the handoff quoted the previous version's attempt amount"
        )
        assert body["razorpay_order_id"] is None, (
            "the handoff offered the previous version's Razorpay order"
        )
        assert body["attempt_id"] is None

    def test_the_handoff_never_leaks_a_secret(
        self, auth_client: TestClient, admitted: Admitted
    ) -> None:
        """The key *id* is public; the key secret and webhook secret are not."""
        body = auth_client.get(f"/v1/checkouts/{admitted.checkout_id}/payment").text
        assert TEST_KEY_SECRET not in body
        assert TEST_WEBHOOK_SECRET not in body

    def test_another_buyers_checkout_is_not_found(
        self, mint_client: Any, admitted: Admitted
    ) -> None:
        stranger, _ = mint_client(buyer_ref="someone-else")
        response = stranger.get(f"/v1/checkouts/{admitted.checkout_id}/payment")
        assert response.status_code == 404


# -------------------------------------------------------------- the client return


class TestClientReturn:
    def test_a_valid_signature_records_evidence_and_never_captures(
        self,
        auth_client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        """ADR 0003 D8, the assertion this whole file exists for.

        A correct implementation and one that treats the browser as capture evidence
        both answer 200 with a cheerful body. The difference is entirely in the four
        database assertions below: the state did not move, the payment id was recorded,
        a reconciliation was enqueued, and **no order was confirmed**.
        """
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        response = auth_client.post(
            "/v1/payments/verify",
            headers=_idem("verify"),
            json={
                "checkout_id": str(admitted.checkout_id),
                "razorpay_order_id": admitted.provider_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": payment_signature(admitted.provider_order_id, payment_id),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accepted"] is True
        assert body["evidence_kind"] == "BROWSER_CALLBACK"
        assert body["attempt_id"] == str(admitted.attempt_id)
        assert body["state"] == "SUBMITTED"

        row = attempt_state(kernel, seeded_tenant.tenant_id, admitted.attempt_id)
        assert row.status == "SUBMITTED", "a browser callback must never move the payment state"
        assert row.provider_payment_id == payment_id

        commands = outbox_of(kernel, seeded_tenant.tenant_id, "RECONCILE_PAYMENT")
        assert len(commands) == 1
        assert commands[0].payload["payment_attempt_id"] == str(admitted.attempt_id)
        assert commands[0].payload["reason"] == "browser_callback"

        assert order_count(kernel, seeded_tenant.tenant_id, admitted.attempt_id) == 0, (
            "a client return must never confirm an order; capture comes only from "
            "WEBHOOK or PROVIDER_FETCH evidence"
        )

    def test_an_invalid_signature_is_401_and_writes_nothing(
        self,
        auth_client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        response = auth_client.post(
            "/v1/payments/verify",
            headers=_idem("bad"),
            json={
                "checkout_id": str(admitted.checkout_id),
                "razorpay_order_id": admitted.provider_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": "0" * 64,
            },
        )
        assert response.status_code == 401, response.text
        assert response.headers["content-type"].startswith("application/problem+json")

        row = attempt_state(kernel, seeded_tenant.tenant_id, admitted.attempt_id)
        assert row.provider_payment_id is None
        assert outbox_of(kernel, seeded_tenant.tenant_id, "RECONCILE_PAYMENT") == []
        _bind(kernel, seeded_tenant.tenant_id)
        callbacks = kernel.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                "AND event_type = 'payment.browser_callback'"
            ),
            {"t": seeded_tenant.tenant_id},
        ).scalar_one()
        assert callbacks == 0, "a refused signature must not even be audited as a callback"

    def test_an_order_belonging_to_another_session_is_refused(
        self,
        mint_client: Any,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        """A leaked Razorpay order id plus a valid signature is still not a payment.

        404 rather than 403: a 403 would confirm the order id names something real, which
        makes this endpoint an oracle for harvesting live order ids.
        """
        stranger, _ = mint_client(buyer_ref="not-the-buyer")
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        response = stranger.post(
            "/v1/payments/verify",
            headers=_idem("stranger"),
            json={
                "checkout_id": str(admitted.checkout_id),
                "razorpay_order_id": admitted.provider_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": payment_signature(admitted.provider_order_id, payment_id),
            },
        )
        assert response.status_code == 404, response.text
        row = attempt_state(kernel, seeded_tenant.tenant_id, admitted.attempt_id)
        assert row.provider_payment_id is None

    def test_a_retried_callback_replays_the_stored_answer(
        self,
        auth_client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        body = {
            "checkout_id": str(admitted.checkout_id),
            "razorpay_order_id": admitted.provider_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": payment_signature(admitted.provider_order_id, payment_id),
        }
        headers = _idem("replay")
        first = auth_client.post("/v1/payments/verify", headers=headers, json=body)
        second = auth_client.post("/v1/payments/verify", headers=headers, json=body)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        assert second.headers.get("Idempotent-Replayed") == "true"
        assert len(outbox_of(kernel, seeded_tenant.tenant_id, "RECONCILE_PAYMENT")) == 1

    def test_a_missing_idempotency_key_is_refused(
        self, auth_client: TestClient, admitted: Admitted
    ) -> None:
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        response = auth_client.post(
            "/v1/payments/verify",
            json={
                "checkout_id": str(admitted.checkout_id),
                "razorpay_order_id": admitted.provider_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": payment_signature(admitted.provider_order_id, payment_id),
            },
        )
        assert response.status_code == 400
        assert response.json()["header"] == "Idempotency-Key"


# ---------------------------------------------------------------- the webhook (D7)


class TestWebhookReceiver:
    def test_a_signed_delivery_stores_the_raw_bytes_and_enqueues(
        self,
        client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        raw = captured_event(
            admitted.provider_order_id, f"pay_{uuid.uuid4().hex[:14]}", admitted.amount.minor
        )
        response = client.post(
            f"/webhooks/razorpay/{seeded_tenant.tenant_slug}",
            content=raw,
            headers={
                "X-Razorpay-Signature": webhook_signature(raw),
                "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:14]}",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {
            "received": True,
            "duplicate": False,
            "inbox_id": body["inbox_id"],
            "event_type": "payment.captured",
        }

        rows = inbox_rows(kernel, seeded_tenant.tenant_id)
        assert len(rows) == 1
        assert bytes(rows[0].raw_body) == raw, "the stored body must be the bytes that were signed"
        assert rows[0].apply_status == "RECEIVED"
        assert rows[0].signature_verified is True
        assert rows[0].duplicate_count == 0
        # Only the allow-listed headers are kept; no Authorization, no cookies.
        assert set(rows[0].headers_redacted) <= {
            "x-razorpay-event-id",
            "x-razorpay-signature",
            "content-type",
            "user-agent",
        }

        commands = outbox_of(kernel, seeded_tenant.tenant_id, "APPLY_WEBHOOK_EVENT")
        assert len(commands) == 1
        assert commands[0].payload["inbox_id"] == str(rows[0].id)

    def test_the_same_event_id_twice_claims_one_row_and_enqueues_once(
        self,
        client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        """At-least-once delivery, exactly-once effect -- enforced by a unique index."""
        raw = captured_event(
            admitted.provider_order_id, f"pay_{uuid.uuid4().hex[:14]}", admitted.amount.minor
        )
        headers = {
            "X-Razorpay-Signature": webhook_signature(raw),
            "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:14]}",
            "Content-Type": "application/json",
        }
        url = f"/webhooks/razorpay/{seeded_tenant.tenant_slug}"
        first = client.post(url, content=raw, headers=headers)
        second = client.post(url, content=raw, headers=headers)

        assert first.status_code == 200
        assert first.json()["duplicate"] is False
        assert second.status_code == 200, "a redelivery is not a failure; 4xx would retry forever"
        assert second.json()["duplicate"] is True
        assert second.json()["inbox_id"] == first.json()["inbox_id"]

        rows = inbox_rows(kernel, seeded_tenant.tenant_id)
        assert len(rows) == 1
        assert rows[0].duplicate_count == 1
        assert len(outbox_of(kernel, seeded_tenant.tenant_id, "APPLY_WEBHOOK_EVENT")) == 1

    def test_a_bad_signature_is_401_and_writes_nothing(
        self, client: TestClient, seeded_tenant: SeededTenant, kernel: Session
    ) -> None:
        raw = captured_event("order_whatever", "pay_whatever", 100)
        response = client.post(
            f"/webhooks/razorpay/{seeded_tenant.tenant_slug}",
            content=raw,
            headers={
                "X-Razorpay-Signature": "deadbeef" * 8,
                "X-Razorpay-Event-Id": "evt_forged",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401, response.text
        assert inbox_rows(kernel, seeded_tenant.tenant_id) == [], (
            "a forged delivery must not claim a dedup key; if it could, it would "
            "suppress the genuine event carrying that id"
        )
        assert outbox_of(kernel, seeded_tenant.tenant_id, "APPLY_WEBHOOK_EVENT") == []

    def test_json_is_never_parsed_before_the_signature_is_verified(
        self, client: TestClient, seeded_tenant: SeededTenant, kernel: Session
    ) -> None:
        """Malformed JSON with a bad signature is a 401, not a parse error.

        The status code is the evidence: a 400 or a 500 mentioning JSON would mean the
        handler decoded an unverified stranger's bytes before authenticating them.
        """
        response = client.post(
            f"/webhooks/razorpay/{seeded_tenant.tenant_slug}",
            content=b'{"event": "payment.captured", "payload": {',
            headers={
                "X-Razorpay-Signature": "00" * 32,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401, response.text
        assert response.json()["title"] == "Signature verification failed"
        assert inbox_rows(kernel, seeded_tenant.tenant_id) == []

    def test_a_body_over_the_cap_is_413(
        self, client: TestClient, seeded_tenant: SeededTenant, kernel: Session
    ) -> None:
        """The cap is enforced while streaming, before the body is buffered."""
        oversized = b"x" * (256 * 1024 + 1)
        response = client.post(
            f"/webhooks/razorpay/{seeded_tenant.tenant_slug}",
            content=oversized,
            headers={"X-Razorpay-Signature": webhook_signature(oversized)},
        )
        assert response.status_code == 413, response.text
        assert response.json()["limit"] == 256 * 1024
        assert inbox_rows(kernel, seeded_tenant.tenant_id) == []

    def test_an_unknown_tenant_slug_is_404_even_with_a_valid_signature(
        self, client: TestClient
    ) -> None:
        raw = captured_event("order_x", "pay_x", 100)
        response = client.post(
            "/webhooks/razorpay/no-such-tenant",
            content=raw,
            headers={"X-Razorpay-Signature": webhook_signature(raw)},
        )
        assert response.status_code == 404

    def test_a_delivery_without_an_event_id_still_deduplicates_on_its_body(
        self,
        client: TestClient,
        admitted: Admitted,
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        """No ``x-razorpay-event-id`` header falls back to a body fingerprint."""
        raw = captured_event(
            admitted.provider_order_id, f"pay_{uuid.uuid4().hex[:14]}", admitted.amount.minor
        )
        headers = {"X-Razorpay-Signature": webhook_signature(raw)}
        url = f"/webhooks/razorpay/{seeded_tenant.tenant_slug}"
        assert client.post(url, content=raw, headers=headers).json()["duplicate"] is False
        assert client.post(url, content=raw, headers=headers).json()["duplicate"] is True
        assert len(inbox_rows(kernel, seeded_tenant.tenant_id)) == 1


# ---------------------------------------------------------------- orders, refunds


@pytest.fixture
def captured(
    admitted: Admitted, seeded_tenant: SeededTenant, capi_kernel_engine: Engine
) -> tuple[Admitted, uuid.UUID, str]:
    """Apply real capture evidence, the only way an order is ever confirmed.

    Uses ``WEBHOOK`` evidence through :func:`transaction_kernel.apply_provider_evidence`,
    which is the same path the worker takes. Nothing in this file writes an ``orders``
    row directly, because a fixture that could would make the D8 assertion above
    meaningless.
    """
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    session = Session(capi_kernel_engine, expire_on_commit=False)
    with session.begin():
        _bind(session, seeded_tenant.tenant_id)
        applied = tk.apply_provider_evidence(
            session,
            tenant_id=seeded_tenant.tenant_id,
            payment_attempt_id=admitted.attempt_id,
            evidence=tk.ProviderEvidence(
                source=tk.EvidenceSource.WEBHOOK,
                provider_payment_id=payment_id,
                provider_order_id=admitted.provider_order_id,
                amount_minor=admitted.amount.minor,
                currency=admitted.amount.currency,
                status="captured",
                provider_status="captured",
                raw_digest=hashlib.sha256(b"evidence").hexdigest(),
                captured_at="2026-01-01T00:00:00Z",
                event_id="evt_fixture",
            ),
            correlation_id=uuid7(),
        )
        assert applied.order_id is not None
        order_id = applied.order_id
        session.execute(
            text("UPDATE checkouts SET status = 'PAID' WHERE tenant_id = :t AND id = :c"),
            {"t": seeded_tenant.tenant_id, "c": admitted.checkout_id},
        )
    session.close()
    return admitted, order_id, payment_id


class TestOrders:
    def test_an_order_shows_its_capture_evidence_and_policy_receipt(
        self, auth_client: TestClient, captured: tuple[Admitted, uuid.UUID, str]
    ) -> None:
        admitted, order_id, payment_id = captured
        response = auth_client.get(f"/v1/orders/{order_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "CONFIRMED"
        assert body["amount_minor"] == admitted.amount.minor
        assert body["content_hash"] == admitted.content_hash
        assert body["policy_receipt_hash"]
        assert body["payment"]["state"] == "CAPTURED"
        assert body["payment"]["razorpay_payment_id"] == payment_id
        assert body["payment"]["capture_evidence"]["kind"] == "WEBHOOK", (
            "an order is confirmed only from provider evidence; BROWSER_CALLBACK must "
            "never appear here"
        )
        assert body["refunds"] == []

    def test_an_order_shows_the_lines_it_was_priced_from(
        self, auth_client: TestClient, captured: tuple[Admitted, uuid.UUID, str]
    ) -> None:
        """The buyer can see *what* they bought, not only what it cost.

        This assertion is the one that was missing. ``order_payload`` returned
        ``quote: null`` on every order for two hundred commits, the buyer surface rendered
        "No quote is retained on this order" as though that were a designed state, and the
        suite stayed green because no test on the order read ever looked. A null here is
        now a failure rather than a silence.

        Every figure is checked against the approved document rather than recomputed, so a
        renderer that started re-pricing against a live catalogue would fail this even if
        its arithmetic were correct: the point is not that the numbers add up, it is that
        they are the numbers the buyer approved.
        """
        admitted, order_id, _ = captured
        content = approved_content(admitted.checkout_id, admitted.version, admitted.amount)

        body = auth_client.get(f"/v1/orders/{order_id}").json()
        quote = body["quote"]
        assert quote is not None, "an order must show the lines it was priced from"

        assert quote["content_hash"] == admitted.content_hash
        assert [(line["sku"], line["quantity"]) for line in quote["lines"]] == [
            (line["sku"], line["quantity"]) for line in content["lines"]
        ]
        assert [line["subtotal_minor"] for line in quote["lines"]] == [
            line["line_minor"] for line in content["lines"]
        ]
        assert quote["items_subtotal_minor"] == content["subtotal_minor"]
        assert quote["items_tax_minor"] == sum(line["tax_minor"] for line in content["lines"])
        assert quote["total_minor"] == body["amount_minor"] == admitted.amount.minor

        rows = (
            quote["items_subtotal_minor"]
            + quote["items_tax_minor"]
            + quote["delivery_fee_minor"]
            + quote["delivery_tax_minor"]
            - quote["discount_minor"]
        )
        assert rows == quote["total_minor"], (
            "the rows a buyer reads must add up to the amount they were charged; a "
            "breakdown that does not is worse than none"
        )

    def test_the_refundable_figure_names_the_deadline_it_expires_on(
        self, auth_client: TestClient, captured: tuple[Admitted, uuid.UUID, str]
    ) -> None:
        """A buyer deciding whether to ask is owed the deadline before it passes.

        The window has governed admission since it began being read; this asserts it also
        reaches the screen the buyer decides on. A platform that refuses a refund on a
        date it never showed anybody has enforced a rule and hidden it.
        """
        _, order_id, _ = captured
        body = auth_client.get(f"/v1/orders/{order_id}/refundable").json()

        assert body["anything_remains"] is True
        assert body["code"] == "OK"
        assert body["explanation"] == ""

        closes_at = datetime.fromisoformat(body["window_closes_at"])
        window = DEFAULT_TERMS[PolicyKind.REFUND.value]["window_days"]
        expected = datetime.now(UTC) + timedelta(days=window)
        assert abs((closes_at - expected).total_seconds()) < 300, (
            f"the deadline must be the {window} days the receipt froze, counted from the "
            "sale rather than from whenever somebody happens to ask"
        )

    def test_another_buyers_order_is_not_found(
        self, mint_client: Any, captured: tuple[Admitted, uuid.UUID, str]
    ) -> None:
        _, order_id, _ = captured
        stranger, _ = mint_client(buyer_ref="nosy")
        assert stranger.get(f"/v1/orders/{order_id}").status_code == 404


class TestRefunds:
    def test_a_refund_is_admitted_under_a_fresh_grant_and_enqueued(
        self,
        auth_client: TestClient,
        captured: tuple[Admitted, uuid.UUID, str],
        seeded_tenant: SeededTenant,
        kernel: Session,
    ) -> None:
        admitted, order_id, _ = captured
        response = auth_client.post(
            f"/v1/orders/{order_id}/refunds",
            headers=_idem("refund"),
            json={"reason": "buyer_requested"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["decision"]["allowed"] is True, body["decision"]
        assert body["refund"]["amount_minor"] == admitted.amount.minor
        assert body["refund"]["state"] == "REFUND_PENDING"
        assert body["refund"]["automatic"] is False
        assert body["order"]["payment"]["state"] == "REFUND_PENDING"

        commands = outbox_of(kernel, seeded_tenant.tenant_id, "REFUND_EXECUTE")
        assert len(commands) == 1
        payload = commands[0].payload
        assert payload["refund_id"] == body["refund"]["refund_id"]
        assert payload["content_hash"] == admitted.content_hash
        assert payload["amount_minor"] == admitted.amount.minor

        _bind(kernel, seeded_tenant.tenant_id)
        grant = kernel.execute(
            text(
                "SELECT id, status, operation, refund_id, outbox_command_id "
                "FROM execution_grants WHERE tenant_id = :t AND operation = 'REFUND_EXECUTE'"
            ),
            {"t": seeded_tenant.tenant_id},
        ).one()
        assert grant.status == "ISSUED", "the grant is spent by the worker, never by the API"
        assert str(grant.refund_id) == body["refund"]["refund_id"], (
            "ADR 0003 D10: a refund grant is bound to its refund row, so a second "
            "partial refund is admissible after the first is consumed"
        )
        assert str(grant.outbox_command_id) == str(commands[0].id), (
            "link_command binds the grant to the one command that carries it"
        )
        assert str(grant.id) == payload["grant_id"]

    def test_a_second_refund_while_one_is_in_flight_is_a_200_denial(
        self, auth_client: TestClient, captured: tuple[Admitted, uuid.UUID, str]
    ) -> None:
        """ADR 0003 D15: a denial is the system working, so it is 200 with the decision."""
        _, order_id, _ = captured
        first = auth_client.post(
            f"/v1/orders/{order_id}/refunds",
            headers=_idem("r1"),
            json={"reason": "buyer_requested"},
        )
        assert first.json()["decision"]["allowed"] is True

        second = auth_client.post(
            f"/v1/orders/{order_id}/refunds",
            headers=_idem("r2"),
            json={"reason": "buyer_requested"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["decision"]["allowed"] is False
        assert second.json()["refund"] is None

    def test_an_agent_session_may_not_request_a_refund(
        self, mint_client: Any, captured: tuple[Admitted, uuid.UUID, str]
    ) -> None:
        """Consent about money is not delegable to the thing that proposed the purchase."""
        _, order_id, _ = captured
        agent, _ = mint_client(actor_type="AGENT")
        response = agent.post(
            f"/v1/orders/{order_id}/refunds",
            headers=_idem("agent"),
            json={"reason": "buyer_requested"},
        )
        assert response.status_code == 403
        assert response.json()["capability"] == "refund.request"

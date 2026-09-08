"""Build unit E: the timeline, the SSE stream, the proof chain, the inspector, step 11.

Every test here drives a **real journey** through the kernel against real PostgreSQL as
``commerce_test_kernel`` -- version 1 approved at 34000, a scenario injection raising the
price, admission refusing the stale approval and creating version 2, a fresh approval at
39500, one Execution Grant, one provider request, a verified webhook and a confirmed
order. Nothing is stubbed and no row is hand-written into a financial table: if the
evidence surfaces are wrong, they are wrong about data the kernel actually produced.

What is proven, in order:

* a complete journey's proof chain verifies ``COMPLETE`` with every applicable check ok;
* editing a stored audit payload through the owner connection is detected -- the chain
  verifier names the break at the right sequence number and the proof verdict fails;
* the timeline is ordered and carries the scenario injection, labelled;
* the SSE stream resumes from ``Last-Event-ID`` and replays nothing before it;
* retained revenue is exactly version 2's captured total minus version 1's stale one;
* the inspector shows one consumed grant against one provider mutation;
* evidence belongs to its buyer: another session gets 404, and merchant evidence needs
  the operator key.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_domain import Money, sha256_hex, uuid7
from commerce_protocols.core.evidence import AGGREGATE_TYPE as PROTOCOL_AGGREGATE
from durable_work.commands import CreateOrderCommand, enqueue_command
from fastapi.testclient import TestClient
from merchant_sim import InjectionKind, ScenarioInjection, StateDelta
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel.checkout_content import ContentLine, build_checkout_content
from transaction_kernel.checkouts import ReceiptInputs
from transaction_kernel.receipts import BuyerVisibleRef, PolicyKind, SaleTerm

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

MILK = "AMUL-DAIRY-001"
RICE = "INDI-STPL-001"

#: Version 1's total, approved by the buyer. Specification 31.1 step 5.
STALE_TOTAL = Money(34000, "INR")
#: Version 2's total after the injected price rise. Step 9.
CORRECTED_TOTAL = Money(39500, "INR")
#: What refusing the stale approval was worth. Step 11, and the arithmetic a test must own
#: rather than read back from the endpoint under test.
RETAINED_MINOR = CORRECTED_TOTAL.minor - STALE_TOTAL.minor

PROVIDER_ORDER_ID = "order_TESTEVIDENCE001"
PROVIDER_PAYMENT_ID = "pay_TESTEVIDENCE001"
PROVIDER_EVENT_ID = "evt_TESTEVIDENCE001"


# ------------------------------------------------------------------------- the journey


@dataclass(frozen=True, slots=True)
class Journey:
    """Identifiers for the seeded journey, so a test asserts against known values."""

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout_id: uuid.UUID
    buyer_ref: str
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    decision_id: uuid.UUID
    command_id: uuid.UUID
    inbox_id: uuid.UUID
    injection_id: uuid.UUID
    stale_hash: str
    corrected_hash: str


def _content(checkout_id: uuid.UUID, version: int, *, rice_minor: int) -> dict[str, Any]:
    """One canonical checkout document. Only the rice price moves between versions."""
    milk = ContentLine(MILK, "Toned milk 1 L", 2, 2800, 5600, 0)
    rice = ContentLine(RICE, "Basmati rice 5 kg", 1, rice_minor, rice_minor, 2400)
    subtotal = milk.line_minor + rice.line_minor
    return build_checkout_content(
        checkout_id=checkout_id,
        version=version,
        currency="INR",
        lines=(milk, rice),
        subtotal_minor=subtotal,
        tax_minor=2400,
        delivery_fee_minor=0,
        discount_minor=0,
        total_minor=subtotal + 2400,
        policy_version="pol-v12",
        catalogue_revision=version - 1,
        source_id="merchant-sim:demo-grocery/v1",
    )


def _receipt_inputs() -> ReceiptInputs:
    return ReceiptInputs(
        policies=tuple(
            SaleTerm(
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
                text_hash=sha256_hex(b"refund-policy-v12"),
            ),
        ),
    )


class FixedMerchant:
    """A merchant state source the test controls, exactly as the kernel's protocol expects.

    Holds the canonical content for the version admission will build next, so that the
    superseding version 2 is a real canonical document rather than the legacy minimal
    shape -- which is what the storefront and the proof verifier both read.
    """

    def __init__(self, checkout_id: uuid.UUID, total: Money, rice_minor: int) -> None:
        self._checkout_id = checkout_id
        self._total = total
        self._rice_minor = rice_minor

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> tk.CurrentMerchantState:
        content = _content(checkout_id, version + 1, rice_minor=self._rice_minor)
        return tk.CurrentMerchantState(
            total=self._total,
            line_items=content["line_items"],
            all_available=True,
            policy_version=content["policy_version"],
            content=content,
        )


@contextmanager
def kernel_tx(engine: Engine, tenant_id: uuid.UUID) -> Iterator[Session]:
    """One kernel-role transaction with the tenant bound as its first statement."""
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(tenant_id)})
            yield session
    finally:
        session.close()


@pytest.fixture
def journey(
    capi_kernel_engine: Engine,
    seeded_tenant: SeededTenant,
    demo_session: MintedSession,
) -> Journey:
    """Seed the eleven-step journey through the kernel, and return its identifiers.

    Each stage runs in its own committed transaction, because that is how the real
    service runs them: one request, one transaction. Driving the whole journey inside a
    single transaction would hide every ordering and visibility question this suite is
    supposed to answer.
    """
    tenant_id = seeded_tenant.tenant_id
    merchant_id = seeded_tenant.merchant_id
    buyer_ref = demo_session.buyer_ref
    cart_id, checkout_id = uuid7(), uuid7()
    correlation_id = uuid7()
    principal = tk.AgentPrincipal(
        principal_id=f"session:{demo_session.session_id}",
        tenant_id=tenant_id,
        actor_type=tk.ActorType.BUYER,
        merchant_id=merchant_id,
        buyer_ref=buyer_ref,
        capabilities=frozenset({"checkout.submit_approved"}),
        correlation_id=correlation_id,
    )

    # --- steps 1-4: cart, version 1, receipt, reservation, buyer approval -----------
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        session.execute(
            text(
                "INSERT INTO carts (id, tenant_id, merchant_id, buyer_ref, lines, status) "
                "VALUES (:id, :t, :m, :b, '[]'::jsonb, 'OPEN')"
            ),
            {"id": cart_id, "t": tenant_id, "m": merchant_id, "b": buyer_ref},
        )
        created = tk.create_checkout(
            session,
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            cart_id=cart_id,
            buyer_ref=buyer_ref,
            content=_content(checkout_id, 1, rice_minor=26000),
            correlation_id=correlation_id,
            checkout_id=checkout_id,
            principal=principal,
        )
        v1 = created.ref
        tk.freeze_for_approval(
            session,
            tenant_id=tenant_id,
            checkout=v1,
            receipt=_receipt_inputs(),
            correlation_id=correlation_id,
            principal=principal,
        )
        approval_v1 = tk.record_approval(
            session,
            tenant_id=tenant_id,
            checkout=v1,
            amount=STALE_TOTAL,
            principal=principal,
            correlation_id=correlation_id,
        )

    # --- step 5: the merchant moves underneath the approved checkout ------------------
    injection = ScenarioInjection(
        injection_id=uuid7(),
        kind=InjectionKind.PRICE_SET,
        deltas=(StateDelta(field="unit_price_minor", before=26000, after=31500),),
        revision_before=0,
        revision_after=1,
        injected_at=datetime.now(UTC),
        note="step 5: rice price rises while version 1 is approved",
        sku=RICE,
        currency="INR",
    )
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        tk.append(
            session,
            tenant=tenant_id,
            aggregate_type="merchant",
            aggregate_id=merchant_id,
            event_type="merchant.state_injected",
            actor_type=tk.ActorType.OPERATOR,
            principal_id="scenario-controller",
            payload=injection.to_audit_payload(),
            correlation_id=correlation_id,
        )
        session.execute(
            text(
                "INSERT INTO scenario_runs (id, tenant_id, merchant_id, injection_id, kind, "
                "payload) VALUES (:id, :t, :m, :i, :k, CAST(:p AS jsonb))"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "i": injection.injection_id,
                "k": injection.kind.value,
                "p": json.dumps(injection.to_audit_payload(), sort_keys=True),
            },
        )

    # --- steps 6-7: the kernel refuses version 1 and creates version 2 ----------------
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        denial = tk.admit(
            session,
            tk.AdmissionRequest(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout=v1,
                amount=STALE_TOTAL,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=f"submit-v1-{checkout_id.hex[:8]}",
                principal=principal,
                correlation_id=correlation_id,
                approval_id=approval_v1.approval_id,
            ),
            FixedMerchant(checkout_id, CORRECTED_TOTAL, 31500),
        )
    assert denial.allowed is False
    assert denial.code is tk.RecoveryCode.REAPPROVAL_REQUIRED
    assert denial.next_version == 2

    # --- step 8: version 2 gets its receipt and a fresh approval ----------------------
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        versions = tk.read_versions(session, tenant_id=tenant_id, checkout_id=checkout_id)
        v2 = next(v for v in versions if v.version == 2).ref
        tk.freeze_for_approval(
            session,
            tenant_id=tenant_id,
            checkout=v2,
            receipt=_receipt_inputs(),
            correlation_id=correlation_id,
            principal=principal,
        )
        approval_v2 = tk.record_approval(
            session,
            tenant_id=tenant_id,
            checkout=v2,
            amount=CORRECTED_TOTAL,
            principal=principal,
            correlation_id=correlation_id,
        )

    # --- step 9a: admission, one attempt, one grant, one durable command --------------
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        decision = tk.admit(
            session,
            tk.AdmissionRequest(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout=v2,
                amount=CORRECTED_TOTAL,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=f"submit-v2-{checkout_id.hex[:8]}",
                principal=principal,
                correlation_id=correlation_id,
                approval_id=approval_v2.approval_id,
            ),
            FixedMerchant(checkout_id, CORRECTED_TOTAL, 31500),
        )
        assert decision.allowed, decision.explanation
        assert decision.grant_id is not None
        assert decision.payment_attempt_id is not None
        attempt_id = decision.payment_attempt_id
        grant_id = decision.grant_id
        attempt = tk.read_attempt(session, tenant_id=tenant_id, payment_attempt_id=attempt_id)
        assert attempt is not None
        command = enqueue_command(
            session,
            CreateOrderCommand(
                tenant_id=str(tenant_id),
                payment_attempt_id=str(attempt_id),
                grant_id=str(grant_id),
                checkout_id=str(checkout_id),
                checkout_version=2,
                content_hash=v2.content_hash,
                amount_minor=CORRECTED_TOTAL.minor,
                currency=CORRECTED_TOTAL.currency,
                receipt=attempt.receipt,
                notes={
                    "tenant_id": str(tenant_id),
                    "checkout_id": str(checkout_id),
                    "payment_attempt_id": str(attempt_id),
                },
                correlation_id=str(correlation_id),
            ),
            idempotency_key=f"submit-v2-{checkout_id.hex[:8]}",
        )
        tk.link_command(
            session,
            tenant_id=tenant_id,
            grant_id=grant_id,
            outbox_command_id=command.command_id,
            correlation_id=correlation_id,
        )
        tk.transition(
            session,
            tenant_id=tenant_id,
            checkout=v2,
            target=tk.CheckoutState.EXECUTION_PENDING,
            reason="admitted",
            actor=tk.ActorType.SYSTEM,
            correlation_id=correlation_id,
        )

    # --- step 9b: the worker consumes the grant, then calls Razorpay ------------------
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        tk.consume_grant(
            session,
            grant_id,
            tk.GrantBinding(
                tenant_id=tenant_id,
                checkout=v2,
                payment_attempt_id=attempt_id,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                amount=CORRECTED_TOTAL,
            ),
        )
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        tk.record_provider_request(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            grant_id=grant_id,
            refund_id=None,
            operation=tk.Operation.PAYMENT_CREATE_ORDER,
            method="POST",
            url="https://api.razorpay.com/v1/orders",
            body_hash=sha256_hex(b"create-order-body"),
            header_names=["Authorization", "Content-Type", "X-Razorpay-Account"],
            http_status=200,
            provider_id=PROVIDER_ORDER_ID,
            outcome_code=tk.RecoveryCode.OK,
            provider_error_code=None,
            response_digest=sha256_hex(b"create-order-response"),
            transport_error=None,
            correlation_id=correlation_id,
        )
        tk.record_create_order_result(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            outcome=tk.payments.ProviderOrderOutcome(
                kind="ok",
                provider_order_id=PROVIDER_ORDER_ID,
                code=tk.RecoveryCode.OK,
                reason="order_created",
            ),
            correlation_id=correlation_id,
        )

    # --- step 10: a verified webhook confirms capture; the order is written -----------
    raw_body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": PROVIDER_PAYMENT_ID}}},
        },
        sort_keys=True,
    ).encode()
    inbox_id = uuid7()
    with kernel_tx(capi_kernel_engine, tenant_id) as session:
        session.execute(
            text(
                "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, provider_event_id, "
                "event_type, body_digest, raw_body, headers_redacted, signature_verified, "
                "payment_id, order_id) VALUES (:id, :t, :d, :e, 'payment.captured', :bd, :rb, "
                "CAST(:h AS jsonb), true, :p, :o)"
            ),
            {
                "id": inbox_id,
                "t": tenant_id,
                "d": f"evt:{PROVIDER_EVENT_ID}",
                "e": PROVIDER_EVENT_ID,
                "bd": sha256_hex(raw_body),
                "rb": raw_body,
                "h": json.dumps({"x-razorpay-event-id": PROVIDER_EVENT_ID}),
                "p": PROVIDER_PAYMENT_ID,
                "o": PROVIDER_ORDER_ID,
            },
        )
        applied = tk.apply_provider_evidence(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=attempt_id,
            evidence=tk.ProviderEvidence(
                source=tk.EvidenceSource.WEBHOOK,
                provider_payment_id=PROVIDER_PAYMENT_ID,
                provider_order_id=PROVIDER_ORDER_ID,
                amount_minor=CORRECTED_TOTAL.minor,
                currency=CORRECTED_TOTAL.currency,
                status="captured",
                provider_status="captured",
                raw_digest=sha256_hex(raw_body),
                event_id=PROVIDER_EVENT_ID,
            ),
            correlation_id=correlation_id,
        )
        assert applied.order_id is not None
        tk.record_webhook_applied(
            session,
            tenant_id=tenant_id,
            inbox_id=inbox_id,
            apply_status="APPLIED",
            apply_reason=applied.reason,
            state_before=applied.state_before,
            state_after=applied.state_after,
            changed=applied.changed,
            outbox_command_id=None,
            correlation_id=correlation_id,
        )

    return Journey(
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        checkout_id=checkout_id,
        buyer_ref=buyer_ref,
        attempt_id=attempt_id,
        grant_id=grant_id,
        decision_id=decision.decision_id,
        command_id=command.command_id,
        inbox_id=inbox_id,
        injection_id=injection.injection_id,
        stale_hash=v1.content_hash,
        corrected_hash=v2.content_hash,
    )


# ---------------------------------------------------------------------- the proof chain


def test_a_complete_journey_proves_complete_with_every_check_ok(
    auth_client: TestClient, journey: Journey
) -> None:
    """Step 10: the chain reaches COMPLETE and every applicable check holds.

    The verdict is not read from a stored field. ``content_hash_recomputed`` re-hashes the
    stored canonical document with ``tk.content_hash``, and ``audit_chain_*`` re-walks the
    streams with ``tk.verify_chain``; both are asserted present, so a future refactor that
    quietly stopped recomputing would fail here rather than keep reporting green.
    """
    response = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/proof")
    assert response.status_code == 200, response.text
    body = response.json()

    verdict = body["verdict"]
    assert verdict["tier"] == "COMPLETE", verdict
    assert verdict["ok"] is True, verdict["failed"]
    assert verdict["failed"] == []

    names = {check["name"] for check in verdict["checks"]}
    assert "content_hash_recomputed" in names
    assert "audit_chain_checkout" in names
    assert "audit_chain_payment_attempt" in names
    for check in verdict["checks"]:
        assert check["ok"] is True, check

    links = body["links"]
    assert links["3_checkout"]["content_hash"] == journey.corrected_hash
    assert links["3_checkout"]["version"] == 2
    assert links["5_approval"]["amount_minor"] == CORRECTED_TOTAL.minor
    assert links["6_kernel_decision"]["allowed"] is True
    assert links["7_grant_and_command"]["grant_id"] == str(journey.grant_id)
    assert links["7_grant_and_command"]["status"] == "CONSUMED"
    assert links["7_grant_and_command"]["consumed_at"] is not None
    assert links["7_grant_and_command"]["command"]["command_type"] == "PAYMENT_CREATE_ORDER"
    assert len(links["8_provider_requests"]) == 1
    assert links["8_provider_requests"][0]["grant_id"] == str(journey.grant_id)
    assert links["9_verified_evidence"]["capture_evidence"]["source"] == "WEBHOOK"
    assert links["10_final_state"]["payment"]["state"] == "CAPTURED"
    assert links["10_final_state"]["order"]["amount_minor"] == CORRECTED_TOTAL.minor
    assert body["audit_streams"]["checkout"]["intact"] is True


def test_the_export_format_carries_the_same_proof_as_an_attachment(
    auth_client: TestClient, journey: Journey
) -> None:
    """``?format=export`` is the inline body in a download envelope, never a second story."""
    inline = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/proof").json()
    export = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/proof?format=export")
    assert export.status_code == 200
    assert "attachment;" in export.headers["content-disposition"]
    assert export.json()["proof"] == inline


def test_tampering_with_a_stored_audit_payload_breaks_the_chain_and_the_verdict(
    auth_client: TestClient, journey: Journey, capi_admin_engine: Engine
) -> None:
    """Edit one committed audit payload as the database owner; both verifiers must notice.

    ``audit_events`` has no UPDATE grant for any application role, so this edit needs the
    owner connection -- which is exactly the threat the hash chain exists for. The check
    is that the break is reported *at the right sequence number*: a verifier that only
    said "something is wrong" would not tell an investigator where to look.
    """
    with capi_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(journey.tenant_id)})
        target_seq = conn.execute(
            text(
                "SELECT seq FROM audit_events WHERE tenant_id = :t AND aggregate_type = "
                "'checkout' AND aggregate_id = :c AND event_type = 'approval.recorded' "
                "ORDER BY seq LIMIT 1"
            ),
            {"t": journey.tenant_id, "c": journey.checkout_id},
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE audit_events SET payload = jsonb_set(payload, '{amount,minor}', "
                "'1') WHERE tenant_id = :t AND aggregate_type = 'checkout' "
                "AND aggregate_id = :c AND seq = :s"
            ),
            {"t": journey.tenant_id, "c": journey.checkout_id, "s": target_seq},
        )

    verified = auth_client.get(f"/v1/audit/streams/checkout/{journey.checkout_id}/verify")
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["intact"] is False
    assert body["code"] == "HUMAN_REVIEW_REQUIRED"
    assert body["first_break"]["kind"] == "SELF_HASH_MISMATCH"
    assert body["first_break"]["at_seq"] == target_seq
    assert body["events_verified"] == target_seq - 1

    proof = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/proof").json()
    assert proof["verdict"]["ok"] is False
    assert "audit_chain_checkout" in proof["verdict"]["failed"]
    assert proof["audit_streams"]["checkout"]["first_break"]["at_seq"] == target_seq


# -------------------------------------------------------------------------- the timeline


def test_the_timeline_is_ordered_and_labels_the_scenario_injection(
    auth_client: TestClient, journey: Journey
) -> None:
    """Specification 26.1 and 31.3 together: one order, and the demo apparatus named."""
    response = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/timeline")
    assert response.status_code == 200, response.text
    body = response.json()
    entries = body["entries"]
    assert entries, "a completed journey has a timeline"

    cursors = [entry["id"] for entry in entries]
    assert cursors == sorted(cursors), "cursors must be strictly sortable"
    assert len(set(cursors)) == len(cursors), "a cursor identifies exactly one row"
    stamps = [entry["occurred_at"] for entry in entries]
    assert stamps == sorted(stamps), "rows are ordered in time"
    assert body["cursor"] == cursors[-1]

    injections = [entry for entry in entries if entry["scenario_injection"]]
    assert len(injections) == 1, "exactly one merchant change was injected"
    assert body["scenario_injections"] == 1
    injected = injections[0]
    assert injected["source"] == "merchant"
    assert injected["details"]["label"] == "SCENARIO_INJECTION"
    assert injected["details"]["injection_id"] == str(journey.injection_id)

    sources = {entry["source"] for entry in entries}
    assert sources == {"checkout", "payment_attempt", "outbox_command", "webhook", "merchant"}

    actions = [entry["action"] for entry in entries]
    assert "admission.denied" in actions
    assert "admission.allowed" in actions
    assert "grant.linked" in actions
    assert "webhook.received:payment.captured" in actions

    denial = next(e for e in entries if e["action"] == "admission.denied")
    assert denial["decision"]["allowed"] is False
    assert denial["decision"]["code"] == "REAPPROVAL_REQUIRED"
    assert denial["decision"]["next_version"] == 2
    # The injection precedes the refusal it caused. That order is the demonstration.
    assert injected["id"] < denial["id"]


def test_the_timeline_redacts_secrets_and_shortens_hashes(
    auth_client: TestClient, journey: Journey
) -> None:
    """Specification 26.1's last line, checked on real payloads rather than a fixture."""
    entries = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/timeline").json()["entries"]
    created = next(e for e in entries if e["action"] == "checkout.version_created")
    assert created["content_hash"] is not None
    assert len(created["content_hash"]) < 64, "the timeline shows a hash shorthand"
    assert journey.corrected_hash not in json.dumps(entries), "no full digest in a timeline row"


def test_the_stream_resumes_from_last_event_id_without_replaying_earlier_rows(
    auth_client: TestClient, journey: Journey
) -> None:
    """Specification 24.2: resumption reads the database, never a remembered position.

    The cursor handed back is the id of a row in the middle of the journey. Everything the
    stream then sends must sort strictly after it and must equal the tail of the timeline
    -- so a client that reconnected mid-payment sees the rest exactly once.
    """
    entries = auth_client.get(f"/v1/checkouts/{journey.checkout_id}/timeline").json()["entries"]
    assert len(entries) > 4
    midpoint = entries[len(entries) // 2]
    expected = [e["id"] for e in entries if e["id"] > midpoint["id"]]

    streamed: list[str] = []
    completed = False
    with auth_client.stream(
        "GET",
        f"/v1/checkouts/{journey.checkout_id}/events",
        headers={"Last-Event-ID": midpoint["id"]},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        block: list[str] = []
        for line in response.iter_lines():
            # SSE frames are blank-line separated and their fields arrive in no
            # guaranteed order, so a frame is parsed whole rather than line by line.
            if line.strip():
                block.append(line)
                continue
            parts = [item.partition(":") for item in block]
            fields = {name.strip(): value.strip() for name, _, value in parts}
            block = []
            if fields.get("event") == "timeline":
                streamed.append(fields["id"])
            elif fields.get("event") == "complete":
                completed = True
                break

    assert completed, "a terminal checkout closes its own stream"
    assert streamed == expected
    assert all(cursor > midpoint["id"] for cursor in streamed)


# ------------------------------------------------------------------------ the inspector


def test_the_inspector_shows_one_consumed_grant_against_one_provider_mutation(
    auth_client: TestClient, journey: Journey
) -> None:
    """The invariant a reviewer came to check, on one page, with its rows beside it."""
    response = auth_client.get(f"/v1/inspector/payment-attempts/{journey.attempt_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["state"] == "CAPTURED"
    assert body["provider_order_id"] == PROVIDER_ORDER_ID
    assert body["provider_payment_id"] == PROVIDER_PAYMENT_ID

    assert len(body["grants"]) == 1
    grant = body["grants"][0]
    assert grant["grant_id"] == str(journey.grant_id)
    assert grant["status"] == "CONSUMED"
    assert grant["consumed_at"] is not None
    assert grant["outbox_command_id"] == str(journey.command_id)

    mutations = [
        row for row in body["provider_requests"] if row["operation"] == "PAYMENT_CREATE_ORDER"
    ]
    assert len(mutations) == 1
    assert mutations[0]["grant_id"] == str(journey.grant_id)
    assert "?" not in mutations[0]["url"]

    assert [c["command_type"] for c in body["commands"]] == ["PAYMENT_CREATE_ORDER"]
    assert len(body["webhook_deliveries"]) == 1
    assert body["webhook_deliveries"][0]["signature_verified"] is True
    assert body["webhook_deliveries"][0]["duplicate"] is False
    assert body["order"]["capture_evidence"]["source"] == "WEBHOOK"
    assert body["state_history"], "the attempt's moves come from the audit stream"
    assert any(row["state_after"] == "CAPTURED" for row in body["state_history"])

    for finding in body["findings"]:
        assert finding["ok"] is True, finding


# ------------------------------------------------------------------ retained revenue


def test_retained_revenue_matches_the_two_versions_arithmetic_exactly(
    auth_client: TestClient, journey: Journey, scenario_headers: dict[str, str]
) -> None:
    """Step 11. Every figure is a committed row and the difference is stated, not estimated."""
    response = auth_client.get(
        f"/v1/merchants/{journey.merchant_id}/evidence/retained-revenue",
        params={"checkout_id": str(journey.checkout_id)},
        headers=scenario_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["stale_version"] == 1
    assert body["stale_approved_minor"] == STALE_TOTAL.minor
    assert body["stale_invalidated_at"] is not None
    assert body["corrected_version"] == 2
    assert body["corrected_total_minor"] == CORRECTED_TOTAL.minor
    assert body["captured_minor"] == CORRECTED_TOTAL.minor
    assert body["captured_from"] == "WEBHOOK"
    assert body["difference_minor"] == RETAINED_MINOR
    assert body["net_retained_minor"] == CORRECTED_TOTAL.minor
    assert body["refunded_minor"] == 0
    assert body["direction"] == "MERCHANT"
    assert body["currency"] == "INR"
    # Specification 9.3: a figure produced by the scenario controller says so.
    assert body["controlled_scenario"] is True
    assert str(RETAINED_MINOR) in body["explanation"]


def test_retained_revenue_defaults_to_the_newest_refused_approval(
    auth_client: TestClient, journey: Journey, scenario_headers: dict[str, str]
) -> None:
    """A console with no fixture id asks for the latest refusal and gets the same figures."""
    response = auth_client.get(
        f"/v1/merchants/{journey.merchant_id}/evidence/retained-revenue",
        headers=scenario_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checkout_id"] == str(journey.checkout_id)
    assert body["stale_version"] == 1
    assert body["difference_minor"] == RETAINED_MINOR


def test_retained_revenue_without_any_checkout_is_a_404_problem(
    mint_client: Any, scenario_headers: dict[str, str]
) -> None:
    """A merchant that has never sold answers honestly rather than with a fixture."""
    operator, minted = mint_client(buyer_ref="operator-of-nothing")
    with operator as client:
        response = client.get(
            f"/v1/merchants/{minted.merchant_id}/evidence/retained-revenue",
            headers=scenario_headers,
        )
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "no refused approval" in response.json()["detail"]


def test_retained_revenue_is_not_buyer_facing(auth_client: TestClient, journey: Journey) -> None:
    """Without the operator key the route does not exist, rather than refusing visibly."""
    response = auth_client.get(
        f"/v1/merchants/{journey.merchant_id}/evidence/retained-revenue",
        params={"checkout_id": str(journey.checkout_id)},
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


# ------------------------------------------------------------------------------ access


def test_another_buyers_evidence_is_not_found_rather_than_forbidden(
    journey: Journey, mint_client: Any
) -> None:
    """404, never 403: a 403 would confirm the checkout exists to whoever guessed its id."""
    other_client, _ = mint_client(buyer_ref="someone-else")
    with other_client as stranger:
        for path in (
            f"/v1/checkouts/{journey.checkout_id}/timeline",
            f"/v1/checkouts/{journey.checkout_id}/proof",
            f"/v1/audit/streams/checkout/{journey.checkout_id}/verify",
            f"/v1/inspector/payment-attempts/{journey.attempt_id}",
        ):
            response = stranger.get(path)
            assert response.status_code == 404, (path, response.text)


def test_the_scenario_key_widens_access_within_the_same_tenant(
    journey: Journey, mint_client: Any, scenario_headers: dict[str, str]
) -> None:
    """An operator inspecting a journey they did not check out is the ADR's "or scenario key"."""
    other_client, _ = mint_client(buyer_ref="operator-viewing")
    with other_client as operator:
        response = operator.get(
            f"/v1/checkouts/{journey.checkout_id}/timeline", headers=scenario_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["entries"]


def test_an_unknown_audit_aggregate_type_is_refused(
    auth_client: TestClient, journey: Journey
) -> None:
    """The aggregate type is a path parameter, so the set of verifiable streams is closed."""
    response = auth_client.get(f"/v1/audit/streams/orders/{journey.checkout_id}/verify")
    assert response.status_code == 404
    assert "orders" in response.json()["detail"]


def test_the_protocol_stream_is_one_a_reviewer_can_actually_verify(
    auth_client: TestClient, scenario_headers: dict[str, str]
) -> None:
    """Two documents promise this route serves the protocol layer's evidence.

    ADR 0005 and the docstring of :mod:`commerce_protocols.core.evidence` both say the
    verifier is already shipped for ``protocol_interaction``. It was not in the allowlist,
    so a reviewer following either was told the stream is not a verifiable type -- a
    written claim with nothing executing it, which is the only kind that can be wrong for
    a long time.

    The id here has no stream behind it on purpose. What is being asserted is that the
    route *accepts the type*: an empty stream verifies as intact and of zero length, which
    is the correct answer to "has this chain been tampered with" when there is no chain.
    A 404 would be the answer to a different question.
    """
    response = auth_client.get(
        f"/v1/audit/streams/{PROTOCOL_AGGREGATE}/{uuid.uuid4()}/verify",
        headers=scenario_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["intact"] is True


def test_the_protocol_stream_is_operator_only(auth_client: TestClient) -> None:
    """Widening the allowlist must not widen who may read it.

    ``checkout`` and ``payment_attempt`` are ownership-checked; every other type falls to
    the operator branch. A buyer session asking for a protocol stream is refused there, not
    at the allowlist, so this is the assertion that the new entry did not open a door.

    The refusal is 404 rather than 403 on purpose, and the detail is the operator key
    rather than the stream: an endpoint that answers "forbidden" has confirmed it exists.
    """
    response = auth_client.get(f"/v1/audit/streams/{PROTOCOL_AGGREGATE}/{uuid.uuid4()}/verify")
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "This endpoint requires the scenario operator key."

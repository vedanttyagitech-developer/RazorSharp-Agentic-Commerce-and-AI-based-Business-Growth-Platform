"""Applying stored webhooks: once, monotonically, and never for somebody else's payment.

Razorpay documents duplicate delivery as normal and gives no ordering guarantee, so both
are treated here as ordinary traffic. What must never happen is a second state change from
a repeated event, or a capture rewound by an authorization that arrives after it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_domain import uuid7
from durable_work import ApplyWebhookEventCommand
from durable_worker.handlers.apply_webhook import handle_apply_webhook
from durable_worker.handlers.create_order import handle_create_order
from durable_worker.settings import WorkerRuntime
from sqlalchemy import text
from sqlalchemy.orm import Session

from conftest import (
    Admitted,
    FakeTransport,
    attempt_of,
    checkout_status,
    json_response,
    order_entity,
    payment_entity,
    store_webhook,
    webhook_body,
)

pytestmark = pytest.mark.db

ORDER_ID = "order_DWKtest000001"
PAYMENT_ID = "pay_DWKtest00001"


def given_order_created(
    runtime: WorkerRuntime, transport: FakeTransport, admitted: Admitted
) -> None:
    """Put the attempt where a webhook can reach it: a provider order exists."""
    transport.extend(
        [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
    )
    handle_create_order(runtime, admitted.create_order_command())


def apply_stored(
    runtime: WorkerRuntime,
    session: Session,
    admitted: Admitted,
    body: dict[str, Any],
    *,
    event_id: str = "evt_DWKtest0001",
    signature_verified: bool = True,
) -> tuple[uuid.UUID, Any]:
    """Store one delivery as the receiver would, commit it, then apply it."""
    inbox_id = store_webhook(
        session,
        tenant_id=admitted.tenant_id,
        body=body,
        event_id=event_id,
        signature_verified=signature_verified,
    )
    session.commit()
    result = handle_apply_webhook(
        runtime,
        ApplyWebhookEventCommand(
            tenant_id=str(admitted.tenant_id),
            inbox_id=str(inbox_id),
            correlation_id=str(admitted.correlation_id),
        ),
        outbox_command_id=uuid7(),
    )
    return inbox_id, result


def inbox_row(session: Session, inbox_id: uuid.UUID) -> Any:
    return session.execute(
        text(
            "SELECT apply_status, apply_reason, state_before, state_after, changed "
            "FROM webhook_inbox WHERE id = :i"
        ),
        {"i": inbox_id},
    ).one()


def captured_body(admitted: Admitted) -> dict[str, Any]:
    return webhook_body(
        "payment.captured",
        payment=payment_entity(
            payment_id=PAYMENT_ID,
            order_id=ORDER_ID,
            amount=admitted.amount.minor,
            status="captured",
        ),
    )


class TestCapture:
    def test_a_verified_capture_pays_the_checkout_and_writes_one_order(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_order_created(runtime, transport, admitted)
        session = kernel_session(admitted.tenant_id)

        inbox_id, result = apply_stored(runtime, session, admitted, captured_body(admitted))

        assert result.code is tk.RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        attempt = attempt_of(after, admitted)
        assert attempt.status is tk.PaymentState.CAPTURED
        assert attempt.provider_payment_id == PAYMENT_ID
        assert checkout_status(after, admitted) == tk.CheckoutState.PAID.value
        orders = after.execute(
            text("SELECT count(*) FROM orders WHERE payment_attempt_id = :a"),
            {"a": admitted.attempt_id},
        ).scalar_one()
        assert orders == 1
        row = inbox_row(after, inbox_id)
        assert row.apply_status == "APPLIED"
        assert row.changed is True
        assert row.state_after == tk.PaymentState.CAPTURED.value

    def test_a_redelivered_command_for_the_same_row_changes_nothing(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_order_created(runtime, transport, admitted)
        session = kernel_session(admitted.tenant_id)
        inbox_id, _ = apply_stored(runtime, session, admitted, captured_body(admitted))

        again = handle_apply_webhook(
            runtime,
            ApplyWebhookEventCommand(
                tenant_id=str(admitted.tenant_id),
                inbox_id=str(inbox_id),
                correlation_id=str(admitted.correlation_id),
            ),
            outbox_command_id=uuid7(),
        )

        assert again.code is tk.RecoveryCode.DUPLICATE_OPERATION
        after = kernel_session(admitted.tenant_id)
        orders = after.execute(
            text("SELECT count(*) FROM orders WHERE payment_attempt_id = :a"),
            {"a": admitted.attempt_id},
        ).scalar_one()
        assert orders == 1


class TestMonotonicity:
    def test_a_late_authorized_after_a_capture_does_not_regress_the_attempt(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """Razorpay does not guarantee ordering, so this delivery is routine, not an error.

        ``monotonic_apply`` is what makes it a no-op: the attempt stays ``CAPTURED`` and
        the inbox row records that the event was received and changed nothing.
        """
        given_order_created(runtime, transport, admitted)
        session = kernel_session(admitted.tenant_id)
        apply_stored(runtime, session, admitted, captured_body(admitted))

        late = webhook_body(
            "payment.authorized",
            payment=payment_entity(
                payment_id=PAYMENT_ID,
                order_id=ORDER_ID,
                amount=admitted.amount.minor,
                status="authorized",
            ),
        )
        inbox_id, result = apply_stored(
            runtime, kernel_session(admitted.tenant_id), admitted, late, event_id="evt_DWKlate01"
        )

        assert result.code is tk.RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.CAPTURED
        row = inbox_row(after, inbox_id)
        assert row.apply_status == "APPLIED"
        assert row.changed is False
        assert row.state_after == tk.PaymentState.CAPTURED.value


class TestCorrelation:
    def test_an_event_for_an_unknown_attempt_is_ignored_rather_than_failed(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_order_created(runtime, transport, admitted)
        stranger = webhook_body(
            "payment.captured",
            payment=payment_entity(
                payment_id="pay_SOMEONEELSE1",
                order_id="order_SOMEONEELSE1",
                amount=admitted.amount.minor,
                status="captured",
            ),
        )

        inbox_id, result = apply_stored(
            runtime, kernel_session(admitted.tenant_id), admitted, stranger
        )

        assert result.code is tk.RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.SUBMITTED
        row = inbox_row(after, inbox_id)
        assert row.apply_status == "IGNORED"
        assert row.apply_reason == "attempt_not_found"

    def test_the_order_notes_correlate_a_delivery_with_no_provider_identifiers(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """The notes are ours: the create-order command put the attempt id in them.

        This is the last resort, and it is the reason the command carries those notes at
        all -- a delivery whose provider identifiers do not correlate can still be matched
        to the attempt that caused it.
        """
        given_order_created(runtime, transport, admitted)
        body = webhook_body(
            "payment.captured",
            payment=payment_entity(
                payment_id=PAYMENT_ID,
                amount=admitted.amount.minor,
                status="captured",
                notes=admitted.notes,
            ),
        )
        del body["payload"]["payment"]["entity"]["order_id"]

        inbox_id, result = apply_stored(runtime, kernel_session(admitted.tenant_id), admitted, body)

        assert result.code is tk.RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.CAPTURED
        assert inbox_row(after, inbox_id).apply_status == "APPLIED"


class TestRefusals:
    def test_a_row_whose_signature_never_verified_is_failed_and_applied_to_nothing(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_order_created(runtime, transport, admitted)

        inbox_id, result = apply_stored(
            runtime,
            kernel_session(admitted.tenant_id),
            admitted,
            captured_body(admitted),
            signature_verified=False,
        )

        assert result.code is tk.RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.SUBMITTED
        row = inbox_row(after, inbox_id)
        assert row.apply_status == "FAILED"
        assert row.apply_reason == "signature_not_verified"

    def test_an_event_carrying_no_payment_state_is_ignored(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_order_created(runtime, transport, admitted)
        body = webhook_body(
            "refund.speed_changed",
            order=order_entity(order_id=ORDER_ID, receipt=admitted.receipt),
        )

        inbox_id, result = apply_stored(runtime, kernel_session(admitted.tenant_id), admitted, body)

        assert result.code is tk.RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.SUBMITTED
        row = inbox_row(after, inbox_id)
        assert row.apply_status == "IGNORED"
        assert row.apply_reason.startswith("no_payment_evidence")

"""Refunds: the stored key goes out, and a lost answer is never resent.

Specification 10.6 and 11.4. The two refund failure states are not interchangeable, and
every test here exists because conflating them is how a buyer is paid back twice.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import transaction_kernel as tk
from action_executor.handlers.apply_webhook import handle_apply_webhook
from action_executor.handlers.create_order import handle_create_order
from action_executor.handlers.reconcile import handle_reconcile_refund
from action_executor.handlers.refund import handle_refund_execute
from action_executor.settings import WorkerRuntime, WorkerSettings, build_runtime
from commerce_domain import ActorType, AgentPrincipal, RecoveryCode, uuid7
from durable_work import ApplyWebhookEventCommand, ReconcileRefundCommand, RefundExecuteCommand
from payment_adapters import IDEMPOTENCY_HEADER, TransportTimeoutError
from sqlalchemy import text
from sqlalchemy.orm import Session

from conftest import (
    Admitted,
    FakeTransport,
    attempt_of,
    json_response,
    order_entity,
    outbox_types,
    payment_entity,
    provider_requests,
    store_webhook,
    webhook_body,
)

pytestmark = pytest.mark.db

ORDER_ID = "order_DWKtest000001"
PAYMENT_ID = "pay_DWKtest00001"
REFUND_ID = "rfnd_DWKtest0001"


def given_captured(
    runtime: WorkerRuntime, transport: FakeTransport, admitted: Admitted, session: Session
) -> None:
    """Drive the attempt to a real capture: an order, then a verified webhook."""
    transport.extend(
        [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
    )
    handle_create_order(runtime, admitted.create_order_command())

    inbox_id = store_webhook(
        session,
        tenant_id=admitted.tenant_id,
        body=webhook_body(
            "payment.captured",
            payment=payment_entity(
                payment_id=PAYMENT_ID,
                order_id=ORDER_ID,
                amount=admitted.amount.minor,
                status="captured",
            ),
        ),
    )
    session.commit()
    handle_apply_webhook(
        runtime,
        ApplyWebhookEventCommand(
            tenant_id=str(admitted.tenant_id),
            inbox_id=str(inbox_id),
            correlation_id=str(admitted.correlation_id),
        ),
        outbox_command_id=uuid7(),
    )


def admit_refund(session: Session, admitted: Admitted) -> RefundExecuteCommand:
    """Admit a full refund the way the buyer-confirmed route will, and build its command."""
    admission = tk.admit_refund(
        session,
        tenant_id=admitted.tenant_id,
        payment_attempt_id=admitted.attempt_id,
        amount=None,
        reason_code="buyer_request",
        principal=AgentPrincipal(
            principal_id="buyer-dwk",
            tenant_id=admitted.tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=admitted.merchant_id,
            capabilities=frozenset({"refund.request"}),
        ),
        correlation_id=admitted.correlation_id,
    )
    assert admission.allowed, admission.decision
    assert admission.refund_id and admission.grant_id and admission.idem_key
    session.commit()
    return RefundExecuteCommand(
        tenant_id=str(admitted.tenant_id),
        refund_id=str(admission.refund_id),
        payment_attempt_id=str(admitted.attempt_id),
        grant_id=str(admission.grant_id),
        checkout_id=str(admitted.checkout_id),
        checkout_version=admitted.version,
        content_hash=admitted.content_hash,
        amount_minor=admitted.amount.minor,
        currency=admitted.amount.currency,
        idem_key=admission.idem_key,
        correlation_id=str(admitted.correlation_id),
    )


def refund_row(session: Session, refund_id: str) -> Any:
    return session.execute(
        text("SELECT status, provider_refund_id, idem_key FROM refunds WHERE id = :r"),
        {"r": uuid.UUID(refund_id)},
    ).one()


def processed_refund(amount: int) -> dict[str, Any]:
    return {
        "id": REFUND_ID,
        "entity": "refund",
        "amount": amount,
        "currency": "INR",
        "payment_id": PAYMENT_ID,
        "status": "processed",
    }


class TestExecution:
    def test_the_kernel_key_is_what_goes_on_the_wire(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """One derivation, in the kernel, stored on the row. The worker only carries it."""
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([json_response(200, processed_refund(admitted.amount.minor))])

        handle_refund_execute(runtime, command)

        sent = transport.requests[-1]
        after = kernel_session(admitted.tenant_id)
        assert sent.headers[IDEMPOTENCY_HEADER] == refund_row(after, command.refund_id).idem_key
        assert sent.method == "POST"

    def test_a_processed_refund_settles_the_row_and_the_attempt(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([json_response(200, processed_refund(admitted.amount.minor))])

        result = handle_refund_execute(runtime, command)

        assert result.followups == ()
        after = kernel_session(admitted.tenant_id)
        row = refund_row(after, command.refund_id)
        assert row.status == "PROCESSED"
        assert row.provider_refund_id == REFUND_ID
        assert attempt_of(after, admitted).status is tk.PaymentState.REFUNDED

    def test_the_request_is_recorded_against_the_grant_and_the_refund(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([json_response(200, processed_refund(admitted.amount.minor))])

        handle_refund_execute(runtime, command)

        after = kernel_session(admitted.tenant_id)
        refund_requests = [
            row
            for row in provider_requests(after, admitted)
            if row.operation == tk.Operation.REFUND_EXECUTE.value
        ]
        assert len(refund_requests) == 1
        assert refund_requests[0].grant_id == uuid.UUID(command.grant_id)


class TestUnknownRefund:
    def test_a_timeout_is_refund_unknown_and_enqueues_reconciliation(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([TransportTimeoutError("refund timed out")])

        result = handle_refund_execute(runtime, command)

        assert result.followups == ("RECONCILE_REFUND",)
        after = kernel_session(admitted.tenant_id)
        assert refund_row(after, command.refund_id).status == "UNKNOWN"
        assert attempt_of(after, admitted).status is tk.PaymentState.REFUND_UNKNOWN
        assert "RECONCILE_REFUND" in outbox_types(after, admitted.tenant_id)

    def test_a_second_delivery_never_sends_a_second_refund(
        self,
        worker_settings: WorkerSettings,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """The bug this whole handler is arranged to prevent, asserted directly."""
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([TransportTimeoutError("refund timed out")])
        handle_refund_execute(runtime, command)

        # An empty script: any provider call at all is an AssertionError.
        second = FakeTransport()
        result = handle_refund_execute(build_runtime(worker_settings, transport=second), command)

        assert second.call_count == 0
        assert result.code is RecoveryCode.OK
        after = kernel_session(admitted.tenant_id)
        refunds = after.execute(
            text("SELECT count(*) FROM refunds WHERE payment_attempt_id = :a"),
            {"a": admitted.attempt_id},
        ).scalar_one()
        assert refunds == 1
        sent = [
            row
            for row in provider_requests(after, admitted)
            if row.operation == tk.Operation.REFUND_EXECUTE.value
        ]
        assert len(sent) == 1


class TestConfirmedFailure:
    def test_a_refused_refund_is_refund_failed_and_may_be_re_admitted(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([json_response(400, {"error": {"code": "BAD_REQUEST_ERROR"}})])

        result = handle_refund_execute(runtime, command)

        assert result.followups == ()
        after = kernel_session(admitted.tenant_id)
        assert refund_row(after, command.refund_id).status == "FAILED"
        assert attempt_of(after, admitted).status is tk.PaymentState.REFUND_FAILED


class TestRefundReconciliation:
    def test_evidence_about_another_order_escalates_instead_of_being_retried(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """Mismatched evidence goes to a person. Asking again cannot make it match.

        The payment sibling has always caught `EvidenceMismatchError` and escalated; this
        path did not, and the difference cost the buyer their refund rather than a retry.

        Uncaught, the exception carried no `.code`, so the loop's `_code_for` fell through
        to its default `CONCURRENT_OPERATION` -- which is *retryable*. The outbox scheduled
        a backoff and redelivered up to `max_attempts`, roughly four hours, then buried the
        command. Nothing on that path escalated, no reconciliation run was recorded, and
        not even the read was written down: the provider was asked the same question eight
        times with no trace it had been asked once. The refund stayed UNKNOWN and the
        attempt stayed REFUND_UNKNOWN, and a fresh `admit_refund` is refused
        `RECONCILIATION_IN_PROGRESS` -- so no route remained by which the money could come
        back.

        Retrying was wrong twice: mismatched evidence does not become matched by asking
        again, and evidence about somebody else's checkout must never settle this one.
        """
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([TransportTimeoutError("refund timed out")])
        handle_refund_execute(runtime, command)

        # The provider answers with a payment that belongs to somebody else's order.
        transport.extend(
            [
                json_response(
                    200,
                    payment_entity(
                        payment_id=PAYMENT_ID,
                        order_id="order_SOMEONEELSE1",
                        amount=admitted.amount.minor,
                        status="captured",
                        amount_refunded=0,
                    ),
                )
            ]
        )
        result = handle_reconcile_refund(
            runtime,
            ReconcileRefundCommand(
                tenant_id=str(admitted.tenant_id),
                refund_id=command.refund_id,
                payment_attempt_id=command.payment_attempt_id,
                reason="refund_unknown",
                attempt_number=1,
                correlation_id=str(admitted.correlation_id),
            ),
        )

        # Handled, not raised. A raised exception is what made this retryable.
        assert result.code is RecoveryCode.OK
        assert result.detail == "refund_reconciliation.evidence_mismatch"

        after = kernel_session(admitted.tenant_id)
        assert refund_row(after, command.refund_id).status == "ESCALATED", (
            "a refund nobody can verify must reach a person, not a backoff"
        )
        # The read is written down even though it settled nothing. A provider asked a
        # question and answered it; an audit that omits that cannot explain the escalation.
        reads = after.execute(
            text(
                "SELECT outcome_code, provider_error_code FROM provider_requests "
                "WHERE tenant_id = :t AND refund_id = :r"
            ),
            {"t": admitted.tenant_id, "r": command.refund_id},
        ).all()
        assert any(row.outcome_code == RecoveryCode.HUMAN_REVIEW_REQUIRED.value for row in reads), (
            "the mismatched read left no provider_requests row"
        )

    def test_a_refund_the_provider_never_made_is_verified_absent(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """Only a verified absence permits another attempt, and it is a fresh admission.

        The evidence is the payment's own refunded total measured against what the local
        ledger has settled: equal totals mean this refund did not land.
        """
        session = kernel_session(admitted.tenant_id)
        given_captured(runtime, transport, admitted, session)
        command = admit_refund(kernel_session(admitted.tenant_id), admitted)
        transport.extend([TransportTimeoutError("refund timed out")])
        handle_refund_execute(runtime, command)

        transport.extend(
            [
                json_response(
                    200,
                    payment_entity(
                        payment_id=PAYMENT_ID,
                        order_id=ORDER_ID,
                        amount=admitted.amount.minor,
                        status="captured",
                        amount_refunded=0,
                    ),
                )
            ]
        )
        result = handle_reconcile_refund(
            runtime,
            ReconcileRefundCommand(
                tenant_id=str(admitted.tenant_id),
                refund_id=command.refund_id,
                payment_attempt_id=command.payment_attempt_id,
                reason="refund_unknown",
                attempt_number=1,
                correlation_id=str(admitted.correlation_id),
            ),
        )

        assert result.detail == "refund_verified_absent"
        after = kernel_session(admitted.tenant_id)
        assert refund_row(after, command.refund_id).status == "FAILED"
        assert attempt_of(after, admitted).status is tk.PaymentState.REFUND_FAILED


def test_provider_pending_refund_is_polled_to_completion(
    runtime, transport, admitted, kernel_session
):
    given_captured(runtime, transport, admitted, kernel_session(admitted.tenant_id))
    command = admit_refund(kernel_session(admitted.tenant_id), admitted)
    pending = processed_refund(admitted.amount.minor)
    pending["status"] = "pending"
    transport.extend([json_response(200, pending)])
    result = handle_refund_execute(runtime, command)
    assert result.followups == ("RECONCILE_REFUND",)
    assert refund_row(kernel_session(admitted.tenant_id), command.refund_id).status == "UNKNOWN"
    transport.extend([json_response(200, processed_refund(admitted.amount.minor))])
    handle_reconcile_refund(
        runtime,
        ReconcileRefundCommand(
            tenant_id=str(admitted.tenant_id),
            refund_id=command.refund_id,
            payment_attempt_id=command.payment_attempt_id,
            reason="refund_pending",
            attempt_number=1,
            correlation_id=str(admitted.correlation_id),
        ),
    )
    assert refund_row(kernel_session(admitted.tenant_id), command.refund_id).status == "PROCESSED"

    calls = transport.call_count
    handle_refund_execute(runtime, command)
    assert transport.call_count == calls, "A processed refund must never be sent again"

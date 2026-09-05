"""Applying stored webhooks: once, monotonically, and never for somebody else's payment.

Razorpay documents duplicate delivery as normal and gives no ordering guarantee, so both
are treated here as ordinary traffic. What must never happen is a second state change from
a repeated event, or a capture rewound by an authorization that arrives after it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_domain import uuid7
from durable_work import ApplyWebhookEventCommand
from durable_worker.handlers.apply_webhook import handle_apply_webhook
from durable_worker.handlers.create_order import handle_create_order
from durable_worker.handlers.stale_capture import admit_stale_refund
from durable_worker.settings import WorkerRuntime
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel.safe_mode import ModeChangeReason, enter_safe_mode

from conftest import (
    Admitted,
    FakeTransport,
    attempt_of,
    audit_types,
    checkout_status,
    grant_command_for_refund,
    invalidate_open_checkout,
    json_response,
    order_entity,
    outbox_commands,
    outbox_types,
    payment_entity,
    refunds_of,
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


def already_refunded_body(admitted: Admitted, *, refunded: int | None = None) -> dict[str, Any]:
    """A capture Razorpay reports as already refunded, in whole or in part.

    Provider status ``refunded`` classifies as *captured* evidence
    (``apply_webhook._STATUS_CLASSIFICATION``) because the capture is a fact the attempt
    must record; ``amount_refunded`` carries what came back. On an invalidated checkout
    that is a stale capture whose money is already home.
    """
    return webhook_body(
        "payment.captured",
        payment=payment_entity(
            payment_id=PAYMENT_ID,
            order_id=ORDER_ID,
            amount=admitted.amount.minor,
            status="refunded",
            amount_refunded=admitted.amount.minor if refunded is None else refunded,
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
        # The negative control for the stale-capture branch below: an ordinary capture
        # refunds nothing and enqueues nothing, so the new branch cannot leak into the
        # path that pays the merchant.
        assert result.followups == ()
        after = kernel_session(admitted.tenant_id)
        attempt = attempt_of(after, admitted)
        assert attempt.status is tk.PaymentState.CAPTURED
        assert attempt.provider_payment_id == PAYMENT_ID
        assert checkout_status(after, admitted) == tk.CheckoutState.PAID.value
        assert outbox_types(after, admitted.tenant_id) == []
        assert refunds_of(after, admitted) == []
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
        assert again.followups == ()
        after = kernel_session(admitted.tenant_id)
        orders = after.execute(
            text("SELECT count(*) FROM orders WHERE payment_attempt_id = :a"),
            {"a": admitted.attempt_id},
        ).scalar_one()
        assert orders == 1
        assert outbox_types(after, admitted.tenant_id) == []


@pytest.fixture
def mode_history_cleanup(admitted: Admitted, dwk_admin_engine: Engine) -> Iterator[None]:
    """Remove this tenant's operating-mode records before the tenant itself goes.

    ``platform_operating_modes`` sits outside row-level security, so the shared
    ``admitted`` fixture does not sweep it, but a tenant-scoped record still holds a
    foreign key onto ``tenants``. Depending on ``admitted`` is what orders this teardown
    before its.
    """
    yield
    with dwk_admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM platform_operating_modes WHERE tenant_id = :t"),
            {"t": admitted.tenant_id},
        )


class TestStaleCapture:
    """A capture that lands after the checkout was invalidated, specification 31.2.

    The demanding half is the count. The platform must refund the buyer exactly once and
    fulfil nothing, and it must hold that line against a provider that duplicates
    deliveries and against two workers reaching the same attempt at once.
    """

    def given_a_late_capture_is_possible(
        self, runtime: WorkerRuntime, transport: FakeTransport, admitted: Admitted, kernel_session
    ) -> None:  # noqa: ANN001
        """A provider order exists, and then the merchant's state moves under it."""
        given_order_created(runtime, transport, admitted)
        invalidate_open_checkout(kernel_session(admitted.tenant_id), admitted)

    def test_a_late_capture_on_an_invalidated_checkout_admits_exactly_one_refund_and_no_order(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)

        inbox_id, result = apply_stored(
            runtime, kernel_session(admitted.tenant_id), admitted, captured_body(admitted)
        )

        assert result.code is tk.RecoveryCode.OK
        assert result.followups == ("REFUND_EXECUTE",)
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.REFUND_PENDING
        orders = after.execute(
            text("SELECT count(*) FROM orders WHERE payment_attempt_id = :a"),
            {"a": admitted.attempt_id},
        ).scalar_one()
        assert orders == 0, "an invalidated version is never fulfilled, whatever the money did"

        rows = refunds_of(after, admitted)
        assert len(rows) == 1
        assert rows[0].reason_code == "STALE_CAPTURE"
        assert rows[0].amount_minor == admitted.amount.minor
        assert rows[0].currency == admitted.amount.currency

        commands = outbox_commands(after, admitted.tenant_id, "REFUND_EXECUTE")
        assert outbox_types(after, admitted.tenant_id) == ["REFUND_EXECUTE"]
        assert grant_command_for_refund(after, admitted.tenant_id, rows[0].id) == commands[0], (
            "the grant must name the command that carries it, or the admission and the "
            "command did not commit together"
        )

        row = inbox_row(after, inbox_id)
        assert row.apply_status == "APPLIED"
        assert row.state_after == tk.PaymentState.STALE_CAPTURE.value, (
            "the inbox row records what the delivery did; REFUND_PENDING is the "
            "platform's own consequence and belongs on the attempt's stream"
        )

    def test_a_redelivered_late_capture_admits_no_second_refund(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """Razorpay redelivering the same capture must not refund the buyer twice.

        The second delivery is a *different* inbox row, so the handler's own claim-under-
        lock does not cover it. What covers it is monotonicity: the first delivery left
        the attempt in ``REFUND_PENDING``, which outranks ``STALE_CAPTURE``, so the kernel
        joins the capture to the state already reached, reports ``stale_capture`` false,
        and the refund branch is never entered a second time.
        """
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)
        apply_stored(runtime, kernel_session(admitted.tenant_id), admitted, captured_body(admitted))

        _, again = apply_stored(
            runtime,
            kernel_session(admitted.tenant_id),
            admitted,
            captured_body(admitted),
            event_id="evt_DWKstale02",
        )

        assert again.code is tk.RecoveryCode.OK
        assert again.followups == ()
        assert again.detail == "no_change.refund_pending"
        after = kernel_session(admitted.tenant_id)
        assert len(refunds_of(after, admitted)) == 1
        assert outbox_types(after, admitted.tenant_id) == ["REFUND_EXECUTE"]

    def test_a_second_worker_racing_the_same_stale_capture_admits_nothing(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The layer underneath monotonicity, reached directly because nothing else can.

        A reconciliation round racing a webhook is the case where two callers both see a
        stale capture. The helper is called here as that second caller would call it, and
        the kernel's duplicate branch is what holds: the existing refund is returned,
        nothing is written, no command is enqueued, and the grant is left pointing at the
        command that already carries it. Enqueueing here would send a second refund;
        linking here would raise and roll back a webhook that was applied correctly.
        """
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)
        apply_stored(runtime, kernel_session(admitted.tenant_id), admitted, captured_body(admitted))
        before = kernel_session(admitted.tenant_id)
        existing = refunds_of(before, admitted)[0]
        linked = grant_command_for_refund(before, admitted.tenant_id, existing.id)

        racing = kernel_session(admitted.tenant_id)
        detail, followups = admit_stale_refund(
            racing,
            tenant_id=admitted.tenant_id,
            payment_attempt_id=admitted.attempt_id,
            correlation_id=admitted.correlation_id,
            learned_from=tk.EvidenceSource.PROVIDER_FETCH.value,
            refund_reported=False,
        )
        racing.commit()

        assert detail == "stale_refund.already_admitted"
        assert followups == ()
        after = kernel_session(admitted.tenant_id)
        assert [row.id for row in refunds_of(after, admitted)] == [existing.id]
        assert outbox_types(after, admitted.tenant_id) == ["REFUND_EXECUTE"]
        assert grant_command_for_refund(after, admitted.tenant_id, existing.id) == linked
        assert "worker.stale_refund_skipped" in audit_types(
            after, admitted.tenant_id, admitted.checkout_id
        ), "a decision not to refund a second time must be visible on the timeline"

    def test_a_capture_the_provider_already_refunded_is_not_refunded_again(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The money is already with the buyer, so sending it again would pay twice.

        The kernel's remaining amount comes from the platform's own ``refunds`` rows, and
        an operator refunding from the Razorpay dashboard writes none -- so the admission
        would read the full capture as outstanding. The evidence that triggers the branch
        is the same evidence that says the money went back, and this is the branch that
        reads it. A case is opened instead, because a provider refund the platform has no
        row for is a ledger only a person can reconcile.
        """
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)

        _, result = apply_stored(
            runtime, kernel_session(admitted.tenant_id), admitted, already_refunded_body(admitted)
        )

        assert result.code is tk.RecoveryCode.OK
        assert result.followups == (), "no REFUND_EXECUTE for money that already came back"
        after = kernel_session(admitted.tenant_id)
        assert refunds_of(after, admitted) == []
        assert outbox_types(after, admitted.tenant_id) == []
        assert result.detail.endswith("stale_refund.provider_already_refunded")
        assert attempt_of(after, admitted).status is tk.PaymentState.STALE_CAPTURE, (
            "escalate does not freeze an attempt in which money has moved, so the "
            "explicit refund path stays open to a reviewer"
        )
        assert "human_review.opened" in audit_types(
            after, admitted.tenant_id, admitted.checkout_id
        ), "withholding the automatic refund must hand the case to a person, not drop it"
        assert "worker.stale_refund_withheld" in audit_types(
            after, admitted.tenant_id, admitted.checkout_id
        )

    def test_a_partially_refunded_capture_is_not_refunded_in_full(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """Half the money is back, and the automatic path cannot answer the other half.

        This is the case that makes escalation the right answer rather than silence. The
        buyer is genuinely owed the difference, but ``admit_stale_capture_refund`` refuses
        anything but a full refund, so a correct amount is not available to this path at
        all. Refunding the full capture would return 150% of it.
        """
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)
        half = admitted.amount.minor // 2

        _, result = apply_stored(
            runtime,
            kernel_session(admitted.tenant_id),
            admitted,
            already_refunded_body(admitted, refunded=half),
        )

        assert result.followups == ()
        after = kernel_session(admitted.tenant_id)
        assert refunds_of(after, admitted) == [], (
            "a full refund here would return more than the merchant ever captured"
        )
        assert outbox_types(after, admitted.tenant_id) == []
        assert "human_review.opened" in audit_types(after, admitted.tenant_id, admitted.checkout_id)

    def test_a_redelivered_provider_refund_opens_no_second_case(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The withheld branch is re-entered on redelivery, unlike the admitted one.

        Nothing moved the attempt off ``STALE_CAPTURE``, so the second delivery raises
        ``stale_capture`` again and reaches this branch again -- where the admitted branch
        would have been shielded by ``REFUND_PENDING`` outranking it. What holds here is
        ``escalate``'s own case-key idempotency, and the skip of the detail event when it
        reports ``opened`` false.
        """
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)
        apply_stored(
            runtime, kernel_session(admitted.tenant_id), admitted, already_refunded_body(admitted)
        )

        _, again = apply_stored(
            runtime,
            kernel_session(admitted.tenant_id),
            admitted,
            already_refunded_body(admitted),
            event_id="evt_DWKstale09",
        )

        assert again.code is tk.RecoveryCode.OK
        assert again.followups == ()
        after = kernel_session(admitted.tenant_id)
        assert refunds_of(after, admitted) == []
        assert outbox_types(after, admitted.tenant_id) == []
        withheld = [
            event
            for event in audit_types(after, admitted.tenant_id, admitted.checkout_id)
            if event == "worker.stale_refund_withheld"
        ]
        assert withheld == ["worker.stale_refund_withheld"], (
            "one case, one explanation, however many times the provider redelivers it"
        )

    @pytest.mark.usefixtures("mode_history_cleanup")
    def test_the_refund_still_happens_with_safe_mode_on(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """Specification 31.2 keeps refunds available while delegated debits are denied.

        Safe Mode exists to stop machine-initiated spending, not to strand a buyer's
        money. ``REFUND_EXECUTE`` is in ``SAFE_MODE_PERMITTED`` and refund grants are in
        ``NEVER_SWEPT``, so the incident must make no difference here at all.
        """
        self.given_a_late_capture_is_possible(runtime, transport, admitted, kernel_session)
        declaring = kernel_session(admitted.tenant_id)
        enter_safe_mode(
            declaring,
            tenant=admitted.tenant_id,
            reason=ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
            actor="operator-dwk",
        )
        declaring.commit()

        _, result = apply_stored(
            runtime, kernel_session(admitted.tenant_id), admitted, captured_body(admitted)
        )

        assert result.followups == ("REFUND_EXECUTE",)
        after = kernel_session(admitted.tenant_id)
        rows = refunds_of(after, admitted)
        assert len(rows) == 1
        assert rows[0].amount_minor == admitted.amount.minor
        assert attempt_of(after, admitted).status is tk.PaymentState.REFUND_PENDING
        assert grant_command_for_refund(after, admitted.tenant_id, rows[0].id) is not None, (
            "a refund grant is never swept, so the command that spends it stays linked"
        )

    def test_a_denial_reports_itself_without_failing_the_delivery(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The helper is called directly because no webhook can reach this branch.

        Inside the handler all three denials are unreachable by construction: the attempt
        was moved to ``STALE_CAPTURE`` in this very transaction, and Safe Mode permits
        ``REFUND_EXECUTE``. Driving the refusal through the handler would be a test that
        cannot fail. What matters is that a refusal is a *return value*: raising would
        roll back the applied capture and the inbox row, leaving Razorpay to redeliver
        into a handler that refuses again forever.
        """
        given_order_created(runtime, transport, admitted)
        apply_stored(runtime, kernel_session(admitted.tenant_id), admitted, captured_body(admitted))
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.CAPTURED

        detail, followups = admit_stale_refund(
            session,
            tenant_id=admitted.tenant_id,
            payment_attempt_id=admitted.attempt_id,
            correlation_id=admitted.correlation_id,
            learned_from=tk.EvidenceSource.WEBHOOK.value,
            refund_reported=False,
        )
        session.commit()

        assert detail == "stale_refund_denied.not_a_stale_capture"
        assert followups == ()
        after = kernel_session(admitted.tenant_id)
        assert refunds_of(after, admitted) == [], "a healthy capture is not refundable by this path"
        assert outbox_types(after, admitted.tenant_id) == []
        assert attempt_of(after, admitted).status is tk.PaymentState.CAPTURED
        assert "refund.denied" in audit_types(after, admitted.tenant_id, admitted.attempt_id), (
            "the kernel puts the refusal on the attempt's chain, so it is explainable "
            "afterwards even though the handler carried it home as an OK"
        )


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

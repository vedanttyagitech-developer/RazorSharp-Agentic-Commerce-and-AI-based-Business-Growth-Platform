"""Reconciliation: the only exit from an unknown outcome, and never a second create.

Specification 10.7. Each test here answers one question about a payment whose result was
lost: what does the platform ask, what does it conclude, and what does it refuse to do.
The refusal is the important half -- every provider call in this file is a ``GET``.
"""

from __future__ import annotations

from typing import Any

import pytest
import transaction_kernel as tk
from durable_work import ReconcilePaymentCommand
from durable_worker.handlers.create_order import handle_create_order
from durable_worker.handlers.reconcile import handle_reconcile_payment
from durable_worker.settings import WorkerRuntime
from payment_adapters import TransportTimeoutError
from sqlalchemy import text
from sqlalchemy.orm import Session

from conftest import (
    Admitted,
    FakeTransport,
    attempt_of,
    checkout_status,
    json_response,
    order_entity,
    outbox_types,
    payment_entity,
    provider_requests,
)

pytestmark = pytest.mark.db

ORDER_ID = "order_DWKtest000001"
PAYMENT_ID = "pay_DWKtest00001"


def given_lost_create(runtime: WorkerRuntime, transport: FakeTransport, admitted: Admitted) -> None:
    """The demonstration's step 9 failure: the create-order response never came back."""
    transport.extend([TransportTimeoutError("create order timed out")])
    handle_create_order(runtime, admitted.create_order_command())


def round_command(admitted: Admitted, number: int = 1) -> ReconcilePaymentCommand:
    return ReconcilePaymentCommand(
        tenant_id=str(admitted.tenant_id),
        payment_attempt_id=str(admitted.attempt_id),
        reason="create_order_unknown.no_response",
        attempt_number=number,
        correlation_id=str(admitted.correlation_id),
    )


def lookup_hit(admitted: Admitted) -> dict[str, Any]:
    return {
        "entity": "collection",
        "count": 1,
        "items": [
            order_entity(order_id=ORDER_ID, receipt=admitted.receipt, amount=admitted.amount.minor)
        ],
    }


def payments_list(admitted: Admitted, *, status: str = "captured") -> dict[str, Any]:
    return {
        "entity": "collection",
        "count": 1,
        "items": [
            payment_entity(
                payment_id=PAYMENT_ID,
                order_id=ORDER_ID,
                amount=admitted.amount.minor,
                status=status,
            )
        ],
    }


def runs(session: Session, admitted: Admitted) -> list[Any]:
    return list(
        session.execute(
            text(
                "SELECT attempt_number, reason, decision, resulting_transition "
                "FROM reconciliation_runs WHERE tenant_id = :t AND payment_attempt_id = :a "
                "ORDER BY attempt_number"
            ),
            {"t": admitted.tenant_id, "a": admitted.attempt_id},
        ).all()
    )


class TestRecovery:
    def test_a_lost_create_is_found_by_receipt_and_settled_from_provider_truth(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """The whole point of the stable receipt: the order is found, not created again."""
        given_lost_create(runtime, transport, admitted)
        transport.extend(
            [json_response(200, lookup_hit(admitted)), json_response(200, payments_list(admitted))]
        )

        result = handle_reconcile_payment(runtime, round_command(admitted))

        assert result.followups == ()
        session = kernel_session(admitted.tenant_id)
        attempt = attempt_of(session, admitted)
        assert attempt.status is tk.PaymentState.CAPTURED
        assert attempt.provider_order_id == ORDER_ID
        assert checkout_status(session, admitted) == tk.CheckoutState.PAID.value

    def test_every_reconciliation_call_is_a_read(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_lost_create(runtime, transport, admitted)
        transport.extend(
            [json_response(200, lookup_hit(admitted)), json_response(200, payments_list(admitted))]
        )

        handle_reconcile_payment(runtime, round_command(admitted))

        methods = [request.method for request in transport.requests[1:]]
        assert methods == ["GET", "GET"], "a reconciliation never re-sends the mutation"
        session = kernel_session(admitted.tenant_id)
        reads = [row for row in provider_requests(session, admitted) if row.grant_id is None]
        assert [row.operation for row in reads] == ["ORDER_LOOKUP", "ORDER_PAYMENTS_FETCH"]
        assert all("?" not in row.url for row in reads), (
            "the receipt travels in identifiers_queried, never in a stored URL"
        )

    def test_the_round_is_recorded_with_its_number_and_decision(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_lost_create(runtime, transport, admitted)
        transport.extend(
            [json_response(200, lookup_hit(admitted)), json_response(200, payments_list(admitted))]
        )

        handle_reconcile_payment(runtime, round_command(admitted))

        session = kernel_session(admitted.tenant_id)
        recorded = runs(session, admitted)
        assert len(recorded) == 1
        assert recorded[0].attempt_number == 1
        assert recorded[0].decision == "evidence.captured"
        assert recorded[0].resulting_transition == "RECONCILING->CAPTURED"


class TestVerifiedAbsence:
    def test_an_order_the_provider_never_created_is_never_created_again_here(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """A verified absence permits a *new admission*, which is the API's decision.

        The worker records what it learned and schedules the next round; it has no branch
        that mints a grant or sends a second create, because a retry is a fresh admission
        by design (specification 10.6).
        """
        given_lost_create(runtime, transport, admitted)
        transport.extend([json_response(200, {"entity": "collection", "count": 0, "items": []})])

        result = handle_reconcile_payment(runtime, round_command(admitted))

        assert result.followups == ("RECONCILE_PAYMENT",)
        assert [request.method for request in transport.requests[1:]] == ["GET"]
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.RECONCILING
        assert runs(session, admitted)[0].decision == "order_verified_absent"
        assert outbox_types(session, admitted.tenant_id) == [
            "RECONCILE_PAYMENT",
            "RECONCILE_PAYMENT",
        ]


class TestBound:
    def test_the_sixth_round_escalates_instead_of_scheduling_a_seventh(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_lost_create(runtime, transport, admitted)
        transport.extend([TransportTimeoutError("lookup timed out")])

        result = handle_reconcile_payment(runtime, round_command(admitted, number=6))

        assert result.followups == ()
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.ESCALATED
        assert runs(session, admitted)[0].attempt_number == 6
        assert outbox_types(session, admitted.tenant_id) == ["RECONCILE_PAYMENT"]

    def test_a_redelivered_round_does_not_advance_the_count(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """The unique index on the run is what keeps the bound an honest number."""
        given_lost_create(runtime, transport, admitted)
        empty = {"entity": "collection", "count": 0, "items": []}
        transport.extend([json_response(200, empty), json_response(200, empty)])

        handle_reconcile_payment(runtime, round_command(admitted))
        handle_reconcile_payment(runtime, round_command(admitted))

        session = kernel_session(admitted.tenant_id)
        assert [row.attempt_number for row in runs(session, admitted)] == [1]


class TestMismatch:
    def test_a_payment_belonging_to_another_order_escalates_and_settles_nothing(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_lost_create(runtime, transport, admitted)
        foreign = {
            "entity": "collection",
            "count": 1,
            "items": [
                payment_entity(
                    payment_id="pay_SOMEONEELSE1",
                    order_id="order_SOMEONEELSE1",
                    amount=admitted.amount.minor,
                    status="captured",
                )
            ],
        }
        transport.extend([json_response(200, lookup_hit(admitted)), json_response(200, foreign)])

        result = handle_reconcile_payment(runtime, round_command(admitted))

        assert result.followups == ()
        session = kernel_session(admitted.tenant_id)
        attempt = attempt_of(session, admitted)
        assert attempt.status is tk.PaymentState.ESCALATED
        assert attempt.provider_payment_id is None, (
            "evidence about another checkout is never recorded against this one"
        )
        assert runs(session, admitted)[0].decision == "evidence_mismatch.escalated"


class TestAlreadyResolved:
    def test_a_round_arriving_after_a_webhook_settled_the_attempt_does_nothing(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        given_lost_create(runtime, transport, admitted)
        transport.extend(
            [json_response(200, lookup_hit(admitted)), json_response(200, payments_list(admitted))]
        )
        handle_reconcile_payment(runtime, round_command(admitted))

        # A second command for the same attempt, now that it is CAPTURED. The transport
        # has nothing scripted, so any provider call at all would fail this test.
        result = handle_reconcile_payment(runtime, round_command(admitted, number=2))

        assert result.code is tk.RecoveryCode.DUPLICATE_OPERATION
        assert result.detail == "already_resolved.captured"
        session = kernel_session(admitted.tenant_id)
        assert [row.attempt_number for row in runs(session, admitted)] == [1]


class TestBuyerReturned:
    """The buyer came back saying they paid -- the other reason a round is enqueued.

    ADR 0003 D8 makes the browser's word a claim and not evidence, and the API keeps that
    promise by recording it as ``BROWSER_CALLBACK`` and enqueuing a round to go and ask
    Razorpay. These tests exist because that round used to arrive on a ``SUBMITTED``
    attempt -- exactly where a *successful* create-order leaves it -- and be refused as
    ``not_reconcilable.submitted``. Nothing was asked and nothing was written, so on a
    stack Razorpay cannot reach with a webhook the refusal to trust the browser had
    nothing left to defer to and no payment could ever be captured.
    """

    def given_submitted_and_returned(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """A create-order that *worked*, then the buyer's return recorded as a claim."""
        transport.extend([json_response(200, lookup_hit(admitted)["items"][0])])
        handle_create_order(runtime, admitted.create_order_command())

        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.SUBMITTED
        verdict = tk.record_browser_callback(
            session,
            tenant_id=admitted.tenant_id,
            payment_attempt_id=admitted.attempt_id,
            provider_order_id=ORDER_ID,
            provider_payment_id=PAYMENT_ID,
            correlation_id=admitted.correlation_id,
            principal_id="session:test",
        )
        assert verdict.accepted
        # The claim moved nothing. That is the invariant the rest of this rests on.
        assert attempt_of(session, admitted).status is tk.PaymentState.SUBMITTED
        session.commit()

    def returned_command(self, admitted: Admitted, number: int = 1) -> ReconcilePaymentCommand:
        return ReconcilePaymentCommand(
            tenant_id=str(admitted.tenant_id),
            payment_attempt_id=str(admitted.attempt_id),
            reason="browser_callback",
            attempt_number=number,
            correlation_id=str(admitted.correlation_id),
        )

    def test_a_return_on_a_submitted_attempt_is_settled_from_the_provider(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        self.given_submitted_and_returned(runtime, transport, admitted, kernel_session)
        # One GET of the payment the browser named. The order id is already on the attempt,
        # so there is nothing to look up by receipt.
        transport.extend(
            [
                json_response(
                    200,
                    payment_entity(
                        payment_id=PAYMENT_ID, order_id=ORDER_ID, amount=admitted.amount.minor
                    ),
                )
            ]
        )

        result = handle_reconcile_payment(runtime, self.returned_command(admitted))

        assert result.code is tk.RecoveryCode.OK
        session = kernel_session(admitted.tenant_id)
        attempt = attempt_of(session, admitted)
        assert attempt.status is tk.PaymentState.CAPTURED
        assert checkout_status(session, admitted) == tk.CheckoutState.PAID.value

        # Captured from the fetch, never from the browser -- D8's whole claim.
        evidence = session.execute(
            text("SELECT capture_evidence FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a"),
            {"t": admitted.tenant_id, "a": admitted.attempt_id},
        ).scalar_one()
        assert evidence["source"] == "PROVIDER_FETCH"

        # And the read stays a read: no grant is consumed to ask a question.
        assert [row.method for row in provider_requests(session, admitted)] == ["POST", "GET"]

    def test_the_round_is_recorded_and_writes_exactly_one_order(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        self.given_submitted_and_returned(runtime, transport, admitted, kernel_session)
        transport.extend(
            [
                json_response(
                    200,
                    payment_entity(
                        payment_id=PAYMENT_ID, order_id=ORDER_ID, amount=admitted.amount.minor
                    ),
                )
            ]
        )
        handle_reconcile_payment(runtime, self.returned_command(admitted))

        # A redelivered round. The transport has nothing left scripted, so any second
        # provider call fails here; the attempt is CAPTURED and the guard now says so.
        second = handle_reconcile_payment(runtime, self.returned_command(admitted, number=2))

        assert second.code is tk.RecoveryCode.DUPLICATE_OPERATION
        assert second.detail == "already_resolved.captured"
        session = kernel_session(admitted.tenant_id)
        orders = session.execute(
            text("SELECT count(*) FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a"),
            {"t": admitted.tenant_id, "a": admitted.attempt_id},
        ).scalar_one()
        assert orders == 1, "a replayed round must not write a second order"
        assert [row.attempt_number for row in runs(session, admitted)] == [1]

    def test_a_return_the_provider_has_not_captured_yet_schedules_another_round(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """`created` is not `captured`. The platform keeps asking rather than concluding."""
        self.given_submitted_and_returned(runtime, transport, admitted, kernel_session)
        transport.extend(
            [
                json_response(
                    200,
                    payment_entity(
                        payment_id=PAYMENT_ID,
                        order_id=ORDER_ID,
                        amount=admitted.amount.minor,
                        status="created",
                    ),
                )
            ]
        )

        result = handle_reconcile_payment(runtime, self.returned_command(admitted))

        assert result.followups == ("RECONCILE_PAYMENT",)
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.SUBMITTED
        assert checkout_status(session, admitted) != tk.CheckoutState.PAID.value
        assert (
            session.execute(
                text("SELECT count(*) FROM orders WHERE tenant_id = :t AND payment_attempt_id = :a"),
                {"t": admitted.tenant_id, "a": admitted.attempt_id},
            ).scalar_one()
            == 0
        ), "an uncaptured payment writes no order, whatever the browser said"

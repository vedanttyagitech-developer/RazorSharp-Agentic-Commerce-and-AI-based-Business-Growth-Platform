"""The create-order handler: one grant, one request, and never a second order.

Every test here is about the same sentence -- *every provider mutation consumes exactly
one Execution Grant, consumed in a committed transaction before the network call* -- and
the first two are the ones that would catch a regression that costs a buyer money.
"""

from __future__ import annotations

import uuid

import pytest
import transaction_kernel as tk
from durable_worker.handlers.create_order import handle_create_order
from durable_worker.settings import WorkerRuntime, WorkerSettings, build_runtime
from payment_adapters import HttpRequest, HttpResponse, TransportTimeoutError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from conftest import (
    SET_TENANT,
    Admitted,
    FakeTransport,
    attempt_of,
    audit_types,
    checkout_status,
    json_response,
    order_entity,
    outbox_types,
    provider_requests,
)

pytestmark = pytest.mark.db

ORDER_ID = "order_DWKtest000001"


def _created_order(admitted: Admitted, *, amount: int | None = None) -> dict[str, object]:
    return order_entity(
        order_id=ORDER_ID,
        receipt=admitted.receipt,
        amount=admitted.amount.minor if amount is None else amount,
    )


class GrantWatchingTransport(FakeTransport):
    """Reads the grant's committed status from a *separate* connection at send time.

    This is the explicit ordering proof. The handler's consumption runs in its own
    transaction and commits before the send; a second connection therefore observes
    ``CONSUMED`` at the moment the request goes out. Were the consumption ever moved into
    the same transaction as the recording -- the tempting simplification -- this
    connection would still see ``ISSUED``, because an uncommitted update is invisible to
    it, and this assertion is what would say so.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        tenant_id: uuid.UUID,
        grant_id: uuid.UUID,
        script: list[HttpResponse | Exception],
    ) -> None:
        super().__init__(script)
        self._engine = engine
        self._tenant_id = tenant_id
        self._grant_id = grant_id
        self.grant_status_at_send: str | None = None

    def send(self, request: HttpRequest) -> HttpResponse:
        session = Session(self._engine)
        try:
            session.begin()
            session.execute(SET_TENANT, {"t": str(self._tenant_id)})
            self.grant_status_at_send = session.execute(
                text("SELECT status FROM execution_grants WHERE id = :g"),
                {"g": self._grant_id},
            ).scalar_one()
            session.rollback()
        finally:
            session.close()
        return super().send(request)


class TestOrdering:
    def test_grant_is_consumed_and_committed_before_the_transport_is_called(
        self,
        worker_settings: WorkerSettings,
        admitted: Admitted,
        dwk_kernel_engine: Engine,
    ) -> None:
        watcher = GrantWatchingTransport(
            dwk_kernel_engine,
            tenant_id=admitted.tenant_id,
            grant_id=admitted.grant_id,
            script=[json_response(200, _created_order(admitted))],
        )
        runtime = build_runtime(worker_settings, transport=watcher)

        handle_create_order(runtime, admitted.create_order_command())

        assert watcher.call_count == 1
        assert watcher.grant_status_at_send == "CONSUMED", (
            "the grant must be CONSUMED and committed before the provider is called; "
            "a crash after the send must find it spent"
        )

    def test_the_audit_chain_records_consumption_before_the_request(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([json_response(200, _created_order(admitted))])

        handle_create_order(runtime, admitted.create_order_command())

        session = kernel_session(admitted.tenant_id)
        events = audit_types(session, admitted.tenant_id, admitted.checkout_id)
        assert events.index("worker.grant_consumed") < events.index("provider.request_recorded")


class TestSuccess:
    def test_records_the_order_and_moves_both_states(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([json_response(200, _created_order(admitted))])

        result = handle_create_order(runtime, admitted.create_order_command())

        assert result.code is tk.RecoveryCode.OK
        assert result.followups == ()
        session = kernel_session(admitted.tenant_id)
        attempt = attempt_of(session, admitted)
        assert attempt.status is tk.PaymentState.SUBMITTED
        assert attempt.provider_order_id == ORDER_ID
        assert checkout_status(session, admitted) == tk.CheckoutState.AWAITING_PAYMENT.value

    def test_records_exactly_one_provider_request_naming_its_grant(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([json_response(200, _created_order(admitted))])

        handle_create_order(runtime, admitted.create_order_command())

        session = kernel_session(admitted.tenant_id)
        requests = provider_requests(session, admitted)
        assert len(requests) == 1
        assert requests[0].operation == tk.Operation.PAYMENT_CREATE_ORDER.value
        assert requests[0].grant_id == admitted.grant_id
        assert requests[0].http_status == 200
        assert "?" not in requests[0].url

    def test_sends_integer_paise_and_the_kernel_receipt(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
    ) -> None:
        transport.extend([json_response(200, _created_order(admitted))])

        handle_create_order(runtime, admitted.create_order_command())

        body = transport.requests[0].body.decode("utf-8")
        assert f'"amount":{admitted.amount.minor}' in body
        assert f'"receipt":"{admitted.receipt}"' in body
        assert len(admitted.receipt) <= 40
        assert str(admitted.attempt_id) in body, "notes must carry the attempt for correlation"


class TestRedelivery:
    def test_a_second_delivery_calls_the_transport_zero_times(
        self,
        worker_settings: WorkerSettings,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        first = FakeTransport([json_response(200, _created_order(admitted))])
        handle_create_order(
            build_runtime(worker_settings, transport=first), admitted.create_order_command()
        )

        # A transport with an empty script raises on any call at all.
        second = FakeTransport()
        result = handle_create_order(
            build_runtime(worker_settings, transport=second), admitted.create_order_command()
        )

        assert second.call_count == 0
        assert result.code is tk.RecoveryCode.DUPLICATE_OPERATION
        session = kernel_session(admitted.tenant_id)
        assert len(provider_requests(session, admitted)) == 1
        assert attempt_of(session, admitted).provider_order_id == ORDER_ID

    def test_a_consumed_grant_with_no_recorded_outcome_becomes_unknown_and_reconciles(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """The crash-in-the-gap case: consumed, then the process died before recording.

        The worker cannot know whether the request was sent, so the only honest state is
        ``UNKNOWN`` -- resolved by looking the order up by receipt, never by sending again.
        """
        arrange = kernel_session(admitted.tenant_id)
        tk.consume_grant(
            arrange,
            admitted.grant_id,
            admitted.create_order_command().grant_binding(),
        )
        arrange.commit()

        result = handle_create_order(runtime, admitted.create_order_command())

        assert runtime.transport.call_count == 0  # type: ignore[attr-defined]
        assert result.code is tk.RecoveryCode.OK
        assert result.followups == ("RECONCILE_PAYMENT",)
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.UNKNOWN
        assert outbox_types(session, admitted.tenant_id) == ["RECONCILE_PAYMENT"]


class TestUnknownOutcome:
    def test_a_timeout_leaves_the_attempt_unknown_and_enqueues_reconciliation(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([TransportTimeoutError("create order timed out")])

        result = handle_create_order(runtime, admitted.create_order_command())

        assert result.followups == ("RECONCILE_PAYMENT",)
        session = kernel_session(admitted.tenant_id)
        attempt = attempt_of(session, admitted)
        assert attempt.status is tk.PaymentState.UNKNOWN
        assert attempt.provider_order_id is None
        assert checkout_status(session, admitted) == tk.CheckoutState.PAYMENT_UNKNOWN.value
        assert outbox_types(session, admitted.tenant_id) == ["RECONCILE_PAYMENT"]

    def test_a_timeout_still_records_the_provider_request(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([TransportTimeoutError("create order timed out")])

        handle_create_order(runtime, admitted.create_order_command())

        session = kernel_session(admitted.tenant_id)
        request = provider_requests(session, admitted)[0]
        assert request.http_status is None
        assert request.transport_error == "TransportError"
        assert request.outcome_code == tk.RecoveryCode.PAYMENT_UNKNOWN.value

    def test_a_5xx_is_unknown_and_never_failed(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([json_response(502, {"error": {"code": "SERVER_ERROR"}})])

        handle_create_order(runtime, admitted.create_order_command())

        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.UNKNOWN


class TestConfirmedFailure:
    def test_a_refused_request_fails_the_attempt_and_releases_the_hold(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([json_response(400, {"error": {"code": "BAD_REQUEST_ERROR"}})])

        result = handle_create_order(runtime, admitted.create_order_command())

        assert result.followups == ()
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.FAILED
        assert checkout_status(session, admitted) == tk.CheckoutState.PAYMENT_FAILED.value
        held = session.execute(
            text("SELECT status FROM reservations WHERE tenant_id = :t AND checkout_id = :c"),
            {"t": admitted.tenant_id, "c": admitted.checkout_id},
        ).scalar_one()
        assert held != "ACTIVE"


class TestEchoMismatch:
    def test_an_order_for_a_different_amount_escalates_and_does_not_reconcile(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        transport.extend([json_response(200, _created_order(admitted, amount=1))])

        result = handle_create_order(runtime, admitted.create_order_command())

        assert result.followups == ()
        session = kernel_session(admitted.tenant_id)
        assert attempt_of(session, admitted).status is tk.PaymentState.ESCALATED
        assert outbox_types(session, admitted.tenant_id) == []

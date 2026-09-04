"""The tick, the transport, the sweeps, and the demo's fault injector.

Three groups. :class:`TestTransport` needs neither a database nor a network: ``httpx``'s
own mock transport answers, so the guarantees the adapter depends on -- one send, no
retry, every status returned, every failure raised as the right type -- are asserted
directly. The rest drive a real tick against a real database.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import transaction_kernel as tk
from commerce_domain import uuid7
from durable_work import ReconcilePaymentCommand
from durable_worker.faults import FaultKind
from durable_worker.handlers.create_order import handle_create_order
from durable_worker.handlers.housekeeping import run_housekeeping
from durable_worker.loop import TenantRef, list_tenants, run_once
from durable_worker.main import build_parser
from durable_worker.settings import WorkerRuntime, WorkerSettings, build_runtime
from durable_worker.transport import HttpxTransport, safe_url
from payment_adapters import (
    ConfigurationError,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
    TransportTimeoutError,
)
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from conftest import (
    Admitted,
    FakeTransport,
    arm_fault,
    attempt_of,
    audit_types,
    enqueue,
    json_response,
    order_entity,
    outbox_types,
    provider_requests,
)

ORDER_ID = "order_DWKtest000001"


# ------------------------------------------------------------------------ transport


def _transport_over(handler: Any) -> HttpxTransport:
    """An :class:`HttpxTransport` whose socket is replaced by a Python callable."""
    return HttpxTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))


class TestTransport:
    def test_it_satisfies_the_adapter_protocol(self) -> None:
        assert isinstance(_transport_over(lambda _r: httpx.Response(200)), HttpTransport)

    def test_a_4xx_comes_back_as_a_response_rather_than_an_exception(self) -> None:
        """A 400 is information the adapter needs -- "definitely not performed" -- not an error."""
        transport = _transport_over(lambda _r: httpx.Response(400, json={"error": {"code": "x"}}))

        response = transport.send(HttpRequest(method="POST", url="https://example.invalid/v1"))

        assert isinstance(response, HttpResponse)
        assert response.status == 400
        assert not response.is_success

    def test_a_timeout_raises_transport_timeout_error(self) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(TransportTimeoutError):
            _transport_over(timeout).send(
                HttpRequest(method="POST", url="https://example.invalid/v1")
            )

    def test_any_other_failure_raises_transport_error(self) -> None:
        """An unclassified failure must read as "unknown", never as "nothing was sent"."""

        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(TransportError):
            _transport_over(refused).send(
                HttpRequest(method="POST", url="https://example.invalid/v1")
            )

    def test_it_sends_exactly_one_request(self) -> None:
        """A transport that retried a POST would turn one order into two, invisibly."""
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "order_1"})

        _transport_over(record).send(
            HttpRequest(method="POST", url="https://example.invalid/v1", body=b'{"a":1}')
        )

        assert len(seen) == 1
        assert seen[0].content == b'{"a":1}'

    def test_basic_auth_is_applied_at_send_time(self) -> None:
        headers: dict[str, str] = {}

        def record(request: httpx.Request) -> httpx.Response:
            headers.update(request.headers)
            return httpx.Response(200)

        _transport_over(record).send(
            HttpRequest(
                method="GET",
                url="https://example.invalid/v1",
                auth=("rzp_test_key", "secret-material"),
            )
        )

        assert headers["authorization"].startswith("Basic ")

    def test_safe_url_keeps_the_resource_and_drops_the_parameters(self) -> None:
        assert (
            safe_url("https://api.razorpay.com/v1/orders?receipt=rcpt_abc")
            == "https://api.razorpay.com/v1/orders"
        )


# ----------------------------------------------------------------------- the tick


class OutboxWatchingTransport(FakeTransport):
    """Reads the leased command's committed status from a separate connection.

    The lease must be committed before the handler runs: a lease transaction held open
    across a provider call holds row locks for the length of a network round trip, and
    every other worker steps over rows it could have been running.
    """

    def __init__(self, engine: Engine, tenant_id: uuid.UUID, script: list[Any]) -> None:
        super().__init__(script)
        self._engine = engine
        self._tenant_id = tenant_id
        self.outbox_state_at_send: tuple[str, int] | None = None

    def send(self, request: HttpRequest) -> HttpResponse:
        session = Session(self._engine)
        try:
            session.begin()
            session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(self._tenant_id)}
            )
            row = session.execute(
                text("SELECT status, attempts FROM outbox_events WHERE tenant_id = :t"),
                {"t": self._tenant_id},
            ).one()
            self.outbox_state_at_send = (str(row.status), int(row.attempts))
            session.rollback()
        finally:
            session.close()
        return super().send(request)


@pytest.mark.db
class TestTick:
    def test_a_tick_leases_dispatches_and_completes(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        arrange = kernel_session(admitted.tenant_id)
        enqueue(arrange, admitted.create_order_command(), link_grant=admitted.grant_id)
        arrange.commit()
        transport.extend(
            [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
        )

        report = run_once(
            runtime, tenants=(TenantRef(admitted.tenant_id, "t"),), housekeeping=False
        )

        assert (report.leased, report.completed, report.failed) == (1, 1, 0)
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).provider_order_id == ORDER_ID
        status = after.execute(
            text("SELECT status FROM outbox_events WHERE tenant_id = :t"),
            {"t": admitted.tenant_id},
        ).scalar_one()
        assert status == "DONE"

    def test_a_completed_command_is_never_leased_again(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        arrange = kernel_session(admitted.tenant_id)
        enqueue(arrange, admitted.create_order_command(), link_grant=admitted.grant_id)
        arrange.commit()
        transport.extend(
            [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
        )
        tenants = (TenantRef(admitted.tenant_id, "t"),)
        run_once(runtime, tenants=tenants)

        # Nothing is scripted for a second delivery, so a provider call would fail here.
        again = run_once(runtime, tenants=tenants)

        assert again.leased == 0

    def test_the_lease_is_committed_before_the_handler_runs(
        self,
        worker_settings: WorkerSettings,
        admitted: Admitted,
        dwk_kernel_engine: Engine,
        kernel_session,
    ) -> None:
        arrange = kernel_session(admitted.tenant_id)
        enqueue(arrange, admitted.create_order_command(), link_grant=admitted.grant_id)
        arrange.commit()
        watcher = OutboxWatchingTransport(
            dwk_kernel_engine,
            admitted.tenant_id,
            [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))],
        )

        run_once(
            build_runtime(worker_settings, transport=watcher),
            tenants=(TenantRef(admitted.tenant_id, "t"),),
        )

        assert watcher.outbox_state_at_send == ("LEASED", 1)

    def test_a_command_the_worker_cannot_act_on_is_buried_with_an_audit_row(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        kernel_session,
    ) -> None:
        """A payload that will not parse next time either is a dead letter, not a retry."""
        stranger = uuid7()
        arrange = kernel_session(admitted.tenant_id)
        enqueue(
            arrange,
            ReconcilePaymentCommand(
                tenant_id=str(admitted.tenant_id),
                payment_attempt_id=str(stranger),
                reason="orphan",
                attempt_number=1,
                correlation_id=str(admitted.correlation_id),
            ),
        )
        arrange.commit()

        report = run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        assert (report.leased, report.completed, report.failed) == (1, 0, 1)
        assert report.dead_letters == 1
        after = kernel_session(admitted.tenant_id)
        row = after.execute(
            text("SELECT id, status FROM outbox_events WHERE tenant_id = :t"),
            {"t": admitted.tenant_id},
        ).one()
        assert row.status == "DEAD"
        assert audit_types(after, admitted.tenant_id, row.id) == ["outbox.dead_letter"]

    def test_tenants_are_enumerated_from_the_database(
        self, runtime: WorkerRuntime, admitted: Admitted
    ) -> None:
        assert admitted.tenant_id in {tenant.tenant_id for tenant in list_tenants(runtime)}


# --------------------------------------------------------------------- housekeeping


@pytest.mark.db
class TestHousekeeping:
    def test_a_past_due_grant_is_expired_so_the_attempt_is_not_stranded(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        dwk_admin_engine: Engine,
        kernel_session,
    ) -> None:
        """Hygiene with teeth: an ``ISSUED`` row holds the one live slot on its attempt."""
        with dwk_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE execution_grants SET expires_at = now() - interval '1 second' "
                    "WHERE id = :g"
                ),
                {"g": admitted.grant_id},
            )

        report = run_housekeeping(runtime, tenant_id=admitted.tenant_id)

        assert report.grants_expired == 1
        after = kernel_session(admitted.tenant_id)
        status = after.execute(
            text("SELECT status FROM execution_grants WHERE id = :g"), {"g": admitted.grant_id}
        ).scalar_one()
        assert status == "EXPIRED"

    def test_a_tick_can_run_the_sweeps_without_leasing_anything(
        self, runtime: WorkerRuntime, admitted: Admitted
    ) -> None:
        report = run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),), housekeeping=True)

        assert report.leased == 0
        assert report.housekeeping is not None
        assert report.housekeeping.grants_expired == 0


# ------------------------------------------------------------------ fault injection


@pytest.mark.db
class TestScenarioFaults:
    def test_an_armed_fault_fires_instead_of_the_provider_call(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        dwk_admin_engine: Engine,
        kernel_session,
    ) -> None:
        """Step 9's lost response, on demand -- and with no request actually sent.

        The fault replaces the send rather than following it, which is what keeps
        "exactly one provider request per consumed grant" literally true while the
        demonstration shows an unknown outcome.
        """
        arm_fault(
            dwk_admin_engine,
            tenant_id=admitted.tenant_id,
            kind=FaultKind.CREATE_ORDER_TIMEOUT.value,
            checkout_id=admitted.checkout_id,
        )

        result = handle_create_order(runtime, admitted.create_order_command())

        assert transport.call_count == 0
        assert result.followups == ("RECONCILE_PAYMENT",)
        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status is tk.PaymentState.UNKNOWN
        request = provider_requests(after, admitted)[0]
        assert request.transport_error == "ScenarioFault:CREATE_ORDER_TIMEOUT"
        assert request.http_status is None
        assert outbox_types(after, admitted.tenant_id) == ["RECONCILE_PAYMENT"]

    def test_a_fault_is_consumed_once_and_left_disarmed(
        self,
        runtime: WorkerRuntime,
        transport: FakeTransport,
        admitted: Admitted,
        dwk_admin_engine: Engine,
        kernel_session,
    ) -> None:
        arm_fault(
            dwk_admin_engine,
            tenant_id=admitted.tenant_id,
            kind=FaultKind.CREATE_ORDER_TIMEOUT.value,
            checkout_id=admitted.checkout_id,
        )
        handle_create_order(runtime, admitted.create_order_command())

        after = kernel_session(admitted.tenant_id)
        row = after.execute(
            text(
                "SELECT armed, consumed_at, payment_attempt_id FROM scenario_faults "
                "WHERE tenant_id = :t"
            ),
            {"t": admitted.tenant_id},
        ).one()
        assert row.armed is False
        assert row.consumed_at is not None
        assert transport.call_count == 0


# ---------------------------------------------------------------- configuration


class TestSettings:
    """The guards that decide whether this process may start at all.

    None of these need a database or a network, and all three are refusals rather than
    warnings: the operator who would have read a warning is not present when a background
    worker starts.
    """

    def _fields(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "PROFILE": "development",
            "DATABASE_URL_WORKER": "postgresql+psycopg://w@localhost:5432/x",
            "DATABASE_URL_KERNEL": "postgresql+psycopg://k@localhost:5432/x",
            "RAZORPAY_KEY_ID": "rzp_test_dwkworkerkey",
            "RAZORPAY_KEY_SECRET": "dwk-test-api-secret",
            "RAZORPAY_WEBHOOK_SECRET": "dwk-test-webhook-sec",
        }
        base.update(overrides)
        return base

    def test_one_url_for_both_roles_is_refused(self) -> None:
        """Sharing a URL erases the grant boundary while leaving everything working."""
        shared = "postgresql+psycopg://same@localhost:5432/x"
        with pytest.raises(ValueError, match="different roles"):
            WorkerSettings(**self._fields(DATABASE_URL_WORKER=shared, DATABASE_URL_KERNEL=shared))

    def test_a_live_key_is_refused_outside_production(self) -> None:
        """The worker is the only process that calls Razorpay, so this guard matters here."""
        with pytest.raises(ConfigurationError):
            WorkerSettings(**self._fields(RAZORPAY_KEY_ID="rzp_live_dwkworkerkey"))

    def test_scenario_faults_do_not_exist_in_production(self) -> None:
        assert WorkerSettings(**self._fields()).scenario_faults_enabled
        assert not WorkerSettings(
            **self._fields(
                PROFILE="production",
                RAZORPAY_KEY_ID="rzp_live_dwkworkerkey",
                RAZORPAY_PRODUCTION_APPROVAL_REF="approval-2026-09-05",
            )
        ).scenario_faults_enabled

    def test_the_repr_carries_no_credential(self) -> None:
        rendered = repr(WorkerSettings(**self._fields()))
        assert "dwk-test-api-secret" not in rendered
        assert "rzp_test_dwkworkerkey" not in rendered

    def test_the_entry_point_parses_a_single_tick(self) -> None:
        assert build_parser().parse_args(["--once"]).once is True
        assert build_parser().parse_args([]).once is False

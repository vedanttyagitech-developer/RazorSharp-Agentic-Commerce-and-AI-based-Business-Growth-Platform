"""The worker half of the hop, and the labels that make it safe to export.

The claim this file exists to hold up is a single sentence: *the correlation id the API
minted when it admitted a money action is the same id the worker reports when it executes
that action against Razorpay.* Nothing else in the telemetry layer matters if that is not
true, because every other number is only useful once you can say which payment it was
about.

The rest is the disclosure rule in its metrics form. A provider path carries payment ids
(``/v1/payments/pay_.../refund``), so the ``operation`` label is built from the path's
*shape* and never from the path -- an id that reaches a label is exported, aggregated and
kept long after the payment it names was settled.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from durable_worker.loop import TenantRef, run_once
from durable_worker.settings import WorkerRuntime
from durable_worker.transport import (
    PROVIDER_LABEL,
    HttpxTransport,
    provider_operation,
)
from payment_adapters import HttpRequest, TransportTimeoutError
from platform_observability import (
    JsonFormatter,
    default_registry,
    reset_default_registry,
)
from sqlalchemy.orm import Session

from conftest import Admitted, FakeTransport, enqueue, json_response, order_entity

ORDER_ID = "order_DWKobs000001"
COMMAND_TYPE = "PAYMENT_CREATE_ORDER"


@pytest.fixture
def json_log() -> Iterator[io.StringIO]:
    """Capture ``durable_worker``'s own records the way the running process renders them.

    The formatter is the thing under test as much as the loop is: the correlation id is
    not passed to any log call, it is read out of the bound scope at *format* time, which
    is what makes it appear on lines written by code that never mentions it.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("durable_worker")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.fixture
def clean_registry() -> Iterator[Any]:
    reset_default_registry()
    yield default_registry()
    reset_default_registry()


# ----------------------------------------------------------------------- the hop


@pytest.mark.db
def test_the_worker_reports_the_correlation_id_the_outbox_row_carried(
    runtime: WorkerRuntime,
    transport: FakeTransport,
    admitted: Admitted,
    kernel_session: Callable[[uuid.UUID], Session],
    json_log: io.StringIO,
) -> None:
    """One id, across a process boundary, with a database row in the middle.

    The API wrote ``outbox_events.correlation_id`` in the same transaction as the
    admission. The worker leases that row in a different process, on a thread that
    inherits no context from anywhere, and binds the row's own id -- so the line it writes
    about this command joins to the API's line about the request that enqueued it. A
    thread that silently inherited an id instead would be asserting causation it does not
    have.
    """
    arrange = kernel_session(admitted.tenant_id)
    enqueue(arrange, admitted.create_order_command(), link_grant=admitted.grant_id)
    arrange.commit()
    transport.extend(
        [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
    )

    report = run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),), housekeeping=False)
    assert (report.leased, report.completed) == (1, 1)

    lines = _lines(json_log)
    assert lines, "the worker logged nothing about a command it completed"
    assert all(line["correlation_id"] == str(admitted.correlation_id) for line in lines), (
        f"every line of one command belongs to one conversation; got "
        f"{[line.get('correlation_id') for line in lines]}"
    )
    assert all(line["tenant_id"] == str(admitted.tenant_id) for line in lines)
    assert all(line["actor_type"] == "WORKER" for line in lines)


@pytest.mark.db
def test_a_completed_command_is_counted_as_a_lease_and_an_attempt(
    runtime: WorkerRuntime,
    transport: FakeTransport,
    admitted: Admitted,
    kernel_session: Callable[[uuid.UUID], Session],
    clean_registry: Any,
) -> None:
    """The two worker numbers, under the tenant whose money moved.

    They are read back through the full label set, tenant included, because there is no
    "value across tenants" in this registry -- that aggregation is Prometheus's job, and
    offering it here would invite somebody to answer a question about one buyer from a
    number that spans all of them.
    """
    arrange = kernel_session(admitted.tenant_id)
    enqueue(arrange, admitted.create_order_command(), link_grant=admitted.grant_id)
    arrange.commit()
    transport.extend(
        [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
    )

    run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),), housekeeping=False)

    tenant = str(admitted.tenant_id)
    assert (
        clean_registry.value(
            "commerce_worker_leases_total", tenant=tenant, command_type=COMMAND_TYPE
        )
        == 1.0
    )
    assert (
        clean_registry.value(
            "commerce_worker_attempts_total",
            tenant=tenant,
            command_type=COMMAND_TYPE,
            outcome="completed",
        )
        == 1.0
    )
    histogram = clean_registry.histogram(
        "commerce_worker_command_duration_seconds",
        tenant=tenant,
        command_type=COMMAND_TYPE,
        outcome="completed",
    )
    assert histogram is not None and histogram.count == 1


@pytest.mark.db
def test_the_exposition_of_a_worked_command_names_no_identifier(
    runtime: WorkerRuntime,
    transport: FakeTransport,
    admitted: Admitted,
    kernel_session: Callable[[uuid.UUID], Session],
    clean_registry: Any,
) -> None:
    """The rendered text, read for what must not be in it.

    A command id, a checkout id and a provider order id are all in scope during this tick
    and none of them is a label anywhere. The tenant is the one identifier that is, and
    that is deliberate: it is the partition every tenant-scoped instrument is keyed by.
    """
    arrange = kernel_session(admitted.tenant_id)
    command_id = enqueue(arrange, admitted.create_order_command(), link_grant=admitted.grant_id)
    arrange.commit()
    transport.extend(
        [json_response(200, order_entity(order_id=ORDER_ID, receipt=admitted.receipt))]
    )

    run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),), housekeeping=False)
    body = clean_registry.render()

    for forbidden in (str(command_id), str(admitted.checkout_id), ORDER_ID, admitted.receipt):
        assert forbidden not in body, f"{forbidden!r} reached an exported series"


# ------------------------------------------------------------- the provider label


class TestProviderOperation:
    """The label is the request's shape. Its cardinality is the size of the API surface."""

    @pytest.mark.parametrize(
        ("method", "url", "expected"),
        [
            ("POST", "https://api.razorpay.com/v1/orders", "POST /orders"),
            (
                "GET",
                "https://api.razorpay.com/v1/orders?receipt=rcpt_01a0",
                "GET /orders",
            ),
            (
                "POST",
                "https://api.razorpay.com/v1/payments/pay_Nx9cQ2/refund",
                "POST /payments/{id}/refund",
            ),
            (
                "GET",
                "https://api.razorpay.com/v1/payments/pay_Nx9cQ2",
                "GET /payments/{id}",
            ),
            (
                "GET",
                "https://api.razorpay.com/v1/orders/order_Nx9cQ2/payments",
                "GET /orders/{id}/payments",
            ),
        ],
    )
    def test_the_shape_survives_and_the_identifier_does_not(
        self, method: str, url: str, expected: str
    ) -> None:
        assert provider_operation(method, url) == expected

    def test_a_receipt_in_the_query_string_never_reaches_the_label(self) -> None:
        """The recovery lookup after a lost create-order carries a business identifier in
        its query string (spec 10.6). It is not a secret; it is also not a label value."""
        label = provider_operation("GET", "https://api.razorpay.com/v1/orders?receipt=rcpt_abc")
        assert "rcpt_abc" not in label

    def test_an_endpoint_nobody_anticipated_is_still_bounded(self) -> None:
        """The vocabulary is an allow-list, so a path this platform has never called
        collapses to placeholders rather than opening an unbounded label."""
        assert provider_operation("POST", "https://api.razorpay.com/v1/xyz/abc123") == (
            "POST /{id}/{id}"
        )


def _transport_over(handler: Any) -> HttpxTransport:
    return HttpxTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))


class TestTheProviderCallIsMeasuredWithoutBeingChanged:
    def test_a_status_the_provider_answered_with_is_classified_not_reported_raw(
        self, clean_registry: Any
    ) -> None:
        """Three outcomes, not sixty. Which 4xx it was is the adapter's question and the
        ``provider_requests`` row's answer; the counter says whether Razorpay is refusing
        us or falling over, which are different incidents."""
        transport = _transport_over(lambda _r: httpx.Response(502))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "durable_worker.transport.default_registry",
                lambda: clean_registry,
            )
            from platform_observability import bind_scope

            with bind_scope(str(uuid.uuid4()), tenant_id="t-obs"):
                response = transport.send(
                    HttpRequest(method="POST", url="https://api.razorpay.com/v1/orders")
                )

        assert response.status == 502
        assert (
            clean_registry.value(
                "commerce_provider_requests_total",
                tenant="t-obs",
                provider=PROVIDER_LABEL,
                operation="POST /orders",
                outcome="server_error",
            )
            == 1.0
        )

    def test_a_timeout_still_raises_the_exception_the_adapter_classifies(
        self, clean_registry: Any
    ) -> None:
        """The measurement must not swallow, delay or retype a failure to obtain a
        response: ``PAYMENT_UNKNOWN`` depends on the adapter seeing this exact exception."""

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        transport = _transport_over(timeout)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("durable_worker.transport.default_registry", lambda: clean_registry)
            from platform_observability import bind_scope

            with bind_scope(str(uuid.uuid4()), tenant_id="t-obs"):
                with pytest.raises(TransportTimeoutError):
                    transport.send(
                        HttpRequest(method="POST", url="https://api.razorpay.com/v1/orders")
                    )

        # Recorded as `error` rather than `timeout`: `timed` classifies anything leaving
        # its block that way, and the alternative -- catching the exception in order to
        # label it -- is exactly what an observability layer may not do.
        assert (
            clean_registry.value(
                "commerce_provider_requests_total",
                tenant="t-obs",
                provider=PROVIDER_LABEL,
                operation="POST /orders",
                outcome="error",
            )
            == 1.0
        )

    def test_an_unscoped_call_records_nothing_rather_than_guessing_a_tenant(
        self, clean_registry: Any
    ) -> None:
        """Outside a bound scope there is no tenant, and inventing one would put a
        provider call on some other tenant's series."""
        transport = _transport_over(lambda _r: httpx.Response(200, json={}))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("durable_worker.transport.default_registry", lambda: clean_registry)
            transport.send(HttpRequest(method="POST", url="https://api.razorpay.com/v1/orders"))

        assert clean_registry.series("commerce_provider_requests_total") == ()
        assert (
            clean_registry.value(
                "observability_dropped_total",
                instrument="commerce_provider_requests_total",
                reason="missing_tenant",
            )
            == 1.0
        )

"""What mounting the telemetry layer must not have changed, and the one thing it added.

``packages/platform-observability`` was complete and imported by nothing. These tests
cover the seams where it now meets this service, and they are written around the two
promises that make mounting it safe:

* **it decides nothing.** A kernel decision is still HTTP 200 whether it allowed or denied
  (ADR 0003 D15), a refusal is still an RFC 9457 problem detail, and the transaction
  boundaries are exactly where they were. The middleware counts and times; it does not
  answer.
* **it discloses nothing.** No log line and no exported series may carry a token, a card
  number, a Razorpay credential or a buyer's checkout id -- and the metric case is the one
  people get wrong, because a resolved path in a label outlives the request by weeks.

The correlation assertions are the point of the whole exercise: one id, resolved once, on
the response, in the log line, and on the outbox row the worker will lease.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Any

import pytest
from commerce_api.observability import UNAUTHENTICATED_TENANT, configure_process_logging
from commerce_domain import Money, RecoveryCode
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_observability import (
    JsonFormatter,
    bind_scope,
    default_registry,
    reset_default_registry,
)
from sqlalchemy import Engine, text

from conftest import MintedSession, merchant_mutation

CORRELATION_HEADER = "X-Correlation-Id"
MILK = "AMUL-DAIRY-001"
_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


def _approved_checkout(client: TestClient, correlation: str) -> dict[str, Any]:
    """Steps 2-4: a cart, a priced line, a checkout and the buyer's approval of it."""
    common = {CORRELATION_HEADER: correlation}
    cart = client.post("/v1/carts", headers=_headers(**common))
    assert cart.status_code == 201, cart.text
    cart_id = cart.json()["cart_id"]

    line = client.put(
        f"/v1/carts/{cart_id}/lines/{MILK}",
        json={"quantity": 2},
        headers=_headers(**common),
    )
    assert line.status_code == 200, line.text

    opened = client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers(**common))
    assert opened.status_code == 201, opened.text
    card = opened.json()

    approved = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers=_headers(**common),
    )
    assert approved.status_code == 200, approved.text
    return card


def _submitted_checkout(client: TestClient, correlation: str) -> dict[str, Any]:
    """The same, submitted: one admitted money action and one outbox command."""
    card = _approved_checkout(client, correlation)
    submitted = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers=_headers(**{CORRELATION_HEADER: correlation}),
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["allowed"] is True, submitted.text
    return {"card": card, "submit": submitted}


# ------------------------------------------------------- the id, end to end in the API


@pytest.mark.db
def test_one_supplied_id_reaches_the_response_and_the_outbox_row_the_worker_will_lease(
    auth_client: TestClient, demo_session: MintedSession, capi_kernel_engine: Engine
) -> None:
    """The API half of the hop, which is the half that has to be exactly right.

    A caller supplies ``X-Correlation-Id``; the middleware adopts it before any router
    runs; ``deps`` reads it back off ``request.state`` instead of resolving the header a
    second time; and it is that id -- not a second one minted mid-request -- that the
    kernel writes onto the outbox command. The worker leases that row later and binds the
    same id, which is why this assertion is worth more than any count of mounted
    middleware.
    """
    correlation = str(uuid.uuid4())
    result = _submitted_checkout(auth_client, correlation)

    # The response says which conversation it belonged to, without being asked.
    assert result["submit"].headers[CORRELATION_HEADER] == correlation

    with capi_kernel_engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(demo_session.tenant_id)})
        commands = (
            conn.execute(
                text(
                    "SELECT command_type FROM outbox_events "
                    "WHERE tenant_id = :tenant AND correlation_id = :correlation"
                ),
                {"tenant": demo_session.tenant_id, "correlation": correlation},
            )
            .scalars()
            .all()
        )

    assert commands == ["PAYMENT_CREATE_ORDER"], (
        "the outbox row must carry the caller's correlation id, not one the API minted "
        f"for itself; found {commands!r}"
    )


@pytest.mark.db
def test_an_absent_header_is_minted_once_and_the_response_says_which_id_was_used(
    auth_client: TestClient,
) -> None:
    """A client that supplies nothing still gets a joinable answer.

    Two ids for one request -- one on the log lines, another on the audit row -- is the
    exact failure this mount was written to avoid, and it is invisible unless the id is
    reported back. So the header is on every response, minted or adopted.
    """
    response = auth_client.post("/v1/carts", headers=_headers())
    assert response.status_code == 201, response.text
    minted = response.headers[CORRELATION_HEADER]
    assert uuid.UUID(minted), minted

    second = auth_client.post("/v1/carts", headers=_headers())
    assert second.headers[CORRELATION_HEADER] != minted, (
        "each request without a supplied id gets its own; reusing one would join two "
        "unrelated conversations"
    )


@pytest.mark.db
def test_a_malformed_header_is_replaced_rather_than_refused(auth_client: TestClient) -> None:
    """The header was never authority, so a client bug in it cannot refuse a payment."""
    response = auth_client.post("/v1/carts", headers=_headers(**{CORRELATION_HEADER: "not-a-uuid"}))
    assert response.status_code == 201, response.text
    assert uuid.UUID(response.headers[CORRELATION_HEADER])


# --------------------------------------------------------------- it decides nothing


@pytest.mark.db
def test_a_kernel_denial_is_still_a_200_carrying_the_decision(
    auth_client: TestClient, api_app: FastAPI, demo_session: MintedSession
) -> None:
    """ADR 0003 D15, re-asserted with the middleware in the stack.

    The price moves under an approved checkout, so the kernel refuses to spend consent the
    buyer gave for a different number. That refusal is the system working, so it is a
    decision in a 200 and not an error status. A telemetry layer that turned it into a 4xx
    -- or that recorded it as one -- would have changed what the platform says about a
    buyer's money.
    """
    correlation = str(uuid.uuid4())
    card = _approved_checkout(auth_client, correlation)

    with merchant_mutation(api_app, demo_session) as scenario:
        scenario.set_price(MILK, Money(4000, "INR"))

    denied = auth_client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers=_headers(**{CORRELATION_HEADER: correlation}),
    )
    assert denied.status_code == 200, denied.text
    body = denied.json()
    assert body["allowed"] is False
    assert body["code"] == RecoveryCode.REAPPROVAL_REQUIRED.value
    assert denied.headers[CORRELATION_HEADER] == correlation


@pytest.mark.db
def test_a_problem_detail_still_looks_like_one(client: TestClient) -> None:
    """The refusal path runs *below* this middleware, so it is untouched and still carries
    the header -- which is what makes a failed request as investigable as a successful one."""
    response = client.get("/v1/orders")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers[CORRELATION_HEADER]


# ------------------------------------------------------------ it discloses nothing


@pytest.fixture
def clean_registry() -> Any:
    """A registry with no series on it, so an exposition assertion is about this test."""
    reset_default_registry()
    yield default_registry()
    reset_default_registry()


@pytest.mark.db
@pytest.mark.usefixtures("clean_registry")
def test_the_route_label_is_the_template_and_never_a_buyer_s_checkout_id(
    auth_client: TestClient,
    client: TestClient,
    operator_headers: dict[str, str],
) -> None:
    """The cardinality rule and the disclosure rule are the same rule (ADR 0007 D5).

    A resolved path in a label is unbounded cardinality *and* a buyer's checkout id in a
    time series that a metrics backend will keep long after the checkout is settled. So
    the label is the route template, and this test reads the real exposition to say so
    rather than trusting the middleware's intent.
    """
    card = _submitted_checkout(auth_client, str(uuid.uuid4()))["card"]
    read = auth_client.get(f"/v1/checkouts/{card['checkout_id']}")
    assert read.status_code == 200, read.text

    exposition = client.get("/v1/ops/metrics", headers=operator_headers)
    assert exposition.status_code == 200, exposition.text
    body = exposition.text

    assert 'route="/v1/checkouts/{checkout_id}"' in body, body
    assert card["checkout_id"] not in body, (
        "a resolved path reached a label: that is a buyer's identifier in an exported "
        "series, and it outlives the request by weeks"
    )


@pytest.mark.db
@pytest.mark.usefixtures("clean_registry")
def test_an_unauthenticated_request_is_counted_under_a_sentinel_rather_than_dropped(
    client: TestClient, operator_headers: dict[str, str]
) -> None:
    """A 401 has no tenant, and dropping it would hide the traffic an incident starts with."""
    assert client.get("/v1/orders").status_code == 401

    body = client.get("/v1/ops/metrics", headers=operator_headers).text
    assert f'tenant="{UNAUTHENTICATED_TENANT}"' in body, body
    assert 'status="4xx"' in body, body


@pytest.mark.db
def test_the_exposition_is_behind_the_operator_key(client: TestClient) -> None:
    """It carries no buyer data by construction, but it does describe the platform's shape,
    so it lives on the operator surface rather than on the buyer-facing route table."""
    assert client.get("/v1/ops/metrics").status_code == 401


def test_every_instrument_is_described_even_before_anything_has_happened(
    clean_registry: Any,
) -> None:
    """ "No data" and "never wired up" have to look different on day one.

    A registry that emitted only the series it happened to have would make an unmounted
    instrument indistinguishable from a quiet one, and the difference would be discovered
    during the first incident rather than before it.
    """
    body = clean_registry.render()
    assert "# HELP commerce_http_requests_total" in body
    assert "# TYPE commerce_worker_dead_letters_total counter" in body
    assert "# HELP commerce_provider_request_duration_seconds" in body


# ------------------------------------------------------------------------- the logs


def test_a_logger_that_never_heard_of_the_redactor_still_comes_out_redacted() -> None:
    """The layer that catches the two-in-the-morning debug statement.

    Redaction happens in the formatter, not at the call site, so it covers uvicorn,
    SQLAlchemy, ``httpx`` and code written after this test. The record below is exactly
    what somebody chasing a signature mismatch writes, and none of it may survive.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("some.package.that.does.not.know")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    correlation = str(uuid.uuid4())
    try:
        with bind_scope(correlation, tenant_id="t-1", actor_type="BUYER"):
            logger.info(
                "verifying %s with key %s for card %s",
                "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
                "rzp_test_ABCDEFGH1234",
                "4111 1111 1111 1111",
            )
    finally:
        logger.removeHandler(handler)

    line = json.loads(stream.getvalue().strip())
    assert line["correlation_id"] == correlation
    assert line["tenant_id"] == "t-1"
    message = line["message"]
    assert "eyJhbGciOiJIUzI1NiJ9" not in message, message
    assert "rzp_test_ABCDEFGH1234" not in message, message
    assert "4111" not in message, message
    assert "[redacted:pan]" in message and "[redacted:provider-credential]" in message


def test_the_lifespan_does_not_take_stderr_from_whoever_already_owns_it() -> None:
    """Why building an app inside pytest does not silence pytest's own log capture.

    ``configure_logging`` replaces the root logger's handlers, which is right for a process
    uvicorn started and wrong inside a test runner that is capturing this very test. The
    lifespan therefore asks first, and a root logger with handlers on it is the answer
    "somebody else is responsible for this process's output".
    """
    root = logging.getLogger()
    existing = list(root.handlers)
    assert existing, "pytest keeps handlers on the root logger for the whole run"

    assert configure_process_logging() is False
    assert list(root.handlers) == existing

    for handler in existing:
        root.removeHandler(handler)
    try:
        assert configure_process_logging() is True
        assert root.handlers
        installed = root.handlers[0]
        assert isinstance(installed.formatter, JsonFormatter)
        # uvicorn's own loggers are re-pointed at root, so the access log -- the one that
        # prints the request line and its query string -- is redacted like everything else.
        access = logging.getLogger("uvicorn.access")
        assert access.propagate is True
        assert access.handlers == []
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in existing:
            root.addHandler(handler)

"""Regression: a validation error whose input is raw bytes is a 422, not a 500.

Found by the security review of 2026-09-05. ``on_request_validation`` passed
``exc.errors()`` straight into the problem body, and for a body that could not be parsed
at all -- JSON sent without ``application/json`` -- Pydantic records the offending value in
each error's ``input`` as the raw request ``bytes``. ``JSONResponse`` cannot serialise
``bytes``, so the 422 handler itself raised and the caller got an unhandled 500. A client
could trigger a 5xx with a trivially malformed request.

The fix routes the error list through :func:`fastapi.encoders.jsonable_encoder` (as
FastAPI's own default handler does), decoding bytes with replacement so even a non-UTF-8
body cannot crash it. These tests cover the helper directly and end to end.
"""

from __future__ import annotations

import json

from commerce_api.errors import _jsonable_errors, _problem_from_exception
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from transaction_kernel.idempotency import IdempotencyKeyReuseError


def test_jsonable_errors_makes_a_bytes_input_serialisable() -> None:
    raw = [
        {
            "type": "json_invalid",
            "loc": ("body", 0),
            "msg": "Expecting value",
            "input": b'\xff\x00 not valid utf-8 {"a":1}',
        }
    ]
    encoded = _jsonable_errors(raw)
    # The whole thing now round-trips through json without raising.
    dumped = json.dumps(encoded)
    assert "json_invalid" in dumped
    # loc/msg/type survive; the parts a client acts on are unchanged.
    assert encoded[0]["msg"] == "Expecting value"
    assert list(encoded[0]["loc"]) == ["body", 0]
    # The bytes input became a string rather than crashing the encoder.
    assert isinstance(encoded[0]["input"], str)


def test_a_body_with_the_wrong_content_type_is_a_clean_422(client: TestClient) -> None:
    """A JSON body sent without ``application/json`` used to 500; it must be a 422 now.

    The endpoint expects a JSON object; the raw bytes arrive under a form content type, so
    Pydantic reports a validation error whose ``input`` is the raw body -- the exact shape
    that crashed the error handler.
    """
    response = client.post(
        "/v1/demo/sessions",
        content=b'{"tenant_slug":"demo","actor_type":"BUYER"}',
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422, response.text
    # The body is well-formed problem JSON -- i.e. serialisation did not blow up.
    body = response.json()
    assert body["title"] == "Request validation failed"
    assert body["status"] == 422


# --------------------------------------------------------- 4xx must not carry the query


def _detail_for(exc: BaseException) -> str:
    """The ``detail`` the API would send a client for this exception."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/orders/x/refunds",
        "headers": [],
        "query_string": b"",
    }
    response = _problem_from_exception(Request(scope), exc)
    return json.loads(bytes(response.body))["detail"]


def test_a_constraint_violation_does_not_return_the_sql_or_the_row() -> None:
    """A 4xx is shown to whoever made the request; the row being written is not theirs.

    ``str()`` on a SQLAlchemy ``StatementError`` appends the statement and its bound
    parameters, and the parameters here are buyer data -- an email, an amount, a hash.
    The handler took ``str(exc)`` for every sub-500 exception, so a duplicate-key collision
    answered with the whole INSERT. The comment beside ``constraint`` had always claimed
    the opposite.
    """
    exc = IntegrityError(
        "INSERT INTO refunds (buyer_email, amount_minor) VALUES (%(email)s, %(amt)s)",
        {"email": "buyer@example.com", "amt": 50_000},
        Exception("duplicate key value violates unique constraint"),
    )
    detail = _detail_for(exc)
    assert "SQL:" not in detail, "the statement reached the client"
    assert "parameters" not in detail, "the bound parameters reached the client"
    assert "buyer@example.com" not in detail, "a buyer's email reached the client"
    assert "INSERT" not in detail
    assert detail, "a 4xx must still say something a caller can act on"


def test_an_ordinary_4xx_still_explains_itself() -> None:
    """The fix must not flatten every 4xx into the same sentence.

    Only SQLAlchemy statement errors carry a query. A classified 4xx carries a message
    written for the caller, and that message is the most useful thing in the body.
    """
    detail = _detail_for(
        IdempotencyKeyReuseError(
            "k-1",
            "CART_CREATE",
            stored_operation="CART_CREATE",
            stored_request_hash="aaa",
            offered_request_hash="bbb",
        )
    )
    assert "idempotency key" in detail and "refused" in detail


# ------------------------------------------- the documented 422 and the sent 422 agree


def test_the_documented_422_is_the_422_the_server_sends(client: TestClient) -> None:
    """A client generated from the schema must bind to the key the server writes.

    FastAPI advertises ``HTTPValidationError`` in ``application/json``, whose failures live
    under ``detail``. This service replaced that handler: the real 422 is
    ``application/problem+json`` and its failures live under ``errors``. A generated client
    bound to ``detail`` finds nothing and reports "rejected, no reason given" for every
    invalid form.
    """
    response = client.post(
        "/v1/demo/sessions",
        content=b'{"tenant_slug":"demo"}',
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    sent = response.json()

    documented = client.get("/openapi.json").json()
    operation = documented["paths"]["/v1/demo/sessions"]["post"]
    content = operation["responses"]["422"]["content"]
    assert list(content) == ["application/problem+json"], (
        "the document promises a media type the server does not send"
    )
    ref = content["application/problem+json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    declared = documented["components"]["schemas"][ref]

    for member in declared["required"]:
        assert member in sent, f"the document requires {member!r}, which the server omitted"
    assert "errors" in sent and isinstance(sent["errors"], list)
    assert "HTTPValidationError" not in documented["components"]["schemas"], (
        "the stale FastAPI validation model is still being advertised"
    )

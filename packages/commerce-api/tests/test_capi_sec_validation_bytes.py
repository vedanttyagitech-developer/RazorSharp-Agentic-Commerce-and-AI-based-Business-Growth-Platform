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

from commerce_api.errors import _jsonable_errors
from fastapi.testclient import TestClient


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

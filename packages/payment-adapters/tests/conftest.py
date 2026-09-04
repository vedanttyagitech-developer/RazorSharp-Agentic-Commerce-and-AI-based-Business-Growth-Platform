"""Shared fixtures. No network, no credentials, no database.

The whole point of the injected transport is visible here: every provider interaction in
this suite is scripted, so a test can assert what happens on a timeout or a 502 without
waiting for one, and continuous integration never depends on Razorpay being reachable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

import pytest
from payment_adapters.razorpay import HttpRequest, HttpResponse, RazorpayConfig, RazorpayProfile

# Named so that they do not read as credentials to a secret scanner, and so that the
# linter's hardcoded-credential rules stay meaningful in files that really do handle keys.
TEST_KEY_ID = "rzp_test_1DP5mmOlF5G5ag"
LIVE_KEY_ID = "rzp_live_1DP5mmOlF5G5ag"
API_KEY_MATERIAL = "api-key-material-for-tests"
WEBHOOK_KEY_MATERIAL = "webhook-key-material-for-tests"


class FakeTransport:
    """Scripted :class:`HttpTransport`. Records every request it is given.

    Raises if called more often than the script allows, which is how the suite proves
    that an adapter call sends exactly one request and never retries internally.
    """

    def __init__(
        self,
        responses: list[HttpResponse] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._error = error
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError(
                f"transport was called {len(self.requests)} times but only "
                f"{len(self.requests) - 1} responses were scripted; the adapter must "
                "send exactly one request per call"
            )
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.requests)


def json_response(status: int, payload: Any) -> HttpResponse:
    """A response whose body is JSON bytes, as a real provider would send."""
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


def sign_body(raw_body: bytes, secret: str) -> str:
    """Produce the ``x-razorpay-signature`` a genuine Razorpay delivery would carry."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def sign_payment(order_id: str, payment_id: str, secret: str) -> str:
    """Produce the ``razorpay_signature`` a genuine client return would carry."""
    message = f"{order_id}|{payment_id}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def webhook_body(
    event: str,
    *,
    payment: Mapping[str, Any] | None = None,
    refund: Mapping[str, Any] | None = None,
    event_created_at: int = 1_767_225_600,
) -> dict[str, Any]:
    """Build a Razorpay webhook body in the provider's documented envelope shape."""
    payload: dict[str, Any] = {}
    contains: list[str] = []
    if payment is not None:
        payload["payment"] = {"entity": dict(payment)}
        contains.append("payment")
    if refund is not None:
        payload["refund"] = {"entity": dict(refund)}
        contains.append("refund")
    return {
        "entity": "event",
        "account_id": "acc_TEST00000000",
        "event": event,
        "contains": contains,
        "payload": payload,
        "created_at": event_created_at,
    }


def payment_entity(
    *,
    payment_id: str = "pay_29QQoUBi66xm2f",
    order_id: str = "order_9A33XWu170gUtm",
    amount: int = 39500,
    currency: str = "INR",
    status: str = "captured",
    amount_refunded: int = 0,
) -> dict[str, Any]:
    return {
        "id": payment_id,
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "amount_refunded": amount_refunded,
    }


@pytest.fixture
def config() -> RazorpayConfig:
    """A valid test-mode configuration."""
    return RazorpayConfig.load(
        key_id=TEST_KEY_ID,
        key_secret=API_KEY_MATERIAL,
        webhook_secret=WEBHOOK_KEY_MATERIAL,
        profile=RazorpayProfile.DEVELOPMENT,
    )

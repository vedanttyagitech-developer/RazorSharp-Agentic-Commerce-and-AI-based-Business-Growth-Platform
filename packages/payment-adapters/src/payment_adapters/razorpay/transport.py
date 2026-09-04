"""The provider boundary: an injected transport, and how its answers become certainty.

No HTTP library is imported here and none is a dependency of this package. The caller
supplies something satisfying :class:`HttpTransport`; tests supply a fake. That is not
only a testing convenience -- it is what makes the classification below reviewable
without a network, and what keeps continuous integration from depending on Razorpay's
uptime or on credentials living in a runner.

Certainty is an allowlist
-------------------------
The single most important decision in this file is :func:`classify_failure`. When a
provider call does not clearly succeed, exactly one question matters:

    **Might the provider have performed the mutation anyway?**

If the answer is "maybe", the outcome is ``PAYMENT_UNKNOWN``, which is deliberately
absent from ``recovery.RETRYABLE``: an unknown outcome is reconciled against
authoritative identifiers, never retried (specification 10.7). If the answer is a
confirmed "no", the outcome is ``PAYMENT_FAILED``, which may be retried under a fresh
Execution Grant.

Getting that backwards in the "unknown -> failed" direction is what charges a buyer
twice. So the set of *definitely did not happen* statuses is an explicit allowlist and
everything else -- every 5xx, every timeout, every unreadable body, every status nobody
anticipated -- falls through to unknown. Being wrong in the unknown direction costs a
reconciliation job. Being wrong in the other direction costs a duplicate charge.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from transaction_kernel.recovery import RecoveryCode

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "TransportError",
    "TransportTimeoutError",
    "classify_failure",
    "parse_json_body",
    "parse_json_body_bytes",
]

#: Razorpay's own guidance is that a create call may take several seconds under load.
#: The number matters less than the fact that a breach is ``UNKNOWN`` and not ``FAILED``.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0

#: HTTP statuses for which the provider is telling us, definitively, that it did not
#: perform the mutation. Nothing may be added here without an argument for why the
#: request cannot possibly have taken effect.
#:
#: * 400 -- the request was rejected by validation before any entity was created.
#: * 401 / 403 -- the credentials were refused, so no authenticated action occurred.
#: * 404 -- the addressed resource does not exist, so nothing was mutated.
#: * 422 -- semantic rejection; Razorpay returns this for business validation failures.
#:
#: Note what is *not* here: 408, 409, 429 and every 5xx. A 429 in particular looks like a
#: clean rejection and is usually one, but "usually" is not the standard for deciding
#: whether a second order may be created.
_DEFINITELY_NOT_PERFORMED: Final[frozenset[int]] = frozenset({400, 401, 403, 404, 422})

#: Statuses that mean the process is misconfigured rather than the payment failed. These
#: must not be retried on a loop and must not be presented to a buyer as a payment
#: problem; they need an operator.
_OPERATOR_FAULT: Final[frozenset[int]] = frozenset({401, 403})


class TransportError(Exception):
    """The request could not be completed and the outcome is not known.

    Raised by a transport implementation for connection failures, TLS failures, DNS
    failures and any other error that leaves the caller unable to say whether the
    provider received and acted on the request.
    """


class TransportTimeoutError(TransportError):
    """The request timed out. The provider may still have performed the mutation.

    A distinct type from ``TransportError`` only so that callers can log it usefully;
    both are classified identically, because both mean the same thing about the money.
    """


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """A fully built, ready-to-send request. Deterministic given its inputs.

    Built by pure functions in :mod:`.orders` and :mod:`.refunds` so that the exact bytes
    and headers can be asserted in a test without a transport at all.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    #: Basic-auth pair, kept out of ``headers`` so that building the Authorization value
    #: -- and therefore holding the secret in a formatted string -- happens once, in the
    #: transport, at the moment of sending.
    auth: tuple[str, str] | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def redacted(self) -> dict[str, Any]:
        """A log-safe view. Omits ``auth`` entirely and never includes header values.

        Specification 11.5 requires logs to redact credentials. Offering this method is
        cheaper than trusting every future log statement to remember.
        """
        return {
            "method": self.method,
            "url": self.url,
            "header_names": sorted(self.headers),
            "body_bytes": len(self.body),
        }


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What the transport observed. Raw bytes, never a parsed body.

    The body stays as bytes because the adapter, not the transport, decides how to read
    it -- and because a transport that parsed JSON would have to invent a behaviour for
    a body that is not JSON, which is precisely the case that must be classified as
    unknown rather than swallowed.
    """

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class HttpTransport(Protocol):
    """The only I/O this package performs, and it performs it through you.

    An implementation must:

    * send ``request`` exactly once per call -- **no internal retries**. A transport that
      silently retries a POST turns one create-order into two orders, and the adapter
      cannot see it happen. Retries belong above the kernel, where a fresh Execution
      Grant is issued for each attempt;
    * raise :class:`TransportTimeoutError` when ``request.timeout_seconds`` is exceeded;
    * raise :class:`TransportError` for any other failure to obtain a response;
    * return :class:`HttpResponse` for every status, including 4xx and 5xx, rather than
      raising for them. A 400 is information the adapter needs, not an error.
    """

    def send(self, request: HttpRequest) -> HttpResponse: ...


def parse_json_body_bytes(raw: bytes) -> dict[str, Any] | None:
    """Parse raw bytes as a JSON object, or return ``None``.

    Returns ``None`` rather than raising so that the "cannot read this" path is the
    natural one to write, instead of sitting inside an ``except`` block that someone
    later narrows. A JSON array or scalar also yields ``None``: every provider entity and
    every webhook body this adapter understands is an object, and a list arriving where
    an object was expected is not something to partially interpret.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError, UnicodeDecodeError, ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def parse_json_body(response: HttpResponse) -> dict[str, Any] | None:
    """Parse a response body as a JSON object, or return ``None``.

    ``None`` means "this body cannot be read as a provider entity", which every caller
    must treat as an unknown outcome rather than as a failure: a 200 with an unreadable
    body most likely means the mutation succeeded and something downstream mangled the
    response.
    """
    return parse_json_body_bytes(response.body)


def classify_failure(status: int) -> RecoveryCode:
    """Map a non-success HTTP status to the outcome the platform may act on.

    Guarantees:

    * a status in the explicit "definitely not performed" allowlist yields a code the
      caller may retry under a **new** Execution Grant;
    * **every other status yields** ``PAYMENT_UNKNOWN``, which is not retryable and whose
      only lawful resolution is reconciliation (specification 10.7);
    * an authentication or authorization refusal yields ``HUMAN_REVIEW_REQUIRED`` rather
      than ``PAYMENT_FAILED``, because credentials that the provider rejects are an
      operator problem and retrying them forever tells a buyer their card failed when
      nothing was ever asked of it.

    Refuses to be clever about 5xx. A gateway timeout carries no information about
    whether the order was written.
    """
    if status in _OPERATOR_FAULT:
        return RecoveryCode.HUMAN_REVIEW_REQUIRED
    if status in _DEFINITELY_NOT_PERFORMED:
        return RecoveryCode.PAYMENT_FAILED
    return RecoveryCode.PAYMENT_UNKNOWN

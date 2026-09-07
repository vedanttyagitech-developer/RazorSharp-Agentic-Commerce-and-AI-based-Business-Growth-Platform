"""The one place in this repository that opens a socket to Razorpay.

ADR 0003 D3 keeps ``payment-adapters`` transport-free so its classification of every
provider answer can be reviewed and tested without a network. This module is the other
half of that decision: a :class:`~payment_adapters.HttpTransport` implementation built on
``httpx``, holding no payment logic at all. It sends bytes and reports what came back.

Three rules, and each of them is a money rule rather than a style preference.

**One send per call, never a retry.** ``httpx`` will silently retry a request at the
connection level if asked to, and a retried ``POST /v1/orders`` is two Razorpay orders for
one checkout version with the adapter unable to see it happen. The transport is
constructed with ``retries=0`` explicitly rather than relying on the default, and it does
not loop. A retry in this platform is a fresh kernel admission carrying a new single-use
Execution Grant; it is never something a client library decides on its own.

**Redirects are not followed.** A 3xx is returned to the adapter, which classifies it as
``PAYMENT_UNKNOWN`` like any other unanticipated status. Following one would re-send the
request body to a host the platform never decided to talk to.

**Every failure to obtain a response is an exception, never a fabricated status.** A
timeout raises :class:`~payment_adapters.TransportTimeoutError` and everything else raises
:class:`~payment_adapters.TransportError`; both mean "the provider may have performed the
mutation anyway", which the adapter turns into ``PAYMENT_UNKNOWN``. Returning, say, a
synthetic 599 would be classified the same way today and could be re-classified as a
failure tomorrow by someone who did not know it was invented here.

Nothing in this module logs, formats or stores a credential, a signature or a request
body. The Basic-auth pair lives on :class:`~payment_adapters.HttpRequest.auth` and is
handed to ``httpx`` at the moment of sending; error messages carry the method and the
path only, with any query string removed, because a receipt or an identifier in a query
string is exactly the kind of value that ends up in a log by accident.

**This is also the one place the provider is measured**, for the same reason it is the one
place the provider is called. One :func:`~platform_observability.timed` block around the
round trip gives every Razorpay call a duration and a verdict, labelled by the *shape* of
the request rather than by its identifiers -- see :func:`provider_operation`. The block
changes nothing: it re-raises whatever the send raised, and every recording inside it is
total, so a broken metric cannot turn a completed payment into a failed one.
"""

from __future__ import annotations

import types
from typing import Final, Self
from urllib.parse import urlsplit, urlunsplit

import httpx
from payment_adapters import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpRequest,
    HttpResponse,
    TransportError,
    TransportTimeoutError,
)
from platform_observability import PROVIDER_TIMING, default_registry, timed

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "PROVIDER_LABEL",
    "HttpxTransport",
    "provider_operation",
    "safe_url",
]

#: Connect timeouts are separated from the overall deadline so that an unreachable host
#: fails fast while a slow create-order still gets the full ADR D13 budget of 20 seconds.
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0

#: A worker that never identifies itself is indistinguishable from anything else in a
#: provider's logs when an incident has to be traced back to this process.
USER_AGENT: Final[str] = "governed-agentic-commerce-worker/0.1"

#: The ``provider`` label value. One provider today; the label exists so that adding a
#: second one is a new value rather than a new instrument.
PROVIDER_LABEL: Final[str] = "razorpay"

#: The path segments that are part of Razorpay's API shape rather than data. Everything
#: else in a path is an identifier -- ``pay_...``, ``order_...`` -- and becomes ``{id}``.
#: An allow-list rather than a pattern match on what *looks* like an id, because the
#: failure directions are not symmetric: a segment wrongly kept is a buyer's payment id in
#: an exported time series that outlives the request by weeks (ADR 0007 D5), and a segment
#: wrongly replaced is one label that reads slightly coarser than it could.
_PATH_VOCABULARY: Final[frozenset[str]] = frozenset(
    {"orders", "payments", "refund", "refunds", "capture", "notes", "settlements"}
)


def safe_url(url: str) -> str:
    """The URL with its query string and any userinfo removed, for logs and errors.

    ``GET /v1/orders?receipt=rcpt_...`` is the recovery lookup, and its query string is a
    business identifier. It is not a secret, but a message that prints it trains everyone
    to expect request parameters in logs, and the next parameter is a credential.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def provider_operation(method: str, url: str) -> str:
    """A bounded ``operation`` label for one provider call: its method and its path shape.

    ``POST https://api.razorpay.com/v1/payments/pay_Nx9c/refund`` becomes
    ``POST /payments/{id}/refund``. The version prefix is dropped because it is constant,
    the query string never appears because a receipt lives there, and any segment outside
    :data:`_PATH_VOCABULARY` is replaced -- so the cardinality of this label is the number
    of endpoints this platform calls, which is five, and no growth in payments can raise
    it.
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if segments and segments[0].startswith("v") and segments[0][1:].isdigit():
        segments = segments[1:]
    shape = "/".join(segment if segment in _PATH_VOCABULARY else "{id}" for segment in segments)
    return f"{method.upper()} /{shape}"


def _status_outcome(status: int) -> str:
    """The verdict for a status the provider actually answered with.

    Deliberately three values and not sixty. *Which* 4xx it was is a question for the
    adapter's classification and for the ``provider_requests`` row that records it; the
    counter's job is to say whether Razorpay is refusing us or falling over, which are
    different incidents with different responses.
    """
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    return "ok"


class HttpxTransport:
    """Send one :class:`HttpRequest` and return one :class:`HttpResponse`. Nothing else.

    Satisfies :class:`payment_adapters.HttpTransport` structurally; the suite asserts the
    ``isinstance`` against that runtime-checkable protocol so a signature drift is caught
    here rather than at the first real payment.

    The client is created once and reused, because a fresh connection per provider call
    adds a TLS handshake to the latency budget of every payment. Close it with
    :meth:`close`, or use the instance as a context manager.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._owns_client = client is None
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            # Explicit, not inherited from the default. ``retries`` is the single setting
            # that decides whether one create-order can become two provider orders.
            transport=httpx.HTTPTransport(retries=0),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            timeout=self._timeout(self._timeout_seconds),
        )

    def _timeout(self, total: float) -> httpx.Timeout:
        return httpx.Timeout(total, connect=min(self._connect_timeout_seconds, total))

    def send(self, request: HttpRequest) -> HttpResponse:
        """Perform exactly one HTTP round trip.

        Returns an :class:`HttpResponse` for every status the provider answers with,
        including 4xx and 5xx: a 400 is information the adapter needs in order to say
        "definitely not performed", not an error.

        Raises :class:`TransportTimeoutError` when the request exceeds
        ``request.timeout_seconds`` and :class:`TransportError` for every other failure to
        obtain a response. The ``except Exception`` fallback is deliberate: an
        unclassified failure must reach the adapter as "the outcome is unknown", and
        letting it escape as some other exception type would let a caller's generic
        handler decide -- probably by retrying -- that nothing was sent.

        The metrics handle comes from the correlation scope the worker bound around this
        leased command, so a provider call is attributed to the tenant whose money it moves
        without this module being handed a tenant it has no other use for. Outside a bound
        scope -- a unit test sending against a stub -- the handle is inert and the call is
        counted as a drop rather than attributed to a guess.

        A failure to obtain a response is recorded as outcome ``error`` rather than as
        ``timeout``: :func:`~platform_observability.timed` classifies any exception leaving
        its block that way, and the alternative would be to catch the exception in order to
        label it, which is precisely the thing an observability layer may not do. The
        distinction survives where it matters -- in the exception type the adapter receives
        and in the ``provider_requests`` row it writes.
        """
        metrics = default_registry().for_current_scope()
        with timed(
            metrics,
            PROVIDER_TIMING,
            provider=PROVIDER_LABEL,
            operation=provider_operation(request.method, request.url),
        ) as span:
            response = self._round_trip(request)
            span.set_outcome(_status_outcome(response.status))
            return response

    def _round_trip(self, request: HttpRequest) -> HttpResponse:
        """The send itself, unchanged: one attempt, no retry, every failure an exception."""
        timeout = request.timeout_seconds or self._timeout_seconds
        try:
            response = self._client.request(
                request.method,
                request.url,
                headers=dict(request.headers),
                content=request.body or None,
                auth=request.auth,
                timeout=self._timeout(float(timeout)),
            )
        except httpx.TimeoutException as exc:
            raise TransportTimeoutError(
                f"{request.method} {safe_url(request.url)} timed out after {timeout}s; "
                "the provider may still have performed the mutation"
            ) from exc
        except Exception as exc:
            raise TransportError(
                f"{request.method} {safe_url(request.url)} failed: {type(exc).__name__}"
            ) from exc

        return HttpResponse(
            status=response.status_code,
            body=response.content,
            headers={key.lower(): value for key, value in response.headers.items()},
        )

    def close(self) -> None:
        """Close the underlying client if this transport created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        self.close()

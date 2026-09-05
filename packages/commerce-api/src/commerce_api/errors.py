"""RFC 9457 problem details, and the one table that decides an error's HTTP status.

ADR 0003 D15 in full:

> Errors are RFC 9457 problem details. ``RecoveryCode`` maps to HTTP status in one table
> in ``commerce_api.errors``; **kernel denials are 200 with the structured decision,
> never 4xx, because a denial is the system working.**

That last clause is the reason this module exists as a separate thing from a generic
exception handler. A refused submit -- stale approval, expired reservation, revoked
authority, Safe Mode -- is not a fault. It is the platform doing exactly what it was
built to do, and it carries a :class:`~transaction_kernel.KernelDecision` with the
deltas, the next version and the reason key the buyer surface needs. Delivering that as
a 409 would tell every HTTP client in the chain to treat consent as a transient failure
and retry it, which is how a buyer gets asked to approve the same purchase four times.

So there are two exits from a service, and only two:

============================  ==================================================
The kernel answered           :func:`decision_response` -- HTTP 200, decision body
Something went wrong          :func:`problem` / :class:`ProblemError` -- RFC 9457
============================  ==================================================

Status selection is deliberately layered, most specific first:

1. :data:`STATUS_BY_EXCEPTION` -- an exception type named explicitly, because a rule
   worth writing down (D9's 422 for a reused idempotency key) should be findable by
   grepping the class name, not deduced through a code.
2. :data:`STATUS_BY_RECOVERY_CODE` -- the D15 table, used for any exception that carries
   a ``code``. The kernel's own error classes do; that is why they have one.
3. 500. An exception nobody classified is a bug in this service, not a client error.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

from commerce_domain import CanonicalizationError, CurrencyMismatchError, DomainError, MoneyError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from payment_adapters import (
    ConfigurationError,
    EvidenceMismatchError,
    RazorpayAdapterError,
    RefundNotPermittedError,
    RequestConstructionError,
    SignatureMismatchError,
    TransportError,
    TransportTimeoutError,
    UnmappableEventError,
)
from platform_db import TenantContextError
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from transaction_kernel import KernelDecision, RecoveryCode
from transaction_kernel.admission import AdmissionError
from transaction_kernel.audit import AuditError
from transaction_kernel.authority import AuthorityError
from transaction_kernel.checkouts import CheckoutTenantError, CheckoutUsageError
from transaction_kernel.grants import GrantError, GrantNotFoundError
from transaction_kernel.idempotency import (
    IdempotencyInFlightError,
    IdempotencyKeyReuseError,
    IdempotencyUsageError,
)
from transaction_kernel.payments import (
    AttemptNotFoundError,
    PaymentsTenantError,
    PaymentsUsageError,
)
from transaction_kernel.refunds import RefundError, RefundNotFoundError, RefundUsageError
from transaction_kernel.safe_mode import SafeModeBlockedError, SafeModeError

from .schemas import DecisionOut, ProblemOut

__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "STATUS_BY_EXCEPTION",
    "STATUS_BY_RECOVERY_CODE",
    "ProblemDetail",
    "ProblemError",
    "decision_payload",
    "decision_response",
    "install_error_handlers",
    "problem",
    "status_for",
]

_log = logging.getLogger("commerce_api.errors")

#: RFC 9457 section 3. A client that content-negotiates sees a problem, not an object it
#: might mistake for a successful payload.
PROBLEM_MEDIA_TYPE: Final[str] = "application/problem+json"

#: :class:`commerce_api.schemas.ProblemOut` under the name the rest of the service uses.
#: The model lives in ``schemas`` because that module is the wire contract and imports
#: nothing from this package; this alias keeps ``from .errors import ProblemDetail``
#: working for code that thinks in errors rather than in schemas.
ProblemDetail = ProblemOut


# --------------------------------------------------------------------- D15: the table


#: The ADR 0003 D15 table: a :class:`RecoveryCode` carried by an *exception* becomes this
#: status. A code carried by a :class:`KernelDecision` becomes 200 and never consults
#: this mapping -- see :func:`decision_response`.
#:
#: Total over the enum on purpose, asserted by ``test_capi_foundation``. A code added to
#: the kernel without a status here would otherwise silently become a 500.
STATUS_BY_RECOVERY_CODE: Final[Mapping[RecoveryCode, int]] = {
    # A success code should never reach an error handler; mapped anyway so the table is
    # total and a mistake shows up as an odd 200 rather than as a KeyError.
    RecoveryCode.OK: 200,
    RecoveryCode.DUPLICATE_OPERATION: 200,
    # State the caller may resolve and retry.
    RecoveryCode.CONCURRENT_OPERATION: 409,
    RecoveryCode.STALE_CHECKOUT: 409,
    RecoveryCode.REAPPROVAL_REQUIRED: 409,
    RecoveryCode.RESERVATION_EXPIRED: 409,
    RecoveryCode.PAYMENT_UNKNOWN: 409,
    RecoveryCode.STALE_CAPTURE: 409,
    RecoveryCode.REFUND_REVIEW_REQUIRED: 409,
    RecoveryCode.RESOLUTION_PLAN_EXPIRED: 409,
    RecoveryCode.HUMAN_REVIEW_REQUIRED: 409,
    # Authority. 403, not 401: the caller is authenticated and simply may not do this.
    RecoveryCode.AUTHORITY_REVOKED: 403,
    RecoveryCode.AUTHORITY_INSUFFICIENT: 403,
    # 402 is the one status that means precisely "the payment did not happen".
    RecoveryCode.PAYMENT_FAILED: 402,
    # Accepted, still running. Not 200: nothing has completed.
    RecoveryCode.PAYMENT_PENDING: 202,
    RecoveryCode.RECONCILIATION_IN_PROGRESS: 202,
    # A permitted remedy that reached an error path is a well-formed answer.
    RecoveryCode.REFUND_ALLOWED: 200,
    RecoveryCode.RESOLUTION_PLAN_ISSUED: 200,
    # The request was understood and refused on its content.
    RecoveryCode.POLICY_EXCEPTION: 422,
    # The kill switch is a temporary condition of the service, not of the request.
    RecoveryCode.SAFE_MODE_ACTIVE: 503,
    # So is a dependency that cannot answer. 409 would be the wrong sentence entirely:
    # it tells the caller the request conflicted with a state it can resolve, which
    # invites a buyer to re-approve a purchase that nothing was ever wrong with.
    RecoveryCode.CONNECTOR_UNAVAILABLE: 503,
}


#: Exception types whose status is decided by name, consulted before the code table.
#: Ordered most specific first and walked with ``isinstance``, so a subclass listed above
#: its base wins.
#:
#: Each entry is here because the type alone determines the answer:
#:
#: * ``IdempotencyKeyReuseError`` -> **422** is ADR 0003 D9. Its ``code`` is
#:   ``POLICY_EXCEPTION``, which maps to 422 as well; it is named here anyway so the rule
#:   is greppable by class rather than inferred.
#: * ``*UsageError`` and ``TenantContextError`` -> **500**. These mean this service called
#:   the kernel wrongly. Reporting a 4xx would blame the client for our bug.
#: * ``TransportTimeoutError`` -> **504** and ``TransportError`` -> **502**: both mean the
#:   provider outcome is unknown, and neither is the caller's fault.
#: * ``ConfigurationError`` -> **500**: a misconfigured process, never a bad request.
STATUS_BY_EXCEPTION: Final[tuple[tuple[type[BaseException], int], ...]] = (
    # --- idempotency (ADR 0003 D9) ---
    (IdempotencyKeyReuseError, 422),
    (IdempotencyInFlightError, 409),
    (IdempotencyUsageError, 500),
    # --- our own bugs ---
    (TenantContextError, 500),
    (CheckoutUsageError, 500),
    (PaymentsUsageError, 500),
    (RefundUsageError, 500),
    (AdmissionError, 500),
    (AuditError, 500),
    (ConfigurationError, 500),
    # --- wrong tenant: authenticated, not entitled ---
    (CheckoutTenantError, 403),
    (PaymentsTenantError, 403),
    (AuthorityError, 403),
    # --- absent things ---
    (GrantNotFoundError, 404),
    (AttemptNotFoundError, 404),
    (RefundNotFoundError, 404),
    # --- the provider ---
    (TransportTimeoutError, 504),
    (TransportError, 502),
    (SignatureMismatchError, 400),
    (RequestConstructionError, 422),
    (UnmappableEventError, 422),
    (EvidenceMismatchError, 409),
    (RefundNotPermittedError, 409),
    (RazorpayAdapterError, 502),
    # --- kill switch ---
    (SafeModeBlockedError, 503),
    (SafeModeError, 503),
    # --- money values ---
    (CurrencyMismatchError, 422),
    (CanonicalizationError, 422),
    (MoneyError, 422),
    # --- database ---
    (IntegrityError, 409),
    (ValidationError, 422),
    # --- broad kernel families that carry no code of their own ---
    (GrantError, 409),
)


def status_for(exc: BaseException) -> int:
    """The HTTP status for an exception, by the layering in the module docstring.

    Never raises. An unclassified exception is a 500, which is the honest answer: this
    service did not anticipate it.
    """
    for exception_type, status in STATUS_BY_EXCEPTION:
        if isinstance(exc, exception_type):
            return status
    code = getattr(exc, "code", None)
    if isinstance(code, RecoveryCode):
        return STATUS_BY_RECOVERY_CODE[code]
    if isinstance(exc, DomainError):
        # A refusal from a deterministic component that carries no code: understood and
        # declined, which is 409 rather than 500.
        return 409
    return 500


# ------------------------------------------------------------------ building a problem


class ProblemError(Exception):
    """Raise this to answer with a specific problem detail.

    For refusals this service decides itself -- no session, not the owner, a missing
    ``Idempotency-Key`` -- where there is no kernel exception to map. Everything the
    handler needs travels on the exception, so a router raises and returns nothing::

        raise ProblemError(404, "Checkout not found", instance=str(request.url.path))

    ``extensions`` become RFC 9457 extension members, siblings of ``title`` and
    ``status``. Put the machine-readable part there (``code``, ``field``, ``constraint``)
    and keep ``detail`` for the sentence a human reads.
    """

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        type_uri: str = "about:blank",
        instance: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(f"{status} {title}: {detail or ''}".strip())
        self.status = status
        self.title = title
        self.detail = detail
        self.type_uri = type_uri
        self.instance = instance
        self.extensions = extensions


def problem(
    status: int,
    title: str,
    detail: str | None = None,
    *,
    type_uri: str = "about:blank",
    instance: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    """Build an RFC 9457 problem response.

    ``status`` and ``title`` are required; ``detail`` is the human sentence; everything
    passed as a keyword becomes an extension member. The media type is always
    ``application/problem+json``.

    Use this where you already have a response to return. Where you are deep in a service
    and want to abort, raise :class:`ProblemError` instead -- the installed handler calls
    this function with the same arguments.
    """
    body = ProblemOut(
        type=type_uri,
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        **extensions,
    )
    return JSONResponse(
        content=body.model_dump(by_alias=True, exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers) if headers else None,
    )


def _problem_from_exception(request: Request, exc: BaseException) -> JSONResponse:
    """Turn any classified exception into a problem, disclosing nothing extra.

    A 5xx never carries ``str(exc)``: an internal message can name a table, a constraint
    or a fragment of a query. It is logged with its traceback instead, keyed by the same
    path the client sees.
    """
    status = status_for(exc)
    extensions: dict[str, Any] = {}
    code = getattr(exc, "code", None)
    if isinstance(code, RecoveryCode):
        extensions["code"] = code.value
    if isinstance(exc, IntegrityError):
        # The constraint name is the actionable part of a 23505: it says *which*
        # uniqueness rule the caller collided with. The SQL and parameters are not
        # included -- they can carry buyer data.
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint:
            extensions["constraint"] = constraint
    if isinstance(exc, ValidationError):
        extensions["errors"] = exc.errors(include_url=False, include_context=False)

    if status >= 500:
        _log.exception("unhandled failure serving %s", request.url.path, exc_info=exc)
        detail = None
    else:
        detail = str(exc)

    return problem(
        status,
        type(exc).__name__,
        detail,
        instance=request.url.path,
        **extensions,
    )


# ---------------------------------------------------------------- D15: the 200 answer


def decision_payload(decision: KernelDecision) -> DecisionOut:
    """The serialisable form of a kernel decision, allowed or denied.

    Split from :func:`decision_response` because most routes embed the decision in a
    larger body -- a submit answers ``{decision, outcome, attempt_id, checkout}`` -- and
    every one of them must serialise the decision identically.
    """
    return DecisionOut.from_kernel(decision)


def decision_response(
    decision: KernelDecision,
    *,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Answer with a kernel decision. **Always HTTP 200, allowed or denied (D15).**

    The decision's fields are the top level of the body::

        {"decision_id": "...", "allowed": false, "code": "REAPPROVAL_REQUIRED",
         "explanation": "...", "checkout": {...}, "deltas": [...], "next_version": 2, ...}

    ``extra`` adds sibling keys for routes whose contract wraps the decision -- pass
    ``{"outcome": ..., "attempt_id": ..., "checkout": ...}`` and they appear beside the
    decision's own fields. A key in ``extra`` that collides with a decision field is
    refused rather than silently overwriting the kernel's answer.

    There is no ``status_code`` parameter, and that is the point: a caller cannot decide
    that this particular denial deserves a 4xx.
    """
    body: dict[str, Any] = decision_payload(decision).model_dump(mode="json")
    if extra:
        collisions = sorted(set(extra) & set(body))
        if collisions:
            raise ValueError(
                f"extra keys {collisions} would overwrite the kernel decision's own "
                "fields; a decision is reported verbatim (ADR 0003 D15)"
            )
        body.update(extra)
    return JSONResponse(content=body, status_code=200, headers=dict(headers) if headers else None)


# ------------------------------------------------------------------------- handlers


#: Roots of every exception family this service classifies. ``DomainError`` covers the
#: kernel's own errors, merchant-sim and the Razorpay adapter; the rest subclass
#: ``Exception`` or ``RuntimeError`` directly and so need naming here.
_MAPPED_ROOTS: Final[tuple[type[Exception], ...]] = (
    DomainError,
    GrantError,
    RefundError,
    RefundUsageError,
    SafeModeError,
    AuthorityError,
    AdmissionError,
    TenantContextError,
    IntegrityError,
    ValidationError,
)


def _http_title(status: int) -> str:
    """The reason phrase for a status, or a generic title for a non-standard one."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that make every error on this app a problem detail.

    Registered against the *roots* of each exception family rather than every leaf:
    Starlette dispatches on the most derived registered class, and
    :func:`status_for` does the fine-grained work. Adding a new kernel error class
    therefore needs no change here -- only a line in one of the two tables if its default
    is wrong.
    """

    async def on_problem(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProblemError)
        return problem(
            exc.status,
            exc.title,
            exc.detail,
            type_uri=exc.type_uri,
            instance=exc.instance or request.url.path,
            **exc.extensions,
        )

    async def on_mapped(request: Request, exc: Exception) -> JSONResponse:
        return _problem_from_exception(request, exc)

    async def on_http_exception(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        headers = getattr(exc, "headers", None)
        return problem(
            exc.status_code,
            _http_title(exc.status_code),
            str(exc.detail) if exc.detail else None,
            instance=request.url.path,
            headers=headers,
        )

    async def on_request_validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return problem(
            422,
            "Request validation failed",
            "The request body, query or path did not match the endpoint's schema.",
            instance=request.url.path,
            errors=exc.errors(),
        )

    async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("unhandled failure serving %s", request.url.path, exc_info=exc)
        return problem(
            500,
            "Internal Server Error",
            None,
            instance=request.url.path,
        )

    app.add_exception_handler(ProblemError, on_problem)
    app.add_exception_handler(StarletteHTTPException, on_http_exception)
    app.add_exception_handler(RequestValidationError, on_request_validation)
    for root in _MAPPED_ROOTS:
        app.add_exception_handler(root, on_mapped)
    app.add_exception_handler(Exception, on_unhandled)

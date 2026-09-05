"""The ACP transport: the external AI-buyer surface, reachable over HTTP.

``commerce_protocols.acp`` is the gate -- two credential mechanisms, a canonical signing
string with audience binding, the freshness window, the nonce store, transport limits,
rate limits and the session mapping -- and until now nothing could reach it. This router is
the transport. It reads the raw bytes, resolves the endpoint, hands the request to
:func:`~commerce_protocols.acp.admit`, and then honours whatever intent the mapping
produced by calling the same services the buyer's browser calls.

The order below is the security property, and it is the order ``webhooks.py`` uses for the
same reason
-------------------------------------------------------------------------------------
1. **Raw bytes first, capped while streaming.** The signature covers the exact bytes the
   caller sent. Parsing and re-serialising produces a different byte sequence, so a handler
   that parses first can never verify a genuine request. ``await request.body()`` buffers
   whatever arrives and lets you measure it afterwards, which makes a limit advisory
   against exactly the caller it exists for; :func:`commerce_api.security.read_capped_body`
   stops at the cap instead.
2. **Resolve the endpoint from the method and the canonical path**, through
   ``acp.route``. The path is not normalised on the way in: the bytes the signature covers
   and the resource this process acts on must be the same string, and every interesting
   path-handling vulnerability of the last twenty years lives in the gap between them.
3. **Bind the tenant to the client the credential names, before anything is read.** See
   below.
4. **Admit**, which verifies the credential, opens the evidence chain, checks the pin, the
   rate limit, the freshness window, the nonce and the idempotency key -- in that order,
   and each refusal evidenced at the stage that refused it.
5. **Map** the admitted request onto one typed intent, which is where the version,
   approval, revocation, expiry and amount invariants are enforced.
6. **Honour** it, through ``basket_service``, ``checkout_service`` and
   ``admission_service``. Not before this line does anything change.

Why the tenant is bound from a pre-lookup
-----------------------------------------
``transaction_kernel.audit`` refuses an evidence row whose tenant is not the transaction's
bound one, and ``admit`` writes its first evidence row as soon as the credential verifies.
So the tenant has to be bound *before* ``admit`` runs -- and the tenant is a property of the
client, which ``admit`` is the thing that establishes.

The resolution is that both mechanisms find their client the same way, through the
``Signature-Client`` header, so this router looks the client up in the same registry by the
same header and binds that client's tenant. **The lookup decides nothing.** It grants no
authority and skips no check: an unregistered client id binds no tenant at all and
``admit`` refuses at credential verification, before an evidence row exists; a registered
one whose credential does not verify is refused there too. What the pre-lookup buys is a
bound tenant for the evidence chain, and what it cannot buy is admission.

The alternative was taking the tenant from a header or a body field, which is the one place
a tenant may never come from (specification invariant 14).

Answers
-------
A protocol rejection is an RFC 9457 problem carrying the ``RecoveryCode`` it was defined
with. A **kernel denial is HTTP 200** with the structured decision (ADR 0003 D15) -- an
external buyer told 409 that a purchase was refused would retry a human's consent, which is
the failure mode that rule exists to prevent.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from commerce_protocols.acp import (
    DEFAULT_MAX_REQUEST_AGE,
    MAX_BODY_BYTES,
    MUTATIONS,
    AcpOperation,
    AcpRequest,
    AdmittedRequest,
    ClientRegistry,
    TokenBucketLimiter,
    admit,
    idempotency_key_for,
    map_request,
    route,
)
from commerce_protocols.acp.auth import SIGNATURE_CLIENT_HEADER
from commerce_protocols.core import EvidenceStage
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from platform_db import set_tenant
from sqlalchemy.orm import Session

from ..deps import (
    IDEMPOTENCY_KEY_HEADER,
    RequestContext,
    merchant_registry,
    session_scope_for,
    settings_of,
)
from ..errors import ProblemError
from ..idempotency import idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..security import BodyTooLargeError, read_capped_body
from ..services import acp_transport, admission_service
from ..services.protocol_transport import (
    keep_refusal_evidence,
    protocol_context,
    registry_principal,
)
from ..settings import Settings

router = APIRouter(tags=["acp"])

#: Where this surface lives. One constant, matching ``acp.sessions.BASE_PATH``: the
#: signature covers the path, so a router that disagreed with the signer about the prefix
#: would refuse every request with a message about cryptography.
BASE_PATH: Final[str] = "/acp/checkout_sessions"


def acp_settings(request: Request) -> Settings:
    """This process's settings, or 404 if the ACP surface does not exist here.

    404 rather than 401, matching the scenario controller's guard (ADR 0003 D11): a
    deployment that has issued no ACP credentials has no ACP surface, and a 401 would
    announce one that is merely shut.
    """
    settings = settings_of(request)
    if not settings.acp_routes_enabled:
        raise ProblemError(
            404,
            "Not Found",
            "The ACP transport is not configured in this profile.",
        )
    return settings


AcpSettings = Annotated[Settings, Depends(acp_settings)]
Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


def rate_limiter(request: Request) -> TokenBucketLimiter:
    """This app's ACP token buckets, created on first use. Separate from the MCP surface's.

    In-process, which would be dishonest in a horizontally scaled service and is not here:
    ADR 0003 D14 fixes this platform at one API process and ``Settings`` refuses
    ``WEB_CONCURRENCY > 1``, so this bucket is the whole system's bucket rather than one
    shard of it.
    """
    limiter = getattr(request.app.state, "acp_limiter", None)
    if not isinstance(limiter, TokenBucketLimiter):
        limiter = acp_transport.new_limiter()
        request.app.state.acp_limiter = limiter
    return limiter


Limiter = Annotated[TokenBucketLimiter, Depends(rate_limiter)]


# ----------------------------------------------------------------------------- routes


@router.post(BASE_PATH, summary="ACP: create a checkout session")
async def create_session(
    request: Request, settings: AcpSettings, registry: Registry, limiter: Limiter
) -> JSONResponse:
    """Open a basket, apply the items, and freeze a version if the session is ready at once."""
    return await _serve(request, settings, registry, limiter)


@router.post(BASE_PATH + "/{session_id}", summary="ACP: update a checkout session")
async def update_session(
    session_id: str, request: Request, settings: AcpSettings, registry: Registry, limiter: Limiter
) -> JSONResponse:
    """Amend the basket, or supply the last piece and freeze a version."""
    return await _serve(request, settings, registry, limiter, named=session_id)


@router.get(BASE_PATH + "/{session_id}", summary="ACP: retrieve a checkout session")
async def retrieve_session(
    session_id: str, request: Request, settings: AcpSettings, registry: Registry, limiter: Limiter
) -> JSONResponse:
    """Read the session as this platform currently projects it. Changes nothing."""
    return await _serve(request, settings, registry, limiter, named=session_id)


@router.post(BASE_PATH + "/{session_id}/complete", summary="ACP: complete a checkout session")
async def complete_session(
    session_id: str, request: Request, settings: AcpSettings, registry: Registry, limiter: Limiter
) -> JSONResponse:
    """The one operation that reaches money, and it still cannot move any by itself.

    Refused unless this platform holds an approval recorded on its own trusted surface
    against the exact version and content hash being completed. The payment credentials an
    ACP completion carries are the external platform's, collected from its own user, and
    their presence is not this platform's buyer having agreed to anything.

    **Always HTTP 200 for a kernel decision**, allowed or denied (ADR 0003 D15).
    """
    return await _serve(request, settings, registry, limiter, named=session_id)


@router.post(BASE_PATH + "/{session_id}/cancel", summary="ACP: ask a human to cancel a session")
async def cancel_session(
    session_id: str, request: Request, settings: AcpSettings, registry: Registry, limiter: Limiter
) -> JSONResponse:
    """A proposal, never an outcome. ``IntentKind`` has no ``CANCEL`` for exactly this reason."""
    return await _serve(request, settings, registry, limiter, named=session_id)


# ------------------------------------------------------------------------- the handler


async def _serve(
    request: Request,
    settings: Settings,
    registry: MerchantRegistry,
    limiter: TokenBucketLimiter,
    *,
    named: str | None = None,
) -> JSONResponse:
    """One handler for all five endpoints, because all five are one pipeline.

    The five routes exist so the surface is legible in OpenAPI and so FastAPI can refuse a
    path this platform does not serve before any of this runs. Which of them was reached is
    decided by ``acp.route`` over the method and the *signed* path, not by which function
    FastAPI dispatched to -- one source of truth for "what was asked", and it is the one the
    signature covers.

    ``named`` is FastAPI's own reading of the path parameter, and it is compared against
    ``acp.route``'s rather than used. Two parsers of one URL that disagree about which
    resource was named is the shape of every proxy-and-origin path confusion there has ever
    been; here it would mean the signature covered one session and the handler acted on
    another. They cannot disagree today -- neither normalises -- and the comparison is what
    keeps that true rather than assumed.
    """
    try:
        raw_body = await read_capped_body(request, limit=MAX_BODY_BYTES)
    except BodyTooLargeError as exc:
        raise ProblemError(
            413,
            "Payload too large",
            f"An ACP request body may not exceed {exc.limit} bytes.",
            limit=exc.limit,
        ) from None

    acp_request = AcpRequest.build(
        method=request.method,
        path=request.url.path,
        headers=dict(request.headers),
        body=raw_body,
        query=request.url.query,
    )
    resolved = route(acp_request.method, acp_request.path)
    if named is not None and resolved.session_id != named:  # pragma: no cover - see above
        raise ProblemError(
            400,
            "Path disagreement",
            "The routed session identifier and the signed path name different sessions.",
        )

    with session_scope_for(settings.database_url_kernel) as db:
        return _handle(
            db,
            settings=settings,
            registry=registry,
            limiter=limiter,
            acp_request=acp_request,
            operation=resolved.operation,
            session_id=resolved.session_id,
            requires_idempotency_key=resolved.requires_idempotency_key,
        )


def _handle(
    db: Session,
    *,
    settings: Settings,
    registry: MerchantRegistry,
    limiter: TokenBucketLimiter,
    acp_request: AcpRequest,
    operation: AcpOperation,
    session_id: str | None,
    requires_idempotency_key: bool,
) -> JSONResponse:
    """Bind the tenant, admit, map, honour. See the module docstring for why in that order."""
    clients: ClientRegistry = settings.acp_registry()
    named = clients.get(acp_request.header(SIGNATURE_CLIENT_HEADER))
    if named is not None:
        # The pre-lookup, and the whole of what it does. It binds a tenant so the evidence
        # chain has one; it grants nothing, and ``admit`` below still has to verify the
        # credential against this same client's secret before a single row is written.
        set_tenant(db, named.tenant_id)

    with keep_refusal_evidence(db):
        admitted = admit(
            db,
            acp_request,
            registry=clients,
            audience=str(settings.acp_audience),
            limiter=limiter,
            requires_idempotency_key=requires_idempotency_key,
        )
        ctx = protocol_context(
            caller=admitted.caller,
            principal=registry_principal(admitted.caller),
            # An ACP request has no session of its own: the credential is per request, and
            # the interaction is what identifies this one. Naming the interaction here means
            # the id in ``RequestContext`` and the id in the evidence chain are the same id,
            # so a row written by a service can be joined to the bytes that caused it.
            session_id=admitted.interaction.interaction_id,
            # The credential's validity horizon for this request, which is the only window
            # an ACP caller has. There is no longer-lived session to expire.
            expires_at=admitted.timestamp + DEFAULT_MAX_REQUEST_AGE,
        )
        projected = (
            None
            if session_id is None
            else acp_transport.load_session(
                db,
                ctx,
                session_id=session_id,
                supplied_now=acp_transport.supplied_by(admitted.body),
            )
        )
        intent = map_request(
            db,
            admitted=admitted,
            operation=operation,
            authority_epoch=0 if projected is None else projected.current_epoch,
            acp_session=None if projected is None else projected.session,
        )
        # Step 4 of specification 13.1. ``admit`` records RECEIVED and AUTHENTICATED and
        # ``map_request`` records nothing, so the row that says what the request *became*
        # is written here. Without it the inspector shows a request that arrived,
        # authenticated and then jumped straight to an answer.
        admitted.interaction.record(
            db,
            EvidenceStage.MAPPED,
            operation=operation.value,
            intent=intent.kind.value,
            moves_money=intent.moves_money,
            names_amount=intent.amount is not None,
            session_id=session_id,
        )

    if operation not in MUTATIONS:
        outcome = acp_transport.honour(
            db,
            ctx,
            registry,
            operation=operation,
            admitted=admitted,
            projected=projected,
            # A retrieval writes nothing and claims no key. ``requires_idempotency_key`` was
            # false at the gate for the same reason, so there is no header to pass on.
            idempotency_key="",
        )
        return _answer(db, admitted, outcome)

    return _mutate(
        db,
        ctx,
        registry,
        acp_request=acp_request,
        operation=operation,
        admitted=admitted,
        projected=projected,
    )


def _mutate(
    db: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    acp_request: AcpRequest,
    operation: AcpOperation,
    admitted: AdmittedRequest,
    projected: acp_transport.Projected | None,
) -> JSONResponse:
    """Run a mutation once per (tenant, key), and answer the same way a retry will.

    The key is scoped by client and by operation before it reaches the kernel's column, so
    one integration cannot burn another's key by guessing it and the same key on an update
    and on that update's completion stays two operations.

    The operation recorded against the key is ``PAYMENT_CREATE_ORDER`` for a completion and
    ``ACP_SESSION_MUTATION`` for everything else, matching what the trusted surface records
    for the same work. A completion additionally hands the key to admission, exactly as
    ``POST /v1/checkouts/{id}/versions/{v}/submit`` does -- one key, one operation, one
    execution.
    """
    presented = acp_request.header(IDEMPOTENCY_KEY_HEADER) or ""
    key = idempotency_key_for(admitted.caller.client_id, operation, presented)
    completing = operation is AcpOperation.COMPLETE_SESSION
    recorded = "PAYMENT_CREATE_ORDER" if completing else acp_transport.MUTATION_OPERATION
    payload = request_fingerprint(
        path_params={"path": acp_request.path, "operation": operation.value},
        body=dict(admitted.body),
    )
    try:
        with idempotent_mutation(db, ctx, key, recorded, payload) as slot:
            outcome = acp_transport.honour(
                db,
                ctx,
                registry,
                operation=operation,
                admitted=admitted,
                projected=projected,
                idempotency_key=key,
            )
            body = _record(db, admitted, outcome)
            slot.store(body)
    except admission_service.ConcurrentAdmission:
        # The single-winner index refused this attempt and the kernel rolled the whole
        # transaction back, this request's idempotency claim included. Answer with the
        # winner rather than a 500: one live attempt is the correct state, and the unused
        # key stays usable for a retry. Same handling as the trusted surface's submit.
        duplicate = admission_service.duplicate_after_race(db, ctx, _checkout_of(projected))
        # No evidence row: ``admit`` rolled the whole transaction back, this interaction's
        # chain included, so there is nothing left to append to. The winner's own
        # interaction carries the story of the attempt that exists.
        body = {
            "session": None,
            "decision": duplicate,
            "interaction_id": str(admitted.interaction.interaction_id),
        }
    return JSONResponse(content=body, status_code=200)


def _checkout_of(projected: acp_transport.Projected | None) -> uuid.UUID:
    """The checkout a losing completion raced for. Present by the time a race is possible."""
    if projected is None or projected.session.checkout_id is None:  # pragma: no cover
        raise ProblemError(500, "Internal Server Error", None)
    return projected.session.checkout_id


def _answer(
    db: Session, admitted: AdmittedRequest, outcome: acp_transport.AcpOutcome
) -> JSONResponse:
    return JSONResponse(content=_record(db, admitted, outcome), status_code=200)


def _record(
    db: Session, admitted: AdmittedRequest, outcome: acp_transport.AcpOutcome
) -> dict[str, Any]:
    """Close the evidence chain, and return the wire body.

    Steps 6 and 7 of specification 13.1. A kernel decision is recorded through
    ``record_decision`` so it is written the same way every other surface writes one --
    allowed and denied identically, deltas included -- and the answer is recorded whether
    or not there was a decision, so an interaction always ends with the thing that went
    back to the caller.

    The decision sits beside the session on the wire rather than inside it. It is the
    kernel's answer, reported verbatim, and nesting it under a protocol object would invite
    a future edit to reshape it into something the protocol found tidier.
    """
    if outcome.kernel_decision is not None:
        admitted.interaction.record_decision(db, outcome.kernel_decision)
    body = {
        "session": outcome.document,
        "decision": outcome.decision,
        "interaction_id": str(admitted.interaction.interaction_id),
    }
    admitted.interaction.record(
        db,
        EvidenceStage.ANSWERED,
        status=outcome.document.get("status"),
        checkout=(outcome.document.get("checkout") or {}).get("checkout_id"),
        decided=outcome.decision is not None,
        allowed=None if outcome.decision is None else outcome.decision.get("allowed"),
    )
    return body

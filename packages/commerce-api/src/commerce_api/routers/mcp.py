"""The MCP transport: the governed tool server, reachable over HTTP.

``commerce_protocols.mcp`` is a complete server that executes nothing, and until now
nothing could reach it. This router is the transport, and it is a transport in the narrow
sense: it reads headers, opens one transaction, binds a tenant, and hands the request to
:class:`~commerce_protocols.mcp.GovernedToolServer`. Every decision about what may happen
belongs to that package or to the services underneath it, and none of them is made here.

Five routes and what each is for
--------------------------------
``GET /.well-known/oauth-protected-resource``
    RFC 9728. What an MCP client fetches to learn which resource indicator this server
    answers to and where to get a token for it. Unauthenticated, because a resource
    server that will not say what it is cannot be talked to, and safe to be so because
    it publishes only its own name.

``POST /v1/mcp/token``
    The token endpoint. A live platform session is the grant; what comes back is a
    short-lived, single-use, audience-bound access token scoped to that session's
    capabilities narrowed to the protocol ceiling. See
    :mod:`commerce_api.services.mcp_transport` for why the exchange is real rather than a
    rename.

``POST /v1/mcp/sessions``
    Presents the access token and establishes a session whose tool allowlist is fixed at
    creation. Spends the token's ``jti`` through the platform's replay guard, so a second
    presentation of the same token is refused rather than granted a second session.

``GET /v1/mcp/tools``
    ``tools/list`` for one session, which is that session's allowlist and nothing wider. A
    tool the session may not call is not described as forbidden; it is not described.

``POST /v1/mcp/tools/call``
    ``tools/call``. Runs specification 13.1's pipeline, dispatches to a service, answers.

Why this is a separate file from ``routers/protocols.py``
---------------------------------------------------------
That router is deliberately read-only -- every route is a GET and a test asserts it over
the route table -- because it publishes profiles, the pinned matrix and the inspector, and
none of those should ever be a place where something happens. This surface has to accept
POSTs, so it lives beside that one rather than inside it, and carries its own tag so the
read-only assertion keeps meaning what it says.

Which transaction, and when the tenant is bound
-----------------------------------------------
The tenant an MCP request runs under is whatever its access token names, and that is not
known until the token has verified -- so it cannot be bound by a dependency before the
handler runs, the way it is for every bearer-token route. These handlers take
:data:`~commerce_api.deps.UnboundKernelSession` and bind the tenant themselves, as the
first statement against that session and before anything is read or written. Until then
every protected table returns nothing and every write is refused, which is the fail-closed
direction. ``transaction_kernel.audit`` refuses an evidence row whose tenant is not the
bound one, so a mistake here fails on the pipeline's first line rather than silently.

What a caller gets back when it is refused
------------------------------------------
A :class:`~commerce_protocols.core.ProtocolRejection` becomes an RFC 9457 problem through
the mapping in :mod:`commerce_api.errors`, carrying the ``RecoveryCode`` it was defined
with. A **kernel denial is not a refusal** and does not go that way: it is HTTP 200 with
the structured decision inside a successful tool result (ADR 0003 D15), because a denial is
the platform working and an MCP client that saw a 4xx would retry the buyer's consent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final

from commerce_domain import AdmissionDecision, AgentPrincipal
from commerce_protocols.acp import TokenBucketLimiter
from commerce_protocols.core import (
    PINS,
    PROTOCOL_CAPABILITIES,
    AuthenticationRejected,
    EvidenceStage,
    Protocol,
    database_now,
)
from commerce_protocols.mcp import (
    AdmittedCall,
    GovernedToolServer,
    McpSession,
    ToolCall,
    scope_for,
)
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from platform_db import set_tenant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..deps import (
    AppSession,
    IdempotencyKey,
    SessionContext,
    UnboundKernelSession,
    merchant_registry,
    settings_of,
)
from ..errors import ProblemError, decision_payload
from ..merchants import MerchantRegistry
from ..schemas import rfc3339
from ..security import bearer_token
from ..services import mcp_transport
from ..services.protocol_transport import (
    MCP_CHILD_ROLE,
    AdmissionDuplicated,
    PlatformAdmission,
    keep_refusal_evidence,
    narrow_to_protocol,
    protocol_context,
)
from ..settings import Settings

router = APIRouter(tags=["mcp"])

#: RFC 9728's well-known location for protected-resource metadata.
RESOURCE_METADATA_PATH: Final[str] = "/.well-known/oauth-protected-resource"

#: The MCP specification's version header, carried on every request that establishes a
#: session. ``require_pin`` refuses an absent one rather than defaulting: a client that has
#: not said which contract it believes it is speaking has not agreed to one.
PROTOCOL_VERSION_HEADER: Final[str] = "MCP-Protocol-Version"

#: The MCP specification's session header.
SESSION_HEADER: Final[str] = "Mcp-Session-Id"


# ------------------------------------------------------------------------ wire shapes


class TokenOut(BaseModel):
    """An OAuth 2.1 token response, plus the resource it is bound to.

    ``scope`` is what was actually granted, not what was asked for -- nothing here accepts
    a requested scope. A client that reads back fewer scopes than it expected is being told
    the truth about its session, and there is no parameter it could have sent to change the
    answer.
    """

    model_config = ConfigDict(extra="forbid")

    access_token: str
    #: RFC 6749 names this field ``token_type`` and its value here is the scheme, not a
    #: credential. The lint that reads the assignment as a hardcoded secret is reading
    #: the field name rather than the value.
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int
    scope: str
    resource: str
    #: The pinned MCP version this token is good against, so a client need not guess.
    protocol_version: str


class SessionOut(BaseModel):
    """One established MCP session and the tools it may call."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    resource: str
    protocol_version: str
    expires_at: str
    tools: list[dict[str, Any]]


class ToolsOut(BaseModel):
    """``tools/list``: this session's allowlist, described in full."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    tools: list[dict[str, Any]]


class ToolCallIn(BaseModel):
    """One ``tools/call``.

    ``nonce`` and ``requested_at`` are required rather than optional, matching
    :class:`~commerce_protocols.mcp.ToolCall`: a surface where the replay guard is opt-in
    is a surface where somebody's integration opts out.

    ``arguments`` is deliberately untyped here. Validating it against the tool's own schema
    is :meth:`~commerce_protocols.mcp.ToolSpec.normalise`'s job, and a second validation in
    this model would be a second opinion about what a tool accepts.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    nonce: str = Field(min_length=1, max_length=128)
    requested_at: datetime
    call_id: str | None = Field(default=None, max_length=128)


# ------------------------------------------------------------------------ the plumbing


def mcp_settings(request: Request) -> Settings:
    """This process's settings, or 404 if the MCP surface does not exist here.

    404 rather than 401, matching the scenario controller's guard (ADR 0003 D11): an
    unconfigured surface is absent, not locked, and a 401 would announce that it is there
    and merely shut.
    """
    settings = settings_of(request)
    if not settings.mcp_routes_enabled:
        raise ProblemError(
            404,
            "Not Found",
            "The MCP transport is not configured in this profile.",
        )
    return settings


McpSettings = Annotated[Settings, Depends(mcp_settings)]
Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


def session_store(request: Request) -> mcp_transport.McpSessionStore:
    """This app's live MCP sessions, created on first use.

    Attached to ``app.state`` rather than held in a module global so two apps in one test
    session hold separate sessions, exactly as they hold separate merchant registries.
    Created here rather than in ``create_app`` because ADR 0003 D2's rule that no build
    unit edits the application factory is worth more than saving one ``getattr``.
    """
    store = getattr(request.app.state, "mcp_sessions", None)
    if not isinstance(store, mcp_transport.McpSessionStore):
        store = mcp_transport.McpSessionStore.empty()
        request.app.state.mcp_sessions = store
    return store


def rate_limiter(request: Request) -> TokenBucketLimiter:
    """This app's MCP token buckets, created on first use. Separate from the ACP surface's.

    Separate because the two limiters key their buckets the same way, so one instance
    serving both would let a client's MCP traffic spend its ACP budget and hide which
    surface was actually being pushed.
    """
    limiter = getattr(request.app.state, "mcp_limiter", None)
    if not isinstance(limiter, TokenBucketLimiter):
        limiter = mcp_transport.new_limiter()
        request.app.state.mcp_limiter = limiter
    return limiter


SessionStore = Annotated[mcp_transport.McpSessionStore, Depends(session_store)]
Limiter = Annotated[TokenBucketLimiter, Depends(rate_limiter)]


def _server(settings: Settings, admission: PlatformAdmission | None) -> GovernedToolServer:
    """A tool server for one request.

    Built per request because :class:`PlatformAdmission` carries this request's context,
    registry and idempotency key, and a server held across requests would carry one
    caller's admission into another's call. The construction is three field assignments.

    ``admission`` is ``None`` on the routes that cannot reach the kernel -- opening a
    session, listing tools -- and a port that raises is passed in its place. A route that
    has no business admitting anything should not be holding something that could.
    """
    return GovernedToolServer(
        resource=str(settings.mcp_resource),
        introspector=mcp_transport.SignedTokenIntrospector(
            secret=_secret(settings), resource=str(settings.mcp_resource)
        ),
        admission=admission if admission is not None else _NoAdmission(),
    )


class _NoAdmission:
    """The admission port for a route that must not be able to admit anything.

    Not a stub and not a placeholder: it is the honest implementation of "this route has no
    route to money". ``open_session`` and ``tools/list`` never call the port, so reaching
    this method means the transport wired a request to the wrong handler, which is a fault
    in this service rather than something a caller did.
    """

    def admit_approved(
        self,
        session: Session,
        *,
        principal: AgentPrincipal,
        checkout_id: uuid.UUID,
        version: int,
        content_hash: str,
    ) -> AdmissionDecision:  # pragma: no cover - unreachable unless the router is miswired
        raise RuntimeError(
            "this route holds no admission port. Only POST /v1/mcp/tools/call may reach "
            "the kernel, and only for checkout.submit_approved, so arriving here means a "
            "request was wired to the wrong handler. Refused an admission for "
            f"{principal.principal_id} on checkout {checkout_id} version {version}, hash "
            f"{content_hash!r}, in a transaction bound={session.in_transaction()}."
        )


def _secret(settings: Settings) -> bytes:
    """The access-token signing key. Present by construction -- ``Settings`` refuses a
    configured MCP resource without one, so this narrowing is for the type checker."""
    secret = settings.mcp_token_secret
    if secret is None:  # pragma: no cover - Settings._check refuses this combination
        raise ProblemError(500, "Internal Server Error", None)
    return secret.get_secret_value().encode("utf-8")


def _bearer(authorization: str | None) -> str:
    """The presented access token, or the protocol layer's own refusal.

    Raised as an :class:`~commerce_protocols.core.AuthenticationRejected` rather than a
    ``ProblemError`` so that an absent credential and an unverifiable one reach the client
    as the same kind of thing, carrying the same ``RecoveryCode``. Distinguishing them here
    would tell an anonymous caller which half of its guess was wrong.
    """
    token = bearer_token(authorization)
    if token is None:
        raise AuthenticationRejected("mcp_access_token_not_presented")
    return token


def _held(store: mcp_transport.McpSessionStore, raw: str | None) -> mcp_transport.StoredSession:
    """The session named by the ``Mcp-Session-Id`` header, or a refusal.

    An unknown session id and a malformed one get the same answer. The header is the only
    thing that names a session, so a caller learning that its identifier parsed but was not
    found would learn that some other identifier of that shape exists.
    """
    try:
        session_id = uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise AuthenticationRejected("mcp_session_not_found") from exc
    held = store.get(session_id)
    if held is None:
        raise AuthenticationRejected("mcp_session_not_found")
    return held


def _resume(
    db: Session,
    *,
    held: mcp_transport.StoredSession,
    limiter: TokenBucketLimiter,
    now: datetime,
) -> tuple[McpSession, AgentPrincipal]:
    """Re-establish, for this request, everything the stored session only remembers.

    Three things happen here and each closes a window the store leaves open.

    The tenant is bound from the *session's* tenant, which came from the access token. It
    is the first statement this transaction runs, so nothing before it can read across a
    tenant and nothing after it can either.

    The originating ``api_sessions`` row is re-read and the protocol principal is
    re-derived from it. A stored session holds a principal minted up to fifteen minutes
    ago; the session behind it may have expired or been minted with capabilities that no
    longer apply, and honouring the stored copy would be honouring authority that had run
    out.

    The rate limit is charged. Keyed on the token's client id and the tenant -- both
    verified -- and never on a header, because limiting on an unverified name lets anyone
    exhaust a legitimate client's bucket by claiming to be it.
    """
    mcp_session = held.mcp_session
    set_tenant(db, mcp_session.tenant_id)
    limiter.take_for(
        now,
        tenant_id=mcp_session.tenant_id,
        client_id=mcp_session.client_id,
        rate=mcp_transport.MCP_CLIENT_RATE,
        reason="mcp_rate_limit_exhausted",
    )
    parent = mcp_transport.parent_principal(
        db,
        api_session_id=held.api_session_id,
        tenant_id=mcp_session.tenant_id,
        now=now,
    )
    principal = narrow_to_protocol(parent, caller=mcp_session.caller, child_role=MCP_CHILD_ROLE)
    if principal != mcp_session.principal:
        # The stored principal and the one the live session now yields disagree, which
        # means the session behind this one was narrowed after the MCP session opened.
        # Refused rather than reconciled: the allowlist was fixed against the wider set,
        # and silently serving the narrower one would leave a session whose published tool
        # list is not the list it will honour.
        raise AuthenticationRejected(
            "originating_session_capabilities_changed",
            session_id=str(mcp_session.session_id),
        )
    return mcp_session, principal


# ----------------------------------------------------------------------------- routes


@router.get(
    RESOURCE_METADATA_PATH,
    summary="RFC 9728 metadata: what this MCP resource server answers to",
)
def resource_metadata(settings: McpSettings, request: Request) -> dict[str, Any]:
    """Unauthenticated by design, and public by content.

    Everything here is a name this server would tell anybody who connected to it: its
    resource indicator, where to exchange a session for a token, the scopes it understands
    and the protocol version it is pinned at. There is no key material, no tenant and no
    client.
    """
    base = str(request.base_url).rstrip("/")
    pin = PINS[Protocol.MCP]
    return {
        "resource": settings.mcp_resource,
        "authorization_servers": [base],
        "token_endpoint": f"{base}/v1/mcp/token",
        "scopes_supported": sorted(scope_for(c) for c in PROTOCOL_CAPABILITIES),
        "bearer_methods_supported": ["header"],
        "mcp_protocol_version": pin.version,
        # Carried here as well as on /v1/protocols so a client that only ever reads this
        # document is still told what an MCP-compatible interface is not.
        "claim_boundary": pin.boundary.value,
        "disclaimer": pin.disclaimer,
    }


@router.post(
    "/v1/mcp/token",
    response_model=TokenOut,
    summary="Exchange a platform session for a short-lived MCP access token",
)
def mint_token(
    settings: McpSettings,
    ctx: SessionContext,
    db: AppSession,
    request: Request,
) -> TokenOut:
    """The token endpoint. The grant is the caller's own session; nothing else is read.

    The scopes are the session's capabilities intersected with the protocol ceiling, so a
    BUYER session's ``checkout.approve`` is gone before the token exists and an OPERATOR
    session gets a token that can read a catalogue and an order and build nothing. There is
    no requested-scope parameter, because a caller that could ask for scope would sooner or
    later be given it.

    No ``Idempotency-Key`` and no idempotency wrapper, deliberately. This writes nothing:
    it reads the clock and returns a signed value. A retry that produced the *same* token
    would be worse than one that produces a new one, because the first token may already
    have been spent on a session and its ``jti`` cannot be claimed twice.
    """
    granted = ctx.principal.capabilities & PROTOCOL_CAPABILITIES
    if not granted:
        raise ProblemError(
            403,
            "Session grants no protocol capability",
            "This session holds nothing an external protocol caller may ever hold, so a "
            "token minted from it could call no tool.",
            actor_type=ctx.actor_type.value,
        )
    issued = mcp_transport.issue_access_token(
        secret=_secret(settings),
        resource=str(settings.mcp_resource),
        issuer=str(request.base_url).rstrip("/"),
        ctx=ctx,
        granted=granted,
        now=database_now(db),
    )
    return TokenOut(
        access_token=issued.token,
        expires_in=int(mcp_transport.MCP_TOKEN_TTL.total_seconds()),
        scope=" ".join(sorted(issued.scopes)),
        resource=str(settings.mcp_resource),
        protocol_version=PINS[Protocol.MCP].version,
    )


@router.post(
    "/v1/mcp/sessions",
    response_model=SessionOut,
    status_code=201,
    summary="Establish a governed MCP session from an access token",
)
def open_mcp_session(
    settings: McpSettings,
    db: UnboundKernelSession,
    store: SessionStore,
    limiter: Limiter,
    authorization: Annotated[str | None, Header()] = None,
    mcp_protocol_version: Annotated[str | None, Header()] = None,
) -> SessionOut:
    """Spend one access token and fix one tool allowlist.

    The introspection happens twice and that is not an oversight. This handler needs the
    tenant *before* it can bind the transaction, and the tenant is a claim inside the
    token, so it reads the token once to learn it; ``open_session`` then reads it again as
    the authoritative check, together with the audience, the declared lifetime, the clock
    and the ``jti`` claim. The first read decides nothing -- if it is wrong about anything,
    the second refuses -- and the alternative was taking the tenant from a header, which is
    the one thing a tenant may never come from.

    The ``jti`` claim is a write, which is why this route runs on the kernel role. It is
    also what makes an access token single-use: a second presentation finds its nonce
    already spent and is refused, so a stolen token leaves a refusal in the evidence chain
    instead of quietly opening a second session.
    """
    presented = _bearer(authorization)
    resource = str(settings.mcp_resource)
    introspector = mcp_transport.SignedTokenIntrospector(
        secret=_secret(settings), resource=resource
    )
    claims = introspector.introspect(presented)
    set_tenant(db, claims.tenant_id)

    now = database_now(db)
    limiter.take_for(
        now,
        tenant_id=claims.tenant_id,
        client_id=claims.client_id,
        rate=mcp_transport.MCP_CLIENT_RATE,
        reason="mcp_rate_limit_exhausted",
    )

    server = _server(settings, None)
    mcp_session = server.open_session(
        db,
        presented=presented,
        announced_version=mcp_protocol_version,
    )
    # The originating session is checked here as well as on every later request. A token
    # whose ``api_sessions`` row expired between the mint and the exchange is a token whose
    # authority is already gone, and opening a fifteen-minute session on it would be
    # granting more than the grant ever had.
    api_session_id = _originating_session(claims.client_id)
    parent = mcp_transport.parent_principal(
        db, api_session_id=api_session_id, tenant_id=claims.tenant_id, now=now
    )
    narrow_to_protocol(parent, caller=mcp_session.caller, child_role=MCP_CHILD_ROLE)
    store.put(mcp_session, api_session_id=api_session_id, now=now)

    return SessionOut(
        session_id=str(mcp_session.session_id),
        resource=resource,
        protocol_version=mcp_session.pin.version,
        expires_at=rfc3339(mcp_session.expires_at),
        tools=list(server.describe_tools(mcp_session)),
    )


@router.get(
    "/v1/mcp/tools",
    response_model=ToolsOut,
    summary="tools/list for one session: its allowlist, and nothing wider",
)
def list_tools(
    settings: McpSettings,
    db: UnboundKernelSession,
    store: SessionStore,
    limiter: Limiter,
    mcp_session_id: Annotated[str | None, Header()] = None,
) -> ToolsOut:
    """What this session may call.

    A tool the session may not call does not appear here at all. Not marked forbidden, not
    listed with a note: absent. A model that cannot see a tool does not spend a turn
    discovering it is denied, and a list that enumerates what a caller may *not* have is a
    map of the platform for anybody who obtains a low-scoped token.
    """
    held = _held(store, mcp_session_id)
    now = database_now(db)
    mcp_session, _ = _resume(db, held=held, limiter=limiter, now=now)
    if not mcp_session.is_live_at(now):
        raise AuthenticationRejected("session_expired", expired_at=rfc3339(mcp_session.expires_at))
    return ToolsOut(
        session_id=str(mcp_session.session_id),
        tools=list(_server(settings, None).describe_tools(mcp_session)),
    )


@router.post(
    "/v1/mcp/tools/call",
    summary="tools/call: one governed tool, admitted, dispatched and answered",
)
def call_tool(
    body: ToolCallIn,
    settings: McpSettings,
    db: UnboundKernelSession,
    store: SessionStore,
    limiter: Limiter,
    registry: Registry,
    key: IdempotencyKey,
    mcp_session_id: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Specification 13.1's pipeline, then a service, then an answer. **Always HTTP 200.**

    Not always 200 in the sense that nothing can fail -- a protocol rejection is a problem
    detail with the status its ``RecoveryCode`` maps to -- but always 200 for a *kernel
    decision*, allowed or denied (ADR 0003 D15). A denied ``checkout.submit_approved``
    comes back as a successful tool result carrying the structured decision, its recovery
    code and its deltas, because that is the platform working and an MCP client told 409
    would retry a buyer's consent.

    ``Idempotency-Key`` is required, and it is required for one reason rather than as a
    blanket rule: the platform's admission refuses to invent one, so the only tool that
    reaches the kernel needs the caller to supply it. The *replay* guard on this surface is
    the call's own ``nonce``, claimed through ``core.replay`` -- a repeated nonce is refused
    outright, which is a different thing from an idempotent retry and the two are kept
    apart deliberately.

    Everything commits together: the evidence chain, the nonce claim, whatever the tool
    wrote, and the admission if there was one. A refusal commits its evidence and nothing
    else, because the rejection propagates out of the handler and the transaction rolls
    back -- which is right, since there is then no state to explain.
    """
    held = _held(store, mcp_session_id)
    now = database_now(db)
    mcp_session, principal = _resume(db, held=held, limiter=limiter, now=now)

    ctx = protocol_context(
        caller=mcp_session.caller,
        principal=principal,
        session_id=mcp_session.session_id,
        expires_at=mcp_session.expires_at,
    )
    admission = PlatformAdmission(ctx=ctx, registry=registry, idempotency_key=key)
    server = _server(settings, admission)

    # The refusal-evidence block covers admission and nothing after it. A rejection raised
    # while a call is being authenticated, validated and mapped has written only evidence,
    # and that evidence is what specification 13.3 asks a reviewer to be able to read. Once
    # the call starts doing something, a refusal must take its partial work with it.
    with keep_refusal_evidence(db):
        admitted = server.admit_call(
            db,
            mcp_session,
            ToolCall(
                tool=body.tool,
                arguments=body.arguments,
                nonce=body.nonce,
                requested_at=body.requested_at,
                call_id=body.call_id,
            ),
        )

    if admitted.spec.reaches_kernel:
        content = _submit(db, server, mcp_session, admitted)
    else:
        content = dict(
            mcp_transport.dispatch(
                db,
                admitted=admitted,
                ctx=ctx,
                registry=registry,
                interaction_id=admitted.interaction.interaction_id,
            )
        )

    result = server.answer(db, admitted, content)
    payload = result.as_payload()
    payload["interaction_id"] = str(admitted.interaction.interaction_id)
    payload["correlation_id"] = str(ctx.correlation_id)
    return JSONResponse(content=payload, status_code=200)


def _submit(
    db: Session,
    server: GovernedToolServer,
    mcp_session: McpSession,
    admitted: AdmittedCall,
) -> dict[str, Any]:
    """The one tool that reaches money, and the two answers it can produce.

    A :class:`~commerce_domain.AdmissionDecision`, allowed or denied, recorded verbatim in
    the evidence chain by the protocol server and returned as the tool's content -- or ADR
    0003 D9's duplicate, where the single-winner index had already decided and no admission
    ran. The duplicate travels as
    :class:`~commerce_api.services.protocol_transport.AdmissionDuplicated` precisely because
    it is not a decision: inventing one would put a decision id in the evidence for a
    decision the kernel never made.

    The duplicate is recorded here rather than left silent. ``record_decision`` is not
    available to it -- there is no decision -- so it is written as an ordinary
    ``DECIDED``-stage row naming the attempt that won, which is what a reviewer
    reconstructing the interaction needs to see.
    """
    try:
        decision = server.submit_approved(db, mcp_session, admitted)
    except AdmissionDuplicated as duplicate:
        admitted.interaction.record(
            db,
            EvidenceStage.DECIDED,
            tool=admitted.spec.name.value,
            allowed=False,
            code=duplicate.body["code"],
            explanation=duplicate.body["explanation"],
            payment_attempt_id=duplicate.body["attempt_id"],
            admission_ran=False,
        )
        return dict(duplicate.body)
    return decision_payload(decision).model_dump(mode="json")


def _originating_session(client_id: str) -> uuid.UUID:
    """The ``api_sessions`` row a token's ``client_id`` names.

    The client id is minted as ``session:<uuid>`` by this deployment's own token endpoint
    and is covered by the token's signature, so it cannot be edited by a caller. Parsed
    rather than trusted: a value that is not of that shape means a token was issued by
    something other than this endpoint, and there is no session behind it to check.
    """
    prefix, _, raw = client_id.partition(":")
    try:
        if prefix != "session":
            raise ValueError(client_id)
        return uuid.UUID(raw)
    except ValueError as exc:
        raise AuthenticationRejected("mcp_access_token_not_verified") from exc

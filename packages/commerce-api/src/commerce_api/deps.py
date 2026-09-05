"""Request-scoped dependencies: who is asking, which database role answers, one transaction.

Everything a router needs before it may touch the kernel is assembled here, and the
shapes below are what the other four build units import. The rules they encode:

**One transaction per request, owned by the dependency.** ``app_session`` and
``kernel_session`` each yield a :class:`~sqlalchemy.orm.Session` inside a single
transaction that commits when the handler returns cleanly and rolls back when it raises.
A service **never** calls ``session.commit()``. That is not style: the kernel's
idempotency record must commit with the effect it describes and never without it
(``transaction_kernel.idempotency``), and a mid-service commit would split the two.

**Reads are the app role; mutations are the kernel role** (ADR 0003 D1). The app role
physically cannot write ``payment_attempts``, ``execution_grants``, ``orders`` or any
other financial table -- the grant set says so -- which is what turns "only the kernel
writes money" from a convention into something the database enforces.

**The tenant is bound as the transaction's first statement.** ``set_tenant`` writes a
transaction-local setting that row-level security reads. Bound first, so no statement in
the transaction can ever run unscoped; transaction-local, so it cannot leak onto the next
request that borrows this pooled connection.

**The tenant comes from the session, never from a request body.** The only thing a client
supplies is a bearer token. The token resolves to an ``api_sessions`` row, and that row
says which tenant, which merchant and which buyer this request is. A ``tenant_id`` in a
JSON body is ignored everywhere in this service.

Why these engines are built here rather than through ``platform_db.get_engine``: that
function resolves a role's URL from ``DATABASE_URL_<ROLE>`` -- literally
``DATABASE_URL_COMMERCE_APP`` -- and falls back to a shared ``DATABASE_URL`` when the
role-specific one is absent. ADR 0003 D1 makes the role boundary the security boundary,
so a silent fallback is precisely the failure this API must not have: it would run every
read with whatever privileges ``DATABASE_URL`` happens to carry, and nothing would look
wrong. :class:`commerce_api.settings.Settings` requires both URLs by name and they are
used verbatim.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Final

from commerce_domain import uuid7
from fastapi import Depends, Request
from platform_db import ApiSession, Checkout, set_tenant
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel import ActorType, AgentPrincipal, CheckoutState

from .errors import ProblemError
from .merchants import MerchantRegistry
from .security import (
    AUTHORIZATION_HEADER,
    SCENARIO_KEY_HEADER,
    bearer_token,
    constant_time_equals,
    hash_token,
)
from .settings import Settings

__all__ = [
    "AGENT_CAPABILITIES",
    "BUYER_CAPABILITIES",
    "MERCHANT_AGENT_CAPABILITIES",
    "OPERATOR_CAPABILITIES",
    "SUPPORT_AGENT_CAPABILITIES",
    "CORRELATION_ID_HEADER",
    "IDEMPOTENCY_KEY_HEADER",
    "AppSession",
    "IdempotencyKey",
    "KernelSession",
    "OwnedCheckout",
    "RequestContext",
    "ScenarioKey",
    "SessionContext",
    "UnboundAppSession",
    "UnboundKernelSession",
    "app_session",
    "assert_owner",
    "engine_for",
    "idempotency_key",
    "kernel_session",
    "merchant_registry",
    "require_owner",
    "require_scenario_key",
    "require_session",
    "session_scope_for",
    "settings_of",
    "unbound_app_session",
    "unbound_kernel_session",
]

IDEMPOTENCY_KEY_HEADER: Final[str] = "Idempotency-Key"
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-Id"

#: Registry B, trusted buyer-surface actions (specification 5.3, invariant 15). Only a
#: session minted for a human on the trusted surface may approve, reject, cancel or ask
#: for a refund. These are held apart from Registry A below because collapsing them is
#: exactly how an agent ends up able to consent on a buyer's behalf.
BUYER_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "catalogue.read",
        "basket.write",
        "checkout.create",
        "checkout.approve",
        "checkout.reject",
        "checkout.cancel",
        "checkout.submit_approved",
        "payment.verify",
        "refund.request",
        "order.read",
    }
)

#: Registry A, agent capabilities. An agent may discover, build a basket, construct a
#: checkout and submit one the buyer has already approved -- ``checkout.submit_approved``
#: is the capability the kernel's admission checks by name. It may not approve, reject,
#: cancel or request a refund: those are consent, and consent is not delegable to the
#: thing that proposed the purchase.
AGENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "catalogue.read",
        "basket.write",
        "checkout.create",
        "checkout.submit_approved",
        "order.read",
    }
)

#: Registry A capabilities the merchant's own agent may hold (specification 6.6). Reads
#: over the merchant's catalogue and its checkout metrics, plus the one proposal action,
#: which stages a change for a human to apply and moves nothing by itself.
#:
#: Defined here rather than in ``services.agent_service`` because that module imports this
#: one, and because a capability set is exactly the kind of thing that must have a single
#: home: two definitions of what an operator may do would eventually disagree, and the
#: disagreement would be invisible until one of them widened.
MERCHANT_AGENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "merchant.catalogue_health.read",
        "merchant.inventory_anomalies.read",
        "merchant.checkout_metrics.read",
        "merchant.growth_proposal.create",
    }
)

#: Support-side Registry A capabilities (specification 6.4.4). ``resolution.evaluate`` and
#: ``support.escalate`` produce a plan and a case; neither approves a refund, because a
#: refund is a buyer's consent and the kernel's admission, never a support decision.
SUPPORT_AGENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"policy.search", "resolution.evaluate", "support.escalate", "support.case.read"}
)

#: Registry C, the merchant operator surface. An operator lists and inspects, and the
#: scenario key on each request is what widens a read from "own" to "tenant"; the session
#: names who is reading for the audit trail.
#:
#: It carries the merchant agent capabilities too, which is what makes the Merchant
#: Copilot able to do anything at all. Without them the Growth Specialist routes
#: correctly, selects the right tool, and is refused at the gate for a capability its
#: session never held -- the system working, but working on an empty stage.
#:
#: Nothing here moves money. There is no approve, no pay, no refund and no revoke, and an
#: OPERATOR session can only be minted by a caller already holding the scenario key, so
#: this widens nothing an anonymous caller can reach.
OPERATOR_CAPABILITIES: Final[frozenset[str]] = (
    frozenset({"catalogue.read", "order.read"})
    | MERCHANT_AGENT_CAPABILITIES
    | SUPPORT_AGENT_CAPABILITIES
)
CAPABILITIES_BY_ACTOR: Final[dict[ActorType, frozenset[str]]] = {
    ActorType.BUYER: BUYER_CAPABILITIES,
    ActorType.AGENT: AGENT_CAPABILITIES,
    ActorType.OPERATOR: OPERATOR_CAPABILITIES,
}


# ------------------------------------------------------------------ engines, sessions


@lru_cache(maxsize=4)
def engine_for(url: str) -> Engine:
    """One engine per connection URL, for the life of the process.

    ``pool_pre_ping`` because a demo laptop suspends and PostgreSQL drops the connection;
    without it the first request after a lid-close fails with a stale socket rather than
    reconnecting. The pool is deliberately small: reuse across requests is the condition
    the tenant-leak test targets, so it must actually happen here too.
    """
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        future=True,
    )


@lru_cache(maxsize=4)
def _sessionmaker_for(url: str) -> sessionmaker[Session]:
    """``expire_on_commit=False`` so a handler may still read what it built after commit."""
    return sessionmaker(bind=engine_for(url), expire_on_commit=False, future=True)


def session_scope_for(url: str) -> _SessionScope:
    """A transactional session on ``url``: commit on clean exit, roll back on exception.

    Same contract as :func:`platform_db.session_scope`, taking an explicit URL instead of
    resolving one from the environment -- see the module docstring for why that
    difference matters.
    """
    return _SessionScope(_sessionmaker_for(url))


class _SessionScope:
    """Context manager form of the above. A class rather than ``@contextmanager`` so it
    can be used both by ``with`` blocks and by FastAPI's generator dependencies without
    a second wrapper."""

    __slots__ = ("_factory", "_session")

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._session: Session | None = None

    def __enter__(self) -> Session:
        session = self._factory()
        self._session = session
        session.begin()
        return session

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        session = self._session
        if session is None:  # pragma: no cover - __exit__ without __enter__
            return
        try:
            if exc_type is None:
                session.commit()
            else:
                session.rollback()
        finally:
            session.close()


# ------------------------------------------------------------------ process singletons


def settings_of(request: Request) -> Settings:
    """The settings this app was created with.

    Read from ``app.state`` rather than from the module-level cache so a test can build
    an app around a ``Settings`` constructed from a dictionary and never touch the
    process environment.
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):  # pragma: no cover - create_app always sets it
        raise RuntimeError("app.state.settings is not set; build the app with create_app()")
    return settings


def merchant_registry(request: Request) -> MerchantRegistry:
    """The process's simulated merchants (ADR 0003 D14: one process, one registry)."""
    registry = getattr(request.app.state, "merchants", None)
    if not isinstance(registry, MerchantRegistry):  # pragma: no cover
        raise RuntimeError("app.state.merchants is not set; build the app with create_app()")
    return registry


# ------------------------------------------------------------------- request identity


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is asking, resolved from the bearer token and from nothing else.

    Constructed once per request by :func:`require_session` and passed to every service.
    ``principal`` is the immutable :class:`~transaction_kernel.AgentPrincipal` the kernel
    audits against; the other fields are the same facts in the form the service layer
    reads them.

    ``correlation_id`` ties every audit row, outbox command and provider request for this
    request together. It is taken from the ``X-Correlation-Id`` header when the caller
    supplies a well-formed UUID -- so a storefront can follow one journey across
    requests -- and minted otherwise. It is an identifier for joining logs and carries no
    authority whatsoever.
    """

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    buyer_ref: str
    principal: AgentPrincipal
    correlation_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime

    @property
    def actor_type(self) -> ActorType:
        return self.principal.actor_type

    def can(self, capability: str) -> bool:
        return self.principal.can(capability)

    def require(self, capability: str) -> None:
        """Refuse the request unless the principal holds ``capability``.

        403, not 401: the caller is authenticated and simply may not do this. The message
        names the capability because a missing capability is a configuration fact, not a
        secret.
        """
        if not self.principal.can(capability):
            raise ProblemError(
                403,
                "Capability not held",
                f"This session may not perform {capability!r}.",
                capability=capability,
                actor_type=self.principal.actor_type.value,
            )


def require_session(request: Request) -> RequestContext:
    """Resolve ``Authorization: Bearer <token>`` into a :class:`RequestContext`.

    Opens its own short-lived app-role transaction rather than depending on
    ``app_session``: ``api_sessions`` is the one table with no row-level security,
    because resolving the token is the step that *discovers* the tenant and so cannot
    already require one.

    Answers 401 for an absent, unknown or expired token, with the same problem title in
    every case: distinguishing "no such token" from "expired token" would confirm to an
    attacker that a token they found was once real.

    Expiry is judged by the database clock read inside the same transaction, so a pod
    with a skewed clock cannot extend or shorten a session.
    """
    settings = settings_of(request)
    token = bearer_token(request.headers.get(AUTHORIZATION_HEADER))
    if token is None:
        raise _unauthorized("This endpoint requires an Authorization: Bearer <token> header.")

    digest = hash_token(token)
    with session_scope_for(settings.database_url_app) as session:
        row = session.execute(
            select(ApiSession).where(ApiSession.token_hash == digest)
        ).scalar_one_or_none()
        now = session.execute(select(func.now())).scalar_one()
        if row is None or not constant_time_equals(row.token_hash, digest):
            raise _unauthorized("The bearer token is not recognised.")
        if row.expires_at <= now:
            raise _unauthorized("The session has expired; mint a new one.")
        record = _SessionRecord(
            session_id=row.id,
            tenant_id=row.tenant_id,
            merchant_id=row.merchant_id,
            buyer_ref=row.buyer_ref,
            actor_type=ActorType(row.actor_type),
            capabilities=frozenset(row.capabilities),
            expires_at=row.expires_at,
        )

    correlation_id = _correlation_id(request)
    principal = AgentPrincipal(
        principal_id=f"session:{record.session_id}",
        tenant_id=record.tenant_id,
        actor_type=record.actor_type,
        agent_role=None,
        merchant_id=record.merchant_id,
        buyer_ref=record.buyer_ref,
        capabilities=record.capabilities,
        correlation_id=correlation_id,
    )
    return RequestContext(
        tenant_id=record.tenant_id,
        merchant_id=record.merchant_id,
        buyer_ref=record.buyer_ref,
        principal=principal,
        correlation_id=correlation_id,
        session_id=record.session_id,
        expires_at=record.expires_at,
    )


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    """The ``api_sessions`` row, detached from the transaction that read it."""

    session_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    buyer_ref: str
    actor_type: ActorType
    capabilities: frozenset[str]
    expires_at: datetime


def _unauthorized(detail: str) -> ProblemError:
    return ProblemError(401, "Not authenticated", detail)


def _correlation_id(request: Request) -> uuid.UUID:
    raw = request.headers.get(CORRELATION_ID_HEADER)
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            # A malformed correlation id is a client bug, not grounds to refuse a
            # payment. Mint one and carry on; the header was never authority.
            pass
    return uuid7()


SessionContext = Annotated[RequestContext, Depends(require_session)]


# -------------------------------------------------------------------- the transactions


def app_session(request: Request, ctx: SessionContext) -> Iterator[Session]:
    """A read transaction as ``commerce_app``, tenant bound as its first statement.

    Use for every GET. The app role has SELECT everywhere and INSERT/UPDATE only on the
    non-financial head tables, so a read path that accidentally writes money fails at the
    database rather than in review.
    """
    settings = settings_of(request)
    with session_scope_for(settings.database_url_app) as session:
        set_tenant(session, ctx.tenant_id)
        yield session


def kernel_session(request: Request, ctx: SessionContext) -> Iterator[Session]:
    """The mutation transaction as ``commerce_kernel``, tenant bound as its first statement.

    Exactly one of these per mutating request, and the whole mutation runs inside it: the
    kernel call, the idempotency record, the head-row update and the outbox enqueue all
    commit together or not at all. Do not commit inside a service; this dependency owns
    the transaction and committing early would publish an idempotency record for work
    that had not finished.
    """
    settings = settings_of(request)
    with session_scope_for(settings.database_url_kernel) as session:
        set_tenant(session, ctx.tenant_id)
        yield session


def unbound_app_session(request: Request) -> Iterator[Session]:
    """An app-role transaction with **no tenant bound**. The one exception, and why.

    Used only where the tenant is being discovered rather than assumed: minting a demo
    session from a tenant slug, and the webhook receiver resolving a tenant from its
    route. Every row-level-security-protected table returns nothing under this session
    until the caller binds a tenant with ``platform_db.set_tenant``, which is the
    fail-closed direction.
    """
    settings = settings_of(request)
    with session_scope_for(settings.database_url_app) as session:
        yield session


def unbound_kernel_session(request: Request) -> Iterator[Session]:
    """A kernel-role transaction with **no tenant bound**, for the protocol transports.

    The counterpart to :func:`unbound_app_session`, and it exists for the same reason and
    one more. An ACP or MCP request does not carry a bearer token this service can resolve:
    it carries an HTTP message signature or an OAuth access token, and the tenant is
    whatever *that* credential names. So the tenant cannot be bound by a dependency before
    the handler runs -- it is not known until the credential has verified.

    The handler therefore binds it itself, with ``platform_db.set_tenant``, as the first
    statement it runs against this session and before it reads or writes anything. Until it
    does, every row-level-security-protected table returns nothing and every write is
    refused, which is the fail-closed direction.

    Kernel role rather than app role because these transports write: an evidence chain, a
    replay nonce, and -- for the one request that reaches admission -- everything a submit
    writes. The app role cannot write a financial table at all, so a transport that
    accidentally reached one would fail at the database rather than in review.
    """
    settings = settings_of(request)
    with session_scope_for(settings.database_url_kernel) as session:
        yield session


AppSession = Annotated[Session, Depends(app_session)]
KernelSession = Annotated[Session, Depends(kernel_session)]
UnboundKernelSession = Annotated[Session, Depends(unbound_kernel_session)]
UnboundAppSession = Annotated[Session, Depends(unbound_app_session)]


# ------------------------------------------------------------------------- ownership


@dataclass(frozen=True, slots=True)
class OwnedCheckout:
    """A checkout head this session is entitled to act on.

    A detached copy rather than the ORM row, because the row belongs to whichever
    transaction read it and a service that held it across a commit would be reading a
    stale object without noticing.
    """

    checkout_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    basket_id: uuid.UUID
    buyer_ref: str
    current_version: int
    status: CheckoutState
    correlation_id: uuid.UUID
    updated_at: datetime


def assert_owner(session: Session, ctx: RequestContext, checkout_id: uuid.UUID) -> OwnedCheckout:
    """Confirm this session owns ``checkout_id``, or refuse with 404.

    Call this from inside a mutation's own kernel transaction. The dependency form,
    :func:`require_owner`, is for reads.

    404 rather than 403 on a checkout belonging to somebody else: a 403 confirms the
    identifier exists, which turns this endpoint into an oracle for enumerating other
    buyers' checkouts. The tenant predicate is written out even though row-level security
    already applies it -- defence in depth, and it makes the intent readable at the call
    site.
    """
    row = session.execute(
        select(Checkout).where(
            Checkout.id == checkout_id,
            Checkout.tenant_id == ctx.tenant_id,
        )
    ).scalar_one_or_none()
    if row is None or row.buyer_ref != ctx.buyer_ref:
        raise ProblemError(
            404,
            "Checkout not found",
            "No checkout with that identifier belongs to this session.",
            checkout_id=str(checkout_id),
        )
    return OwnedCheckout(
        checkout_id=row.id,
        tenant_id=row.tenant_id,
        merchant_id=row.merchant_id,
        basket_id=row.basket_id,
        buyer_ref=row.buyer_ref,
        current_version=row.current_version,
        status=CheckoutState(row.status),
        correlation_id=row.correlation_id,
        updated_at=row.updated_at,
    )


def require_owner(
    checkout_id: uuid.UUID, ctx: SessionContext, session: AppSession
) -> OwnedCheckout:
    """Dependency form of :func:`assert_owner` for read endpoints.

    The route's path parameter **must be named** ``checkout_id`` and typed ``uuid.UUID``;
    FastAPI resolves it from the path by that name. Declare it as
    ``owner: Annotated[OwnedCheckout, Depends(require_owner)]``.
    """
    return assert_owner(session, ctx, checkout_id)


# --------------------------------------------------------------- scenario, idempotency


def require_scenario_key(request: Request) -> str:
    """Guard the scenario controller and the operator views (ADR 0003 D11).

    Three outcomes, and the difference matters:

    * **404** when the profile is production, or when no ``SCENARIO_KEY`` is configured.
      The ADR says these routes "do not exist" outside a demo, and a 401 would announce
      that they do.
    * **401** when the route exists and the ``X-Scenario-Key`` header is absent or wrong.
      Here the endpoint is real and the operator simply mistyped a key, so saying so is
      help rather than disclosure.
    * the key, otherwise.

    The comparison is constant time.
    """
    settings = settings_of(request)
    if not settings.scenario_routes_enabled:
        raise ProblemError(
            404,
            "Not Found",
            "This endpoint does not exist in this profile.",
        )
    configured = settings.scenario_key
    supplied = request.headers.get(SCENARIO_KEY_HEADER)
    if configured is None or not constant_time_equals(supplied, configured.get_secret_value()):
        raise ProblemError(
            401,
            "Not authenticated",
            f"This endpoint requires a valid {SCENARIO_KEY_HEADER} header.",
        )
    return supplied or ""


def idempotency_key(request: Request) -> str:
    """The ``Idempotency-Key`` header, required on every mutation (specification 24.1).

    400 when it is missing or blank. It is refused rather than generated because a
    server-generated key defeats the purpose entirely: the client's retry would carry a
    different key and execute the operation a second time.

    The value is opaque and is not validated for shape here --
    :func:`commerce_api.idempotency.idempotent_mutation` refuses one the column cannot
    hold, so there is one place that knows the limit.
    """
    supplied = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if not supplied or not supplied.strip():
        raise ProblemError(
            400,
            "Idempotency-Key required",
            f"Every mutation must carry an {IDEMPOTENCY_KEY_HEADER} header so a retry "
            "replays the original result instead of executing a second time.",
            header=IDEMPOTENCY_KEY_HEADER,
        )
    return supplied.strip()


IdempotencyKey = Annotated[str, Depends(idempotency_key)]
ScenarioKey = Annotated[str, Depends(require_scenario_key)]

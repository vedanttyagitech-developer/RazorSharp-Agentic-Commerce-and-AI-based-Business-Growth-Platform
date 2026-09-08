"""The MCP surface's credential, its sessions, and what its thirteen tools actually do.

:mod:`commerce_protocols.mcp` is a complete governed tool server that executes nothing. It
needs two ports supplied and one credential defined, and this module is where a deployment
supplies them. Read it as three answers.

**What a token is here.** MCP is OAuth 2.1: a client presents an access token minted by an
authorization server, and this process is the resource server that checks it. This
deployment colocates the two, because the only identity it has is the ``api_sessions``
bearer token every other surface uses and there is no external identity provider to
delegate to. So the token endpoint takes a live platform session as the grant and issues a
short-lived, single-use, audience-bound access token scoped to that session's capabilities,
narrowed to the protocol ceiling.

That is a real exchange rather than a rename, and three properties come out of it that the
platform session does not have. The access token is audience-bound, so one minted here is
inert against another deployment. It declares exactly :data:`MCP_TOKEN_TTL`, which *is*
``mcp.authorization.MAX_TOKEN_LIFETIME`` rather than a copy of it, so the surface cannot be
configured into minting a credential it would then refuse. And its ``jti`` is claimed
through the platform's replay guard, so one token opens exactly one session and a second
presentation of a stolen one is a refusal in the evidence chain rather than a silent second
session.

**What a session is.** :class:`~commerce_protocols.mcp.McpSession` fixes a tool allowlist
at creation and holds a principal that cannot consent. It lives in this process, in a store
attached to the app, and never in the database: specification 25.4's ``protocol_sessions``
table was decided against (``docs/KNOWN_GAPS.md``), and a fifteen-minute in-process session
needs no schema. One process is a configured fact rather than an assumption -- ADR 0003
D14, enforced by ``Settings`` refusing ``WEB_CONCURRENCY > 1`` -- which is the same
reasoning ``acp.auth``'s rate limiter is written on.

The originating ``api_sessions`` row is re-read on every request the session serves, and
that is the part worth not losing. Without it a fifteen-minute MCP session would outlive an
expired browser session by up to fifteen minutes, and the tool call would run under
authority that had already run out.

**What a tool does.** Every one of the thirteen calls a service in this package with a
:class:`~commerce_api.deps.RequestContext` built by
:mod:`commerce_api.services.protocol_transport` -- the same functions the buyer's browser
reaches, under an identity that is provably narrower. There is no tool here with a private
route to anything, which is why :func:`dispatch` is a closed match over a closed enum and
not a handler map.

Three of the thirteen are proposals, and what they leave behind is worth stating plainly
because the alternative would be a lie. ``order.propose_cancellation``, ``refund.propose``
and ``support.escalate`` change nothing. What they produce is the protocol interaction's own
evidence stream -- a gapless, hash-chained, tenant-scoped record of the ask, readable at
``GET /v1/inspector/protocols/{interaction_id}`` and correlated to the buyer's journey at
``GET /v1/checkouts/{id}/protocol-evidence``. The result says exactly that and names the
identifier, rather than reporting a support case this platform does not create.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from commerce_domain import ActorType, AgentPrincipal, b64url, b64url_decode, uuid7
from commerce_protocols.acp import DEFAULT_CLIENT_RATE, RateLimit, TokenBucketLimiter
from commerce_protocols.core import AuthenticationRejected, StateRejected
from commerce_protocols.mcp import (
    MAX_TOKEN_LIFETIME,
    AccessToken,
    AdmittedCall,
    McpSession,
    ToolName,
    scope_for,
)
from merchant_sim import ProductView
from platform_db import ApiSession
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import RequestContext, assert_owner
from ..errors import ProblemError
from ..merchants import MerchantRegistry
from ..security import constant_time_equals
from . import cart_service, catalogue_service, checkout_service, refund_service

__all__ = [
    "MAX_LIVE_SESSIONS",
    "MCP_CLIENT_RATE",
    "MCP_TOKEN_TTL",
    "IssuedToken",
    "McpSessionStore",
    "SignedTokenIntrospector",
    "StoredSession",
    "dispatch",
    "issue_access_token",
    "parent_principal",
]

#: Domain separation for the token MAC. Prefixed to the signed bytes so a value minted for
#: this purpose can never verify as anything else this process HMACs with the same key.
_TOKEN_DOMAIN: Final[bytes] = b"MCP-ACCESS-TOKEN-v1\n"

#: How long an issued access token declares itself valid for. This *is* the ceiling
#: ``mcp.authorization`` refuses a longer declared window at, aliased rather than copied:
#: two numbers that must agree are one number, or one day they will not.
MCP_TOKEN_TTL: Final = MAX_TOKEN_LIFETIME

#: What one MCP client may spend. The same budget as an ACP client and for the same reason:
#: twenty at once covers a model working through a cart line by line, two per second is
#: far more than a conversation needs and far less than a loop.
MCP_CLIENT_RATE: Final[RateLimit] = DEFAULT_CLIENT_RATE

#: How many live sessions one process will hold. A session is small, expires in fifteen
#: minutes and is pruned on every access, so this is not a capacity plan -- it is the bound
#: that stops a caller minting sessions faster than they expire from growing the store
#: without limit.
MAX_LIVE_SESSIONS: Final[int] = 512

#: How many search results a tool answers with when the caller names no limit. Small,
#: because these go into a model's context and a grounded shortlist is worth more there
#: than a page.
_DEFAULT_SEARCH_LIMIT: Final[int] = 5


# ------------------------------------------------------------------------ the credential


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """One minted access token and the facts a client needs in order to use it."""

    token: str
    token_id: str
    expires_at: datetime
    scopes: frozenset[str]


def issue_access_token(
    *,
    secret: bytes,
    resource: str,
    issuer: str,
    ctx: RequestContext,
    granted: frozenset[str],
    now: datetime,
) -> IssuedToken:
    """Exchange a live platform session for a short-lived MCP access token.

    Every claim comes from the session and none from a request body. ``tenant_id``,
    ``merchant_id`` and ``sub`` are what the bearer token resolved to; ``scope`` is the
    session's own capabilities, already intersected with the protocol ceiling by the
    caller. A client that would like more scope has to be given a wider session, on a
    surface where widening one is a deliberate act.

    ``aud`` is written as a JSON array holding one entry. A bare string would work and
    would be easier to parse, but RFC 8707 resource indicators and JWT audiences are both
    legitimately multi-valued, so the introspector has to handle a list correctly anyway --
    and a verifier whose list-handling path its own issuer never exercises is a path nobody
    has tested.

    ``sid`` names the ``api_sessions`` row this token was minted from. It carries no
    authority: it is what lets every later request re-read that row and stop honouring the
    token the moment the session behind it expires.
    """
    token_id = str(uuid7())
    scopes = frozenset(scope_for(capability) for capability in granted)
    claims = {
        "jti": token_id,
        "iss": issuer,
        "aud": [resource],
        "sub": ctx.buyer_ref,
        "client_id": f"session:{ctx.session_id}",
        "tenant_id": str(ctx.tenant_id),
        "merchant_id": str(ctx.merchant_id),
        "sid": str(ctx.session_id),
        "scope": " ".join(sorted(scopes)),
        "iat": int(now.timestamp()),
        "exp": int((now + MCP_TOKEN_TTL).timestamp()),
    }
    payload = b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return IssuedToken(
        token=f"{payload}.{_mac(secret, payload)}",
        token_id=token_id,
        expires_at=now + MCP_TOKEN_TTL,
        scopes=scopes,
    )


def _mac(secret: bytes, payload: str) -> str:
    """The signature over one token's payload, domain-separated. See :data:`_TOKEN_DOMAIN`."""
    material = _TOKEN_DOMAIN + payload.encode("ascii")
    return b64url(hmac.new(secret, material, hashlib.sha256).digest())


@dataclass(frozen=True, slots=True)
class SignedTokenIntrospector:
    """Verifies the tokens this deployment issued. The one place a presented one is read.

    Implements :class:`~commerce_protocols.mcp.TokenIntrospector`, and inherits its
    contract whole: every failure raises the *same*
    :class:`~commerce_protocols.core.AuthenticationRejected` with the same reason. A bad
    signature, a truncated token, a claim set missing a field and an audience this server
    does not answer to are indistinguishable from outside, because a verifier that
    distinguishes them is a verifier an attacker can interrogate one bit at a time.

    Note what this class does *not* check: expiry, issuance skew and the declared lifetime
    ceiling. Those belong to ``mcp.authorization.open_session``, which judges them against
    the database clock rather than this process's, and duplicating them here would be a
    second clock to keep in step.

    ``resource`` is this server's RFC 8707 resource indicator, from configuration. The
    audience reduction below is why it is here rather than left to the caller.
    """

    secret: bytes
    resource: str

    def introspect(self, presented: str) -> AccessToken:
        try:
            return self._introspect(presented)
        except AuthenticationRejected:
            raise
        except Exception as exc:
            # Every malformed-input failure -- base64, JSON, a missing key, a UUID that
            # will not parse -- collapses into the same refusal. Letting a ValueError
            # escape would make a 500 the signal that a caller had found an interesting
            # shape, which is the oracle this class exists not to be.
            raise _not_verified() from exc

    def _introspect(self, presented: str) -> AccessToken:
        payload, _, mac = presented.partition(".")
        if not payload or not mac or not constant_time_equals(mac, _mac(self.secret, payload)):
            raise _not_verified()
        claims = json.loads(b64url_decode(payload))
        if not isinstance(claims, dict):
            raise _not_verified()
        scope = str(claims.get("scope") or "")
        return AccessToken(
            token_id=str(claims["jti"]),
            client_id=str(claims["client_id"]),
            subject=str(claims["sub"]),
            issuer=str(claims["iss"]),
            audience=self._reduce_audience(claims.get("aud")),
            tenant_id=uuid.UUID(str(claims["tenant_id"])),
            merchant_id=uuid.UUID(str(claims["merchant_id"])),
            scopes=frozenset(scope.split()),
            issued_at=datetime.fromtimestamp(int(claims["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
            buyer_ref=str(claims["sub"]),
        )

    def _reduce_audience(self, raw: object) -> str:
        """The one resource this server validated, from an audience of any arity.

        A list containing this server's resource reduces to that resource; a list without
        it is refused. Taking ``raw[0]`` would hand ``open_session`` a value this server
        never checked, and its equality test would then pass on somebody else's resource
        whenever that resource happened to be listed first.

        ``open_session`` compares the returned value against the resource again. That
        comparison can no longer fail, and it is deliberately left in place: it is the
        assertion that this reduction did its job, and removing it would mean a protocol
        package trusting an adapter it does not own.
        """
        named = [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]
        if self.resource not in named:
            raise _not_verified()
        return self.resource


def _not_verified() -> AuthenticationRejected:
    """One refusal for every way a token can fail to verify. See the class docstring."""
    return AuthenticationRejected("mcp_access_token_not_verified")


# ---------------------------------------------------------------------------- sessions


@dataclass(frozen=True, slots=True)
class StoredSession:
    """A live MCP session and the platform session whose authority it borrowed."""

    mcp_session: McpSession
    api_session_id: uuid.UUID


@dataclass(slots=True)
class McpSessionStore:
    """This app's live MCP sessions. Attached to the app, never to the module.

    Per app rather than per process for the reason ``create_app`` gives about the merchant
    registry: two apps in one test session must not share state, or one test's session
    leaks into another's assertions. In production there is one app and one process.

    Every access prunes what has expired, so a session that has run out is gone before it
    can be found rather than found and then refused. That matters for the store's size, not
    for security: :meth:`~commerce_protocols.mcp.McpSession.is_live_at` is checked against
    the database clock inside the protocol server on every call regardless.
    """

    _sessions: dict[uuid.UUID, StoredSession]

    @staticmethod
    def empty() -> McpSessionStore:
        return McpSessionStore(_sessions={})

    def put(self, mcp_session: McpSession, *, api_session_id: uuid.UUID, now: datetime) -> None:
        self._prune(now)
        if len(self._sessions) >= MAX_LIVE_SESSIONS:
            raise ProblemError(
                503,
                "Too many live MCP sessions",
                f"This process holds {MAX_LIVE_SESSIONS} live MCP sessions. Sessions last "
                "fifteen minutes and are not renewed; retry once one expires.",
                retry_after_seconds=60,
            )
        self._sessions[mcp_session.session_id] = StoredSession(
            mcp_session=mcp_session, api_session_id=api_session_id
        )

    def get(self, session_id: uuid.UUID) -> StoredSession | None:
        """The session, live or not.

        Liveness is deliberately not judged here. This store has no clock it should be
        trusted with: the answer must come from the database, and the caller cannot read
        the database until it knows which tenant to bind, which is a fact it gets from the
        session this method returns. So the lookup comes first and
        ``McpSession.is_live_at`` follows, against ``core.replay.database_now``.
        """
        return self._sessions.get(session_id)

    def _prune(self, now: datetime) -> None:
        expired = [
            key for key, held in self._sessions.items() if not held.mcp_session.is_live_at(now)
        ]
        for key in expired:
            del self._sessions[key]


def new_limiter() -> TokenBucketLimiter:
    """A fresh limiter for one app.

    Separate from the ACP surface's instance on purpose: the two share bucket key shapes,
    so one limiter serving both would let a client's MCP traffic throttle its ACP traffic
    and hide which surface was actually being pushed.
    """
    return TokenBucketLimiter()


def parent_principal(
    session: Session, *, api_session_id: uuid.UUID, tenant_id: uuid.UUID, now: datetime
) -> AgentPrincipal:
    """The platform session an MCP session borrowed its authority from, as it stands now.

    Re-read rather than remembered, and that is the whole point of the function. An MCP
    session lives fifteen minutes; the browser session behind it has its own expiry and its
    own capability list, and a protocol session holding a copy of either would go on
    honouring authority that had lapsed. Reading it here means the narrowing in
    :func:`~commerce_api.services.protocol_transport.narrow_to_protocol` is against what
    that session holds at the moment of the call.

    The tenant predicate is written out even though the caller already knows the tenant.
    ``api_sessions`` is the one table with no row-level security -- resolving a token is
    what *discovers* a tenant, so it cannot already require one -- which makes this the one
    lookup in the service where a missing predicate would genuinely cross tenants.
    """
    row = session.execute(
        select(ApiSession).where(ApiSession.id == api_session_id, ApiSession.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if row is None or row.expires_at <= now:
        raise AuthenticationRejected("originating_session_is_no_longer_live")
    return AgentPrincipal(
        principal_id=f"session:{row.id}",
        tenant_id=row.tenant_id,
        actor_type=ActorType(row.actor_type),
        merchant_id=row.merchant_id,
        buyer_ref=row.buyer_ref,
        capabilities=frozenset(row.capabilities),
    )


# ------------------------------------------------------------------------ tool dispatch


def dispatch(
    session: Session,
    *,
    admitted: AdmittedCall,
    ctx: RequestContext,
    registry: MerchantRegistry,
    interaction_id: uuid.UUID,
) -> Mapping[str, Any]:
    """Run one admitted tool call against this platform's own services.

    A match over the closed :class:`~commerce_protocols.mcp.ToolName` enum with no default
    arm, so a tool added to the registry and forgotten here is a type error before it is a
    runtime one. That is the only version of "the dispatcher is complete" worth having.

    ``checkout.submit_approved`` is present only to refuse. It is the one tool that reaches
    money, and it is served by
    :meth:`~commerce_protocols.mcp.GovernedToolServer.submit_approved` in the router
    instead, so a reader of this function can see that nothing reachable from it admits
    anything.
    """
    arguments = admitted.arguments.accepted
    tool = admitted.spec.name

    match tool:
        case ToolName.CATALOGUE_SEARCH:
            return _catalogue_search(registry, ctx, arguments)
        case ToolName.CATALOGUE_PRODUCT:
            return _product(
                catalogue_service.product_view(
                    registry, merchant_id=ctx.merchant_id, sku=str(arguments["sku"])
                )
            )
        case ToolName.INVENTORY_CHECK:
            return _inventory(registry, ctx, arguments)
        case ToolName.BASKET_CREATE:
            return _basket(cart_service.create_cart(session, ctx, registry))
        case ToolName.BASKET_UPDATE:
            return _basket(
                cart_service.set_line(
                    session,
                    ctx,
                    registry,
                    cart_id=_as_uuid(arguments["basket_id"]),
                    sku=str(arguments["sku"]),
                    quantity=int(arguments["quantity"]),
                )
            )
        case ToolName.QUOTE_REQUEST:
            return _basket(
                cart_service.read_cart(session, ctx, registry, _as_uuid(arguments["basket_id"]))
            )
        case ToolName.RESERVATION_REQUEST:
            return _reserved(
                checkout_service.open_checkout(
                    session, ctx, registry, cart_id=_as_uuid(arguments["basket_id"])
                )
            )
        case ToolName.CHECKOUT_SUBMIT_FOR_APPROVAL:
            return _approval_handoff(session, ctx, arguments)
        case ToolName.CHECKOUT_SUBMIT_APPROVED:
            raise StateRejected(
                "submit_approved_does_not_dispatch_to_a_service",
                tool=tool.value,
                intent=admitted.spec.intent.value,
            )
        case ToolName.ORDER_TRACK:
            return _order(session, ctx, _as_uuid(arguments["order_id"]))
        case (
            ToolName.ORDER_PROPOSE_CANCELLATION
            | ToolName.REFUND_PROPOSE
            | ToolName.SUPPORT_ESCALATE
        ):
            return _proposal(tool, arguments, interaction_id, ctx)


def _catalogue_search(
    registry: MerchantRegistry, ctx: RequestContext, arguments: Mapping[str, str | int]
) -> dict[str, Any]:
    """Grounded search. Every hit names its SKU, the only id a model may reuse afterwards."""
    results = catalogue_service.search_catalogue(
        registry,
        merchant_id=ctx.merchant_id,
        query=str(arguments["query"]),
        locale=catalogue_service.locale_from(_optional_str(arguments.get("locale"))),
        limit=int(arguments.get("limit", _DEFAULT_SEARCH_LIMIT)),
    )
    return {
        "query": results.query,
        "source": results.source,
        "catalogue_revision": results.catalogue_revision,
        "hits": [_product(hit.view) for hit in results.hits],
    }


def _inventory(
    registry: MerchantRegistry, ctx: RequestContext, arguments: Mapping[str, str | int]
) -> dict[str, Any]:
    """Availability for a quantity, answered from the merchant's live position."""
    status = registry.store(ctx.merchant_id).check_inventory(str(arguments["sku"]))
    wanted = int(arguments["quantity"])
    return {
        "sku": status.sku,
        "requested_units": wanted,
        "available_units": status.available_units,
        "can_fulfil": status.can_fulfil(wanted),
        "source": status.freshness.source,
        "catalogue_revision": status.freshness.catalogue_revision,
    }


def _product(view: ProductView) -> dict[str, Any]:
    """One product, reduced to what a model can act on.

    Reduced rather than dumped. A tool result is screened by
    ``commerce_protocols.mcp.results``, which bounds depth, node count and text length, and
    a full catalogue record pushes against all three for no gain: what a model needs is the
    SKU it may reference, the price in minor units it must quote, and whether the thing can
    be bought right now.
    """
    return {
        "sku": view.sku,
        "name": view.display_name(devanagari=False),
        "category": str(view.product.category),
        "unit_price_minor": view.unit_price.minor,
        "currency": view.unit_price.currency,
        "is_available": view.is_available,
        "stock_units": view.stock_units,
    }


def _basket(body: Mapping[str, Any]) -> dict[str, Any]:
    """A cart and its re-quote, reduced. Every figure stays an integer of minor units.

    ``content_hash`` is carried through because it is the bytes an approval would later
    bind to, and a model that has seen it can echo it back at
    ``checkout.submit_approved`` -- which is the one place echoing it means anything.
    """
    quote = body.get("quote")
    priced = quote if isinstance(quote, Mapping) else {}
    return {
        "basket_id": body["cart_id"],
        "code": body["code"],
        "lines": [
            {"sku": line["sku"], "quantity": line["quantity"]} for line in body.get("lines") or []
        ],
        "total_minor": priced.get("total_minor"),
        "currency": priced.get("currency"),
        "content_hash": priced.get("content_hash"),
        "unavailable": [item.get("sku") for item in body.get("unavailable") or []],
        "stale": body.get("stale"),
        "catalogue_revision": priced.get("catalogue_revision"),
    }


def _reserved(card: Mapping[str, Any]) -> dict[str, Any]:
    """A freshly frozen checkout version, and what has to happen next.

    ``approved`` is stated as ``False`` rather than left out. A model reading this result
    must not be able to infer from an absence that consent has been given: the version
    exists, it is immutable, it holds a reservation, and no human has decided anything.
    """
    return {
        "checkout_id": card["checkout_id"],
        "version": card["version"],
        "content_hash": card["content_hash"],
        "policy_receipt_hash": card["policy_receipt_hash"],
        "total_minor": card["amount_minor"],
        "currency": card["currency"],
        "reservation_expires_at": card["expires_at"],
        "approved": False,
        "next_step": (
            "The buyer approves or rejects this exact version and hash on the trusted "
            "surface. No protocol caller can record that decision."
        ),
    }


def _approval_handoff(
    session: Session, ctx: RequestContext, arguments: Mapping[str, str | int]
) -> dict[str, Any]:
    """Ask the buyer to decide, by naming the version and where the decision is taken.

    The checkout is read first, so the handoff cannot name a checkout this caller does not
    own or a version that does not exist. ``read_checkout`` answers 404 for somebody else's
    checkout rather than 403, which is the same non-disclosure every other read in this
    service makes.
    """
    checkout_id = _as_uuid(arguments["checkout_id"])
    version = int(arguments["version"])
    body = checkout_service.read_checkout(session, ctx, checkout_id)
    named = [v for v in body.get("versions") or [] if v.get("version") == version]
    if not named:
        raise StateRejected(
            "checkout_has_no_such_version", checkout_id=str(checkout_id), version=version
        )
    return {
        "checkout_id": str(checkout_id),
        "version": version,
        "state": named[0].get("state"),
        "content_hash": named[0].get("content_hash"),
        "total_minor": named[0].get("amount_minor"),
        "currency": named[0].get("currency"),
        "awaiting": "buyer_decision_on_trusted_surface",
        "approved": False,
        "next_step": (
            "The buyer approves this exact version and hash on the trusted surface. This "
            "tool asked; it did not approve, and it cannot."
        ),
    }


def _order(session: Session, ctx: RequestContext, order_id: uuid.UUID) -> dict[str, Any]:
    """An order's state and how the platform learned the money moved.

    ``capture_evidence`` is part of the answer rather than a detail. An order exists only
    where a webhook or a provider fetch put it there, never a browser callback (ADR 0003
    D8), so a model relaying the state should be relaying its provenance with it.

    The evidence source is read from ``kind``, because that is what
    :class:`~commerce_api.schemas.CaptureEvidenceOut` calls it on the wire. The column it
    is rendered from is ``orders.capture_evidence->>'source'``, and this once read that
    name off the wire object, which does not carry it: every answer this tool has ever
    given said ``null`` here. That is the paragraph above stated in reverse -- an external
    model was told this platform cannot say how it learned the money moved -- and it was
    invisible because a null in a nullable field reads as "no evidence yet" rather than as
    a mistake.

    Ownership is asserted through the *checkout*, matching ``GET /v1/orders/{id}``:
    ownership lives on ``checkouts.buyer_ref``, and an order disagreeing with its checkout
    about who bought it would be a data fault rather than an authorisation question.
    """
    order = refund_service.load_order(session, ctx, order_id=order_id)
    assert_owner(session, ctx, order.checkout_id)
    payload = refund_service.order_payload(session, ctx, order).model_dump(mode="json")
    payment = payload.get("payment") or {}
    evidence = payment.get("capture_evidence") or {}
    return {
        "order_id": payload["order_id"],
        "state": payload["state"],
        "total_minor": payload["amount_minor"],
        "currency": payload["currency"],
        "checkout_id": payload["checkout_id"],
        "checkout_version": payload["version"],
        "payment_state": payment.get("state"),
        "capture_evidence_source": evidence.get("kind"),
        "refund_count": len(payload.get("refunds") or []),
    }


def _proposal(
    tool: ToolName,
    arguments: Mapping[str, str | int],
    interaction_id: uuid.UUID,
    ctx: RequestContext,
) -> dict[str, Any]:
    """A request for a human, and an honest statement of what it left behind.

    No amount, and there is no way for one to arrive: the tool registry refuses an argument
    named for money, so ``arguments`` cannot carry a figure however the model phrased its
    reason.

    ``recorded_as`` is the protocol interaction, which is a real gapless hash-chained
    record a person can open at the path this result names. Reporting a case identifier
    instead would be reporting something this platform does not create, and specification
    29.4's rule about proposals is exactly that they must not overstate what happened.
    """
    return {
        "tool": tool.value,
        "accepted": True,
        "changed_anything": False,
        "order_id": _optional_str(arguments.get("order_id")),
        "recorded_as": str(interaction_id),
        "inspect_at": f"/v1/inspector/protocols/{interaction_id}",
        "correlation_id": str(ctx.correlation_id),
        "next_step": (
            "A person reviews this request. Nothing has been cancelled, refunded or "
            "promised, and no amount has been named."
        ),
    }


def _as_uuid(value: str | int) -> uuid.UUID:
    """A validated UUID argument. ``ArgumentKind.UUID`` already parsed it; this narrows it."""
    return uuid.UUID(str(value))


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)

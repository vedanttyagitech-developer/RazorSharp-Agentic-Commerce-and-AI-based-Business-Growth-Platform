"""OAuth 2.1 style authorization for the MCP surface, specification 17.4.

An MCP server is a resource server. The failure this module exists to prevent is the one
RFC 8707 was written for and specification 29.7 names as a required test: a client obtains
a perfectly valid access token for *some other* resource server -- a calendar, a wiki, a
different tenant's commerce endpoint -- and presents it here. Every signature verifies,
every claim is well formed, and if this server does not check who the token was minted for,
it hands an attacker a governed commerce session on the strength of a credential its owner
never intended for commerce. The check is one comparison; leaving it out has never looked
like a bug in a code review, which is exactly why it is asserted here and tested by name.

Four defences, and each covers a gap the others leave.

**Audience.** The token names the resource it was issued for and this server names itself.
A mismatch is refused before anything else about the token matters.

**Lifetime.** A token whose validity window is longer than :data:`MAX_TOKEN_LIFETIME` is
refused *even if it has not expired*. Checking expiry alone accepts a year-long bearer
credential right up until the year is out; checking the window refuses the design. The
ceiling is five minutes, the same number as specification 16.3's request-freshness window
and ADR 0003 D13's execution-grant TTL, so there is one duration to remember rather than
three to keep in step.

**Replay.** The token's ``jti`` is claimed as a nonce through
:func:`commerce_protocols.core.replay.claim_nonce`, so one access token establishes exactly
one MCP session. This is stricter than a bearer token normally is, deliberately: a
credential that can open unlimited sessions is a credential whose theft leaves no trace,
whereas single use turns the thief's second attempt into a refusal that lands in the
evidence chain. Session establishment is rare, so the strictness costs nothing.

**Scope.** Scopes are the *only* thing that decides what the session may call, and they can
only ever narrow. The capability set is the token's scopes intersected with
``PROTOCOL_CAPABILITIES``, which contains no consent capability, and the intersection
happens again inside ``principal_for``. A token scoped ``mcp:checkout.approve`` therefore
grants precisely nothing, silently and totally -- which is the right response to a
misconfiguration nobody has noticed yet.

The token itself never survives this module. What is presented is a string; what is kept is
a :class:`~commerce_protocols.core.evidence.Fingerprint` of it. :class:`McpSession` has no
field that can hold a bearer credential, so specification 17.3's "no token passthrough" is
not a rule the rest of the server has to observe -- it has nothing to pass through.
"""

from __future__ import annotations

import typing
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from commerce_domain import AgentPrincipal, uuid7
from sqlalchemy.orm import Session

from ..core import (
    PROTOCOL_CAPABILITIES,
    AuthenticatedCaller,
    AuthenticationRejected,
    Fingerprint,
    Protocol,
    ProtocolPin,
    UnsupportedVersionError,
    VersionRejected,
    assert_never_consents,
    claim_nonce,
    database_now,
    fingerprint,
    principal_for,
    require_pin,
)
from .tools import TOOLS, ToolName

__all__ = [
    "MAX_TOKEN_LIFETIME",
    "MCP_SCOPE_PREFIX",
    "SESSION_LIFETIME",
    "AccessToken",
    "McpSession",
    "TokenIntrospector",
    "capabilities_for_scopes",
    "open_session",
    "scope_for",
]

#: Scopes are namespaced so a token minted for another surface of the same authorization
#: server cannot accidentally read as a commerce entitlement here. A scope without this
#: prefix grants nothing; it is not an error, because an authorization server legitimately
#: puts ``openid`` and ``profile`` in the same list.
MCP_SCOPE_PREFIX: Final[str] = "mcp:"

#: The longest validity window an access token may declare. See the module docstring: this
#: refuses the *design* of a long-lived bearer credential, not merely an expired one.
MAX_TOKEN_LIFETIME: Final[timedelta] = timedelta(minutes=5)

#: How long a session lives once established. Longer than the token because the token's job
#: ends at establishment -- the allowlist is fixed, the principal is built, and nothing
#: afterwards re-reads the credential -- and short enough that a stolen session identifier
#: is worth little. Admitting a call and submitting an approved checkout each re-read it
#: against the database clock, so a session that runs out between the two is refused at the
#: half that reaches money rather than carried through on the earlier check.
SESSION_LIFETIME: Final[timedelta] = timedelta(minutes=15)

#: Tolerance for genuine clock skew between the authorization server and this database.
#: Folded into the same window as freshness rather than configured separately, for the
#: reason ``core.replay`` gives: two knobs invite somebody to widen one of them.
_ISSUANCE_SKEW: Final[timedelta] = timedelta(seconds=30)

#: The namespace a token's ``jti`` is claimed under, inside the client's nonce space. Both
#: guards ride the same store -- ``core.replay`` claims one idempotency key per nonce -- so
#: without a namespace on each side the two would share one flat space, and a tool call
#: could spend the key a later access token needs. Its counterpart is ``call`` in
#: :mod:`.server`; the pair is what lets an audit separate "this token was presented twice"
#: from "this call was replayed", which are different incidents with different responses.
_SESSION_CLAIM_PREFIX: Final[str] = "session"


@dataclass(frozen=True, slots=True)
class AccessToken:
    """The verified claims of one access token, as an authorization server issued them.

    This is deliberately the *introspected* form. Whether the deployment verifies a signed
    JWT locally or calls RFC 7662 introspection is a deployment decision that changes with
    the identity provider; what may not change is what this server checks afterwards, so
    the checks are written against the claims rather than against a token format.

    There is no field for the token string, and its absence is load-bearing. A structure
    that could hold the credential is a structure somebody logs, and the only copy of the
    presented bytes this package keeps is a fingerprint.

    ``tenant_id`` and ``merchant_id`` come from here and from nowhere else. That is
    specification 17.3's fourth bullet in its positive form: the tenant is a fact about the
    authenticated session, established before the model has said anything at all.
    """

    token_id: str
    client_id: str
    subject: str
    issuer: str
    audience: str
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    scopes: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    buyer_ref: str | None = None

    @property
    def lifetime(self) -> timedelta:
        return self.expires_at - self.issued_at


class TokenIntrospector(typing.Protocol):
    """The one thing in this package that ever sees a presented bearer credential.

    Implementations verify a signed JWT or call the authorization server's introspection
    endpoint and return :class:`AccessToken`. They raise
    :class:`~commerce_protocols.core.errors.AuthenticationRejected` for a credential that
    does not verify -- and say nothing about *why*, for the reason ``SignatureRejected``
    gives: a verifier that distinguishes "unknown key" from "bad signature" is a verifier
    an attacker can interrogate.
    """

    def introspect(self, presented: str) -> AccessToken: ...


def scope_for(capability: str) -> str:
    """The scope string an authorization server issues to grant one capability."""
    return f"{MCP_SCOPE_PREFIX}{capability}"


def capabilities_for_scopes(scopes: frozenset[str]) -> frozenset[str]:
    """Protocol capabilities named by these scopes, intersected with the ceiling.

    Intersection rather than validation, for the reason ``principal_for`` gives about
    misconfigured grants: a scope naming ``checkout.approve`` is a mistake somebody made
    once at the authorization server, and the safe response is to drop it at every use
    rather than to refuse every request from that client until a human notices. The drop is
    silent to the caller and total.
    """
    named = {
        scope.removeprefix(MCP_SCOPE_PREFIX)
        for scope in scopes
        if scope.startswith(MCP_SCOPE_PREFIX)
    }
    return frozenset(named) & PROTOCOL_CAPABILITIES


@dataclass(frozen=True, slots=True)
class McpSession:
    """One established MCP session: a fixed identity and a fixed tool allowlist.

    Specification 17.4 requires the allowlist to be fixed at session creation, and this is
    where "fixed" is made to mean something. The dataclass is frozen, so nothing mutates
    it; and :meth:`__post_init__` re-derives the invariant, so the obvious way around a
    frozen dataclass -- ``dataclasses.replace`` with a wider set -- is refused too. A tool
    is in the allowlist only if the principal holds the capability that tool requires, and
    the principal's capabilities were already intersected with the protocol ceiling.

    ``token_fingerprint`` is what remains of the credential. There is no field it could be
    stored in even if a future edit wanted to.
    """

    session_id: uuid.UUID
    pin: ProtocolPin
    caller: AuthenticatedCaller
    principal: AgentPrincipal
    resource: str
    allowed_tools: frozenset[ToolName]
    established_at: datetime
    expires_at: datetime
    token_id: str
    token_fingerprint: Fingerprint

    def __post_init__(self) -> None:
        assert_never_consents(self.principal)
        unknown = self.allowed_tools - set(ToolName)
        if unknown:
            raise AuthenticationRejected(
                "session_allowlist_names_an_unregistered_tool",
                tools=sorted(str(name) for name in unknown),
            )
        ungranted = sorted(
            tool.value
            for tool in self.allowed_tools
            if not self.principal.can(TOOLS[tool].capability)
        )
        if ungranted:
            raise AuthenticationRejected(
                "session_allowlist_exceeds_granted_capabilities", tools=ungranted
            )
        if self.expires_at <= self.established_at:
            raise AuthenticationRejected("session_expires_before_it_begins")

    @property
    def tenant_id(self) -> uuid.UUID:
        """The tenant, from the authenticated token. Never from a tool argument."""
        return self.caller.tenant_id

    @property
    def client_id(self) -> str:
        return self.caller.client_id

    def may_call(self, tool: ToolName) -> bool:
        return tool in self.allowed_tools

    def is_live_at(self, moment: datetime) -> bool:
        return moment < self.expires_at


def open_session(
    session: Session,
    *,
    introspector: TokenIntrospector,
    presented: str,
    resource: str,
    announced_version: str | None,
    registered_tools: frozenset[ToolName] | None = None,
    correlation_id: uuid.UUID | None = None,
) -> McpSession:
    """Establish one MCP session from one access token, or refuse.

    The order is specification 13.1's, and the order matters. The pinned version is checked
    before the credential is introspected, because a caller speaking a contract this
    platform has no fixtures for gets the same answer whoever it is. The audience and the
    declared lifetime are checked before the clock, because they are facts about the
    token's *design* and a design flaw should not be reported as "expired". The nonce is
    claimed last of the credential checks, so a token refused for any earlier reason keeps
    its ``jti`` usable -- otherwise anyone who observed a token id could burn a legitimate
    client's token by presenting it into a check they knew would fail.

    ``registered_tools`` is the client's registration at the authorization server: the
    per-client half of specification 17.4's "per-tenant/per-client scopes". It can only
    narrow what the scopes already granted.
    """
    try:
        pin = require_pin(Protocol.MCP, announced_version)
    except UnsupportedVersionError as exc:
        raise VersionRejected(
            "mcp_version_not_pinned", announced=exc.announced, supported=exc.supported
        ) from exc

    token = introspector.introspect(presented)
    credential = fingerprint(presented)

    if token.audience != resource:
        raise AuthenticationRejected(
            "token_audience_mismatch", audience=token.audience, resource=resource
        )
    if token.lifetime > MAX_TOKEN_LIFETIME:
        raise AuthenticationRejected(
            "token_lifetime_exceeds_ceiling",
            lifetime_seconds=int(token.lifetime.total_seconds()),
            maximum_seconds=int(MAX_TOKEN_LIFETIME.total_seconds()),
        )
    if token.lifetime <= timedelta(0):
        raise AuthenticationRejected("token_expires_before_it_is_issued")

    now = database_now(session)
    if token.expires_at <= now:
        raise AuthenticationRejected(
            "token_expired", expired_seconds_ago=int((now - token.expires_at).total_seconds())
        )
    if token.issued_at > now + _ISSUANCE_SKEW:
        raise AuthenticationRejected(
            "token_issued_in_the_future",
            skew_seconds=int((token.issued_at - now).total_seconds()),
        )

    granted = capabilities_for_scopes(token.scopes)
    allowed = frozenset(tool for tool, spec in TOOLS.items() if spec.capability in granted)
    if registered_tools is not None:
        allowed &= registered_tools
    if not allowed:
        raise AuthenticationRejected(
            "token_grants_no_tool", client_id=token.client_id, scopes=sorted(token.scopes)
        )

    claim_nonce(
        session,
        protocol=Protocol.MCP,
        client_id=token.client_id,
        nonce=f"{_SESSION_CLAIM_PREFIX}:{token.token_id}",
        request_digest=credential.digest,
    )

    caller = AuthenticatedCaller(
        protocol=Protocol.MCP,
        client_id=token.client_id,
        tenant_id=token.tenant_id,
        merchant_id=token.merchant_id,
        authenticated_by=f"oauth2.1 access token, audience {resource}, issuer {token.issuer}",
        correlation_id=correlation_id or uuid7(),
        buyer_ref=token.buyer_ref,
        granted=granted,
    )
    principal = principal_for(caller)
    assert_never_consents(principal)

    return McpSession(
        session_id=uuid7(),
        pin=pin,
        caller=caller,
        principal=principal,
        resource=resource,
        allowed_tools=allowed,
        established_at=now,
        expires_at=now + SESSION_LIFETIME,
        token_id=token.token_id,
        token_fingerprint=credential,
    )

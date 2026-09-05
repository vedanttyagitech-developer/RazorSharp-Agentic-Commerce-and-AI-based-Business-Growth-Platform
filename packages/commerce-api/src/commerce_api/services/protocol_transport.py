"""What an external protocol caller becomes before it may touch anything.

``commerce_protocols`` authenticates ACP and MCP callers and turns them into typed
intents. It has no idea what a checkout is, and deliberately so. This module is the other
half: it turns an authenticated protocol caller into the one identity the services in this
package already understand -- :class:`~commerce_api.deps.RequestContext` -- and it is the
only place in the service where that translation happens.

Everything the ACP and MCP routers can reach, they reach through a ``RequestContext``
built here. That is the security argument, and it is an argument about reachability rather
than about checks: a protocol caller calls ``basket_service.create_basket`` and
``admission_service.submit_checkout``, the same functions the buyer's own browser calls,
with a context whose capabilities are provably narrower. There is no protocol-only service,
no protocol-only query and no second admission path, so there is nothing for a protocol
caller to reach that a buyer session could not.

Three narrowings, and each closes a hole the others leave
---------------------------------------------------------
**The ceiling.** ``core.identity.principal_for`` intersects whatever a tenant granted with
:data:`~commerce_protocols.core.PROTOCOL_CAPABILITIES`, which holds no consent capability
and never will. A registry entry or a token scope naming ``checkout.approve`` grants
nothing, silently and totally.

**The parent.** A ceiling bounds what a protocol caller may hold *in general*. It says
nothing about whether the particular grant behind this request was ever that wide, and for
MCP there is a particular grant: an access token is minted from a live ``api_sessions``
row, so the session that authorised the exchange is the parent and the protocol principal
must be a subset of it. :func:`narrow_to_protocol` puts that comparison through
:meth:`~transaction_kernel.AgentPrincipal.subset_for`, which raises on any widening, and
then re-asserts the result against the parent's own set -- because ``subset_for`` is
applied to the parent's capabilities and ``principal_for`` is applied to the ceiling, and
only comparing the two outputs proves that the second did not undo the first.

**The buyer.** ``RequestContext.buyer_ref`` comes from the credential and from nowhere
else, and :func:`commerce_api.deps.assert_owner` refuses a checkout belonging to a
different one with a 404. So even a caller holding every capability the ceiling allows can
only act on the baskets, checkouts and orders of the single buyer its credential names.

What is deliberately *not* here
-------------------------------
There is no ``kernel_admission`` that calls :func:`transaction_kernel.admit`.
:class:`PlatformAdmission` calls ``admission_service.submit_checkout``, which is the
function the trusted surface's ``POST /v1/checkouts/{id}/versions/{v}/submit`` calls. A
protocol adapter with its own route to the kernel would be a second admission, and the
whole of specification 17 exists to prevent exactly that.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from commerce_protocols.core import (
    PROTOCOL_CAPABILITIES,
    AuthenticatedCaller,
    AuthenticationRejected,
    ProtocolRejection,
    assert_never_consents,
    principal_for,
)
from sqlalchemy.orm import Session
from transaction_kernel import AgentPrincipal, KernelDecision

from ..deps import RequestContext
from ..merchants import MerchantRegistry
from . import admission_service

__all__ = [
    "AdmissionDuplicated",
    "PlatformAdmission",
    "keep_refusal_evidence",
    "narrow_to_protocol",
    "registry_principal",
    "protocol_context",
]

#: How an MCP-derived principal names itself in the delegation chain and the audit row. A
#: reviewer reading ``session:<uuid>/mcp_caller`` in a chain can tell at a glance that a
#: browser session was exchanged for a protocol one, and which session it was.
MCP_CHILD_ROLE: Final[str] = "mcp_caller"


class AdmissionDuplicated(Exception):  # noqa: N818 - an answer, not a failure
    """A submit found an attempt already in flight. ADR 0003 D9, not a kernel decision.

    :class:`PlatformAdmission` cannot return this through the
    :class:`~commerce_protocols.mcp.KernelAdmission` port, whose return type is a
    :class:`~transaction_kernel.KernelDecision`, because no admission ran: the
    single-winner index had already decided, and
    ``admission_service._duplicate_body`` is emphatic that inventing a decision here would
    put a decision id in the evidence for a decision the kernel never made.

    So it travels as a signal carrying the body the trusted surface would have returned,
    and the router answers with it verbatim. The caller sees the one live attempt, which
    is what D9 requires of every surface.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__("a payment attempt is already in flight for this checkout")
        self.body = body


@contextmanager
def keep_refusal_evidence(db: Session) -> Iterator[None]:
    """Commit the evidence a protocol refusal wrote, then let the refusal propagate.

    Specification 13.3 asks that a reviewer be able to see *which check refused* a request
    without re-running it, and the protocol adapters write exactly that: a ``REJECTED`` row
    naming the stage that failed, appended to the interaction's own hash chain. Those rows
    land in this transaction, and a transaction that rolls back on the way out of the
    handler takes them with it -- leaving a refused request that never happened as far as
    the audit trail is concerned.

    So a :class:`~commerce_protocols.core.ProtocolRejection` commits before it is re-raised.
    The rollback the session scope then performs finds no open transaction and does
    nothing; the durable rows stay.

    **Wrap the authenticate-and-map phase only, never the acting phase.** Everything in the
    transaction while a request is being authenticated, evidenced and mapped is evidence, so
    committing it commits exactly the rows a reviewer needs. Once the request starts *doing*
    something -- amending a basket, freezing a version, reaching admission -- a refusal must
    take its partial work with it, and a block like this one would keep it instead. Both
    transports are written that way, and the boundary is where the ``with`` ends rather than
    a property anybody has to remember.
    """
    try:
        yield
    except ProtocolRejection:
        db.commit()
        raise


def narrow_to_protocol(
    parent: AgentPrincipal,
    *,
    caller: AuthenticatedCaller,
    child_role: str,
) -> AgentPrincipal:
    """Derive a protocol principal from the session principal that authorised it.

    Two derivations run over the same capability set and both must agree.

    ``subset_for`` narrows the *parent* and refuses to widen it, so a caller cannot end up
    holding something the session it came from never held. Its output is discarded except
    for that refusal: it copies the parent's :class:`~transaction_kernel.ActorType`, and an
    MCP request recorded as ``BUYER`` would tell an auditor the opposite of the truth about
    where it came from.

    ``principal_for`` then builds the principal that is actually used, with
    ``ActorType.PROTOCOL`` and the delegation chain that names the external client. It
    intersects against the ceiling rather than against the parent, which is why the
    subset assertion below is not redundant: the two functions bound different things, and
    only comparing their outputs proves that the wider of the two did not win.
    """
    granted = caller.granted & parent.capabilities & PROTOCOL_CAPABILITIES
    # Raises on any widening. The result is not used; the refusal is.
    parent.subset_for(child_role, granted)

    principal = principal_for(
        AuthenticatedCaller(
            protocol=caller.protocol,
            client_id=caller.client_id,
            tenant_id=caller.tenant_id,
            merchant_id=caller.merchant_id,
            authenticated_by=caller.authenticated_by,
            correlation_id=caller.correlation_id,
            buyer_ref=caller.buyer_ref,
            granted=granted,
        )
    )
    if not principal.capabilities <= parent.capabilities:  # pragma: no cover - see above
        excess = sorted(principal.capabilities - parent.capabilities)
        raise AuthenticationRejected(
            "protocol_principal_exceeds_the_session_that_minted_it", capabilities=excess
        )
    if principal.tenant_id != parent.tenant_id:  # pragma: no cover - copied from the caller
        raise AuthenticationRejected("protocol_principal_names_another_tenant")
    assert_never_consents(principal)
    return principal


def registry_principal(caller: AuthenticatedCaller) -> AgentPrincipal:
    """The protocol principal for a caller whose grant *is* its registry entry.

    ACP has no parent session to narrow against: an external AI buyer holds credentials
    this deployment issued it, and what it may do is what
    :attr:`~commerce_protocols.acp.AcpClient.granted` says, intersected with the ceiling.
    There is no wider principal to compare it to, so the ceiling is the only narrowing
    available -- and it is enough, because the ceiling is what excludes consent.

    The subset assertion below is therefore against the ceiling rather than against a
    parent. ``principal_for`` already performs that intersection; asserting it again costs
    nothing and turns "the intersection happened" from something a reader has to trace into
    something the code states.
    """
    principal = principal_for(caller)
    if not principal.capabilities <= PROTOCOL_CAPABILITIES:  # pragma: no cover - see above
        excess = sorted(principal.capabilities - PROTOCOL_CAPABILITIES)
        raise AuthenticationRejected("protocol_principal_exceeds_the_ceiling", capabilities=excess)
    assert_never_consents(principal)
    return principal


def protocol_context(
    *,
    caller: AuthenticatedCaller,
    principal: AgentPrincipal,
    session_id: uuid.UUID,
    expires_at: datetime,
) -> RequestContext:
    """The identity every service in this package already knows how to refuse.

    ``buyer_ref`` is required rather than optional, and the refusal is the point. A
    protocol credential that names no buyer can still discover a catalogue, but every
    other service in this package decides ownership by comparing ``buyer_ref`` -- so a
    context carrying ``None`` would compare equal to any row whose buyer reference was also
    null, and "acts for nobody" would quietly become "acts for whoever else acts for
    nobody". A credential is either bound to a buyer or it cannot construct anything.

    ``expires_at`` is the protocol session's, not the underlying browser session's. The
    two are checked separately: this one bounds how long a protocol session lives, and the
    ``api_sessions`` row behind it is re-read on every request that has one.
    """
    if caller.buyer_ref is None:
        raise AuthenticationRejected(
            "protocol_credential_names_no_buyer", client_id=caller.client_id
        )
    assert_never_consents(principal)
    return RequestContext(
        tenant_id=caller.tenant_id,
        merchant_id=caller.merchant_id,
        buyer_ref=caller.buyer_ref,
        principal=principal,
        correlation_id=caller.correlation_id,
        session_id=session_id,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class PlatformAdmission:
    """The :class:`~commerce_protocols.mcp.KernelAdmission` port, over the one admission.

    Holds the three things the platform's admission needs and the protocol layer must never
    be able to supply: the request context, this app's merchant registry, and the
    idempotency key the mutation is claimed under. None of them can be reached from a tool
    argument -- the context came from the access token, the registry from ``app.state`` and
    the key from a header the transport read before the model's arguments existed.

    :meth:`admit_approved` matches the port's signature exactly and does not widen it.
    There is no ``amount`` because the amount is whatever the buyer's recorded approval
    says; no ``tenant_id`` because the tenant travels inside ``principal``; no
    ``capabilities`` and no credential because nothing a model says may add either.
    """

    ctx: RequestContext
    registry: MerchantRegistry
    idempotency_key: str

    def admit_approved(
        self,
        session: Session,
        *,
        principal: AgentPrincipal,
        checkout_id: uuid.UUID,
        version: int,
        content_hash: str,
    ) -> KernelDecision:
        """Hand an already-approved version to the same admission the browser uses.

        ``principal`` is compared against the context's rather than replacing it. The
        context is what ``submit_checkout`` will act under, and it was built from the
        credential; the principal arrives from the MCP session, which was built from the
        same credential. They are two paths to one fact, so a disagreement means the
        transport wired one of them from somewhere else, and continuing would run the
        admission under an identity the other half never agreed to.

        ``content_hash`` is not passed down and does not need to be: ``submit_checkout``
        reads the stored version's own hash and hands *that* to the kernel, which then
        re-checks it against the locked row. Comparing here as well is the cheap half of
        that -- a caller echoing a hash the platform does not hold has not re-read the
        checkout, and telling it so before a lock is taken is a better answer than a
        denial three services later.
        """
        if principal != self.ctx.principal:
            raise AuthenticationRejected(
                "admission_principal_is_not_the_transport_principal",
                presented=principal.principal_id,
                expected=self.ctx.principal.principal_id,
            )
        outcome = admission_service.submit_checkout_outcome(
            session,
            self.ctx,
            self.registry,
            checkout_id=checkout_id,
            version=version,
            idempotency_key=self.idempotency_key,
            expected_content_hash=content_hash,
        )
        if outcome.decision is None:
            raise AdmissionDuplicated(outcome.body)
        return outcome.decision

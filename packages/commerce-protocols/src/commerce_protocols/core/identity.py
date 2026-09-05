"""Who an external protocol caller is, and the ceiling on what it may ever ask for.

Step 1 of specification 13.1. An adapter authenticates a caller and turns it into a
:class:`~transaction_kernel.AgentPrincipal`, which is the only identity the kernel
understands. This module owns that translation, and owns the capability ceiling that makes
it safe.

The ceiling is the point of the module. :data:`PROTOCOL_CAPABILITIES` is the complete set
an external party may ever hold, and it is a strict subset of the trusted buyer surface's
set: discovery, basket construction, checkout construction, submitting something already
approved, reading orders. It contains no ``checkout.approve``, no ``checkout.reject``, no
``checkout.cancel`` and no ``refund.request``, and it never will, because those four are
consent. Specification 5.3 separates Registry A from Registry B precisely so that the thing
which proposed a purchase cannot also consent to it, and an external protocol caller is
the furthest-away possible instance of "the thing that proposed the purchase".

That ceiling is enforced twice, on purpose. :func:`principal_for` intersects whatever a
tenant configured against the ceiling, so a misconfiguration cannot widen it; and
:func:`assert_never_consents` re-asserts the invariant as a property anyone can call.
Belt and braces on the one rule whose violation would be invisible in a passing demo.

A note on ``ActorType.PROTOCOL``. The kernel already has this member and audits against it.
Using it rather than ``AGENT`` matters for the audit trail: a reviewer reading an audit row
must be able to tell that the request arrived over UCP from an external buyer platform, not
from this platform's own copilot, because those two have very different blast radii and the
row is the only place that difference is recorded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from transaction_kernel import ActorType, AgentPrincipal

from .errors import AuthenticationRejected
from .pins import Protocol

__all__ = [
    "CONSENT_CAPABILITIES",
    "PROTOCOL_CAPABILITIES",
    "AuthenticatedCaller",
    "assert_never_consents",
    "principal_for",
]

#: Everything an external protocol caller may ever be granted. A tenant may configure less;
#: nothing can configure more. Mirrors ``commerce_api.deps.AGENT_CAPABILITIES`` because an
#: external AI buyer is an agent that happens to arrive over a protocol rather than over
#: this platform's own session -- it is not entitled to more for having travelled further.
PROTOCOL_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "catalogue.read",
        "basket.write",
        "checkout.create",
        "checkout.submit_approved",
        "order.read",
    }
)

#: Registry B in full: every capability that belongs to the trusted buyer surface and must
#: never be held by an external protocol caller. Named here so the exclusion is a stated fact
#: with a test against it rather than an absence somebody has to notice.
#:
#: The first four are consent, which is what the name is about. ``payment.verify`` is here for
#: a different reason and is the one worth explaining: it is not consent, it is the
#: client-return verification of ADR 0003 D8, and it is bound to the buyer's own payment
#: session. A caller holding it could present a return for somebody else's checkout. It is
#: Registry B, so it is listed, and the alternative -- a second set for "buyer-only but not
#: consent" -- would be two lists to keep complete instead of one.
#:
#: This set cannot be derived from ``commerce_api.deps``, which is where Registry B is
#: actually defined, because commerce-api depends on this package and the import would be a
#: cycle (ADR 0003 D2). ``test_capi_protocols`` asserts the two stay in step from the side
#: that can see both, so a Registry B capability added there and forgotten here fails a test
#: rather than becoming quietly grantable.
CONSENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "checkout.approve",
        "checkout.reject",
        "checkout.cancel",
        "refund.request",
        "payment.verify",
    }
)


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    """The outcome of step 1: a caller this platform recognises.

    ``client_id`` is the external party's stable identifier -- an ACP API-key subject, a UCP
    platform identity, an MCP OAuth client. ``authenticated_by`` names the mechanism, and it
    is recorded in evidence because "we checked" is not evidence and "we verified an HTTP
    message signature over these headers" is.

    ``buyer_ref`` is present only when the external party is acting for a buyer this
    platform already knows. It is never taken from a request body: specification 17.3
    forbids accepting a tenant id from model arguments as authoritative, and the same
    reasoning applies to a buyer reference, which is the thing that decides whose checkout
    a caller may read.
    """

    protocol: Protocol
    client_id: str
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    authenticated_by: str
    correlation_id: uuid.UUID
    buyer_ref: str | None = None
    #: What the tenant configured for this client. Intersected with the ceiling below.
    granted: frozenset[str] = PROTOCOL_CAPABILITIES


def principal_for(caller: AuthenticatedCaller) -> AgentPrincipal:
    """The kernel principal for an authenticated external caller.

    Capabilities are the *intersection* of what the tenant granted and what a protocol
    caller may ever hold. Intersection rather than validation because the failure mode
    matters: a configuration that names ``checkout.approve`` is a mistake somebody made
    once, and the safe response is to drop it silently at every use rather than to refuse
    every request from that client until a human notices. The drop is invisible to the
    caller and total.

    ``delegation_chain`` records the external client, so a sub-agent derived from this
    principal through :meth:`AgentPrincipal.subset_for` carries the provenance of the
    outside party all the way into the audit row.
    """
    capabilities = caller.granted & PROTOCOL_CAPABILITIES
    return AgentPrincipal(
        principal_id=f"protocol:{caller.protocol.value.lower()}:{caller.client_id}",
        tenant_id=caller.tenant_id,
        actor_type=ActorType.PROTOCOL,
        agent_role=f"{caller.protocol.value.lower()}_caller",
        merchant_id=caller.merchant_id,
        buyer_ref=caller.buyer_ref,
        capabilities=capabilities,
        delegation_chain=(f"client:{caller.client_id}",),
        correlation_id=caller.correlation_id,
    )


def assert_never_consents(principal: AgentPrincipal) -> None:
    """Refuse a principal that holds any consent capability.

    This should be unreachable: :func:`principal_for` intersects the ceiling away. It is
    called anyway at the point where a protocol adapter is about to act, because the cost
    of the check is nothing and the cost of being wrong is an external party approving a
    purchase on a human's behalf -- the one failure this whole architecture exists to make
    impossible.

    Raises rather than returning a decision. A principal carrying consent capability is not
    a caller asking for too much; it is a construction bug, and continuing past it would be
    choosing to find out later.
    """
    held = principal.capabilities & CONSENT_CAPABILITIES
    if held:
        raise AuthenticationRejected(
            "protocol_principal_holds_consent_capability",
            principal_id=principal.principal_id,
            capabilities=sorted(held),
        )

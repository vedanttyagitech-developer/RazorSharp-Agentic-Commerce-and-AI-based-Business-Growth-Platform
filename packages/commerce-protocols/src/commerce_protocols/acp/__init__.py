"""The ACP-compatible external AI-buyer surface, specification 16.

This is the adapter for the one caller the platform does not run: an AI buyer operated by
somebody else, which arrives over the public internet holding a credential and asking for a
purchase. Every other entry point in this system belongs to us -- the buyer copilot, the
trusted approval surface, the worker. This one does not, and the package is arranged around
that single fact.

The rule the whole thing exists to keep is the project's governing one. Agents propose;
deterministic systems authorize and execute. An ACP session is a proposal, no matter how
confidently the external platform frames it, and the strongest thing an ACP request can do
is submit a version a human has already approved on a surface the external party cannot
reach. There is no path from here to money that does not go through the same kernel
admission the trusted UI goes through, and there is no path from here to consent at all --
:data:`~commerce_protocols.core.PROTOCOL_CAPABILITIES` holds no consent capability and
:class:`~commerce_protocols.core.IntentKind` has no member that could express one.

Three modules, three jobs:

:mod:`~commerce_protocols.acp.auth`
    Specification 16.3's front door. Two credential mechanisms, a canonical signing string
    with audience binding, the five-minute freshness window and the nonce store from
    ``core.replay``, transport limits, rate limits, and the idempotency key mutations
    require. It is the largest module here because it is the only one an attacker talks to.

:mod:`~commerce_protocols.acp.sessions`
    Specification 16.1's mapping. ACP sessions become
    :class:`~commerce_protocols.core.ProtocolIntent` values, and the version, approval,
    revocation, idempotency and payment-state invariants are enforced on the way through.

:mod:`~commerce_protocols.acp.simulator`
    The deterministic external AI buyer the tests drive. Signs correctly by default,
    misbehaves one named way at a time.

and one more that is not about traffic at all:

:mod:`~commerce_protocols.acp.claims`
    Specification 16.2's boundary, as a function rather than as a promise. This project may
    say it has an ACP-compatible interface validated locally against the pinned public
    schema. It may not say a merchant is live in ChatGPT Instant Checkout, and
    :func:`~commerce_protocols.acp.claims.assert_claim_permitted` raises rather than lets it.
"""

from __future__ import annotations

from .auth import (
    ACCEPTED_CONTENT_TYPE,
    MAX_BODY_BYTES,
    AcpClient,
    AcpRequest,
    AdmittedRequest,
    ClientRegistry,
    PayloadRejected,
    RateLimit,
    RateLimited,
    TokenBucketLimiter,
    admit,
    sign,
    signing_string,
)
from .claims import (
    CLAIM_FOR_BOUNDARY,
    FORBIDDEN_CLAIM,
    PERMITTED_CLAIM,
    OverclaimError,
    SurfaceStatus,
    assert_claim_permitted,
    describe_surface,
)
from .sessions import (
    MUTATIONS,
    AcpOperation,
    AcpRoute,
    AcpSession,
    AcpSessionStatus,
    approval_handoff,
    idempotency_key_for,
    map_request,
    parse_amount,
    route,
)
from .simulator import AcpBuyerSimulator, Credential, Misbehaviour

__all__ = [
    "ACCEPTED_CONTENT_TYPE",
    "CLAIM_FOR_BOUNDARY",
    "FORBIDDEN_CLAIM",
    "MAX_BODY_BYTES",
    "MUTATIONS",
    "PERMITTED_CLAIM",
    "AcpBuyerSimulator",
    "AcpClient",
    "AcpOperation",
    "AcpRequest",
    "AcpRoute",
    "AcpSession",
    "AcpSessionStatus",
    "AdmittedRequest",
    "ClientRegistry",
    "Credential",
    "Misbehaviour",
    "OverclaimError",
    "PayloadRejected",
    "RateLimit",
    "RateLimited",
    "SurfaceStatus",
    "TokenBucketLimiter",
    "admit",
    "approval_handoff",
    "assert_claim_permitted",
    "describe_surface",
    "idempotency_key_for",
    "map_request",
    "parse_amount",
    "route",
    "sign",
    "signing_string",
]

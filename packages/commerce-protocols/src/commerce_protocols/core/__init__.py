"""The protocol-neutral core, specification 13.1 and 13.3.

Every adapter in this package -- UCP, AP2, ACP, MCP -- is written against exactly these
types and no others. That is what makes the claim in specification 13.1 true rather than
aspirational: the domain and the kernel do not depend on any protocol, because a protocol
never reaches them. It reaches this layer, becomes a :class:`ProtocolIntent`, and the code
below cannot tell where it came from.

The pipeline an adapter follows, in order, is specification 13.1's seven steps:

1. authenticate the caller -- :mod:`commerce_protocols.core.identity`
2. validate the pinned schema and version -- :mod:`commerce_protocols.core.pins`
3. verify signatures, timestamps, nonce, audience and replay -- :mod:`.replay`, plus each
   protocol's own cryptography
4. map protocol objects into a typed internal command -- :mod:`.intent`
5. produce a ``VerifiedAuthorityProof`` when financial authority is valid -- the kernel's
   own type, deliberately not redefined here
6. call the same deterministic kernel every other surface calls
7. map the resulting state and structured error back into the protocol

with :mod:`.evidence` recording each step, and :mod:`.errors` carrying every refusal.

Step 5 is worth reading twice. ``VerifiedAuthorityProof`` lives in
``commerce_domain.contracts`` and this package imports it rather than defining a
protocol-side equivalent. There is exactly one description of what a verified mandate
entitles somebody to, it belongs to the kernel, and an adapter's job is to fill it in
honestly or not at all.
"""

from __future__ import annotations

from .errors import (
    AuthenticationRejected,
    CorrelationRejected,
    MandateRejected,
    ProtocolRejection,
    ReplayRejected,
    SchemaRejected,
    SignatureRejected,
    StateRejected,
    VersionRejected,
)
from .evidence import (
    AGGREGATE_TYPE,
    EvidenceStage,
    Fingerprint,
    ProtocolInteraction,
    fingerprint,
    open_interaction,
)
from .identity import (
    CONSENT_CAPABILITIES,
    PROTOCOL_CAPABILITIES,
    AuthenticatedCaller,
    assert_never_consents,
    principal_for,
)
from .intent import PROPOSAL_INTENTS, READ_ONLY_INTENTS, IntentKind, ProtocolIntent
from .pins import (
    PINS,
    ClaimBoundary,
    Protocol,
    ProtocolPin,
    UnsupportedProtocolError,
    UnsupportedVersionError,
    require_pin,
)
from .replay import (
    DEFAULT_MAX_REQUEST_AGE,
    REPLAY_OPERATION,
    assert_fresh,
    claim_nonce,
    database_now,
    nonce_key,
)

__all__ = [
    "AGGREGATE_TYPE",
    "CONSENT_CAPABILITIES",
    "DEFAULT_MAX_REQUEST_AGE",
    "PINS",
    "PROPOSAL_INTENTS",
    "PROTOCOL_CAPABILITIES",
    "READ_ONLY_INTENTS",
    "REPLAY_OPERATION",
    "AuthenticatedCaller",
    "AuthenticationRejected",
    "ClaimBoundary",
    "CorrelationRejected",
    "EvidenceStage",
    "Fingerprint",
    "IntentKind",
    "MandateRejected",
    "Protocol",
    "ProtocolIntent",
    "ProtocolInteraction",
    "ProtocolPin",
    "ProtocolRejection",
    "ReplayRejected",
    "SchemaRejected",
    "SignatureRejected",
    "StateRejected",
    "UnsupportedProtocolError",
    "UnsupportedVersionError",
    "VersionRejected",
    "assert_fresh",
    "assert_never_consents",
    "claim_nonce",
    "database_now",
    "fingerprint",
    "nonce_key",
    "open_interaction",
    "principal_for",
    "require_pin",
]

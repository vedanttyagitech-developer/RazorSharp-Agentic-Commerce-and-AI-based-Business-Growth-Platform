"""UCP 2026-08-25, specification 14.

The surface a merchant publishes and the lifecycle it answers, adapted onto the
protocol-neutral core. Four modules, and the division between them is the specification's
own: :mod:`.profile` is what a merchant publishes about itself (14.1), :mod:`.lifecycle`
translates UCP objects to and from internal ones (14.2), :mod:`.checkout` decides the status
of a Complete Checkout call (14.3), and :mod:`.continuation` carries the escalation handoff
that 14.3 requires when the platform cannot complete a payment by itself.

The single most important thing in this package is a status that is *not* returned.
Specification 14.3 says the P0 Standard Checkout path answers ``requires_escalation`` with a
short-lived, single-use trusted ``continue_url``, and never ``complete_in_progress``,
because Razorpay test mode has no headless charge path and a status that claims the request
was accepted would leave a conforming client polling for a completion that is never coming.
:mod:`.checkout` makes that structural rather than conventional.
"""

from __future__ import annotations

from .checkout import (
    AsyncGround,
    Completed,
    CompleteOutcome,
    CompletionContext,
    Escalation,
    Incomplete,
    InProgress,
    decide_completion,
    escalate_for_razorpay_handoff,
    outcome_payload,
)
from .continuation import (
    DEFAULT_CONTINUATION_TTL,
    MAX_CONTINUATION_TTL,
    ContinuationClaims,
    consume_continuation,
    issue_continuation,
)
from .lifecycle import (
    LIFECYCLE_OBJECTS,
    UCP_INTENT_BY_OPERATION,
    checkout_projection,
    intent_for,
    line_items_from,
    total_of,
)
from .messages import (
    CODE_MERCHANT_STATE_CHANGED,
    CODE_NO_HEADLESS_PAYMENT_PATH,
    CODE_RESERVATION_EXPIRED,
    CODE_STALE_CHECKOUT,
    SEVERITIES,
    escalation_message,
    message_payload,
    recoverable_message,
)
from .profile import (
    MERCHANT_PROFILE_PATH,
    PLATFORM_PROFILE_PATH,
    SUPPORTED_CAPABILITIES,
    BusinessProfile,
    jwks_document,
    profile_document,
)

__all__ = [
    "CODE_MERCHANT_STATE_CHANGED",
    "CODE_NO_HEADLESS_PAYMENT_PATH",
    "CODE_RESERVATION_EXPIRED",
    "CODE_STALE_CHECKOUT",
    "DEFAULT_CONTINUATION_TTL",
    "LIFECYCLE_OBJECTS",
    "MAX_CONTINUATION_TTL",
    "MERCHANT_PROFILE_PATH",
    "PLATFORM_PROFILE_PATH",
    "SEVERITIES",
    "SUPPORTED_CAPABILITIES",
    "UCP_INTENT_BY_OPERATION",
    "AsyncGround",
    "BusinessProfile",
    "CompleteOutcome",
    "Completed",
    "CompletionContext",
    "ContinuationClaims",
    "Escalation",
    "InProgress",
    "Incomplete",
    "checkout_projection",
    "consume_continuation",
    "decide_completion",
    "escalate_for_razorpay_handoff",
    "escalation_message",
    "intent_for",
    "issue_continuation",
    "jwks_document",
    "line_items_from",
    "message_payload",
    "outcome_payload",
    "profile_document",
    "recoverable_message",
    "total_of",
]

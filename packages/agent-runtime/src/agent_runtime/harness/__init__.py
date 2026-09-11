"""The copilot harness: Python that owns session, tenant, routing and binding.

A harness calls no model and has no prompt. ``RazorAI`` routes among Shopping, Checkout
and Support. It binds the harness principal to a specialist through ``bind`` (a subset,
never wider), runs the grounding hook before the specialist and the post-check and
specification 6.1 rules after, and writes the transcript in the reference event shape.
Nothing here imports ``google.adk``.

There was a second harness. ``MerchantCopilot`` routed between Growth and Case on the
merchant surface, and it was removed with them; ``is_merchant_principal`` stays, because
the buyer harness still has to be able to refuse a principal that is not a buyer's.
"""

from .base import (
    REGISTRY_A_CAPABILITIES,
    ROLE_CAPABILITIES,
    Binding,
    BindingError,
    BoundSpecialist,
    GroundingHook,
    HandBack,
    Harness,
    HarnessConfigurationError,
    PrincipalRefusedError,
    SpecialistInput,
    SpecialistReply,
    SpecialistRunner,
    ToolsetBuilder,
    TurnResult,
    bind,
    enforce_conversational_rules,
    factory_toolset,
    is_buyer_principal,
    is_merchant_principal,
    prefetch_grounding,
)
from .merchant import MerchantCopilot
from .razorai import RazorAI
from .routing import (
    BUYER_SPECIALISTS,
    MERCHANT_SPECIALISTS,
    Clarification,
    Route,
    Specialist,
    route_buyer,
    route_merchant,
)
from .session import (
    CopilotSession,
    InMemorySessionStore,
    Modality,
    SessionRefusedError,
    SessionStore,
    session_tag,
)
from .transcript import AgentEvent, Transcript, TranscriptTurn, events_for_calls, to_sse

__all__ = [
    "AgentEvent",
    "BUYER_SPECIALISTS",
    "Binding",
    "BindingError",
    "BoundSpecialist",
    "Clarification",
    "CopilotSession",
    "GroundingHook",
    "HandBack",
    "Harness",
    "HarnessConfigurationError",
    "InMemorySessionStore",
    "MERCHANT_SPECIALISTS",
    "MerchantCopilot",
    "Modality",
    "PrincipalRefusedError",
    "REGISTRY_A_CAPABILITIES",
    "ROLE_CAPABILITIES",
    "RazorAI",
    "Route",
    "SessionRefusedError",
    "SessionStore",
    "Specialist",
    "SpecialistInput",
    "SpecialistReply",
    "SpecialistRunner",
    "ToolsetBuilder",
    "Transcript",
    "TranscriptTurn",
    "TurnResult",
    "bind",
    "enforce_conversational_rules",
    "events_for_calls",
    "factory_toolset",
    "is_buyer_principal",
    "is_merchant_principal",
    "prefetch_grounding",
    "route_buyer",
    "route_merchant",
    "session_tag",
    "to_sse",
]

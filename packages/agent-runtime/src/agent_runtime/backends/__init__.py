"""Commerce backends: the only operations an agent may call.

``CommerceBackend`` is the interface; ``InMemoryBackend`` runs over the deterministic
merchant simulator (tests and demo fallback); ``HttpBackend`` speaks to the ADR 0003 API.
``InMemoryTrustedSurface`` is Registry B for the demo harness and is deliberately not a
backend method.

``CommerceBackend`` is the buyer surface and ``SupportBackend`` is the post-purchase
support surface, each its own protocol. The tool factory offers a specialist's rows
only against a backend that actually implements the protocol they need, so a missing
surface leaves those rows reported as unbuilt rather than holding a closure with nothing
behind it.
"""

from .base import (
    AGENT_OPERATIONS,
    NEVER_ON_AGENT_SURFACE,
    ApprovalCard,
    BackendError,
    CartQuote,
    CartView,
    CheckoutStatus,
    CheckoutView,
    CommerceBackend,
    OrderResolution,
    OrderState,
    OrderView,
    PaymentSummary,
    PolicyAtSale,
    PolicyKind,
    PolicyTerm,
    PricedLine,
    Problem,
    ProductCard,
    Provenance,
    RefundRecord,
    RemedyConfirmation,
    RemedyOption,
    RemedyOutcome,
    ResolutionPlan,
    SearchPage,
    SupportBackend,
    UnavailableLine,
    WithheldReason,
    WithheldRemedy,
    backend_problem,
)
from .http import HttpBackend, parse_problem
from .memory import InMemoryBackend, InMemoryTrustedSurface, compute_deltas

__all__ = [
    "AGENT_OPERATIONS",
    "NEVER_ON_AGENT_SURFACE",
    "ApprovalCard",
    "BackendError",
    "CartQuote",
    "CartView",
    "CheckoutStatus",
    "CheckoutView",
    "CommerceBackend",
    "HttpBackend",
    "InMemoryBackend",
    "InMemoryTrustedSurface",
    "OrderResolution",
    "OrderState",
    "OrderView",
    "PaymentSummary",
    "PolicyAtSale",
    "PolicyKind",
    "PolicyTerm",
    "PricedLine",
    "Problem",
    "ProductCard",
    "Provenance",
    "RefundRecord",
    "RemedyConfirmation",
    "RemedyOption",
    "RemedyOutcome",
    "ResolutionPlan",
    "SearchPage",
    "SupportBackend",
    "UnavailableLine",
    "WithheldReason",
    "WithheldRemedy",
    "backend_problem",
    "compute_deltas",
    "parse_problem",
]

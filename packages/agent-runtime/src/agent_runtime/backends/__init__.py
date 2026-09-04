"""Commerce backends: the only operations an agent may call.

``CommerceBackend`` is the interface; ``InMemoryBackend`` runs over the deterministic
merchant simulator (tests and demo fallback); ``HttpBackend`` speaks to the ADR 0003 API.
``InMemoryTrustedSurface`` is Registry B for the demo harness and is deliberately not a
backend method.
"""

from .base import (
    AGENT_OPERATIONS,
    NEVER_ON_AGENT_SURFACE,
    ApprovalCard,
    BackendError,
    BasketQuote,
    BasketView,
    CheckoutStatus,
    CheckoutView,
    CommerceBackend,
    OrderState,
    OrderView,
    PaymentSummary,
    PricedLine,
    Problem,
    ProductCard,
    Provenance,
    RefundRecord,
    SearchPage,
    UnavailableLine,
    backend_problem,
)
from .http import HttpBackend, parse_problem
from .memory import InMemoryBackend, InMemoryTrustedSurface, compute_deltas

__all__ = [
    "AGENT_OPERATIONS",
    "NEVER_ON_AGENT_SURFACE",
    "ApprovalCard",
    "BackendError",
    "BasketQuote",
    "BasketView",
    "CheckoutStatus",
    "CheckoutView",
    "CommerceBackend",
    "HttpBackend",
    "InMemoryBackend",
    "InMemoryTrustedSurface",
    "OrderState",
    "OrderView",
    "PaymentSummary",
    "PricedLine",
    "Problem",
    "ProductCard",
    "Provenance",
    "RefundRecord",
    "SearchPage",
    "UnavailableLine",
    "backend_problem",
    "compute_deltas",
    "parse_problem",
]

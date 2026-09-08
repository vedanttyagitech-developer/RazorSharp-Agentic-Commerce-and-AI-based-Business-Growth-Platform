"""The Merchant Action Controller: what a merchant may change, and who agreed to it.

Merchant identity and permissions, typed proposals, a canonical action hash, human
approval, and the lifecycle in between. It invokes the business modules that own their
domains -- catalogue, pricing, inventory, policy -- and duplicates none of their
arithmetic.

**It holds no financial authority and imports no kernel.** It cannot update an approval, a
checkout version, a Policy-at-Sale Receipt, a reservation, a payment attempt, a refund or an
Execution Grant, and a boundary test asserts the import half of that so the packaging half
cannot quietly regress. A merchant-initiated financial remedy goes through a separately
authorised financial API and the kernel, which is a different request with different
permissions rather than a wider version of this one.
"""

from .actions import (
    ACTION_KEYS,
    ACTION_VERSION,
    LIVE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    MerchantAction,
    MerchantActionError,
    MerchantActionKind,
    MerchantActionResult,
    MerchantActionState,
    action_hash,
    build_action_content,
    may_move,
)

__all__ = [
    "ACTION_KEYS",
    "ACTION_VERSION",
    "LIVE_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "MerchantAction",
    "MerchantActionError",
    "MerchantActionKind",
    "MerchantActionResult",
    "MerchantActionState",
    "action_hash",
    "build_action_content",
    "may_move",
]

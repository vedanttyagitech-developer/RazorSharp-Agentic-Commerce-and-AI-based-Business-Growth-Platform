"""Merchant fee policy: the inputs to the one function allowed to compute a total.

Kept in its own module so that both the store (which holds the live policy and lets a
scenario injection change it) and the fee engine (which applies it) can import it without
a cycle.

Every rate here is an integer in basis points. There is no percentage float anywhere in
this package: 18% is ``1800``, and the arithmetic that consumes it is integer arithmetic
with an explicit rounding rule. A float rate is how a total ends up one paisa away from
the invoice on a different machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from commerce_domain import Money

from .catalogue import CURRENCY

__all__ = ["BP_SCALE", "DEFAULT_FEE_POLICY", "FeePolicy"]

#: Basis-point denominator. 10 000 bp = 100%.
BP_SCALE: Final[int] = 10_000


@dataclass(frozen=True, slots=True)
class FeePolicy:
    """Delivery pricing and tax rules for one merchant.

    Threshold semantics are inclusive and are evaluated against the pre-tax item subtotal.
    Both choices are stated here because they are exactly what a buyer disputes: "my cart
    said 499 and I still paid delivery". Pre-tax is the honest basis -- a buyer can add up
    the shelf prices they saw and predict the outcome, which they cannot do if the
    threshold silently counts GST -- and inclusive means the advertised number is
    reachable rather than one paisa out of reach.
    """

    base_delivery_fee: Money
    free_delivery_threshold: Money
    delivery_tax_bp: int
    currency: str = CURRENCY

    def __post_init__(self) -> None:
        if self.base_delivery_fee.currency != self.currency:
            raise ValueError("delivery fee currency must match the policy currency")
        if self.free_delivery_threshold.currency != self.currency:
            raise ValueError("free-delivery threshold currency must match the policy currency")
        if self.base_delivery_fee.is_negative:
            raise ValueError("a negative delivery fee is a discount, not a fee")
        if self.free_delivery_threshold.is_negative:
            raise ValueError("free-delivery threshold cannot be negative")
        if not 0 <= self.delivery_tax_bp <= BP_SCALE:
            raise ValueError("delivery_tax_bp must be a rate in basis points")

    def qualifies_for_free_delivery(self, items_subtotal: Money) -> bool:
        """True when this pre-tax subtotal earns free delivery.

        Inclusive at the boundary: a subtotal exactly equal to the threshold qualifies.
        """
        if items_subtotal.currency != self.currency:
            raise ValueError("subtotal currency does not match the fee policy")
        # bool(): Money compares fine at runtime, but commerce-domain ships no py.typed
        # marker yet, so mypy sees Any here. Remove the call once it does.
        return bool(items_subtotal >= self.free_delivery_threshold)

    def delivery_fee_for(self, items_subtotal: Money) -> Money:
        """Delivery fee for this pre-tax subtotal. Zero once the threshold is reached."""
        if self.qualifies_for_free_delivery(items_subtotal):
            return Money.zero(self.currency)
        return self.base_delivery_fee

    def gap_to_free_delivery(self, items_subtotal: Money) -> Money:
        """How much more, pre-tax, earns free delivery. Zero once delivery is already free.

        Specification 6.2: the deterministic fee engine computes the threshold gap and the
        agent only phrases the nudge. If the agent subtracted these numbers itself it
        could quote a gap that is off by a paisa, or invent one that was never real.

        The gap is measured against the fee actually charged, not against the threshold
        alone. A merchant whose base delivery fee is zero delivers free to everybody, so
        there is nothing to spend more to reach; reporting a threshold gap there would
        nudge a buyer to add items for a discount they already have, and would contradict
        :class:`~merchant_sim.fees.Quote`, which requires a zero gap whenever no delivery
        fee is charged.
        """
        if self.delivery_fee_for(items_subtotal).is_zero:
            return Money.zero(self.currency)
        return self.free_delivery_threshold - items_subtotal


#: The Demo Grocery Store's published policy: Rs 25.00 delivery, free over Rs 499.00,
#: 18% GST on the delivery service.
DEFAULT_FEE_POLICY: Final[FeePolicy] = FeePolicy(
    base_delivery_fee=Money(2500, CURRENCY),
    free_delivery_threshold=Money(49900, CURRENCY),
    delivery_tax_bp=1800,
)

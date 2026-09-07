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

__all__ = ["BP_SCALE", "DEFAULT_FEE_POLICY", "FeePolicy", "Promotion"]

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


@dataclass(frozen=True, slots=True)
class Promotion:
    """One merchant offer, live between two instants.

    Deliberately the smallest thing that is still a real offer: a cart-wide percentage or
    a cart-wide flat amount, one at a time, with a start and an end. No stacking, no
    bundles, no per-product targeting, no budget and no coupon code. Each of those is a
    separate product decision, and a demo that implements them badly is worse than one
    that implements a single offer honestly.

    The window is carried in epoch milliseconds rather than as a live check against a
    clock, because the store owns the clock and the promotion is a value: two quotes taken
    at the same instant against the same catalogue must price identically, and a rule that
    consulted ``now()`` for itself could not promise that.

    Percentages are basis points for the same reason tax is: a rate expressed as a float
    cannot be reproduced exactly, and the discount has to be an integer number of paise
    that the buyer, the receipt and the provider all agree on.
    """

    offer_id: str
    label: str
    percent_bp: int | None = None
    flat: Money | None = None
    effective_from_epoch_ms: int = 0
    effective_to_epoch_ms: int = 0
    enabled: bool = True
    currency: str = CURRENCY

    def __post_init__(self) -> None:
        if not self.offer_id.strip():
            raise ValueError("a promotion needs an offer_id")
        if not self.label.strip():
            raise ValueError("a promotion needs a label a buyer can read")
        if (self.percent_bp is None) == (self.flat is None):
            raise ValueError("a promotion is either a percentage or a flat amount, not both")
        if self.percent_bp is not None and not 0 < self.percent_bp <= BP_SCALE:
            raise ValueError("percent_bp must be a rate in basis points above zero")
        if self.flat is not None:
            if self.flat.currency != self.currency:
                raise ValueError("a flat discount must be in the promotion currency")
            if self.flat.minor <= 0:
                raise ValueError("a flat discount of nothing is not an offer")
        if self.effective_to_epoch_ms <= self.effective_from_epoch_ms:
            raise ValueError("a promotion must end after it starts")

    def is_live_at(self, epoch_ms: int) -> bool:
        """Whether this offer applies at that instant. Start inclusive, end exclusive."""
        return (
            self.enabled and self.effective_from_epoch_ms <= epoch_ms < self.effective_to_epoch_ms
        )

    def discount_on(self, items_subtotal: Money, *, payable_before_discount: Money) -> Money:
        """What this offer takes off, in whole paise, clamped so something is still payable.

        Two bounds, and the second is the one that is easy to miss. The raw discount is
        capped at the item subtotal so an offer can never exceed the goods; it is then
        capped again at one paisa below the whole payable amount, because a cart of
        zero-tax items over the free-delivery threshold has no tax and no fee to absorb
        the difference, and a hundred-percent offer there would produce a total of exactly
        zero. A zero-amount order is not a payment, no provider will accept one, and the
        buyer would have approved a purchase that cannot be executed.

        Rounding is half-up on the integer subtotal, the same rule tax uses, so the two
        figures on one quote are produced by one arithmetic.
        """
        if self.flat is not None:
            raw = self.flat.minor
        else:
            assert self.percent_bp is not None
            raw = (items_subtotal.minor * self.percent_bp + BP_SCALE // 2) // BP_SCALE
        capped = min(raw, items_subtotal.minor, max(payable_before_discount.minor - 1, 0))
        return Money(max(capped, 0), self.currency)


#: The Demo Grocery Store's published policy: Rs 25.00 delivery, free over Rs 499.00,
#: 18% GST on the delivery service.
DEFAULT_FEE_POLICY: Final[FeePolicy] = FeePolicy(
    base_delivery_fee=Money(2500, CURRENCY),
    free_delivery_threshold=Money(49900, CURRENCY),
    delivery_tax_bp=1800,
)

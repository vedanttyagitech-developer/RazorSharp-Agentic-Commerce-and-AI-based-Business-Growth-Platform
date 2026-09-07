"""The grounding ledger: what a reply is allowed to assert this turn.

Specification 20.1 and 20.4: a model may reference only catalogue IDs the merchant
actually returned, and every money fact must come from a structured tool result. The
ledger is the record of what was returned. The reply post-check reads it; any SKU or
amount in the reply that the ledger does not know is, by definition, not grounded.

The ledger is per turn. Yesterday's price is not evidence for today's sentence, and a
price from the previous turn is not either: prices move under a checkout (that is the
demonstration), so an amount in prose must come from a tool result *this turn*. Which
SKUs exist is a session fact and lives in ``core.provenance``; which amounts are
true is a turn fact and lives here (ADR 0004 section 2.4).

Unit counts are held here for the same reason and are not a variety of amount. "Only 2
left" is not money, so it passes every currency-shaped check untouched, and it is the
cheapest scarcity claim a model can invent: a shelf count is the one number a buyer reads
as a reason to hurry. Stock moves under a cart exactly as price moves under a checkout,
so a count in prose has to come from a tool result this turn as well, and it is recorded
separately from ``amounts_minor`` so that a price of 200 paise can never ground a claim
that 200 units remain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from commerce_domain import Money
from transaction_kernel import KernelDecision

from ..backends.base import (
    ApprovalCard,
    CartQuote,
    CartView,
    CheckoutView,
    OrderView,
    ProductCard,
)

__all__ = ["GroundedProduct", "GroundingLedger"]

#: A delta path names the line it concerns: ``lines[AMUL-DAIRY-001].unit_price_minor``.
_LINE_PATH: Final[re.Pattern[str]] = re.compile(r"^lines\[([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class GroundedProduct:
    """A product the merchant returned this turn, for offering as an alternative."""

    sku: str
    name: str
    unit_price: Money
    is_available: bool


@dataclass(slots=True)
class GroundingLedger:
    """Grounded SKUs, money facts and payment states seen this turn."""

    products: dict[str, GroundedProduct] = field(default_factory=dict)
    amounts_minor: set[int] = field(default_factory=set)
    #: Every shelf count a tool reported this turn, from any SKU. Membership only: the
    #: post-check is lexical and cannot tell which product a sentence means, so it asks
    #: the weaker, honest question -- did any read this turn return this count at all.
    stock_counts: set[int] = field(default_factory=set)
    currencies: set[str] = field(default_factory=set)
    payment_states: set[str] = field(default_factory=set)
    decisions: list[KernelDecision] = field(default_factory=list)

    # ---- recording --------------------------------------------------------

    def record_money(self, money: Money) -> None:
        self.amounts_minor.add(money.minor)
        self.currencies.add(money.currency)

    def record_stock(self, units: int) -> None:
        """A shelf count a merchant read returned. Never a quantity the buyer chose."""
        self.stock_counts.add(units)

    def record_merchant_product(
        self, sku: str, label: str, *, stock_units: int | None, is_available: bool
    ) -> None:
        """A product a merchant-side read named, and the shelf count it reported.

        The merchant reads are the one grounded source that returns a SKU with no price
        beside it, so they need their own recorder: ``record_product`` takes a
        :class:`~agent_runtime.backends.base.ProductCard`, and a merchant looking at their
        own inventory is not reading product cards. Without this a Growth Specialist could
        not name a single product it had just read -- the post-check would find no SKU in
        this ledger, drop the sentence, and tell the merchant their own catalogue could not
        be verified. That is measured rather than predicted: it is what the first live
        model-backed growth turn did.

        ``unit_price`` is zero because none was read, following ``record_cart`` and
        ``record_decision``, which record the same way for a SKU the platform named without
        pricing. Nothing calls :meth:`record_money` here, so a zero price can never ground
        an amount in prose.

        ``label`` is the merchant's own text as the caller decided to draw it -- their name,
        or a safe label standing in for one the fence quarantined. It must not equal the
        SKU: a grounded product whose name is its own SKU is how the conversational rules
        recognise a line the merchant reported unavailable to a *buyer*, and a merchant
        reading their inventory should not be handed that buyer-facing sentence.
        """
        self.products.setdefault(
            sku,
            GroundedProduct(
                sku=sku,
                name=label if label and label != sku else f"catalogue item {sku}",
                unit_price=Money.zero("INR"),
                is_available=is_available,
            ),
        )
        if stock_units is not None:
            self.record_stock(stock_units)

    def record_product(self, card: ProductCard) -> None:
        self.products[card.sku] = GroundedProduct(
            sku=card.sku,
            name=card.name,
            unit_price=card.unit_price,
            is_available=card.is_available,
        )
        self.record_money(card.unit_price)
        self.record_stock(card.stock_units)

    def record_quote(self, quote: CartQuote) -> None:
        for money in quote.amounts():
            self.record_money(money)
        for line in quote.lines:
            self.products.setdefault(
                line.sku,
                GroundedProduct(
                    sku=line.sku, name=line.name, unit_price=line.unit_price, is_available=True
                ),
            )

    def record_cart(self, view: CartView) -> None:
        if view.quote is not None:
            self.record_quote(view.quote)
        for line in view.unavailable:
            # The merchant said how many it could actually have fulfilled, and that is the
            # honest form of "there are only three": a count this read returned, offered
            # to explain a line it could not price rather than to hurry the buyer along.
            self.record_stock(line.available_units)
            # An unavailable line is still a real SKU the merchant named; the reply may
            # mention it (to say it is unavailable) without being ungrounded.
            self.products.setdefault(
                line.sku,
                GroundedProduct(
                    sku=line.sku, name=line.sku, unit_price=Money.zero("INR"), is_available=False
                ),
            )

    def record_approval(self, card: ApprovalCard) -> None:
        self.record_quote(card.quote)

    def record_checkout(self, view: CheckoutView) -> None:
        for card in view.versions:
            self.record_approval(card)
        if view.payment is not None:
            self.payment_states.add(view.payment.state)

    def record_order(self, view: OrderView) -> None:
        """An order is the one past-tense fact source; its payment state is provider-verified."""
        for money in view.amounts():
            self.record_money(money)
        if view.quote is not None:
            self.record_quote(view.quote)
        self.payment_states.add(view.payment.state)

    def record_decision(self, decision: KernelDecision, currency: str) -> None:
        """A refusal's deltas are facts: the amounts on both sides, and the SKUs they name.

        A SKU the kernel names in a delta path is as grounded as one a search returned;
        the reply must be able to say which line moved. It is recorded under its own SKU
        as its name so it is never offered as an alternative (it has no merchant text).
        """
        self.decisions.append(decision)
        for delta in decision.deltas:
            match = _LINE_PATH.match(delta.field_path)
            if match is not None:
                self.products.setdefault(
                    match.group(1),
                    GroundedProduct(
                        sku=match.group(1),
                        name=match.group(1),
                        unit_price=Money.zero(currency),
                        is_available=delta.reason != "ITEM_DELISTED",
                    ),
                )
            if not delta.field_path.endswith("_minor"):
                continue
            for value in (delta.approved, delta.current):
                if isinstance(value, int) and not isinstance(value, bool):
                    self.amounts_minor.add(value)
                    self.currencies.add(currency)

    # ---- queries ----------------------------------------------------------

    def knows_sku(self, sku: str) -> bool:
        return sku in self.products

    def knows_amount(self, minor: int) -> bool:
        return minor in self.amounts_minor

    def knows_stock_count(self, units: int) -> bool:
        return units in self.stock_counts

    def skus(self) -> tuple[str, ...]:
        return tuple(self.products)

    def payment_captured(self) -> bool:
        """True only when a structured read reported provider-verified capture."""
        return "CAPTURED" in self.payment_states

    def alternatives(self, limit: int = 5) -> tuple[GroundedProduct, ...]:
        """Grounded, in-stock products to offer instead of an unverifiable one (20.4 step 3)."""
        available = [p for p in self.products.values() if p.is_available and p.name != p.sku]
        return tuple(available[:limit])

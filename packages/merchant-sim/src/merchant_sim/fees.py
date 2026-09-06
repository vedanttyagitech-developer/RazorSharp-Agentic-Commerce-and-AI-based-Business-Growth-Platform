"""The fee engine. The only thing in this platform that computes a basket total.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
The agent never does arithmetic on money. It may not add a line, apply a rate, subtract a
discount or work out how far a basket is from free delivery. It calls
:func:`quote_basket`, receives a :class:`Quote`, and reads numbers out of it.

That is not a style preference. A language model that adds two prices is a system that
sometimes adds them wrong, and a wrong total is a wrong charge -- it flows through the
checkout hash, into the buyer's approval, into the Execution Grant and out to the payment
provider, and every downstream component faithfully preserves the error. Confining
arithmetic to one deterministic function makes the total reproducible, hashable and
disputable against a single implementation.

:class:`Quote` enforces this at construction: it re-derives its own total from its
components and refuses to exist if they disagree. A hand-built Quote with an "adjusted"
total cannot be created.

ROUNDING
--------
Tax is computed per line -- ``unit_price * quantity``, then the rate -- with half-up
rounding on integer minor units:

    tax = (base_minor * rate_bp + 5000) // 10000

Per line rather than on the aggregate, because that is how a GST invoice is issued and how
a line-level refund must later be computed; rounding the aggregate and then splitting it
would make a partial refund fail to reconcile by a paisa. Half-up rather than
banker's rounding, because it matches Indian invoicing convention and, more importantly,
because it is one fixed rule: two pods, two runs and two months apart must produce the
same paisa.

No float, no Decimal, no division that can produce a fraction of a paisa. Every value here
is :class:`commerce_domain.Money` over integer minor units.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import Money, canonical_hash
from transaction_kernel import RecoveryCode

from .errors import InvalidBasketError
from .grounding import Freshness
from .policy import BP_SCALE, FeePolicy
from .store import MerchantStore

__all__ = [
    "BasketLine",
    "Quote",
    "QuoteLine",
    "QuoteResult",
    "Unavailability",
    "quote_basket",
    "tax_on",
]

_ROUNDING_HALF: Final[int] = BP_SCALE // 2


def tax_on(base: Money, rate_bp: int) -> Money:
    """Half-up tax on an integer minor-unit base.

    Guarantees an exact integer result with no float or Decimal anywhere in the path.
    Refuses a negative base or a rate outside 0-10000 basis points: a negative taxable
    base means a credit note, which is a different document with different rules, and
    silently taxing it would produce a negative tax line nobody reviewed.
    """
    if rate_bp < 0 or rate_bp > BP_SCALE:
        raise ValueError(f"rate_bp must be between 0 and {BP_SCALE}, got {rate_bp}")
    if base.is_negative:
        raise ValueError("tax base cannot be negative; a credit is not a quote line")
    return Money((base.minor * rate_bp + _ROUNDING_HALF) // BP_SCALE, base.currency)


@dataclass(frozen=True, slots=True)
class BasketLine:
    """What the buyer wants: a SKU and a whole number of units. Carries no price.

    The absence of a price field is deliberate. If a caller could hand the fee engine a
    price, an agent could hand it a hallucinated one and the resulting total would look
    perfectly consistent all the way to the payment provider.
    """

    sku: str
    quantity: int

    def __post_init__(self) -> None:
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise InvalidBasketError(f"{self.sku}: quantity must be an int")
        if self.quantity <= 0:
            raise InvalidBasketError(f"{self.sku}: quantity must be positive")


@dataclass(frozen=True, slots=True)
class QuoteLine:
    """One priced line. ``subtotal`` is ``unit_price * quantity``, exactly.

    ``delivery_promise_days`` is the merchant's promise for this product, copied off the
    catalogue record so the surface showing the line can draw "Get it by ..." beside it
    without a second read. It is carried here rather than resolved into a date for the
    reasons :mod:`merchant_sim.catalogue` gives: the merchant states a duration, and only
    the surface knows the buyer's calendar day.
    """

    sku: str
    name: str
    unit_price: Money
    quantity: int
    subtotal: Money
    tax_bp: int
    tax: Money
    delivery_promise_days: int

    def __post_init__(self) -> None:
        if self.subtotal != self.unit_price * self.quantity:
            raise ValueError(f"{self.sku}: line subtotal does not equal price x quantity")
        if self.tax != tax_on(self.subtotal, self.tax_bp):
            raise ValueError(f"{self.sku}: line tax does not match the stated rate")

    def to_content(self) -> dict[str, Any]:
        """Canonicalizable line for the checkout content.

        The ``sku`` and ``quantity`` keys are the shape the Transaction Assurance Kernel
        reads when it derives held inventory from ``checkout_versions.content -> 'lines'``.
        Renaming either breaks reservation accounting.

        ``delivery_promise_days`` is deliberately absent. These bytes are what a buyer's
        approval binds to, and adding a key to them changes the hash of every checkout
        this store has ever produced for no gain: the promise is catalogue data recoverable
        from the SKU, it does not change what is being bought or what it costs, and a
        buyer who approved a basket did not approve a delivery date. The one thing that
        would follow from putting it here is that the kernel would have to compare it at
        revalidation and demand a fresh approval when a merchant moved a promise by a day.
        """
        return {
            "sku": self.sku,
            "name": self.name,
            "quantity": self.quantity,
            "unit_price_minor": self.unit_price.minor,
            "subtotal_minor": self.subtotal.minor,
            "tax_bp": self.tax_bp,
            "tax_minor": self.tax.minor,
        }


@dataclass(frozen=True, slots=True)
class Quote:
    """An exact, reproducible basket total and its components.

    Guarantees, checked at construction and therefore true of every Quote that exists:

    * ``total == items_subtotal + items_tax + delivery_fee + delivery_tax``, to the paisa.
    * ``items_subtotal`` equals the sum of the line subtotals and ``items_tax`` the sum of
      the line taxes.
    * Every amount is in one currency.
    * ``free_delivery_applied`` agrees with ``delivery_fee`` being zero.

    Refuses to exist otherwise. That is the point: there is no way to construct a Quote
    whose headline total disagrees with the components shown beside it, so the number the
    buyer approves and the number that reaches the payment provider cannot diverge.
    """

    lines: tuple[QuoteLine, ...]
    items_subtotal: Money
    items_tax: Money
    delivery_fee: Money
    delivery_tax: Money
    total: Money
    free_delivery_applied: bool
    gap_to_free_delivery: Money
    currency: str
    freshness: Freshness

    def __post_init__(self) -> None:
        if not self.lines:
            raise InvalidBasketError("a quote must price at least one line")
        amounts = (
            self.items_subtotal,
            self.items_tax,
            self.delivery_fee,
            self.delivery_tax,
            self.total,
            self.gap_to_free_delivery,
        )
        if any(amount.currency != self.currency for amount in amounts):
            raise ValueError("every amount in a quote must share the quote currency")

        expected_subtotal = sum((line.subtotal for line in self.lines), Money.zero(self.currency))
        if self.items_subtotal != expected_subtotal:
            raise ValueError("items_subtotal does not equal the sum of the line subtotals")
        expected_tax = sum((line.tax for line in self.lines), Money.zero(self.currency))
        if self.items_tax != expected_tax:
            raise ValueError("items_tax does not equal the sum of the line taxes")

        recomputed = self.items_subtotal + self.items_tax + self.delivery_fee + self.delivery_tax
        if self.total != recomputed:
            raise ValueError(
                f"quote total {self.total} does not equal the sum of its components "
                f"{recomputed}; the fee engine is the only thing that may compute a total"
            )
        if self.free_delivery_applied != self.delivery_fee.is_zero:
            raise ValueError("free_delivery_applied disagrees with the delivery fee charged")
        if self.free_delivery_applied and not self.gap_to_free_delivery.is_zero:
            raise ValueError("free delivery already applies; the gap must be zero")

    def to_checkout_content(self) -> dict[str, Any]:
        """The immutable checkout content this quote would become.

        Integers and strings only, so :func:`commerce_domain.canonical_hash` can hash it
        under the integer-only JCS profile. ``lines`` uses the ``sku``/``quantity`` shape
        the kernel reads for reservation accounting.
        """
        return {
            "currency": self.currency,
            "lines": [line.to_content() for line in self.lines],
            "items_subtotal_minor": self.items_subtotal.minor,
            "items_tax_minor": self.items_tax.minor,
            "delivery_fee_minor": self.delivery_fee.minor,
            "delivery_tax_minor": self.delivery_tax.minor,
            "total_minor": self.total.minor,
            "free_delivery_applied": self.free_delivery_applied,
            "source": self.freshness.source,
            "catalogue_revision": self.freshness.catalogue_revision,
        }

    def content_hash(self) -> str:
        """Canonical content hash of this quote.

        Deliberately excludes ``observed_at``: two identical baskets priced a second apart
        against the same catalogue revision are the same checkout and must hash the same,
        or every re-read would look like a material change and demand a fresh approval.
        A change that matters -- a price, a quantity, a fee -- moves the revision and the
        amounts, and therefore moves the hash.
        """
        # str(): canonical_hash is Any to mypy until commerce-domain ships py.typed.
        return str(canonical_hash(self.to_checkout_content()))


@dataclass(frozen=True, slots=True)
class Unavailability:
    """Why one requested line could not be priced.

    ``listed`` and ``available_units`` are reported separately because they lead to
    different conversations: a delisted item needs a substitute, while a listed item at
    zero units may simply be restocked, and an item with some units may just need a
    smaller quantity.
    """

    sku: str
    requested: int
    available_units: int
    listed: bool


@dataclass(frozen=True, slots=True)
class QuoteResult:
    """A quote, or a structured refusal carrying a kernel recovery code.

    Guarantees: ``code is RecoveryCode.OK`` if and only if ``quote`` is present, and a
    refusal always names at least one unavailable line. Modelled on
    :class:`transaction_kernel.KernelDecision`: the decision travels in the code, never in
    prose, so an agent can translate it but cannot overrule it.
    """

    code: RecoveryCode
    quote: Quote | None = None
    unavailable: tuple[Unavailability, ...] = ()
    freshness: Freshness | None = None

    def __post_init__(self) -> None:
        if (self.code is RecoveryCode.OK) != (self.quote is not None):
            raise ValueError("a quote is present exactly when the code is OK")
        if self.code is not RecoveryCode.OK and not self.unavailable:
            raise ValueError("a refusal must name the lines it could not price")

    @property
    def ok(self) -> bool:
        return self.code is RecoveryCode.OK

    def require(self) -> Quote:
        """The quote, or raise. For call sites that have already checked ``ok``."""
        if self.quote is None:
            raise InvalidBasketError(f"no quote was produced: {self.code}")
        return self.quote


def quote_basket(
    lines: Sequence[BasketLine],
    *,
    store: MerchantStore,
    policy: FeePolicy | None = None,
) -> QuoteResult:
    """Price a basket exactly, against live merchant state.

    Guarantees:

    * The returned total is the exact integer minor-unit sum of every component, and the
      :class:`Quote` refuses to exist if it is not.
    * Prices, stock and availability are read live from ``store`` at one revision, and the
      quote carries that revision so a later read can prove it stale.
    * Delivery is free at or above the threshold, evaluated on the pre-tax item subtotal,
      to the paisa.
    * Each priced line carries that product's delivery promise, copied off the catalogue
      record unchanged. This engine resolves no dates and reads no clock; it repeats what
      the merchant declared so a surface can render it beside the line it belongs to.

    Refuses:

    * An empty basket, a non-positive quantity or a repeated SKU -- :class:`InvalidBasketError`.
      A repeated SKU is refused rather than summed because the kernel derives reserved
      units from the checkout lines and must see one authoritative quantity per SKU.
    * An unknown SKU -- :class:`~merchant_sim.errors.UnknownSkuError`. A product ID the
      merchant never issued is a defect, not a shopping outcome.
    * A delisted or insufficiently stocked SKU -- ``RecoveryCode.STALE_CHECKOUT`` with the
      offending lines named. This is the closest member of the kernel's closed recovery
      enum: the basket the buyer is holding no longer corresponds to merchant state and
      must be rebuilt and re-approved before anything else happens. See the module report
      note requesting a dedicated ``CATALOGUE_ITEM_UNAVAILABLE`` member.
    """
    if not lines:
        raise InvalidBasketError("cannot quote an empty basket")

    seen: set[str] = set()
    for line in lines:
        if line.sku in seen:
            raise InvalidBasketError(
                f"{line.sku} appears twice; merge it into one line so the reserved "
                "quantity per SKU is unambiguous"
            )
        seen.add(line.sku)

    fee_policy = policy if policy is not None else store.fee_policy
    # One freshness stamp for the whole quote. Taking a stamp per line would let a basket
    # straddle two revisions and produce a total that never existed at any single instant.
    freshness = store.freshness()
    currency = fee_policy.currency

    unavailable: list[Unavailability] = []
    quote_lines: list[QuoteLine] = []

    for line in lines:
        view = store.get_product(line.sku)  # raises UnknownSkuError for a fabricated ID
        inventory = store.check_inventory(line.sku)
        if not inventory.can_fulfil(line.quantity):
            unavailable.append(
                Unavailability(
                    sku=line.sku,
                    requested=line.quantity,
                    available_units=inventory.available_units,
                    listed=view.is_listed,
                )
            )
            continue
        if view.unit_price.currency != currency:
            raise ValueError(
                f"{line.sku} is priced in {view.unit_price.currency} but the fee policy "
                f"is {currency}; a mixed-currency basket has no single total"
            )
        subtotal = view.unit_price * line.quantity
        quote_lines.append(
            QuoteLine(
                sku=line.sku,
                name=view.product.name_en,
                unit_price=view.unit_price,
                quantity=line.quantity,
                subtotal=subtotal,
                tax_bp=view.product.tax_bp,
                tax=tax_on(subtotal, view.product.tax_bp),
                delivery_promise_days=view.product.delivery_promise_days,
            )
        )

    if unavailable:
        # Refuse the basket whole. Pricing the remainder would hand the buyer a total for
        # a basket they never asked for, and approving it would bind consent to it.
        return QuoteResult(
            code=RecoveryCode.STALE_CHECKOUT,
            unavailable=tuple(unavailable),
            freshness=freshness,
        )

    zero = Money.zero(currency)
    items_subtotal = sum((line.subtotal for line in quote_lines), zero)
    items_tax = sum((line.tax for line in quote_lines), zero)
    delivery_fee = fee_policy.delivery_fee_for(items_subtotal)
    delivery_tax = tax_on(delivery_fee, fee_policy.delivery_tax_bp)
    total = items_subtotal + items_tax + delivery_fee + delivery_tax

    quote = Quote(
        lines=tuple(quote_lines),
        items_subtotal=items_subtotal,
        items_tax=items_tax,
        delivery_fee=delivery_fee,
        delivery_tax=delivery_tax,
        total=total,
        free_delivery_applied=delivery_fee.is_zero,
        gap_to_free_delivery=fee_policy.gap_to_free_delivery(items_subtotal),
        currency=currency,
        freshness=freshness,
    )
    return QuoteResult(code=RecoveryCode.OK, quote=quote, freshness=freshness)

"""Live merchant state: price, stock, availability and the fee policy in force.

This is the authoritative source for everything volatile in the demo. The kernel does not
trust it -- the kernel re-derives and re-compares -- but the agent may only learn a price,
a stock level or a fee from here, never from its own memory of a previous turn.

TWO INVARIANTS
--------------
1. **Nothing changes merchant state except a labelled injection.** :meth:`MerchantStore.
   mutate` is the only mutator on this class and it accepts nothing but a
   :class:`~merchant_sim.injection.ScenarioInjection`, which cannot be constructed without
   the ``SCENARIO_INJECTION`` label. Specification 31.3 requires that a demo-injected
   failure is never confused with an organic one; making the label the entry condition of
   the only mutator is what makes that structural rather than aspirational.

2. **Every read carries provenance.** ``get_product``, ``check_inventory`` and search all
   return a :class:`~merchant_sim.grounding.Freshness` stamping the source and the
   catalogue revision the read was taken at. A caller that holds a result across a
   mutation can prove it is stale by comparing revisions -- which is precisely the
   version N -> N+1 story: quote at revision N, inject, and revision N+1 makes the earlier
   quote provably unusable.

The revision counter is the freshness token. It starts at zero and advances by exactly one
per applied injection, so the injection log is a replayable total order over the store.
"""

from __future__ import annotations

from dataclasses import dataclass

from commerce_domain import Money

from .catalogue import CATALOGUE, PRODUCTS_BY_SKU, Product
from .errors import ScenarioError, UnknownSkuError
from .grounding import SOURCE_ID, Clock, Freshness, system_clock
from .injection import InjectionKind, ScenarioInjection
from .policy import DEFAULT_FEE_POLICY, FeePolicy

__all__ = ["InventoryStatus", "MerchantStore", "ProductView"]


@dataclass(frozen=True, slots=True)
class ProductView:
    """A product as it exists right now: catalogue record plus live merchant state.

    Immutable and detached. Holding one across a mutation gives a stale view, which
    ``freshness.is_stale_against(store.revision)`` reports honestly.
    """

    product: Product
    unit_price: Money
    stock_units: int
    is_listed: bool
    is_available: bool
    freshness: Freshness

    def __post_init__(self) -> None:
        # Availability is listing AND stock. Kept as a checked derivation rather than a
        # free field so no caller can be handed a view claiming a delisted or empty SKU is
        # sellable.
        if self.is_available != (self.is_listed and self.stock_units > 0):
            raise ValueError(f"{self.product.sku}: availability contradicts listing and stock")

    @property
    def sku(self) -> str:
        return self.product.sku

    def display_name(self, *, devanagari: bool) -> str:
        """Buyer-facing name. Presentation only."""
        return self.product.display_name(devanagari=devanagari)


@dataclass(frozen=True, slots=True)
class InventoryStatus:
    """Whether a SKU can be sold right now, and how many units are behind that answer."""

    sku: str
    available_units: int
    is_available: bool
    freshness: Freshness

    def can_fulfil(self, quantity: int) -> bool:
        """True when ``quantity`` units can be sold from this position.

        Refuses a non-positive quantity: "can I have zero of these" is not a question the
        inventory service should answer with an encouraging True.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return self.is_available and self.available_units >= quantity


class MerchantStore:
    """In-memory merchant state for the Demo Grocery Store.

    Deterministic: constructing a store from the fixture always yields the same prices,
    stock and revision. No randomness, no I/O, no database.
    """

    __slots__ = (
        "_available",
        "_clock",
        "_fee_policy",
        "_injections",
        "_price",
        "_revision",
        "_stock",
    )

    def __init__(self, *, clock: Clock = system_clock, fee_policy: FeePolicy | None = None) -> None:
        """Seed live state from the immutable catalogue fixture.

        ``clock`` is injected so tests can freeze it. It stamps ``observed_at`` for display
        and audit only; see :mod:`merchant_sim.grounding` for why no decision may depend
        on it.
        """
        self._clock: Clock = clock
        self._price: dict[str, Money] = {}
        self._stock: dict[str, int] = {}
        self._available: dict[str, bool] = {}
        self._fee_policy: FeePolicy = fee_policy if fee_policy is not None else DEFAULT_FEE_POLICY
        self._revision: int = 0
        self._injections: list[ScenarioInjection] = []
        self._seed_baseline()

    # ---- baseline ---------------------------------------------------------

    def _seed_baseline(self) -> None:
        for product in CATALOGUE:
            self._price[product.sku] = product.list_price
            self._stock[product.sku] = product.baseline_stock
            # A product with no baseline stock is still listed; it is simply out of stock.
            # Availability is the merchant's decision to sell, kept separate from having
            # units, so "delisted" and "sold out" stay distinguishable in the demo.
            self._available[product.sku] = True

    # ---- provenance -------------------------------------------------------

    @property
    def revision(self) -> int:
        """Catalogue revision. Advances by one per applied injection."""
        return self._revision

    @property
    def fee_policy(self) -> FeePolicy:
        """The fee policy currently in force. Replaced only by an injection."""
        return self._fee_policy

    @property
    def injections(self) -> tuple[ScenarioInjection, ...]:
        """Append-only log of every change ever made to this store."""
        return tuple(self._injections)

    def freshness(self) -> Freshness:
        """Stamp for a read taken now."""
        return Freshness(
            source=SOURCE_ID, catalogue_revision=self._revision, observed_at=self._clock()
        )

    def is_stale(self, freshness: Freshness) -> bool:
        """True when merchant state has moved since ``freshness`` was taken."""
        return freshness.is_stale_against(self._revision)

    # ---- reads ------------------------------------------------------------

    def _require(self, sku: str) -> Product:
        try:
            return PRODUCTS_BY_SKU[sku]
        except KeyError:
            # Loud, not empty: specification 20.1 forbids acting on a product ID the
            # merchant never returned, and a hallucinated SKU must not look out of stock.
            raise UnknownSkuError(f"no such SKU in the demo catalogue: {sku!r}") from None

    def get_product(self, sku: str) -> ProductView:
        """Live view of one product. Raises :class:`UnknownSkuError` for an unknown SKU."""
        product = self._require(sku)
        return ProductView(
            product=product,
            unit_price=self._price[sku],
            stock_units=self._stock[sku],
            is_listed=self._available[sku],
            is_available=self._available[sku] and self._stock[sku] > 0,
            freshness=self.freshness(),
        )

    def is_listed(self, sku: str) -> bool:
        """The merchant's decision to sell this SKU, with stock excluded.

        Separate from :meth:`check_inventory` because "we stopped selling this" and "we
        ran out" lead to different buyer conversations and different substitution advice,
        and a zero-stock SKU would otherwise be indistinguishable from a delisted one.
        """
        self._require(sku)
        return self._available[sku]

    def check_inventory(self, sku: str) -> InventoryStatus:
        """Live sellable position for one SKU.

        ``is_available`` combines the merchant's listing decision with stock on hand: a
        delisted SKU is unavailable even with units behind it, and a listed SKU with zero
        units is unavailable too. Callers must not re-derive this from
        ``available_units > 0`` alone.
        """
        self._require(sku)
        units = self._stock[sku]
        return InventoryStatus(
            sku=sku,
            available_units=units,
            is_available=self._available[sku] and units > 0,
            freshness=self.freshness(),
        )

    def all_skus(self) -> tuple[str, ...]:
        """Every catalogue SKU, in fixture order. Stable across runs."""
        return tuple(product.sku for product in CATALOGUE)

    # ---- the only mutator -------------------------------------------------

    def mutate(self, injection: ScenarioInjection) -> None:
        """Apply one labelled scenario injection. The only way this store ever changes.

        Guarantees: on success the revision equals ``injection.revision_after`` and the
        injection is appended to the log. On any refusal nothing is written -- state and
        revision are validated before the first assignment, so a rejected injection cannot
        leave the store half-changed.

        Refuses: an injection built against a different revision (two controllers racing
        would otherwise silently interleave and the log would no longer replay), an unknown
        SKU, a delta whose ``before`` does not match current state (the demo operator was
        looking at a stale screen), a negative resulting stock, a non-positive price, a
        negative fee or threshold, a price injection that would re-denominate a SKU into
        another currency, and a kind with no apply rule.

        Not thread-safe. The revision check is a read-then-write with no lock, so the
        compare-and-set defends against a *stale* controller, not against two controllers
        mutating one store from two threads. Every caller in this package is sequential;
        keep it that way, or put a lock around this method.
        """
        if injection.revision_before != self._revision:
            raise ScenarioError(
                "injection was built against catalogue revision "
                f"{injection.revision_before}, store is at {self._revision}; "
                "rebuild it so the injection log stays a replayable total order"
            )

        match injection.kind:
            case InjectionKind.CATALOGUE_RESET:
                self._seed_baseline()
                self._fee_policy = DEFAULT_FEE_POLICY
            case InjectionKind.STOCK_SET | InjectionKind.STOCK_DECREMENT:
                sku = self._checked_sku(injection)
                after = self._checked_int(injection, current=self._stock[sku])
                if after < 0:
                    raise ScenarioError(f"{sku}: stock cannot go negative")
                self._stock[sku] = after
            case InjectionKind.PRICE_SET:
                sku = self._checked_sku(injection)
                after = self._checked_int(injection, current=self._price[sku].minor)
                if after <= 0:
                    raise ScenarioError(f"{sku}: a demo price must stay positive")
                held = self._price[sku].currency
                if injection.currency is not None and injection.currency != held:
                    # Re-denominating a SKU is not a demo lever. Allowing it would leave
                    # the store in a state the fee engine cannot price at all, since a
                    # basket spanning two currencies has no single total.
                    raise ScenarioError(
                        f"{sku} is priced in {held}; an injection may not re-denominate "
                        f"it to {injection.currency}"
                    )
                self._price[sku] = Money(after, held)
            case InjectionKind.AVAILABILITY_SET:
                sku = self._checked_sku(injection)
                delta = injection.delta
                if not isinstance(delta.before, bool) or not isinstance(delta.after, bool):
                    raise ScenarioError(f"{sku}: availability is a boolean flag")
                if delta.before != self._available[sku]:
                    raise ScenarioError(f"{sku}: availability changed under the injection")
                self._available[sku] = delta.after
            case InjectionKind.DELIVERY_FEE_SET:
                after = self._checked_int(
                    injection, current=self._fee_policy.base_delivery_fee.minor
                )
                if after < 0:
                    raise ScenarioError("a negative delivery fee is a discount, not a fee")
                self._fee_policy = FeePolicy(
                    base_delivery_fee=Money(after, self._fee_policy.currency),
                    free_delivery_threshold=self._fee_policy.free_delivery_threshold,
                    delivery_tax_bp=self._fee_policy.delivery_tax_bp,
                    currency=self._fee_policy.currency,
                )
            case InjectionKind.FREE_DELIVERY_THRESHOLD_SET:
                after = self._checked_int(
                    injection, current=self._fee_policy.free_delivery_threshold.minor
                )
                if after < 0:
                    raise ScenarioError("free-delivery threshold cannot be negative")
                self._fee_policy = FeePolicy(
                    base_delivery_fee=self._fee_policy.base_delivery_fee,
                    free_delivery_threshold=Money(after, self._fee_policy.currency),
                    delivery_tax_bp=self._fee_policy.delivery_tax_bp,
                    currency=self._fee_policy.currency,
                )
            case _:  # pragma: no cover - unreachable while InjectionKind is exhaustive
                # A new InjectionKind that nobody wired up here would otherwise fall
                # straight through to the revision bump and the log append, writing an
                # audit record for a change that never happened. An injection log that
                # claims a mutation the store did not make is worse than no log.
                raise ScenarioError(f"{injection.kind} has no apply rule in MerchantStore")

        self._revision = injection.revision_after
        self._injections.append(injection)

    def _checked_sku(self, injection: ScenarioInjection) -> str:
        # ScenarioInjection already guarantees a SKU is present for these kinds; this
        # checks it is a real one before any state is touched.
        sku = injection.sku
        if sku is None:  # pragma: no cover - guarded by ScenarioInjection.__post_init__
            raise ScenarioError(f"{injection.kind} must name a SKU")
        self._require(sku)
        return sku

    def _checked_int(self, injection: ScenarioInjection, *, current: int) -> int:
        delta = injection.delta
        if isinstance(delta.before, bool) or isinstance(delta.after, bool):
            raise ScenarioError(f"{delta.field}: expected integers, got a boolean flag")
        if delta.before != current:
            # Compare-and-set. The operator built this injection from a screen showing
            # `before`; if state has moved since, applying `after` blindly would overwrite
            # a change nobody meant to discard.
            raise ScenarioError(
                f"{delta.field}: expected current value {delta.before}, store holds {current}"
            )
        return delta.after

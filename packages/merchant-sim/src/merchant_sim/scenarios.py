"""The demo scenario controller.

Specification 31.3: private, demo-only controls that inject stock decrements, price and
fee changes and unavailability, every one of them labelled ``SCENARIO_INJECTION`` and never
mixed with organic production data.

This is the lever that drives the panel demo's version N -> N+1 story. The buyer approves a
checkout at revision N; the controller changes a price and takes an item out of stock; the
quote at revision N is now provably stale, the kernel refuses to create a payment against
it, and the buyer is asked to approve version N+1 with the exact delta shown.

WHY THE CONTROLLER OWNS NO STATE
--------------------------------
Every method here reads the store, builds a :class:`~merchant_sim.injection.
ScenarioInjection` describing the change, and hands it to
:meth:`~merchant_sim.store.MerchantStore.mutate`. The controller holds nothing of its own,
so there is no second copy of merchant state to drift out of step, and two controllers
pointed at one store cannot interleave silently -- the second one's injection is refused
because it was built against a revision the store has already left behind.

Determinism: the only non-deterministic values produced here are the injection's UUIDv7 and
its ``injected_at`` stamp, both of which are audit metadata. No decision anywhere in this
package reads either. Freeze the store's clock and every observable outcome -- stock,
price, availability, fees, quotes, search order -- is byte-for-byte reproducible.
"""

from __future__ import annotations

from commerce_domain import Money, uuid7

from .errors import ScenarioError
from .injection import InjectionKind, ScenarioInjection, StateDelta
from .store import MerchantStore

__all__ = ["ScenarioController"]


class ScenarioController:
    """Demo-only controls over one :class:`~merchant_sim.store.MerchantStore`.

    Never expose this on a buyer-facing or agent-facing surface. Every method mutates
    authoritative merchant state; the label exists so that the resulting failure is
    attributable, not so that the mutation is safe.
    """

    __slots__ = ("_store",)

    def __init__(self, store: MerchantStore) -> None:
        self._store = store

    # ---- internals --------------------------------------------------------

    def _inject(
        self,
        kind: InjectionKind,
        *,
        deltas: tuple[StateDelta, ...],
        note: str,
        sku: str | None = None,
        currency: str | None = None,
    ) -> ScenarioInjection:
        revision = self._store.revision
        injection = ScenarioInjection(
            injection_id=uuid7(),
            kind=kind,
            deltas=deltas,
            revision_before=revision,
            revision_after=revision + 1,
            injected_at=self._store.freshness().observed_at,
            note=note,
            sku=sku,
            currency=currency,
        )
        # mutate() validates against live state and raises without writing anything, so a
        # refused injection never reaches the log and never advances the revision.
        self._store.mutate(injection)
        return injection

    # ---- inventory --------------------------------------------------------

    def set_stock(self, sku: str, units: int, *, note: str = "") -> ScenarioInjection:
        """Set a SKU's stock to an exact number of units.

        Refuses a negative level: negative stock is not a demo scenario, it is a bug that
        would let the fee engine price a basket the merchant cannot fulfil.
        """
        if units < 0:
            raise ScenarioError(f"{sku}: stock cannot be set negative")
        current = self._store.check_inventory(sku).available_units
        return self._inject(
            InjectionKind.STOCK_SET,
            deltas=(StateDelta(field="stock_units", before=current, after=units),),
            note=note or f"stock set to {units}",
            sku=sku,
        )

    def decrement_stock(self, sku: str, units: int = 1, *, note: str = "") -> ScenarioInjection:
        """Remove units from a SKU, as a competing buyer would.

        Refuses to take more than is there. Clamping to zero would hide the interesting
        case -- the demo wants to show a basket losing a race for the last unit, not a
        store quietly inventing a floor.
        """
        if units <= 0:
            raise ScenarioError(f"{sku}: decrement must be positive")
        current = self._store.check_inventory(sku).available_units
        if units > current:
            raise ScenarioError(
                f"{sku}: cannot remove {units} units, only {current} on hand; "
                "use set_stock(0) to model a sell-out"
            )
        return self._inject(
            InjectionKind.STOCK_DECREMENT,
            deltas=(StateDelta(field="stock_units", before=current, after=current - units),),
            note=note or f"{units} unit(s) taken by a competing buyer",
            sku=sku,
        )

    def sell_out(self, sku: str, *, note: str = "") -> ScenarioInjection:
        """Take a SKU to zero units while leaving it listed."""
        return self.set_stock(sku, 0, note=note or "sold out during checkout")

    def set_availability(self, sku: str, available: bool, *, note: str = "") -> ScenarioInjection:
        """List or delist a SKU, independently of how many units are on hand.

        Delisting and selling out are separate levers on purpose: "the merchant stopped
        selling this" and "someone else bought the last one" are different stories to a
        buyer and produce different substitution advice.
        """
        # The listing flag, not ProductView.is_available: the latter folds in stock, so a
        # sold-out but still-listed SKU would look like it had already been delisted.
        listed = self._store.is_listed(sku)
        if listed == available:
            raise ScenarioError(
                f"{sku}: already {'listed' if available else 'delisted'}; "
                "an injection that changes nothing would still advance the revision"
            )
        return self._inject(
            InjectionKind.AVAILABILITY_SET,
            deltas=(StateDelta(field="available", before=listed, after=available),),
            note=note or ("relisted" if available else "delisted by the merchant"),
            sku=sku,
        )

    def make_unavailable(self, sku: str, *, note: str = "") -> ScenarioInjection:
        """Delist a SKU. The item stops being sellable even with units on the shelf."""
        return self.set_availability(sku, False, note=note or "item made unavailable")

    def make_available(self, sku: str, *, note: str = "") -> ScenarioInjection:
        """Relist a previously delisted SKU."""
        return self.set_availability(sku, True, note=note or "item relisted")

    # ---- pricing ----------------------------------------------------------

    def set_price(self, sku: str, price: Money, *, note: str = "") -> ScenarioInjection:
        """Change a SKU's unit price.

        This is the lever behind the demo's material-delta moment: a price moved after
        approval is exactly what the kernel must catch before it creates a payment.
        """
        view = self._store.get_product(sku)
        if price.currency != view.unit_price.currency:
            raise ScenarioError(
                f"{sku}: priced in {view.unit_price.currency}, cannot set {price.currency}"
            )
        if price.minor <= 0:
            raise ScenarioError(f"{sku}: a demo price must stay positive")
        if price == view.unit_price:
            raise ScenarioError(f"{sku}: price is already {price}; injection would be a no-op")
        return self._inject(
            InjectionKind.PRICE_SET,
            deltas=(
                StateDelta(
                    field="unit_price_minor",
                    before=view.unit_price.minor,
                    after=price.minor,
                ),
            ),
            note=note or f"price moved to {price}",
            sku=sku,
            currency=price.currency,
        )

    # ---- fees -------------------------------------------------------------

    def set_delivery_fee(self, fee: Money, *, note: str = "") -> ScenarioInjection:
        """Change the base delivery fee for the whole store."""
        policy = self._store.fee_policy
        if fee.currency != policy.currency:
            raise ScenarioError(f"fee policy is {policy.currency}, cannot set {fee.currency}")
        if fee.is_negative:
            raise ScenarioError("a negative delivery fee is a discount, not a fee")
        if fee == policy.base_delivery_fee:
            raise ScenarioError(f"delivery fee is already {fee}; injection would be a no-op")
        return self._inject(
            InjectionKind.DELIVERY_FEE_SET,
            deltas=(
                StateDelta(
                    field="base_delivery_fee_minor",
                    before=policy.base_delivery_fee.minor,
                    after=fee.minor,
                ),
            ),
            note=note or f"delivery fee moved to {fee}",
            currency=fee.currency,
        )

    def set_free_delivery_threshold(self, threshold: Money, *, note: str = "") -> ScenarioInjection:
        """Move the free-delivery threshold, changing who qualifies mid-basket."""
        policy = self._store.fee_policy
        if threshold.currency != policy.currency:
            raise ScenarioError(f"fee policy is {policy.currency}, cannot set {threshold.currency}")
        if threshold.is_negative:
            raise ScenarioError("free-delivery threshold cannot be negative")
        if threshold == policy.free_delivery_threshold:
            raise ScenarioError(f"threshold is already {threshold}; injection would be a no-op")
        return self._inject(
            InjectionKind.FREE_DELIVERY_THRESHOLD_SET,
            deltas=(
                StateDelta(
                    field="free_delivery_threshold_minor",
                    before=policy.free_delivery_threshold.minor,
                    after=threshold.minor,
                ),
            ),
            note=note or f"free-delivery threshold moved to {threshold}",
            currency=threshold.currency,
        )

    # ---- reset ------------------------------------------------------------

    def reset(self, *, note: str = "") -> ScenarioInjection:
        """Restore the fixture baseline: every price, stock level, listing and fee.

        Still an injection, and still logged. A reset is a change to merchant state like
        any other, and the revision it produces is what makes every quote taken before it
        provably stale -- which is the correct outcome, because the world they were priced
        against no longer exists.
        """
        return self._inject(
            InjectionKind.CATALOGUE_RESET,
            deltas=(),
            note=note or "catalogue restored to fixture baseline",
        )

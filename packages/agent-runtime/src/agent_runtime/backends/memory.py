"""In-process backend over the deterministic merchant simulator.

Two jobs. It is the backend the behavioural tests drive, and it is the demo fallback when
the HTTP API is unreachable: every read and every quote comes from
:mod:`merchant_sim`, so a search result or a total is the same on every machine.

WHAT THIS SIMULATES AND WHAT IT DOES NOT
----------------------------------------
The real admission transaction runs in PostgreSQL as the ``commerce_kernel`` role (ADR
0003 D1). This backend reproduces the *decisions* an agent must handle -- ``OK``,
``STALE_CHECKOUT``, ``REAPPROVAL_REQUIRED`` with exact deltas, ``AUTHORITY_INSUFFICIENT``,
``DUPLICATE_OPERATION`` -- so the agent layer can be tested against every recovery code
without a database. It issues no real Execution Grant and moves no money; a "grant id"
here is a placeholder so :class:`commerce_domain.AdmissionDecision`'s own invariant (an
allowed decision names its grant) holds.

The trusted surface (approve, reject, provider capture) is :class:`InMemoryTrustedSurface`,
a separate object that is never handed to an agent. ``InMemoryBackend`` itself has no
approve method, so there is nothing for a capability gate to miss.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import uuid4

from commerce_domain import (
    AdmissionDecision,
    CheckoutRef,
    Delta,
    Money,
    RecoveryCode,
    uuid7,
    uuid7_str,
)
from merchant_sim import (
    BasketLine,
    Clock,
    InvalidBasketError,
    Locale,
    MerchantStore,
    ProductView,
    Quote,
    QuoteResult,
    UnknownSkuError,
    quote_basket,
    system_clock,
)
from merchant_sim import search as catalogue_search

from .base import (
    ApprovalCard,
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
    PricedLine,
    ProductCard,
    Provenance,
    SearchPage,
    SupportBackend,
    SupportCase,
    UnavailableLine,
    backend_problem,
)

__all__ = ["InMemoryBackend", "InMemoryTrustedSurface", "compute_deltas"]

#: Reason keys on a Delta. Closed set, rendered per language; unknown keys pass through.
REASON_PRICE_CHANGED: Final[str] = "PRICE_CHANGED"
REASON_QUANTITY_REDUCED: Final[str] = "QUANTITY_REDUCED"
REASON_ITEM_UNAVAILABLE: Final[str] = "ITEM_UNAVAILABLE"
REASON_ITEM_DELISTED: Final[str] = "ITEM_DELISTED"
REASON_TAX_CHANGED: Final[str] = "TAX_CHANGED"
REASON_SUBTOTAL_CHANGED: Final[str] = "SUBTOTAL_CHANGED"
REASON_DELIVERY_FEE_CHANGED: Final[str] = "DELIVERY_FEE_CHANGED"
REASON_DELIVERY_TAX_CHANGED: Final[str] = "DELIVERY_TAX_CHANGED"
REASON_TOTAL_CHANGED: Final[str] = "TOTAL_CHANGED"
REASON_FREE_DELIVERY_CHANGED: Final[str] = "FREE_DELIVERY_CHANGED"
REASON_NOT_QUOTABLE: Final[str] = "NOT_QUOTABLE"


#: Below this many units a listed product is reported as running low. A threshold rather
#: than a percentage, because a merchant restocks in units and "twenty percent left" of a
#: product that ships in threes is not a sentence anybody acts on.
LOW_STOCK_UNITS: Final[int] = 3


@dataclass(slots=True)
class _Basket:
    lines: dict[str, int]
    quoted_revision: int | None = None


@dataclass(frozen=True, slots=True)
class _Version:
    number: int
    quote: CartQuote
    status: CheckoutStatus


@dataclass(slots=True)
class _Checkout:
    checkout_id: str
    cart_id: str
    versions: list[_Version]
    payment: PaymentSummary | None = None
    admitted: AdmissionDecision | None = None
    order_id: str | None = None

    @property
    def current(self) -> _Version:
        return self.versions[-1]


def _provenance(store: MerchantStore) -> Provenance:
    stamp = store.freshness()
    return Provenance(
        source=stamp.source,
        catalogue_revision=stamp.catalogue_revision,
        observed_at=stamp.observed_at,
    )


def _quote_card(quote: Quote) -> CartQuote:
    return CartQuote(
        lines=tuple(
            PricedLine(
                sku=line.sku,
                name=line.name,
                quantity=line.quantity,
                unit_price=line.unit_price,
                subtotal=line.subtotal,
                tax_bp=line.tax_bp,
                tax=line.tax,
            )
            for line in quote.lines
        ),
        items_subtotal=quote.items_subtotal,
        items_tax=quote.items_tax,
        delivery_fee=quote.delivery_fee,
        delivery_tax=quote.delivery_tax,
        total=quote.total,
        free_delivery_applied=quote.free_delivery_applied,
        gap_to_free_delivery=quote.gap_to_free_delivery,
        currency=quote.currency,
        discount=quote.discount_amount,
        offer_label=quote.offer_label,
        content_hash=quote.content_hash(),
        provenance=Provenance(
            source=quote.freshness.source,
            catalogue_revision=quote.freshness.catalogue_revision,
            observed_at=quote.freshness.observed_at,
        ),
        free_delivery_threshold=None,
    )


def compute_deltas(
    approved: CartQuote,
    current: CartQuote | None,
    unavailable: Sequence[UnavailableLine] = (),
) -> tuple[Delta, ...]:
    """Every material difference between an approved quote and the merchant's state now.

    Field paths follow the checkout content shape (``lines[SKU].unit_price_minor``,
    ``total_minor``) so the same path names what the kernel hashed. Money is reported in
    minor units, as integers, because that is what the approval hash was computed over.
    """
    deltas: list[Delta] = []
    now_by_sku = {line.sku: line for line in current.lines} if current is not None else {}
    unavailable_by_sku = {line.sku: line for line in unavailable}

    for line in approved.lines:
        now = now_by_sku.get(line.sku)
        if now is None:
            gone = unavailable_by_sku.get(line.sku)
            reason = (
                REASON_ITEM_DELISTED
                if gone is not None and not gone.listed
                else REASON_ITEM_UNAVAILABLE
            )
            units = gone.available_units if gone is not None else 0
            deltas.append(Delta(f"lines[{line.sku}].quantity", line.quantity, units, reason))
            continue
        if now.unit_price != line.unit_price:
            deltas.append(
                Delta(
                    f"lines[{line.sku}].unit_price_minor",
                    line.unit_price.minor,
                    now.unit_price.minor,
                    REASON_PRICE_CHANGED,
                )
            )
        if now.quantity != line.quantity:
            deltas.append(
                Delta(
                    f"lines[{line.sku}].quantity",
                    line.quantity,
                    now.quantity,
                    REASON_QUANTITY_REDUCED,
                )
            )
        if now.tax != line.tax:
            deltas.append(
                Delta(
                    f"lines[{line.sku}].tax_minor",
                    line.tax.minor,
                    now.tax.minor,
                    REASON_TAX_CHANGED,
                )
            )

    if current is None:
        deltas.append(Delta("total_minor", approved.total.minor, None, REASON_NOT_QUOTABLE))
        return tuple(deltas)

    aggregate: tuple[tuple[str, Money, Money, str], ...] = (
        (
            "items_subtotal_minor",
            approved.items_subtotal,
            current.items_subtotal,
            REASON_SUBTOTAL_CHANGED,
        ),
        ("items_tax_minor", approved.items_tax, current.items_tax, REASON_TAX_CHANGED),
        (
            "delivery_fee_minor",
            approved.delivery_fee,
            current.delivery_fee,
            REASON_DELIVERY_FEE_CHANGED,
        ),
        (
            "delivery_tax_minor",
            approved.delivery_tax,
            current.delivery_tax,
            REASON_DELIVERY_TAX_CHANGED,
        ),
        ("total_minor", approved.total, current.total, REASON_TOTAL_CHANGED),
    )
    for path, before, after, reason in aggregate:
        if before != after:
            deltas.append(Delta(path, before.minor, after.minor, reason))
    if approved.free_delivery_applied != current.free_delivery_applied:
        deltas.append(
            Delta(
                "free_delivery_applied",
                approved.free_delivery_applied,
                current.free_delivery_applied,
                REASON_FREE_DELIVERY_CHANGED,
            )
        )
    return tuple(deltas)


class InMemoryBackend(CommerceBackend, SupportBackend):
    """Deterministic backend over one :class:`merchant_sim.MerchantStore`.

    Implements the merchant, review-queue and support surfaces as well as the buyer one,
    because the simulator holds every side of the shop in this process. A production
    backend would not: those reads answer to different principals, which is why each lives
    on its own protocol rather than on :class:`CommerceBackend`.

    ``descriptions`` is an optional overlay of merchant copy per SKU. The simulator's
    catalogue has names only; a description overlay is how a test (or a demo) plants
    merchant-authored text -- including hostile text -- without editing the fixture.

    ``cases`` is the review queue, and it is empty by default because that is the true
    answer here rather than a convenient one. A case is opened by the Reconciliation
    Service against a payment provider, and this backend has neither: it moves no money
    and reaches no provider, so it has escalated nothing. An empty queue is therefore a
    measurement -- this backend looked at every case it holds and found none -- and not a
    fixture that has yet to be filled in. A test or a demo supplies records to read.

    ``policies`` and ``resolutions`` are the support surface, keyed by order id, and empty
    by default for the same reason and with the same force. A Policy-at-Sale Receipt is
    issued by the kernel when a buyer approves, and a finding is raised against a payment
    provider; this backend issues no receipt and reaches no provider. Synthesising terms
    from the simulator's *current* catalogue would be the easy mistake here and the worst
    one available: the whole purpose of a receipt is that a sale is governed by the
    document frozen when it was made.
    """

    def __init__(
        self,
        store: MerchantStore | None = None,
        *,
        descriptions: Mapping[str, str] | None = None,
        policies: Sequence[PolicyAtSale] = (),
        resolutions: Sequence[OrderResolution] = (),
        clock: Clock = system_clock,
    ) -> None:
        self._store = store if store is not None else MerchantStore(clock=clock)
        self._descriptions: dict[str, str] = dict(descriptions or {})
        self._policies: dict[str, PolicyAtSale] = {item.order_id: item for item in policies}
        #: One live case per order, so an agent asked twice returns the first.
        self._cases: dict[str, SupportCase] = {}
        self._resolutions: dict[str, OrderResolution] = {
            item.order_id: item for item in resolutions
        }
        self._baskets: dict[str, _Basket] = {}
        self._checkouts: dict[str, _Checkout] = {}
        self._orders: dict[str, str] = {}
        self.submit_calls: int = 0

    @property
    def store(self) -> MerchantStore:
        """The merchant state. Hand it to a ``ScenarioController``; never to an agent."""
        return self._store

    # ---- support surface --------------------------------------------------
    #
    # Both empty by default, for the reason the case queue is. A Policy-at-Sale Receipt is
    # issued by the kernel when a buyer approves, and a finding is raised by the
    # Reconciliation Service against a payment provider. This backend has neither: it
    # issues no receipt and reaches no provider. So "this backend holds no at-sale terms
    # for that order" is a measurement rather than a fixture nobody filled in, and it is
    # reported as the refusal a missing order gets rather than as an empty policy set --
    # which an agent would read as "no rules apply", and answer a buyer accordingly.

    async def open_support_case(
        self,
        order_id: str,
        reason: str,
        note: str,  # noqa: ARG002 - the buyer's words are for a person, and this double has none
    ) -> SupportCase:
        """Open a case against an order this backend actually holds terms for.

        Kept in memory beside the policies, and keyed by order so a second call returns the
        first case rather than a second one -- the same answer the real platform gives,
        because an agent asked twice must not fill a queue with one complaint.

        Refuses an order it does not know, for the reason ``order_policy`` does: a distinct
        answer for an unknown order is an existence oracle over identifiers.
        """
        if order_id not in self._policies:
            raise backend_problem(
                "unknown-order-support",
                status=404,
                title="No such order",
                detail="This backend holds nothing for that order.",
            )
        existing = self._cases.get(order_id)
        if existing is not None:
            return existing
        case = SupportCase(
            case_id=f"case_{uuid4().hex[:16]}",
            order_id=order_id,
            reason=reason,
            status="OPEN",
        )
        self._cases[order_id] = case
        return case

    async def order_policy(self, order_id: str) -> PolicyAtSale:
        """The at-sale terms this backend was given for an order. Unknown is a 404 problem.

        Never synthesised from the simulator's current catalogue, which is the one thing
        that would be easy to do here and wrong everywhere: the simulator's prices and
        rules are today's, and the whole purpose of a receipt is that a sale is governed by
        the document frozen when it was made.
        """
        policy = self._policies.get(order_id)
        if policy is None:
            raise backend_problem(
                "unknown-order-policy",
                status=404,
                title="No at-sale policy",
                detail="This backend holds no Policy-at-Sale Receipt for that order.",
                order_id=order_id,
            )
        return policy

    async def order_resolution(self, order_id: str) -> OrderResolution:
        """The findings and plans this backend was given for an order.

        An order this backend knows about with nothing supplied for it resolves to zero
        findings -- which is the true answer, since nothing here can diverge -- while an
        order it has never seen is the same 404 ``order_track`` gives. Collapsing those two
        would let an agent report "nothing is wrong" about a sale that does not exist.
        """
        resolution = self._resolutions.get(order_id)
        if resolution is not None:
            return resolution
        checkout_id = self._orders.get(order_id)
        if checkout_id is None:
            raise backend_problem(
                "unknown-order", status=404, title="Unknown order", order_id=order_id
            )
        # The state is read back off the attempt rather than written as a constant here,
        # so this answer and ``order_track``'s cannot drift apart about one order.
        view = self._order_view(self._require_checkout(checkout_id), order_id)
        # No TTL: this backend issued no plan, so it has no window to report. Naming one
        # anyway would be quoting the platform's declared figure from a process that never
        # asked the platform anything.
        return OrderResolution(order_id=order_id, recorded_state=view.payment.state, findings=0)

    # ---- catalogue --------------------------------------------------------

    def _card(self, view: ProductView, locale: Locale) -> ProductCard:
        return ProductCard(
            sku=view.sku,
            name=view.display_name(devanagari=locale.uses_devanagari),
            description=self._descriptions.get(view.sku, ""),
            category=view.product.category.value,
            unit_label=view.product.unit_label,
            unit_price=view.unit_price,
            stock_units=view.stock_units,
            is_listed=view.is_listed,
            is_available=view.is_available,
            provenance=Provenance(
                source=view.freshness.source,
                catalogue_revision=view.freshness.catalogue_revision,
                observed_at=view.freshness.observed_at,
            ),
        )

    async def search(self, query: str, locale: Locale, limit: int) -> SearchPage:
        if limit <= 0:
            raise backend_problem("invalid-limit", status=422, title="limit must be positive")
        results = catalogue_search(query, locale, store=self._store, limit=limit)
        return SearchPage(
            query=query,
            locale=locale,
            hits=tuple(self._card(hit.view, locale) for hit in results.hits),
            provenance=Provenance(
                source=results.freshness.source,
                catalogue_revision=results.freshness.catalogue_revision,
                observed_at=results.freshness.observed_at,
            ),
        )

    def _view_or_problem(self, sku: str) -> ProductView:
        try:
            return self._store.get_product(sku)
        except UnknownSkuError as exc:
            # Loud and structured: a SKU the merchant never issued must not look out of
            # stock (specification 20.4).
            raise backend_problem(
                "unknown-sku",
                status=404,
                title="Unknown catalogue item",
                detail=str(exc),
                sku=sku,
                code="UNKNOWN_CATALOGUE_ITEM",
            ) from None

    async def product(self, sku: str) -> ProductCard:
        return self._card(self._view_or_problem(sku), Locale.EN)

    # ---- cart -----------------------------------------------------------

    def _require_basket(self, cart_id: str) -> _Basket:
        cart = self._baskets.get(cart_id)
        if cart is None:
            raise backend_problem("unknown-cart", status=404, title="Unknown cart", cart_id=cart_id)
        return cart

    def _basket_view(self, cart_id: str) -> CartView:
        cart = self._require_basket(cart_id)
        provenance = _provenance(self._store)
        lines = tuple(cart.lines.items())
        stale = cart.quoted_revision is not None and cart.quoted_revision != self._store.revision
        if not lines:
            return CartView(cart_id, RecoveryCode.OK, (), None, (), stale, provenance)
        result = self._quote(lines)
        cart.quoted_revision = self._store.revision
        if result.ok:
            return CartView(
                cart_id,
                RecoveryCode.OK,
                lines,
                _quote_card(result.require()),
                (),
                stale,
                provenance,
            )
        return CartView(
            cart_id,
            result.code,
            lines,
            None,
            tuple(
                UnavailableLine(u.sku, u.requested, u.available_units, u.listed)
                for u in result.unavailable
            ),
            stale,
            provenance,
        )

    def _quote(self, lines: Sequence[tuple[str, int]]) -> QuoteResult:
        try:
            return quote_basket(
                [BasketLine(sku, quantity) for sku, quantity in lines], store=self._store
            )
        except InvalidBasketError as exc:
            raise backend_problem(
                "invalid-cart", status=422, title="Cart cannot be priced", detail=str(exc)
            ) from None

    async def basket_create(self) -> CartView:
        cart_id = uuid7_str()
        self._baskets[cart_id] = _Basket(lines={})
        return self._basket_view(cart_id)

    async def basket_set_line(self, cart_id: str, sku: str, quantity: int) -> CartView:
        cart = self._require_basket(cart_id)
        if isinstance(quantity, bool) or quantity < 0:
            raise backend_problem(
                "invalid-quantity", status=422, title="Quantity must be zero or positive"
            )
        self._view_or_problem(sku)
        if quantity == 0:
            cart.lines.pop(sku, None)
        else:
            cart.lines[sku] = quantity
        return self._basket_view(cart_id)

    async def basket_get(self, cart_id: str) -> CartView:
        return self._basket_view(cart_id)

    # ---- checkout ---------------------------------------------------------

    def _require_checkout(self, checkout_id: str) -> _Checkout:
        checkout = self._checkouts.get(checkout_id)
        if checkout is None:
            raise backend_problem(
                "unknown-checkout", status=404, title="Unknown checkout", checkout_id=checkout_id
            )
        return checkout

    @staticmethod
    def _card_for(checkout: _Checkout, version: _Version) -> ApprovalCard:
        return ApprovalCard(
            checkout_id=checkout.checkout_id,
            version=version.number,
            content_hash=version.quote.content_hash,
            status=version.status,
            quote=version.quote,
        )

    def _checkout_view(self, checkout: _Checkout) -> CheckoutView:
        return CheckoutView(
            checkout_id=checkout.checkout_id,
            current_version=checkout.current.number,
            versions=tuple(self._card_for(checkout, v) for v in checkout.versions),
            payment=checkout.payment,
        )

    async def checkout_create(self, cart_id: str) -> ApprovalCard:
        view = self._basket_view(cart_id)
        if view.quote is None:
            raise backend_problem(
                "cart-not-quotable",
                status=409,
                title="Cart cannot be checked out",
                detail="the cart is empty or names items the merchant cannot fulfil",
                code=view.code.value,
                unavailable=[u.sku for u in view.unavailable],
            )
        checkout_id = str(uuid7())
        checkout = _Checkout(
            checkout_id=checkout_id,
            cart_id=cart_id,
            versions=[_Version(1, view.quote, CheckoutStatus.PENDING_APPROVAL)],
        )
        self._checkouts[checkout_id] = checkout
        return self._card_for(checkout, checkout.current)

    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        return self._checkout_view(self._require_checkout(checkout_id))

    @staticmethod
    def _ref(checkout: _Checkout, version: _Version) -> CheckoutRef:
        return CheckoutRef(
            checkout_id=uuid.UUID(checkout.checkout_id),
            version=version.number,
            content_hash=version.quote.content_hash,
        )

    @staticmethod
    def _denied(
        code: RecoveryCode,
        explanation: str,
        checkout: CheckoutRef,
        *,
        deltas: tuple[Delta, ...] = (),
        next_version: int | None = None,
        payment_attempt_id: uuid.UUID | None = None,
    ) -> AdmissionDecision:
        return AdmissionDecision(
            decision_id=uuid7(),
            allowed=False,
            code=code,
            explanation=explanation,
            checkout=checkout,
            deltas=deltas,
            next_version=next_version,
            payment_attempt_id=payment_attempt_id,
        )

    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> AdmissionDecision:
        """Simulated admission. Reproduces the kernel's decision shapes, not its authority.

        Order of checks mirrors the real admission transaction: identity of the version,
        then approval, then revalidation against live merchant state. Revalidation runs
        even though the version was approved, because that is the whole point -- an
        approval binds consent to a hash, and the hash is only good while the merchant's
        state still produces it.
        """
        self.submit_calls += 1
        checkout = self._require_checkout(checkout_id)
        current = checkout.current
        ref = self._ref(checkout, current)

        if checkout.admitted is not None:
            # ADR 0003 D9: a second submit sees the one live attempt, never a second one.
            return self._denied(
                RecoveryCode.DUPLICATE_OPERATION,
                "already_admitted",
                ref,
                payment_attempt_id=checkout.admitted.payment_attempt_id,
            )
        if version != current.number:
            return self._denied(
                RecoveryCode.STALE_CHECKOUT,
                "version_superseded",
                ref,
                next_version=current.number
                if current.status is CheckoutStatus.PENDING_APPROVAL
                else None,
            )
        if content_hash != current.quote.content_hash:
            return self._denied(RecoveryCode.STALE_CHECKOUT, "content_hash_mismatch", ref)
        if current.status is CheckoutStatus.PENDING_APPROVAL:
            return self._denied(RecoveryCode.AUTHORITY_INSUFFICIENT, "approval_missing", ref)
        if current.status is not CheckoutStatus.APPROVED:
            return self._denied(RecoveryCode.STALE_CHECKOUT, "version_not_approvable", ref)

        lines = tuple((line.sku, line.quantity) for line in current.quote.lines)
        result = self._quote(lines)
        if result.ok:
            fresh = _quote_card(result.require())
            if fresh.content_hash == current.quote.content_hash:
                return self._admit(checkout, current, ref)
            deltas = compute_deltas(current.quote, fresh)
            return self._supersede(checkout, current, ref, fresh, deltas)

        unavailable = tuple(
            UnavailableLine(u.sku, u.requested, u.available_units, u.listed)
            for u in result.unavailable
        )
        # Build N+1 from what the merchant can still fulfil. The buyer approves the reduced
        # cart, or walks away; version N is never revived (specification 6.3).
        short = {u.sku: u for u in unavailable}
        reduced: list[tuple[str, int]] = []
        for sku, quantity in lines:
            gone = short.get(sku)
            if gone is None:
                reduced.append((sku, quantity))
            elif gone.listed and gone.available_units > 0:
                reduced.append((sku, min(quantity, gone.available_units)))
        if not reduced:
            deltas = compute_deltas(current.quote, None, unavailable)
            self._invalidate(checkout, current)
            return self._denied(
                RecoveryCode.STALE_CHECKOUT, "basket_unfulfillable", ref, deltas=deltas
            )
        reduced_result = self._quote(reduced)
        if not reduced_result.ok:  # pragma: no cover - reduced to available units above
            deltas = compute_deltas(current.quote, None, unavailable)
            self._invalidate(checkout, current)
            return self._denied(
                RecoveryCode.STALE_CHECKOUT, "basket_unfulfillable", ref, deltas=deltas
            )
        fresh = _quote_card(reduced_result.require())
        deltas = compute_deltas(current.quote, fresh, unavailable)
        return self._supersede(checkout, current, ref, fresh, deltas)

    @staticmethod
    def _invalidate(checkout: _Checkout, version: _Version) -> None:
        checkout.versions[-1] = replace(version, status=CheckoutStatus.INVALIDATED)

    def _supersede(
        self,
        checkout: _Checkout,
        version: _Version,
        ref: CheckoutRef,
        fresh: CartQuote,
        deltas: tuple[Delta, ...],
    ) -> AdmissionDecision:
        self._invalidate(checkout, version)
        successor = _Version(version.number + 1, fresh, CheckoutStatus.PENDING_APPROVAL)
        checkout.versions.append(successor)
        return self._denied(
            RecoveryCode.REAPPROVAL_REQUIRED,
            "material_change",
            ref,
            deltas=deltas,
            next_version=successor.number,
        )

    def _admit(self, checkout: _Checkout, version: _Version, ref: CheckoutRef) -> AdmissionDecision:
        attempt_id = uuid7()
        checkout.versions[-1] = replace(version, status=CheckoutStatus.ADMITTED)
        checkout.payment = PaymentSummary(attempt_id=str(attempt_id), state="CREATED")
        decision = AdmissionDecision(
            decision_id=uuid7(),
            allowed=True,
            code=RecoveryCode.OK,
            explanation="admitted",
            checkout=ref,
            grant_id=uuid7(),
            payment_attempt_id=attempt_id,
        )
        checkout.admitted = decision
        return decision

    # ---- orders -----------------------------------------------------------

    def _order_view(self, checkout: _Checkout, order_id: str) -> OrderView:
        """Build the order from the admitted version and the verified attempt.

        The version is read back rather than remembered separately: an order that
        disagreed with the checkout it was made from about the amount would be exactly the
        fault the content hash exists to make impossible.
        """
        admitted = next(
            (v for v in checkout.versions if v.status is CheckoutStatus.ADMITTED),
            checkout.current,
        )
        payment = checkout.payment
        if payment is None:  # pragma: no cover - an order is only written after capture
            raise backend_problem("no-attempt", status=409, title="Order has no payment attempt")
        return OrderView(
            order_id=order_id,
            checkout_id=checkout.checkout_id,
            version=admitted.number,
            content_hash=admitted.quote.content_hash,
            state=OrderState.CONFIRMED,
            amount=admitted.quote.total,
            payment=payment,
            quote=admitted.quote,
        )

    async def order_track(self, order_id: str) -> OrderView:
        checkout_id = self._orders.get(order_id)
        if checkout_id is None:
            raise backend_problem(
                "unknown-order", status=404, title="Unknown order", order_id=order_id
            )
        return self._order_view(self._require_checkout(checkout_id), order_id)


class InMemoryTrustedSurface:
    """Registry B for the demo harness. Never part of :class:`CommerceBackend`.

    Holds the backend privately; an agent holds a backend, never this. ``approve`` echoes
    the hash and amount the buyer saw, as the real endpoint requires (ADR 0003), so a test
    cannot approve a version whose facts it never looked at.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: InMemoryBackend) -> None:
        self._backend = backend

    def _current(self, checkout_id: str, version: int) -> tuple[_Checkout, _Version]:
        checkout = self._backend._require_checkout(checkout_id)  # noqa: SLF001 - same module
        current = checkout.current
        if current.number != version:
            raise backend_problem(
                "version-not-current",
                status=409,
                title="Only the current version can be decided",
                current_version=current.number,
            )
        if current.status is not CheckoutStatus.PENDING_APPROVAL:
            raise backend_problem(
                "version-already-decided", status=409, title=f"Version is {current.status}"
            )
        return checkout, current

    def approve(
        self, checkout_id: str, version: int, *, content_hash: str, total_minor: int
    ) -> ApprovalCard:
        checkout, current = self._current(checkout_id, version)
        if content_hash != current.quote.content_hash or total_minor != current.quote.total.minor:
            raise backend_problem(
                "approval-echo-mismatch",
                status=422,
                title="Approval must echo the hash and amount shown",
            )
        checkout.versions[-1] = replace(current, status=CheckoutStatus.APPROVED)
        return InMemoryBackend._card_for(checkout, checkout.current)  # noqa: SLF001

    def reject(self, checkout_id: str, version: int) -> ApprovalCard:
        checkout, current = self._current(checkout_id, version)
        checkout.versions[-1] = replace(current, status=CheckoutStatus.REJECTED)
        return InMemoryBackend._card_for(checkout, checkout.current)  # noqa: SLF001

    def record_provider_capture(self, checkout_id: str) -> PaymentSummary:
        """Stand in for verified provider evidence (webhook or reconciliation).

        Exists so a test can show that a success sentence is permitted only once the
        ledger has seen ``CAPTURED`` from a structured read, never from admission alone.
        """
        checkout = self._backend._require_checkout(checkout_id)  # noqa: SLF001
        if checkout.payment is None:
            raise backend_problem("no-attempt", status=409, title="Nothing was admitted")
        checkout.payment = PaymentSummary(
            attempt_id=checkout.payment.attempt_id,
            state="CAPTURED",
            capture_evidence="PROVIDER_FETCH",
        )
        if checkout.order_id is None:
            # An order is written only from verified capture evidence (ADR 0003 D8), which
            # is why it is created here and nowhere on the admission path.
            order_id = uuid7_str()
            checkout.order_id = order_id
            self._backend._orders[order_id] = checkout.checkout_id  # noqa: SLF001
        return checkout.payment

    def order_id_for(self, checkout_id: str) -> str | None:
        """The order this checkout produced, once capture evidence created one.

        A harness convenience, not an agent operation: in the real system the buyer gets
        the order identifier from the trusted surface, and the agent is told it.
        """
        return self._backend._require_checkout(checkout_id).order_id  # noqa: SLF001

"""The simulator as the kernel sees it: a ``MerchantStateSource`` and a content producer.

Three bridges live here, and the direction of authority is the same for all of them: the
kernel owns the contract, the simulator satisfies it.

* :func:`content_from_quote` turns a :class:`~merchant_sim.fees.Quote` into canonical
  checkout content through :func:`transaction_kernel.checkout_content.build_checkout_content`
  (ADR 0003 D6). The simulator never defines the hashed shape; if the kernel's builder
  refuses the numbers, the quote is wrong, not the contract.
* :class:`SimMerchantStateSource` answers admission step 8: given the version the buyer
  approved, re-quote those exact lines against the store *now* and report the total, the
  availability and the canonical content the kernel should compare against the approval.
* :func:`receipt_inputs_for` freezes the Demo Grocery Store's rules into the
  :class:`~transaction_kernel.checkouts.ReceiptInputs` a Policy-at-Sale Receipt needs,
  covering every :class:`~transaction_kernel.receipts.PolicyKind` explicitly -- a kind
  the store has no programme for is recorded as ``allowed: false``, never omitted.

Why revalidation re-quotes instead of re-hashing
------------------------------------------------
The fee engine is the only thing allowed to compute a total. So the state source does not
re-derive prices and add them up; it hands the approved SKUs and quantities back to
:func:`~merchant_sim.fees.quote_basket` and reports what comes out. An unchanged store
reproduces the approved total and content byte for byte, which is what lets the kernel
prove that nothing moved. A changed price, fee or threshold changes the total and the
hash; a stock decrement below the approved quantity makes ``all_available`` false, and
the reported content is the cart *without* the unavailable lines -- the version N+1 a
buyer can actually approve -- because the fee engine refuses to price a cart it cannot
fulfil, and asking the buyer to approve one would bind consent to a fiction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from commerce_domain import Money, canonical_hash
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel import RecoveryCode
from transaction_kernel.admission import CurrentMerchantState
from transaction_kernel.checkout_content import ContentLine, build_checkout_content, lines_of
from transaction_kernel.checkouts import ReceiptInputs
from transaction_kernel.receipts import BuyerVisibleRef, PolicyKind, SaleTerm

from .errors import MerchantSimError
from .fees import BasketLine, Quote, quote_basket
from .grounding import SOURCE_ID
from .policy import FeePolicy, Promotion
from .store import MerchantStore

__all__ = [
    "DEFAULT_POLICY_PREFIX",
    "ROUNDING_POLICY_VERSION",
    "TAX_POLICY_VERSION",
    "RevalidationError",
    "SimMerchantStateSource",
    "content_from_quote",
    "receipt_inputs_for",
]

#: The demo store's policy document ids are ``<prefix>/<kind>``.
DEFAULT_POLICY_PREFIX: Final[str] = "demo-grocery"

#: Versions of the two arithmetic rules the fee engine applies. Named, so a receipt says
#: which rounding produced the paisa the buyer agreed to (see :mod:`merchant_sim.fees`).
TAX_POLICY_VERSION: Final[str] = "demo-grocery-gst/1"
ROUNDING_POLICY_VERSION: Final[str] = "half-up-per-line/1"

# Row-level security scopes this to the tenant bound to the caller's transaction, which
# is the admission transaction. There is no tenant argument to spoof.
_SELECT_CONTENT = text(
    "SELECT content FROM checkout_versions WHERE checkout_id = :c AND version = :v"
)


class RevalidationError(MerchantSimError):
    """The approved version could not be read back well enough to re-quote it.

    Raised, never returned: an unreadable version must abort admission, because reporting
    it as "unchanged" would admit a payment against bytes nobody could verify.

    It carries ``CONNECTOR_UNAVAILABLE`` because that is what this is from the kernel's
    side of the protocol: admission asked the merchant state source for authoritative
    truth and got no answer. The underlying cause differs -- here an unreadable version,
    in a deployment a connector that stopped responding -- but the buyer's situation does
    not, and neither does their recovery: nothing was charged, nothing needs re-approving,
    and the operation becomes possible again when the source does. Without a code the API
    could only fall back to its "understood and declined" default and answer 409, which
    says the caller has a state conflict to resolve when it has nothing to resolve at all
    (see ``commerce_api.errors.STATUS_BY_RECOVERY_CODE``).
    """

    code = RecoveryCode.CONNECTOR_UNAVAILABLE


# --------------------------------------------------------------------------- content


def content_from_quote(
    quote: Quote,
    *,
    checkout_id: uuid.UUID,
    version: int,
    policy_version: str,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Canonical checkout content for one quote. Pure.

    Field mapping, stated once so a reviewer can check it against the quote:

    * each :class:`~merchant_sim.fees.QuoteLine` becomes a line with ``unit_minor`` the
      unit price, ``line_minor`` the line subtotal and ``tax_minor`` the line tax;
    * ``subtotal_minor`` is ``items_subtotal``; ``tax_minor`` is ``items_tax`` plus
      ``delivery_tax`` (the whole tax charged); ``delivery_fee_minor`` is the fee before
      its tax; ``discount_minor`` is what the running offer took off, or zero;
    * ``catalogue_revision`` is the quote's freshness revision and ``source_id`` defaults
      to the quote's own provenance stamp.

    The builder re-checks the arithmetic, so a quote and its content cannot disagree.
    """
    lines = tuple(
        ContentLine(
            sku=line.sku,
            name=line.name,
            quantity=line.quantity,
            unit_minor=line.unit_price.minor,
            line_minor=line.subtotal.minor,
            tax_minor=line.tax.minor,
        )
        for line in quote.lines
    )
    return build_checkout_content(
        checkout_id=checkout_id,
        version=version,
        currency=quote.currency,
        lines=lines,
        subtotal_minor=quote.items_subtotal.minor,
        tax_minor=quote.items_tax.minor + quote.delivery_tax.minor,
        delivery_fee_minor=quote.delivery_fee.minor,
        discount_minor=quote.discount_amount.minor,
        total_minor=quote.total.minor,
        policy_version=policy_version,
        catalogue_revision=quote.freshness.catalogue_revision,
        source_id=source_id if source_id is not None else quote.freshness.source,
    )


# ------------------------------------------------------------------------ state source


class SimMerchantStateSource:
    """Re-read authoritative merchant state for admission, from one in-memory store.

    ``policy_version`` is the merchant-policy version the store is operating under; it is
    reported back to the kernel unchanged and stamped into every content document this
    source builds. ``source_id`` is the provenance stamp for the same documents.
    """

    __slots__ = ("_policy_version", "_source_id", "_store")

    def __init__(
        self, store: MerchantStore, *, policy_version: str, source_id: str = SOURCE_ID
    ) -> None:
        if not policy_version:
            raise ValueError("policy_version must be a non-empty merchant policy version")
        if not source_id:
            raise ValueError("source_id must be a non-empty provenance id")
        self._store = store
        self._policy_version = policy_version
        self._source_id = source_id

    @property
    def store(self) -> MerchantStore:
        return self._store

    def _approved_lines(
        self, session: Session, checkout_id: uuid.UUID, version: int
    ) -> tuple[ContentLine, ...]:
        row = session.execute(_SELECT_CONTENT, {"c": checkout_id, "v": version}).one_or_none()
        if row is None:
            raise RevalidationError(
                f"checkout {checkout_id} version {version} is not visible in this "
                "transaction; nothing to revalidate"
            )
        content: Mapping[str, Any] = row.content
        # lines_of validates the whole document and raises ContentContractError for a
        # version whose content is not canonical -- the fail-closed direction.
        return lines_of(content)

    def _state(
        self,
        quote: Quote,
        *,
        checkout_id: uuid.UUID,
        version: int,
        all_available: bool,
    ) -> CurrentMerchantState:
        content = content_from_quote(
            quote,
            checkout_id=checkout_id,
            version=version,
            policy_version=self._policy_version,
            source_id=self._source_id,
        )
        return CurrentMerchantState(
            total=quote.total,
            line_items=dict(content["line_items"]),
            all_available=all_available,
            policy_version=self._policy_version,
            content=content,
        )

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        """Admission step 8: what would this exact cart cost, and is it all there, now?

        Reads the approved version's content under the transaction's tenant, re-quotes
        its SKUs and quantities against the store's current state, and reports:

        * unchanged store -- the approved total and a content document that hashes
          exactly as the approved one did (the regression the adapter tests pin);
        * price, fee or threshold moved -- the new total and content, ``all_available``
          true;
        * a line no longer fulfillable -- ``all_available`` false, with the total and
          content of the remaining lines (so version N+1 is something the buyer can
          approve), or a zero total and no content when nothing remains.
        """
        approved = self._approved_lines(session, checkout_id, version)
        cart = [BasketLine(sku=line.sku, quantity=line.quantity) for line in approved]

        result = quote_basket(cart, store=self._store)
        if result.ok:
            return self._state(
                result.require(), checkout_id=checkout_id, version=version, all_available=True
            )

        unavailable = {item.sku for item in result.unavailable}
        remaining = [line for line in cart if line.sku not in unavailable]
        if not remaining:
            return CurrentMerchantState(
                total=Money.zero(self._store.fee_policy.currency),
                line_items={},
                all_available=False,
                policy_version=self._policy_version,
                content=None,
            )
        partial = quote_basket(remaining, store=self._store)
        if not partial.ok:  # pragma: no cover - every unavailable line was removed above
            raise RevalidationError(
                f"the fee engine refused the fulfillable remainder of checkout {checkout_id}: "
                f"{partial.code}"
            )
        return self._state(
            partial.require(), checkout_id=checkout_id, version=version, all_available=False
        )


# ---------------------------------------------------------------------------- receipt


def _discount_policy(promotion: Promotion | None, *, prefix: str, version: int) -> SaleTerm:
    """The DISCOUNT rule this sale is governed by, frozen into its receipt.

    With no offer running the terms record ``{"allowed": False}`` -- an explicit "no
    discount programme" rather than an omission, because an omitted kind is filled in later
    from the merchant's *current* policy, which is the retroactive change the receipt
    exists to prevent.

    With an offer running the policy takes the offer's own id and version, not the store's.
    ``_validate_policies`` keys its version map on ``policy_id``, so a DISCOUNT rule at its
    own identity sits beside the other five without contradiction -- and two different
    offers become two distinguishable rules rather than both reading "discount, version 1".
    The window is recorded because it is the term a buyer would dispute: what they were
    promised, and until when.
    """
    if promotion is None:
        return SaleTerm(
            kind=PolicyKind.DISCOUNT,
            policy_id=f"{prefix}/discount",
            policy_version=version,
            terms={"allowed": False},
        )
    terms: dict[str, Any] = {
        "allowed": True,
        "offer_id": promotion.offer_id,
        "label": promotion.label,
        "basis": "CART_TOTAL",
        "effective_from_epoch_ms": promotion.effective_from_epoch_ms,
        "effective_to_epoch_ms": promotion.effective_to_epoch_ms,
        # What a refund returns is the amount actually paid, never the offer's face value.
        # Recorded here because it is a term of the sale, not a property of the code.
        "refund_basis": "PAID_AMOUNT",
        "credit_expires_on_refund": True,
    }
    if promotion.percent_bp is not None:
        terms["kind"] = "PERCENT"
        terms["percent_bp"] = promotion.percent_bp
    else:
        assert promotion.flat is not None
        terms["kind"] = "FLAT"
        terms["flat_minor"] = promotion.flat.minor
    return SaleTerm(
        kind=PolicyKind.DISCOUNT,
        policy_id=f"{prefix}/discount/{promotion.offer_id}",
        policy_version=version,
        terms=terms,
    )


def _policies(
    fee: FeePolicy, *, prefix: str, version: int, promotion: Promotion | None = None
) -> tuple[SaleTerm, ...]:
    currency = fee.currency
    return (
        SaleTerm(
            kind=PolicyKind.CANCELLATION,
            policy_id=f"{prefix}/cancellation",
            policy_version=version,
            terms={
                "allowed": True,
                "cutoff": "BEFORE_DISPATCH",
                "fee": Money.zero(currency),
            },
        ),
        SaleTerm(
            kind=PolicyKind.REFUND,
            policy_id=f"{prefix}/refund",
            policy_version=version,
            terms={
                "allowed": True,
                "window_days": 7,
                "method": "ORIGINAL_INSTRUMENT",
                "partial_allowed": True,
            },
        ),
        SaleTerm(
            kind=PolicyKind.SUBSTITUTION,
            policy_id=f"{prefix}/substitution",
            policy_version=version,
            terms={"allowed": False},
        ),
        SaleTerm(
            kind=PolicyKind.DELIVERY,
            policy_id=f"{prefix}/delivery",
            policy_version=version,
            terms={
                "base_fee": fee.base_delivery_fee,
                "free_delivery_threshold": fee.free_delivery_threshold,
                "delivery_tax_bp": fee.delivery_tax_bp,
                "threshold_basis": "PRE_TAX_ITEMS_INCLUSIVE",
            },
        ),
        _discount_policy(promotion, prefix=prefix, version=version),
        SaleTerm(
            kind=PolicyKind.FULFILMENT,
            policy_id=f"{prefix}/fulfilment",
            policy_version=version,
            terms={"mode": "QUICK_COMMERCE", "promise_minutes": 30},
        ),
    )


def receipt_inputs_for(
    source: MerchantStore | FeePolicy,
    *,
    policy_version: int = 1,
    policy_prefix: str = DEFAULT_POLICY_PREFIX,
    policy_uri: str = "https://demo.invalid/policies",
    carry_forward: Sequence[SaleTerm] = (),
) -> ReceiptInputs:
    """The Demo Grocery Store's rules as receipt inputs, one policy per ``PolicyKind``.

    The delivery terms are copied from the fee policy *in force* (a store, or a policy
    handed in directly), so a fee injection between two receipts produces two different
    receipt hashes: the receipt records the fee the buyer was shown, which is the whole
    point of freezing it. The buyer-visible reference hashes the rendered policy set, so
    what the buyer could read is provable later even though the demo has no policy page.

    ``carry_forward`` is how a monetary requote keeps its promises. Pass the rules the
    kernel read off the retired version's own receipt
    (:func:`transaction_kernel.bound_terms_for_requote`) and each replaces the current one
    of the same kind, keeping the ``policy_id`` and ``policy_version`` it was recorded
    under. The buyer-visible hash is computed over the composed set, so it still describes
    what this buyer could read rather than what a new buyer would be shown.
    """
    fee = source.fee_policy if isinstance(source, MerchantStore) else source
    promotion = source.promotion if isinstance(source, MerchantStore) else None
    current = _policies(fee, prefix=policy_prefix, version=policy_version, promotion=promotion)
    retained = {policy.kind: policy for policy in carry_forward}
    policies = tuple(retained.get(policy.kind, policy) for policy in current)
    rendered: Sequence[Mapping[str, Any]] = [policy.as_content() for policy in policies]
    return ReceiptInputs(
        policies=policies,
        tax_policy_version=TAX_POLICY_VERSION,
        rounding_policy_version=ROUNDING_POLICY_VERSION,
        buyer_visible_refs=(
            BuyerVisibleRef(
                label="Demo Grocery Store policies",
                uri=policy_uri,
                text_hash=str(canonical_hash(list(rendered))),
            ),
        ),
    )

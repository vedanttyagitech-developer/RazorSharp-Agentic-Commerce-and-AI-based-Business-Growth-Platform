"""The wire contract: every response shape the buyer surface and the agents consume.

This module exists so that the shape of a checkout is decided once rather than five
times. Five build units write routers over the same kernel; if each serialised a
``KernelDecision`` its own way, the storefront would need five parsers and the
reconciliation between them would happen at demo time.

Three rules hold everywhere here, from specification 24.1:

* **Money is an integer count of minor units beside an ISO 4217 code.** Never a float,
  never a formatted string that something later parses back. Where a human has to read
  an amount, :class:`MoneyOut` carries a ``display`` string *alongside* the integer, so
  presentation never becomes arithmetic.
* **Time is RFC 3339 in UTC**, rendered with a trailing ``Z`` by :func:`rfc3339`.
* **Identifiers are strings**, because a UUID that survives a JSON round trip as a string
  compares equal on both sides and a UUID that is sometimes an object does not.

Field names follow ``apps/buyer-web/src/lib/api/types.ts``, which was written against
ADR 0003's endpoint catalogue before this module existed. Where this module differs from
that file it is because the kernel's real types admit a value the provisional schema did
not (a version with no receipt yet, an approval card built before its quote is loaded);
those differences are listed in the build report so the frontend widens rather than
crashes. The direction of authority is the other way round for everything else: this is
what the server sends, and the client matches it.

Every ``of``/``from_kernel`` constructor is a classmethod on the model rather than a
function elsewhere, so there is exactly one way to turn a kernel value into wire JSON and
a unit cannot quietly invent a second.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from commerce_domain import Money
from merchant_sim import Freshness, ProductView, Quote, SearchHit, Unavailability
from pydantic import BaseModel, ConfigDict, Field
from transaction_kernel import (
    CheckoutRef,
    CheckoutState,
    Delta,
    KernelDecision,
    PaymentState,
    RecoveryCode,
    lines_of,
)

__all__ = [
    "ApprovalCardOut",
    "ApprovalRecordOut",
    "AttemptOut",
    "BasketLineOut",
    "BasketOut",
    "CaptureEvidenceOut",
    "CheckoutOut",
    "CheckoutRefOut",
    "DecisionOut",
    "DeltaOut",
    "FreshnessOut",
    "MoneyOut",
    "CataloguePageOut",
    "ListScope",
    "OrderOut",
    "OrderSummaryOut",
    "OrdersPageOut",
    "RefundListItemOut",
    "RefundsPageOut",
    "OrderState",
    "ProblemOut",
    "ProductOut",
    "QuoteLineOut",
    "QuoteOut",
    "RefundOut",
    "ReservationOut",
    "SearchHitOut",
    "UnavailabilityOut",
    "VersionSummaryOut",
    "rfc3339",
    "uuid_str",
]


def rfc3339(moment: datetime) -> str:
    """Render an instant as RFC 3339 UTC with a ``Z`` suffix.

    A naive datetime is treated as UTC rather than as local time. Every timestamp this
    platform serialises comes from PostgreSQL's ``timestamptz``, so naive here means a
    driver handed back a value without its zone -- and guessing the server's local zone
    would silently move a reservation deadline by hours.
    """
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def uuid_str(value: uuid.UUID | None) -> str | None:
    """Identifiers cross the wire as strings; ``None`` stays ``None``."""
    return None if value is None else str(value)


class OrderState(StrEnum):
    """Mirror of the ``orders.status`` CHECK constraint in ``platform_db.schema_service``.

    Declared here rather than imported because the kernel has no order-status enum: the
    column's constraint is the definition, and this is its wire-facing copy. A test in
    the orders unit asserts the two never drift.
    """

    CONFIRMED = "CONFIRMED"
    FULFILMENT_BLOCKED = "FULFILMENT_BLOCKED"
    CANCELLED = "CANCELLED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"


class _Out(BaseModel):
    """Base for every response model: strict, and populatable by field name.

    ``extra="forbid"`` is specification 24.1's "strict request and response schemas". On
    a response it is a build-time guard rather than a runtime one: a unit that passes a
    field this contract does not declare fails its own test instead of shipping a key the
    storefront's parser rejects.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- money


class MoneyOut(_Out):
    """An exact amount. ``minor`` is authoritative; ``display`` is for humans only.

    ``display`` is the currency's exact decimal string (``39500`` INR -> ``"395.00"``)
    with no symbol and no grouping, because a locale-formatted amount is a rendering
    decision the buyer surface owns and a symbol baked in here would be wrong the first
    time this runs in another currency.
    """

    minor: int
    currency: str
    display: str

    @classmethod
    def of(cls, amount: Money) -> MoneyOut:
        return cls(minor=amount.minor, currency=amount.currency, display=amount.format_decimal())


# ------------------------------------------------------------------------ grounding


class FreshnessOut(_Out):
    """Provenance for a catalogue read (specification 6.2, 20.1).

    ``catalogue_revision`` is the freshness token: the buyer surface compares the
    revision a quote was priced at against the revision a later read reports, and that
    comparison is what makes "the price moved under you" visible before admission does
    it for real.
    """

    source: str
    catalogue_revision: int
    observed_at: str

    @classmethod
    def of(cls, freshness: Freshness) -> FreshnessOut:
        return cls(
            source=freshness.source,
            catalogue_revision=freshness.catalogue_revision,
            observed_at=rfc3339(freshness.observed_at),
        )


# ------------------------------------------------------------------------ catalogue


class ProductOut(_Out):
    """A product as it exists right now: catalogue record plus live merchant state.

    ``is_listed`` and ``is_available`` are reported separately on purpose. Sold out and
    delisted lead to different conversations, and collapsing them into one boolean is how
    an agent tells a buyer "we do not sell that" about something that is back in stock
    tomorrow.
    """

    sku: str
    display_name: str
    name_en: str
    name_hi: str
    category: str
    unit_label: str
    unit_price_minor: int
    unit_price: MoneyOut
    currency: str
    tax_bp: int
    stock_units: int
    is_listed: bool
    is_available: bool
    freshness: FreshnessOut

    @classmethod
    def of(cls, view: ProductView, *, devanagari: bool = False) -> ProductOut:
        product = view.product
        return cls(
            sku=view.sku,
            display_name=view.display_name(devanagari=devanagari),
            name_en=product.name_en,
            name_hi=product.name_hi,
            category=str(product.category),
            unit_label=product.unit_label,
            unit_price_minor=view.unit_price.minor,
            unit_price=MoneyOut.of(view.unit_price),
            currency=view.unit_price.currency,
            tax_bp=product.tax_bp,
            stock_units=view.stock_units,
            is_listed=view.is_listed,
            is_available=view.is_available,
            freshness=FreshnessOut.of(view.freshness),
        )


class CataloguePageOut(_Out):
    """A page of the merchant's catalogue, with the shape of the whole beside it.

    ``matched`` counts the rows the filter selected, not the page; ``counts_by_category``
    counts the whole catalogue, so a merchant can see a category is empty rather than
    inferring it from a filtered view. ``revision`` is the merchant state the page was
    read at: a price injected after this read makes the page stale, and the revision is
    how a console notices.
    """

    products: list[ProductOut]
    next_cursor: str | None
    limit: int
    matched: int
    counts_by_category: dict[str, int]
    revision: int


class SearchHitOut(ProductOut):
    """A product plus why the search returned it.

    ``matched_terms`` is what makes discovery *grounded* rather than plausible: the buyer
    surface can show which token matched, and a reviewer can check that a Hinglish query
    hit a real index term instead of a model's guess.
    """

    score: int
    matched_terms: list[str]

    @classmethod
    def of_hit(cls, hit: SearchHit, *, devanagari: bool = False) -> SearchHitOut:
        base = ProductOut.of(hit.view, devanagari=devanagari)
        return cls(
            **base.model_dump(),
            score=hit.score,
            matched_terms=list(hit.matched_terms),
        )


# -------------------------------------------------------------------- basket, quote


class QuoteLineOut(_Out):
    """One priced line. Every amount is a minor-unit integer computed by merchant-sim.

    The API never adds two of these together. The total on :class:`QuoteOut` comes from
    the fee engine, which is the only component permitted to compute a basket total.

    ``tax_bp`` is the *rate* the engine applied, and it is an input to pricing rather than
    part of the priced result: :data:`transaction_kernel.checkout_content.LINE_KEYS` does
    not carry it, so a line rebuilt from an approved checkout document has no rate to
    report and sends ``None``. ``None`` here means "the approved bytes do not state this",
    which is not the same claim as ``0``, and a rate cannot be recovered from the tax and
    the subtotal because many rates round to the same paisa.
    """

    sku: str
    name: str
    quantity: int
    unit_price_minor: int
    subtotal_minor: int
    tax_bp: int | None
    tax_minor: int


class UnavailabilityOut(_Out):
    """Why a requested line could not be priced. Never merged into the quote."""

    sku: str
    requested: int
    available_units: int
    listed: bool

    @classmethod
    def of(cls, unavailable: Unavailability) -> UnavailabilityOut:
        return cls(
            sku=unavailable.sku,
            requested=unavailable.requested,
            available_units=unavailable.available_units,
            listed=unavailable.listed,
        )


class QuoteOut(_Out):
    """A deterministic quote: an offer, and never an authorization.

    ``content_hash`` is the hash of the canonical checkout content this quote would
    produce (``transaction_kernel.checkout_content``). It is carried on the quote so the
    buyer surface can show, before checkout exists, the exact bytes an approval would
    later bind to.

    Two constructors, and the difference between them is the whole point. :meth:`of`
    renders a live :class:`~merchant_sim.Quote` -- an offer, complete with the shop-front
    figures that belong to an offer. :meth:`of_content` renders an approved checkout
    document, which is a smaller thing: it states what was priced and nothing about what
    might have been. ``gap_to_free_delivery_minor`` is ``None`` there because the free
    delivery threshold is merchant policy, not a priced figure, and the document does not
    carry it; the honest answer is "not stated", never a computed nudge on a screen whose
    job is consent.
    """

    currency: str
    lines: list[QuoteLineOut]
    items_subtotal_minor: int
    items_tax_minor: int
    delivery_fee_minor: int
    delivery_tax_minor: int
    total_minor: int
    total: MoneyOut
    free_delivery_applied: bool
    gap_to_free_delivery_minor: int | None
    source: str
    catalogue_revision: int
    content_hash: str

    @classmethod
    def of(cls, quote: Quote, *, content_hash: str) -> QuoteOut:
        return cls(
            currency=quote.currency,
            lines=[
                QuoteLineOut(
                    sku=line.sku,
                    name=line.name,
                    quantity=line.quantity,
                    unit_price_minor=line.unit_price.minor,
                    subtotal_minor=line.subtotal.minor,
                    tax_bp=line.tax_bp,
                    tax_minor=line.tax.minor,
                )
                for line in quote.lines
            ],
            items_subtotal_minor=quote.items_subtotal.minor,
            items_tax_minor=quote.items_tax.minor,
            delivery_fee_minor=quote.delivery_fee.minor,
            delivery_tax_minor=quote.delivery_tax.minor,
            total_minor=quote.total.minor,
            total=MoneyOut.of(quote.total),
            free_delivery_applied=quote.free_delivery_applied,
            gap_to_free_delivery_minor=quote.gap_to_free_delivery.minor,
            source=quote.freshness.source,
            catalogue_revision=quote.freshness.catalogue_revision,
            content_hash=content_hash,
        )

    @classmethod
    def of_content(cls, content: Mapping[str, Any], *, content_hash: str) -> QuoteOut:
        """The breakdown an approved checkout version states about itself.

        Not a re-quote. Every figure below is read out of the immutable, hashed document
        the buyer's approval binds to, so the lines shown beside a total are the lines
        that total is made of, however long ago the version was written and whatever the
        merchant charges now. Re-pricing here would be the one unforgivable bug on a
        consent screen: an amount that is not the amount approved.

        Three figures are arithmetic on the document rather than keys of it, and each is
        exact rather than inferred:

        * ``items_tax_minor`` is the sum of the line taxes. ``Quote`` refuses to exist
          unless its ``items_tax`` equals that sum, and :func:`merchant_sim.content_from_quote`
          copies each line tax across unchanged, so the sum is the same integer.
        * ``delivery_tax_minor`` is the document's whole ``tax_minor`` less that sum,
          because ``content_from_quote`` writes ``tax_minor`` as items tax plus delivery
          tax and nothing else. The content validator guarantees it cannot go negative.
        * ``free_delivery_applied`` is the delivery fee being zero. That is not a guess
          about a programme: ``Quote.__post_init__`` refuses any quote whose flag and
          whose zero fee disagree, so the two statements are one statement.

        Raises :class:`ValueError` if the components do not add up to the stated total --
        which today means a document carrying a discount, a key no field here can show. A
        card whose rows do not sum to the amount it asks consent for must not render at
        all; that is the same refusal :class:`~merchant_sim.Quote` makes at construction.
        """
        lines = lines_of(content)
        currency = str(content["currency"])
        items_subtotal = int(content["subtotal_minor"])
        items_tax = sum(line.tax_minor for line in lines)
        delivery_fee = int(content["delivery_fee_minor"])
        delivery_tax = int(content["tax_minor"]) - items_tax
        total = Money(int(content["total_minor"]), currency)

        stated = items_subtotal + items_tax + delivery_fee + delivery_tax
        if stated != total.minor:
            raise ValueError(
                f"checkout content {content_hash} totals {total.minor} but the components "
                f"this quote can show add up to {stated}; the difference is a figure the "
                "approval card would have to hide, so it refuses to render one"
            )

        return cls(
            currency=currency,
            lines=[
                QuoteLineOut(
                    sku=line.sku,
                    name=line.name,
                    quantity=line.quantity,
                    unit_price_minor=line.unit_minor,
                    subtotal_minor=line.line_minor,
                    tax_bp=None,
                    tax_minor=line.tax_minor,
                )
                for line in lines
            ],
            items_subtotal_minor=items_subtotal,
            items_tax_minor=items_tax,
            delivery_fee_minor=delivery_fee,
            delivery_tax_minor=delivery_tax,
            total_minor=total.minor,
            total=MoneyOut.of(total),
            free_delivery_applied=delivery_fee == 0,
            gap_to_free_delivery_minor=None,
            source=str(content["source_id"]),
            catalogue_revision=int(content["catalogue_revision"]),
            content_hash=content_hash,
        )


class BasketLineOut(_Out):
    """Buyer intent: what was asked for, not what it costs."""

    sku: str
    quantity: int


class BasketOut(_Out):
    """A basket and its current re-quote.

    ``code`` is the fee engine's :class:`RecoveryCode`, so an unpriceable basket says why
    in the same closed vocabulary the kernel uses. ``stale`` is true when merchant state
    has moved since the stored quote was priced -- the signal the UI turns into
    "Revalidating" (specification 8.2).
    """

    basket_id: str
    lines: list[BasketLineOut]
    code: RecoveryCode
    quote: QuoteOut | None
    unavailable: list[UnavailabilityOut]
    freshness: FreshnessOut
    stale: bool


# ------------------------------------------------------------- checkout, approval


class CheckoutRefOut(_Out):
    """Identity of one immutable checkout version. The unit an approval binds to."""

    checkout_id: str
    version: int
    content_hash: str

    @classmethod
    def of(cls, ref: CheckoutRef) -> CheckoutRefOut:
        return cls(
            checkout_id=str(ref.checkout_id),
            version=ref.version,
            content_hash=ref.content_hash,
        )


class DeltaOut(_Out):
    """One material difference between what was approved and what is true now.

    This is step 7 of the demonstration made legible: not "something changed" but the
    field path, the approved value and the current one. ``approved`` and ``current`` are
    untyped because a delta can be about an amount, a stock count or an availability
    flag, and coercing them to strings here would lose the integer a UI wants to format.
    """

    field_path: str
    approved: Any
    current: Any
    reason: str

    @classmethod
    def of(cls, delta: Delta) -> DeltaOut:
        return cls(
            field_path=delta.field_path,
            approved=delta.approved,
            current=delta.current,
            reason=delta.reason,
        )


class ReservationOut(_Out):
    """The temporary hold behind an approval card. Expiry is the database's clock."""

    reservation_id: str
    state: str
    expires_at: str


class ApprovalCardOut(_Out):
    """What the trusted surface shows the buyer before they approve (specification 4.3).

    The approve request echoes exactly ``content_hash``, ``amount_minor`` and
    ``currency`` from this object and nothing else. That echo is the whole point of the
    card: consent is bound to bytes the buyer was shown, not to a checkout id whose
    contents may have moved.

    ``quote`` is optional because the card is built from the kernel's immutable version
    content, which exists before the merchant-sim quote that produced it is reloaded.
    ``previous_version`` and ``deltas`` are populated only on a reapproval card (version
    N+1), where they are the evidence for why a second approval is being asked for.
    """

    checkout_id: str
    version: int
    content_hash: str
    policy_receipt_id: str
    policy_receipt_hash: str
    amount_minor: int
    currency: str
    total: MoneyOut
    expires_at: str
    reservation: ReservationOut | None = None
    quote: QuoteOut | None = None
    previous_version: int | None = None
    deltas: list[DeltaOut] = Field(default_factory=list)


class ApprovalRecordOut(_Out):
    """A recorded approval: proof that a specific human said yes to specific bytes."""

    approval_id: str
    version: int
    content_hash: str
    policy_receipt_hash: str
    amount_minor: int
    currency: str
    approved_at: str
    expires_at: str
    authority_epoch: int


class CaptureEvidenceOut(_Out):
    """How the platform learned a payment was captured (ADR 0003 D8).

    ``kind`` is never ``BROWSER_CALLBACK``: a browser callback is recorded evidence of a
    buyer's return, and capture is applied only from ``WEBHOOK`` or ``PROVIDER_FETCH``.
    """

    kind: str
    reference: str
    verified_at: str


class AttemptOut(_Out):
    """Summary of the one live payment attempt for a checkout.

    A checkout has at most one non-terminal attempt -- a partial unique index enforces
    it -- which is why this is a single object rather than a list.
    """

    attempt_id: str
    version: int
    state: PaymentState
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    grant_id: str | None
    capture_evidence: CaptureEvidenceOut | None
    reconciliation_attempts: int


class VersionSummaryOut(_Out):
    """One checkout version in the history the timeline and the inspector render.

    ``policy_receipt_hash`` is nullable because a version exists for an instant before
    its Policy-at-Sale Receipt is issued, and because an invalidated draft may never get
    one. A UI that needs a receipt hash should read it from the approval card.
    """

    version: int
    state: CheckoutState
    content_hash: str
    policy_receipt_hash: str | None
    amount_minor: int
    currency: str
    created_at: str
    approval: ApprovalRecordOut | None = None


class CheckoutOut(_Out):
    """The whole checkout as the buyer surface needs it: head, history, card, attempt.

    One object rather than four endpoints because every UI state in specification 8.2 is
    a function of these fields together, and a surface that fetched them separately could
    render an approval card for a version the attempt has already moved past.
    """

    checkout_id: str
    basket_id: str
    state: CheckoutState
    current_version: int
    versions: list[VersionSummaryOut]
    approval_card: ApprovalCardOut | None
    attempt: AttemptOut | None
    order_id: str | None
    deltas: list[DeltaOut]
    cancellable: bool
    updated_at: str


# -------------------------------------------------------------------- the decision


class DecisionOut(_Out):
    """``transaction_kernel.KernelDecision``, verbatim (specification 26.3).

    This is the single most important shape in the API. ADR 0003 D15: a denial is
    delivered as HTTP 200 carrying this object, never as a 4xx, because a denial is the
    system working correctly and an error status invites a client to retry it as though
    it were a fault.

    ``explanation`` is a stable reason key, not a sentence. An agent renders it into the
    buyer's language; nothing may alter the fields around it.
    """

    decision_id: str
    allowed: bool
    code: RecoveryCode
    explanation: str
    checkout: CheckoutRefOut | None
    deltas: list[DeltaOut]
    grant_id: str | None
    payment_attempt_id: str | None
    next_version: int | None
    correlation_id: str | None

    @classmethod
    def from_kernel(cls, decision: KernelDecision) -> DecisionOut:
        """The one permitted serialisation of a kernel decision."""
        return cls(
            decision_id=str(decision.decision_id),
            allowed=decision.allowed,
            code=decision.code,
            explanation=decision.explanation,
            checkout=None if decision.checkout is None else CheckoutRefOut.of(decision.checkout),
            deltas=[DeltaOut.of(delta) for delta in decision.deltas],
            grant_id=uuid_str(decision.grant_id),
            payment_attempt_id=uuid_str(decision.payment_attempt_id),
            next_version=decision.next_version,
            correlation_id=uuid_str(decision.correlation_id),
        )


# ------------------------------------------------------------------ orders, refunds


class RefundOut(_Out):
    """One refund against a captured payment. ``automatic`` marks the stale-capture path.

    An automatic refund is the platform protecting a buyer from a capture that landed on
    an invalidated checkout (specification 10.8); a buyer-requested one is a support
    action. They are counted differently in retained-revenue evidence, so the flag is
    part of the contract rather than a note in the timeline.
    """

    refund_id: str
    amount_minor: int
    currency: str
    state: PaymentState
    reason: str
    automatic: bool
    created_at: str


class OrderOut(_Out):
    """A confirmed sale, bound to the exact policy and bytes the buyer approved.

    ``content_hash`` and ``policy_receipt_hash`` are copied onto the order so that "what
    were the terms of this sale" is answerable from the order alone, without trusting
    that the version row still says what it said at capture.
    """

    order_id: str
    checkout_id: str
    version: int
    content_hash: str
    policy_receipt_hash: str
    state: OrderState
    amount_minor: int
    currency: str
    amount: MoneyOut
    quote: QuoteOut | None
    payment: AttemptOut
    refunds: list[RefundOut]
    created_at: str


# ------------------------------------------------------------------------ problems


class ListScope(StrEnum):
    """How wide a collection read was: the caller's own rows, or the whole tenant.

    Reported on every page so a console labels what it shows rather than assuming it.
    ``TENANT`` is reached only with a valid scenario key on the request, the P0 stand-in
    for the merchant operator surface (ADR 0003 D11); nothing in a request body or a
    cursor can widen a scope.
    """

    OWN = "own"
    TENANT = "tenant"


class OrderSummaryOut(_Out):
    """One row of the order list: enough to triage, with the identifiers to drill in.

    ``refunded_minor`` is the database's integer sum over *settled* refund rows for this
    order's attempt; pending and unknown refunds are counted in ``refund_count`` but not
    in the sum, because money the provider has not confirmed returning has not returned.
    ``age_seconds`` is by the database clock, the one that stamped the row.
    """

    order_id: str
    checkout_id: str
    version: int
    payment_attempt_id: str
    policy_receipt_hash: str
    state: OrderState
    amount_minor: int
    currency: str
    amount: MoneyOut
    capture_evidence: CaptureEvidenceOut | None
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    refunded_minor: int
    refund_count: int
    created_at: str
    age_seconds: int


class OrdersPageOut(_Out):
    """A page of orders and, separately, the counts across the whole scope.

    ``next_cursor`` is ``null`` on the last page. ``counts`` has every order state
    present, zeros included, so "none" is distinguishable from "not measured".
    """

    orders: list[OrderSummaryOut]
    next_cursor: str | None
    limit: int
    scope: ListScope
    counts: dict[str, int]


class RefundListItemOut(_Out):
    """One row of the refund list, with its wire state and the row status behind it.

    ``state`` is what ``RefundOut.state`` would say for the same row; ``row_status`` is
    the ``refunds.status`` column verbatim, for an operator reconciling against the
    database. ``captured_minor`` is the order's captured total, which is what decides
    whether a settled refund was partial; it is ``null`` when no order row exists for
    the attempt, which cannot happen for a refund the platform created but is left
    honest rather than defaulted.
    """

    refund_id: str
    order_id: str | None
    checkout_id: str
    payment_attempt_id: str
    amount_minor: int
    currency: str
    amount: MoneyOut
    captured_minor: int | None
    state: PaymentState
    row_status: str
    reason: str
    automatic: bool
    provider_refund_id: str | None
    created_at: str
    updated_at: str
    age_seconds: int


class RefundsPageOut(_Out):
    """A page of refunds and the counts across the whole scope, every state included."""

    refunds: list[RefundListItemOut]
    next_cursor: str | None
    limit: int
    scope: ListScope
    counts: dict[str, int]


class ProblemOut(BaseModel):
    """An RFC 9457 problem detail. The only error shape this API emits.

    ``extra="allow"`` because RFC 9457 extension members are how a problem carries the
    thing a client needs to act: the ``code`` of a refused operation, the constraint a
    write violated, the field a body got wrong. The five standard members below are
    always present in that order; anything else is an extension.

    ``type`` is declared through an alias so the attribute name does not shadow the
    builtin. Serialise with ``by_alias=True`` -- :func:`commerce_api.errors.problem`
    does.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type_: str = Field(default="about:blank", alias="type")
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None

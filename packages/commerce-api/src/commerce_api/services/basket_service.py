"""Baskets and the deterministic quote over them. Step 2 of the demonstration.

A basket is buyer intent and nothing else: a set of SKUs and whole-unit quantities. It
carries no prices of its own, and this module never adds two numbers together. Every
amount on the wire comes from :func:`merchant_sim.quote_basket`, which is the only
component permitted to compute a total, because a service that could price a line could
also price it differently from the merchant and nobody would notice until the payment.

Three properties this module is responsible for:

**A quote is an offer, never an authorization.** It is recomputed on every read, and the
stored copy on ``baskets.quote`` exists only so the buyer surface can show what was last
displayed. Nothing downstream trusts it; the binding copy is created when a checkout
version is written, and only the kernel writes that.

**Staleness is reported, not hidden.** The basket stores the catalogue revision it was
priced at. When the store has moved on, ``stale`` is true and the re-quote beside it is
the current truth. That flag is the storefront's early warning for the same condition
admission enforces for real (specification 8.2, "Revalidating").

**A line write locks the basket row.** ``SELECT ... FOR UPDATE`` before the read-modify-
write, so two tabs setting quantities on one basket serialize instead of one silently
overwriting the other's lines.

Why the quote's ``content_hash`` is a preview
---------------------------------------------
:class:`commerce_api.schemas.QuoteOut` carries the hash of the canonical checkout content
this basket would produce, built through
:func:`transaction_kernel.checkout_content.build_checkout_content` -- the kernel owns that
shape (ADR 0003 D6) and merchant-sim satisfies it. The document includes ``checkout_id``
and ``version``, which do not exist until :func:`transaction_kernel.create_checkout` mints
them, so the basket stamps its own id and version 1 in their place. Everything else --
every line, every amount, the policy version, the catalogue revision -- is byte-identical
to what version 1 will hash. The binding hash, the one an approval echoes and admission
compares, is the one on the approval card, and it is the only one this service ever
compares anything against.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import uuid7
from merchant_sim import (
    BasketLine,
    InvalidBasketError,
    MerchantStore,
    Quote,
    QuoteResult,
    UnknownSkuError,
    content_from_quote,
    quote_basket,
)
from platform_db import Basket, Checkout
from sqlalchemy import select
from sqlalchemy.orm import Session
from transaction_kernel import (
    CheckoutState,
    RecoveryCode,
    ReleaseCause,
    content_hash,
    current_version,
    release,
    transition,
)

from ..deps import RequestContext
from ..errors import ProblemError
from ..merchants import MerchantRegistry
from ..schemas import (
    BasketLineOut,
    BasketOut,
    FreshnessOut,
    QuoteOut,
    UnavailabilityOut,
)

__all__ = [
    "MAX_BASKET_LINES",
    "MAX_LINE_QUANTITY",
    "SUPERSEDED",
    "ExpectedBasket",
    "basket_binding",
    "basket_body",
    "create_basket",
    "load_basket",
    "lock_basket",
    "quote_lines",
    "read_basket",
    "set_line",
    "stored_lines",
]

#: A quick-commerce basket. Bounded because every line is re-priced and re-reserved on
#: every checkout, and an unbounded basket is an unbounded admission transaction.
MAX_BASKET_LINES = 40

#: Per-line ceiling. The merchant simulator's stock is small; a quantity beyond this is a
#: typo or a probe, and either way refusing it is cheaper than reserving it.
MAX_LINE_QUANTITY = 99


# ------------------------------------------------------------------------ line storage


def stored_lines(basket: Basket) -> list[dict[str, Any]]:
    """The basket's lines as stored: ``[{"sku": str, "quantity": int}, ...]``, SKU order.

    Sorted here rather than on write so that a row written by any path reads back in one
    order. The canonical content the kernel hashes is sorted by SKU too, so two clients
    adding the same items in different orders produce one hash and one approval.
    """
    lines: list[dict[str, Any]] = []
    for entry in basket.lines:
        sku = str(entry["sku"])
        quantity = int(entry["quantity"])
        if quantity > 0:
            lines.append({"sku": sku, "quantity": quantity})
    return sorted(lines, key=lambda line: str(line["sku"]))


def quote_lines(lines: Sequence[dict[str, Any]], store: MerchantStore) -> QuoteResult | None:
    """Price the basket, or ``None`` when there is nothing to price.

    An empty basket is not a refusal -- there is simply no basket yet -- so it is reported
    as ``None`` rather than as a :class:`~transaction_kernel.RecoveryCode`, which would
    tell the buyer surface something went wrong.
    """
    if not lines:
        return None
    return quote_basket(
        [BasketLine(sku=str(line["sku"]), quantity=int(line["quantity"])) for line in lines],
        store=store,
    )


def preview_content_hash(quote: Quote, *, basket_id: uuid.UUID, policy_version: str) -> str:
    """The canonical hash this basket would produce as checkout version 1.

    See the module docstring: the basket's own id stands in for a checkout id that does
    not exist yet. Built through the kernel's builder, so a quote the builder refuses
    fails here rather than at approval time.
    """
    content = content_from_quote(
        quote,
        checkout_id=basket_id,
        version=1,
        policy_version=policy_version,
    )
    return content_hash(content)


# -------------------------------------------------------------- what a proposal binds to

#: The refusal key a superseded proposal carries. Stable, because a buyer surface renders
#: this one as the gate working -- an amber re-proposal showing what moved -- rather than
#: as a failure, and it can only tell the two apart by the key.
SUPERSEDED: str = "proposal_superseded"


@dataclass(frozen=True, slots=True)
class ExpectedBasket:
    """The state a proposal was built against, echoed back by the surface executing it.

    RazorAI reads a basket and a product, shows the buyer what adding a line would mean,
    and the buyer presses confirm some seconds or minutes later. In between, another tab
    can edit the same basket and the merchant can move a price or the whole catalogue.
    This is the buyer's half of that race made checkable: the three facts the proposal
    rested on, sent back with the press so the server can refuse a confirmation of
    something that is no longer true.

    ``basket_content_hash`` is the basket's *current* preview hash -- the canonical bytes
    of the checkout this basket would produce as version 1, exactly as
    :class:`~commerce_api.schemas.QuoteOut` carries it on every read. ``None`` is not a
    missing value: it is the positive claim that the basket held nothing priceable, which
    is what an empty basket's quote is.

    Binding the whole basket rather than the one line is deliberate. If another surface
    added onions between the proposal and the press, this line's price has not moved but
    the basket the buyer was shown has, and a line-scoped check would let a card execute
    beside a total it can no longer stand behind.

    What this proves, precisely: the basket, this product's price and the catalogue are
    unchanged since the proposal was built. It does **not** prove the buyer was shown that
    proposal -- nothing signs it -- so nothing here or on the wire calls it a signature.
    """

    basket_content_hash: str | None
    unit_price_minor: int
    catalogue_revision: int


def basket_binding(basket: Basket, *, registry: MerchantRegistry) -> dict[str, Any]:
    """The two facts about a basket a proposal binds to, as the wire carries them.

    Re-quoted rather than read off ``baskets.quote``: the stored quote is what the buyer
    last saw, and binding to it would bind to a memory instead of to the merchant.
    """
    store = registry.store(basket.merchant_id)
    result = quote_lines(stored_lines(basket), store)
    hash_now: str | None = None
    if result is not None and result.quote is not None:
        hash_now = preview_content_hash(
            result.quote, basket_id=basket.id, policy_version=registry.policy_version()
        )
    return {
        "basket_content_hash": hash_now,
        "catalogue_revision": store.freshness().catalogue_revision,
    }


def _assert_unchanged(
    basket: Basket, *, registry: MerchantRegistry, sku: str, expected: ExpectedBasket
) -> None:
    """Refuse a confirmation of a proposal the world has moved past.

    Recompute-and-compare, under the basket's own row lock, so what is compared is what
    the write is about to be applied to. The refusal carries the current figures, which is
    the difference between "that did not work" and "here is what it costs now".
    """
    store = registry.store(basket.merchant_id)
    current = basket_binding(basket, registry=registry)
    try:
        current["unit_price_minor"] = store.get_product(sku).unit_price.minor
    except UnknownSkuError:
        # Delisted or withdrawn since the proposal. There is no price to compare, and that
        # is itself the answer: whatever the buyer was shown is no longer on sale.
        current["unit_price_minor"] = None
    if (
        current["basket_content_hash"] == expected.basket_content_hash
        and current["unit_price_minor"] == expected.unit_price_minor
        and current["catalogue_revision"] == expected.catalogue_revision
    ):
        return
    raise ProblemError(
        409,
        "That proposal is out of date",
        "The basket, the price or the catalogue moved after this was prepared, so it was "
        "not applied. Nothing changed. Here is what the store says now.",
        reason=SUPERSEDED,
        basket_id=str(basket.id),
        sku=sku,
        expected={
            "basket_content_hash": expected.basket_content_hash,
            "unit_price_minor": expected.unit_price_minor,
            "catalogue_revision": expected.catalogue_revision,
        },
        current=current,
    )


# ------------------------------------------------------------------------ wire bodies


def basket_body(basket: Basket, *, registry: MerchantRegistry) -> dict[str, Any]:
    """Re-quote the basket against live merchant state and render it for the wire.

    The stored quote is never returned. It exists so the surface can show what the buyer
    last saw; what this returns is what the merchant says now, which is the only figure a
    checkout may be built from.
    """
    store = registry.store(basket.merchant_id)
    lines = stored_lines(basket)
    result = quote_lines(lines, store)
    freshness = store.freshness()

    quote_out: QuoteOut | None = None
    unavailable: list[UnavailabilityOut] = []
    code = RecoveryCode.OK
    if result is not None:
        code = result.code
        unavailable = [UnavailabilityOut.of(item) for item in result.unavailable]
        if result.quote is not None:
            quote_out = QuoteOut.of(
                result.quote,
                content_hash=preview_content_hash(
                    result.quote,
                    basket_id=basket.id,
                    policy_version=registry.policy_version(),
                ),
            )

    body = BasketOut(
        basket_id=str(basket.id),
        lines=[
            BasketLineOut(sku=str(line["sku"]), quantity=int(line["quantity"])) for line in lines
        ],
        code=code,
        quote=quote_out,
        unavailable=unavailable,
        freshness=FreshnessOut.of(freshness),
        # Any difference counts, not only an increase: a scenario reset restores the
        # baseline without raising the revision, and the buyer's quote is still from a
        # different world than the one that would now be charged.
        stale=basket.catalogue_revision is not None and basket.catalogue_revision != store.revision,
    )
    return body.model_dump(mode="json")


# --------------------------------------------------------------------------- reads


def load_basket(
    session: Session, ctx: RequestContext, basket_id: uuid.UUID, *, lock: bool = False
) -> Basket:
    """The buyer's own basket, or 404.

    404 rather than 403 for a basket belonging to somebody else, for the same reason
    :func:`commerce_api.deps.assert_owner` does it: a 403 confirms the identifier exists
    and turns this endpoint into an oracle for enumerating other buyers' baskets.

    ``lock`` takes ``FOR UPDATE`` so a read-modify-write of the lines is serialized. The
    tenant predicate is written out even though row-level security already applies it:
    defence in depth, and it makes the intent readable at the call site.
    """
    statement = select(Basket).where(Basket.id == basket_id, Basket.tenant_id == ctx.tenant_id)
    if lock:
        statement = statement.with_for_update()
    basket = session.execute(statement).scalar_one_or_none()
    if basket is None or basket.buyer_ref != ctx.buyer_ref:
        raise ProblemError(
            404,
            "Basket not found",
            "No basket with that identifier belongs to this session.",
            basket_id=str(basket_id),
        )
    return basket


def lock_basket(session: Session, ctx: RequestContext, basket_id: uuid.UUID) -> Basket:
    """The buyer's basket, held ``FOR UPDATE`` for the rest of this transaction."""
    return load_basket(session, ctx, basket_id, lock=True)


def read_basket(
    session: Session, ctx: RequestContext, registry: MerchantRegistry, basket_id: uuid.UUID
) -> dict[str, Any]:
    """``GET /v1/baskets/{id}``: the basket re-quoted at current merchant state."""
    return basket_body(load_basket(session, ctx, basket_id), registry=registry)


# ------------------------------------------------------------------------ mutations


def create_basket(
    session: Session, ctx: RequestContext, registry: MerchantRegistry
) -> dict[str, Any]:
    """Open an empty basket for this session's buyer.

    The merchant comes from the session, never from the request: the bearer token says
    which merchant this buyer is shopping, and a body that could name another one would
    let a basket be priced against a merchant the buyer never chose.
    """
    basket = Basket(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        merchant_id=ctx.merchant_id,
        buyer_ref=ctx.buyer_ref,
        lines=[],
        quote=None,
        catalogue_revision=None,
        status="OPEN",
    )
    session.add(basket)
    # Flushed, not committed: the request-scoped dependency owns the transaction. The
    # flush is only so the server-side defaults are readable while building the response.
    session.flush()
    return basket_body(basket, registry=registry)


#: Checkout states a cart may be taken back from. Everything later has an order at the
#: provider or money in flight, and there the refusal is the honest answer.
_REOPENABLE_FROM: Final[frozenset[CheckoutState]] = frozenset(
    {CheckoutState.APPROVAL_REQUIRED, CheckoutState.APPROVED}
)


@dataclass(frozen=True, slots=True)
class _ReopenRefusal:
    """Why a closed cart stayed closed. Carried into the 409, never as ``basket_status``."""

    reason: str
    checkout_id: str | None = None
    checkout_state: str | None = None


def _reopen_for_edit(
    session: Session, ctx: RequestContext, basket: Basket
) -> _ReopenRefusal | None:
    """Take a cart back from its own checkout so the buyer can change it.

    ``None`` means the cart was reopened and the write may proceed; a refusal says why it
    was not.

    Why this exists. Opening a checkout closes the cart, and until now every later write
    answered 409 "start a new one". That reads as a small rule and behaves as a dead end:
    the assistant offers a second item at the approval card -- which is exactly where a
    shop would offer one -- the buyer says yes, and the add fails against a cart that has
    become a checkout. The buyer is left holding an approval for the order they no longer
    want and a suggestion they cannot accept.

    So the cart comes back, and the version they were looking at ends. Version N goes to
    ``INVALIDATED`` and its hold is released; the cart returns to ``OPEN``; the write lands;
    and the next checkout is version N+1 with its own content, its own hash and its own
    approval. Nothing the buyer already consented to is reused for a different total, which
    is the whole point of binding an approval to bytes.

    The line is drawn at ``EXECUTION_PENDING``. From there a grant is issued and a provider
    order may exist, and no edit to a cart can be allowed to reach around that.
    """
    checkout = session.execute(
        select(Checkout).where(Checkout.tenant_id == ctx.tenant_id, Checkout.basket_id == basket.id)
    ).scalar_one_or_none()
    if checkout is None:
        # Closed with no checkout behind it: abandoned, and not this function's to revive.
        return _ReopenRefusal("cart_abandoned")

    current = current_version(session, tenant_id=ctx.tenant_id, checkout_id=checkout.id)
    if current is None:
        return _ReopenRefusal("checkout_version_missing", str(checkout.id))
    if current.status not in _REOPENABLE_FROM:
        return _ReopenRefusal("payment_in_flight", str(checkout.id), current.status.value)

    # Ending a version the buyer was asked to approve is a consent act, and this route is
    # gated only by ``basket.write`` -- which agents hold precisely because building a cart
    # is theirs to do. Approving, rejecting and cancelling are withheld from them on the
    # stated principle that consent is not delegable to the thing that proposed the
    # purchase, and a line write that quietly retired a recorded approval would hand back
    # the reject the capability table refuses. So the supersede needs the buyer's own
    # authority; an agent asking for one more item still has to put the card in front of
    # a person.
    if not ctx.can("checkout.cancel"):
        return _ReopenRefusal(
            "approval_retirement_not_delegable", str(checkout.id), current.status.value
        )

    # The hold first: stock a buyer is no longer approving should not stay off the shelf
    # while they rebuild the cart. Released as CANCELLED, which the kernel accepts from
    # ACTIVE and from CONSUMED.
    release(
        session,
        checkout_id=checkout.id,
        checkout_version=current.version,
        cause=ReleaseCause.CANCELLED,
    )
    transition(
        session,
        tenant_id=ctx.tenant_id,
        checkout=current.ref,
        target=CheckoutState.INVALIDATED,
        reason="cart_reopened_by_buyer",
        actor=ctx.actor_type,
        correlation_id=ctx.correlation_id,
        principal_id=ctx.principal.principal_id,
    )
    basket.status = "OPEN"
    session.flush()
    return None


def set_line(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    basket_id: uuid.UUID,
    sku: str,
    quantity: int,
    expected: ExpectedBasket | None = None,
) -> dict[str, Any]:
    """Set one line to an absolute quantity; ``0`` removes it.

    Absolute rather than incremental on purpose. An increment is not idempotent, so a
    retried request would add the item twice; an absolute quantity retried is the same
    quantity, which is what makes ``Idempotency-Key`` meaningful here rather than
    decorative.

    The basket row is locked before its lines are read, so two surfaces editing one
    basket serialize instead of one silently overwriting the other.

    ``expected`` is optional and is the buyer's own +/- button's absence made explicit: a
    tap on the basket page has no proposal behind it and nothing to be stale against, so
    it sends none. A confirmation of something RazorAI proposed does send one, and
    :func:`_assert_unchanged` refuses it if the world moved in between. See
    :class:`ExpectedBasket`.
    """
    if quantity < 0 or quantity > MAX_LINE_QUANTITY:
        raise ProblemError(
            422,
            "Invalid quantity",
            f"quantity must be between 0 and {MAX_LINE_QUANTITY}; 0 removes the line.",
            field="quantity",
            quantity=quantity,
        )

    basket = lock_basket(session, ctx, basket_id)
    if basket.status != "OPEN":
        refusal = _reopen_for_edit(session, ctx, basket)
        if refusal is not None:
            # Deliberately WITHOUT ``basket_status``. The storefront reads any 409 carrying
            # that key as "this basket is gone" and recovers by opening a fresh basket and
            # replaying the single line into it (``use-basket.ts`` ``isBasketGone`` and
            # ``writeLine``) -- which, on a cart whose payment is in flight, would throw
            # away every other line the buyer had. This refusal means the opposite of gone:
            # the cart is intact and busy, and the surface must leave it alone.
            raise ProblemError(
                409,
                "Cart is being paid for",
                "This cart's checkout has already gone to payment and cannot be changed. "
                "Wait for the payment to finish, or start a new cart.",
                basket_id=str(basket_id),
                reason=refusal.reason,
                checkout_id=refusal.checkout_id,
                checkout_state=refusal.checkout_state,
            )

    store = registry.store(basket.merchant_id)
    if quantity > 0:
        # Resolve the SKU before storing it. A product identifier the merchant never
        # issued must fail loudly here rather than sit in a basket looking like an item
        # that happens to be out of stock (specification 20.1).
        try:
            store.get_product(sku)
        except UnknownSkuError:
            raise ProblemError(
                404,
                "Product not found",
                "No product with that SKU exists in this merchant's catalogue.",
                sku=sku,
            ) from None

    # After the lock and after the SKU resolves, before a single line is touched. Under
    # the lock so the comparison is against the state this write will actually land on,
    # and before the mutation so a refusal leaves the basket exactly as it was.
    if expected is not None:
        _assert_unchanged(basket, registry=registry, sku=sku, expected=expected)

    lines = {str(line["sku"]): int(line["quantity"]) for line in stored_lines(basket)}
    if quantity == 0:
        lines.pop(sku, None)
    else:
        lines[sku] = quantity
    if len(lines) > MAX_BASKET_LINES:
        raise ProblemError(
            422,
            "Basket too large",
            f"A basket may hold at most {MAX_BASKET_LINES} distinct products.",
            field="sku",
            lines=len(lines),
        )

    basket.lines = [{"sku": key, "quantity": value} for key, value in sorted(lines.items())]

    result = quote_lines(basket.lines, store)
    if result is not None and result.quote is not None:
        basket.quote = result.quote.to_checkout_content()
        basket.catalogue_revision = result.quote.freshness.catalogue_revision
    else:
        # An unpriceable or empty basket stores no quote. Keeping the previous one would
        # leave the surface showing a total for a basket that cannot currently be sold.
        basket.quote = None
        basket.catalogue_revision = store.revision if result is not None else None
    session.flush()
    return basket_body(basket, registry=registry)


def basket_quote_or_refuse(basket: Basket, registry: MerchantRegistry) -> Quote:
    """The priced basket, or a problem naming why it cannot be priced.

    Used by checkout construction, which cannot proceed on an offer that does not exist.
    The refusal carries the unavailable lines so the buyer surface can offer a
    substitution rather than only reporting failure.
    """
    lines = stored_lines(basket)
    if not lines:
        raise ProblemError(
            409,
            "Basket is empty",
            "Add at least one line before starting a checkout.",
            basket_id=str(basket.id),
        )
    try:
        result = quote_basket(
            [BasketLine(sku=str(line["sku"]), quantity=int(line["quantity"])) for line in lines],
            store=registry.store(basket.merchant_id),
        )
    except InvalidBasketError as exc:  # pragma: no cover - stored_lines already excludes these
        raise ProblemError(
            409, "Basket cannot be priced", str(exc), basket_id=str(basket.id)
        ) from exc
    if result.quote is None:
        raise ProblemError(
            409,
            "Basket cannot be priced",
            "One or more lines are unavailable at the requested quantity.",
            basket_id=str(basket.id),
            code=result.code.value,
            unavailable=[UnavailabilityOut.of(item).model_dump() for item in result.unavailable],
        )
    return result.quote

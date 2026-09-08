"""Cards: what a specialist may put on the buyer's or the merchant's screen.

ADR 0004 section 1.7 settled the shape and this module is the part that was missing. The
principle is one sentence: **the model selects, the server supplies.** A ``present_*``
tool takes identifiers and nothing else, and every fact on the resulting card is joined
here from the backend's own structured records. There is no argument through which a model
could hand us a price, a name, a total or a state, so there is nothing to validate away.

That is stricter than sanitising model output, and it is stricter for a reason worth
stating. A card is the most persuasive surface in the product: it looks like the platform
speaking rather than the assistant speaking, and a buyer reads a number on a card as a
fact about their money. If a model could populate one, every guarantee the kernel makes
upstream would be undone at the last inch.

Two consequences follow, and both are deliberate:

* **An identifier the session has not grounded is refused**, not rendered empty. The
  provenance gate has already established which SKUs, carts, checkouts, orders and
  proposals this session legitimately saw; a present call naming anything else is a model
  guessing, and a guessed identifier that renders as a blank card is worse than one that
  is visibly refused.
* **A card with nothing left after that refusal does not render.** Dropping the bad
  identifiers and drawing whatever survives would let a model widen a request and quietly
  keep the part that worked.

Merchant-authored text -- product names, descriptions, case notes -- is fenced before it
reaches a model and is sanitised again here, because a card is rendered as markup by two
different frontends and neither should have to think about it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import AdmissionDecision

from ..backends.base import (
    ApprovalCard,
    CartQuote,
    CartView,
    CheckoutView,
    OrderView,
    ProductCard,
)
from ..core.fencing import sanitize_label
from .money import display_amount, display_minor

__all__ = [
    "MAX_CARD_ITEMS",
    "MAX_CHIPS",
    "MAX_LABEL_CHARS",
    "NOT_MEASURED",
    "Chip",
    "approval_card",
    "cart_card",
    "decision_card",
    "plan_card",
    "product_card",
]

#: How long a label may be before it is cut with an ellipsis. A chip is read at a glance
#: and a card row sits in a fixed column, so the cap belongs here rather than in CSS: a
#: label that overflows on one surface and not another is a label nobody checked.
MAX_LABEL_CHARS: Final[int] = 64

#: Four, following the reference implementation. A chip row is a glance, and a glance that
#: needs scrolling has stopped being one. The cap is applied after sanitising so a model
#: cannot buy itself a fifth chip by making four of them empty.
MAX_CHIPS: Final[int] = 4

#: What a metrics row reads when the platform cannot derive its figure. Absent is not
#: zero: "0 refunds" and "we did not count refunds" are different answers, and a merchant
#: deciding whether to chase a refund backlog is entitled to know which one they are
#: looking at. A dash would be ambiguous in the other direction, so the card says it.
NOT_MEASURED: Final[str] = "not measured"

#: The most rows any card renders. A present call naming forty SKUs is a model dumping its
#: context onto the screen rather than choosing what matters; the overflow is reported as a
#: count so the surface can say "and 36 more" honestly instead of silently truncating.
MAX_CARD_ITEMS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class Chip:
    """A short label beside a card. Sanitised, never model-authored prose."""

    label: str
    tone: str = "neutral"

    def to_payload(self) -> dict[str, str]:
        return {"label": self.label, "tone": self.tone}


def _chips(*candidates: Chip | None) -> list[dict[str, str]]:
    """Sanitise, drop the empty ones, then cap. Order is the caller's priority order."""
    out: list[dict[str, str]] = []
    for chip in candidates:
        if chip is None:
            continue
        label = sanitize_label(chip.label, MAX_LABEL_CHARS)
        if not label:
            continue
        out.append({"label": label, "tone": chip.tone})
        if len(out) == MAX_CHIPS:
            break
    return out


def _envelope(kind: str, items: list[dict[str, Any]], **rest: Any) -> dict[str, Any]:
    """One shape for every card, so a frontend switches on ``kind`` and nothing else.

    ``count`` is the number selected and ``items`` is what fits, which lets a surface say
    how many were left out rather than pretending the page is the whole answer -- the same
    reason the order and refund collections report counts across the scope rather than
    across the page.
    """
    return {
        "ok": True,
        "card": {"kind": kind, "items": items[:MAX_CARD_ITEMS], "count": len(items), **rest},
    }


def _money(amount: Any) -> dict[str, Any]:
    """An amount as integer minor units plus the string a human reads. Never a float."""
    return {"minor": amount.minor, "currency": amount.currency, "display": display_amount(amount)}


# --------------------------------------------------------------------------- buyer cards


def product_card(products: list[ProductCard]) -> dict[str, Any]:
    """Products the buyer is choosing between.

    ``is_listed`` and ``is_available`` stay separate all the way to the screen. Sold out
    and delisted lead to different conversations -- one is worth waiting for and the other
    is not -- and collapsing them into a single boolean is how an assistant tells somebody
    "we do not sell that" about something that is back tomorrow.
    """
    items = [
        {
            "sku": product.sku,
            "name": sanitize_label(product.name, MAX_LABEL_CHARS),
            "unit_label": sanitize_label(product.unit_label, MAX_LABEL_CHARS),
            "unit_price": _money(product.unit_price),
            "is_listed": product.is_listed,
            "is_available": product.is_available,
            "availability": (
                "available"
                if product.is_available
                else ("sold_out" if product.is_listed else "delisted")
            ),
            "stock_units": product.stock_units,
            "catalogue_revision": product.provenance.catalogue_revision,
        }
        for product in products
    ]
    return _envelope("product", items)


def _quote_block(quote: CartQuote) -> dict[str, Any]:
    """Every money fact of a quote, each one the fee engine's own integer.

    The free-delivery gap is carried rather than derived. It is the one figure an agent is
    most tempted to compute -- subtract the total from a threshold and say the difference --
    and computing it is how an assistant promises free delivery at a number the fee engine
    would not honour.
    """
    return {
        "items_subtotal": _money(quote.items_subtotal),
        "items_tax": _money(quote.items_tax),
        "delivery_fee": _money(quote.delivery_fee),
        "delivery_tax": _money(quote.delivery_tax),
        "total": _money(quote.total),
        "free_delivery_applied": quote.free_delivery_applied,
        "gap_to_free_delivery": _money(quote.gap_to_free_delivery),
        "currency": quote.currency,
        "content_hash": quote.content_hash,
    }


def cart_card(view: CartView) -> dict[str, Any]:
    """The cart as the fee engine priced it, with the lines it could not price beside it.

    An unavailable line is shown rather than dropped. A cart that silently loses an item
    between one turn and the next is a cart the buyer will not trust, and the removal is
    a write they are entitled to make themselves.
    """
    quote = view.quote
    items = (
        []
        if quote is None
        else [
            {
                "sku": line.sku,
                "name": sanitize_label(line.name, MAX_LABEL_CHARS),
                "quantity": line.quantity,
                "unit_price": _money(line.unit_price),
                "subtotal": _money(line.subtotal),
                "tax": _money(line.tax),
            }
            for line in quote.lines
        ]
    )
    # The merchant simulator reports why a line could not be priced, and the distinction
    # survives to the screen: too few units left is a quantity the buyer can lower, and a
    # delisted product is not. Deriving one word from the two fields here keeps that
    # decision in one place rather than in each frontend.
    unavailable = [
        {
            "sku": line.sku,
            "requested": line.requested,
            "available_units": line.available_units,
            "listed": line.listed,
            "reason_key": (
                "delisted"
                if not line.listed
                else ("sold_out" if line.available_units == 0 else "insufficient_stock")
            ),
        }
        for line in view.unavailable
    ]
    return _envelope(
        "cart",
        items,
        cart_id=view.cart_id,
        quote=None if quote is None else _quote_block(quote),
        unavailable=unavailable,
        chips=_chips(
            Chip(f"{len(items)} items") if items else None,
            Chip("free delivery", "good")
            if quote is not None and quote.free_delivery_applied
            else None,
        ),
    )


def approval_card(card: ApprovalCard) -> dict[str, Any]:
    """The exact version the buyer is being asked to consent to, and where they do it.

    ``content_hash`` is on the card because consent binds to bytes rather than to a
    description of them: approving echoes this hash back, and a version that moved
    underneath the buyer is refused instead of silently approved.

    ``where`` is a constant, not a sentence the model composed. It is the one claim on
    this card that must never drift, because a card that named the wrong place to approve
    would be an invitation to approve somewhere that records no consent.
    """
    return _envelope(
        "approval",
        [
            {
                "sku": line.sku,
                "name": sanitize_label(line.name, MAX_LABEL_CHARS),
                "quantity": line.quantity,
                "subtotal": _money(line.subtotal),
            }
            for line in card.quote.lines
        ],
        checkout_id=card.checkout_id,
        version=card.version,
        content_hash=card.content_hash,
        status=str(card.status),
        quote=_quote_block(card.quote),
        total=_money(card.total),
        expires_at=None if card.expires_at is None else card.expires_at.isoformat(),
        where="trusted_surface",
        chips=_chips(
            Chip(f"version {card.version}"),
            Chip("approval required", "warn"),
        ),
    )


def decision_card(decision: AdmissionDecision, view: CheckoutView | None = None) -> dict[str, Any]:
    """What the kernel decided, and every field that moved if it refused.

    The deltas come from the decision rather than from the checkout, because the decision
    is the only object that holds both sides: what the buyer approved and what is current
    now. A checkout read afterwards shows the current version and has already forgotten
    what it superseded.

    The delta list is the point. A refusal saying only "something changed" asks the buyer
    to take the platform's word for it; one that names the field, the value they approved
    and the value now current lets them check. Nothing here is computed -- the kernel sent
    both sides of every delta, and this renders them.
    """
    deltas = [
        {
            "field_path": delta.field_path,
            "approved": delta.approved,
            "current": delta.current,
            "reason": delta.reason,
        }
        for delta in decision.deltas
    ]
    return _envelope(
        "decision",
        deltas,
        decision_id=str(decision.decision_id),
        allowed=decision.allowed,
        code=str(decision.code),
        explanation=decision.explanation,
        next_version=decision.next_version,
        checkout_id=None if view is None else view.checkout_id,
        current_version=None if view is None else view.current_version,
        total=None if view is None else _money(view.current.total),
        where="trusted_surface",
        chips=_chips(
            Chip("approved", "good") if decision.allowed else Chip("refused", "warn"),
            Chip(f"{len(deltas)} changed", "warn") if deltas else None,
            Chip(f"version {decision.next_version} required")
            if decision.next_version is not None
            else None,
        ),
    )


def plan_card(order: OrderView, *, steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A support plan against one order: what is verified, and what happens next.

    Every step is deterministic and produced by the resolution service, never by the
    model. The card says what the platform will do; it does not promise a timeline beyond
    the recorded target, and it offers no control that resolves anything, because in P0
    nothing in the product does.
    """
    return _envelope(
        "plan",
        steps or [],
        order_id=order.order_id,
        state=str(order.state),
        amount=_money(order.amount),
        payment_state=str(order.payment.state),
        capture_evidence=getattr(order.payment, "capture_evidence", None),
        refunds=[
            {
                "refund_id": refund.refund_id,
                "state": str(refund.state),
                "amount": _money(refund.amount),
            }
            for refund in order.refunds
        ],
        resolved_here=False,
        chips=_chips(
            Chip(str(order.state)),
            Chip(f"{len(order.refunds)} refunds") if order.refunds else None,
        ),
    )


# ------------------------------------------------------------------------ merchant cards


def _reading(row: Mapping[str, Any]) -> dict[str, Any]:
    """One row's figure, normalised: minor units, count, the string read, and whether measured.

    Deciding this here rather than in each caller is the whole guarantee. A row that
    arrives without a figure prints :data:`NOT_MEASURED` and carries ``measured: False``,
    so there is no way to build a metrics card that draws a zero for something nobody
    counted -- not even by accident, and not by a caller who passed the display string
    itself, because there is no longer a parameter for one.

    A ``bool`` is rejected as a figure although Python calls it an ``int``: ``True``
    rendering as the count ``1`` is a bug that would look like a measurement.
    """
    minor = row.get("value_minor")
    count = row.get("count")
    minor = minor if isinstance(minor, int) and not isinstance(minor, bool) else None
    count = count if isinstance(count, int) and not isinstance(count, bool) else None
    if minor is not None:
        display = display_minor(minor, str(row.get("currency", "INR")))
    elif count is not None:
        display = str(count)
    else:
        return {"value_minor": None, "count": None, "display": NOT_MEASURED, "measured": False}
    return {"value_minor": minor, "count": count, "display": display, "measured": True}

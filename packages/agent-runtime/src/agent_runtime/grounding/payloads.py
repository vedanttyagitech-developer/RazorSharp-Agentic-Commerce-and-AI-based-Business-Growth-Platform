"""Tool results as the model sees them: fenced merchant text beside structured facts.

Every builder here does three things at once and in one place, so they cannot drift:
fence the merchant-authored strings, record the structured facts in the turn's grounding
ledger, and produce a JSON-safe dict. Money appears twice on purpose -- as integer minor
units (the fact) and as a display string (what the model should copy into prose). The
model is told to copy the display string; the post-check verifies it did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from transaction_kernel import KernelDecision

from ..backends.base import (
    ApprovalCard,
    BasketQuote,
    BasketView,
    CheckoutView,
    OrderView,
    ProductCard,
    SearchPage,
)
from ..rendering.messages import reason_text, recovery_text, render_decision
from ..rendering.money import display_amount, display_minor
from .fence import UNTRUSTED_DATA_NOTICE, fence_untrusted

if TYPE_CHECKING:
    # Runtime import would be circular: turn.py imports the ledger this package exports.
    from ..turn import TurnContext

__all__ = [
    "approval_payload",
    "basket_payload",
    "cart_payload",
    "checkout_payload",
    "decision_payload",
    "order_payload",
    "product_payload",
    "search_payload",
]


def product_payload(card: ProductCard, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    merchant_text = card.name if not card.description else f"{card.name}. {card.description}"
    fenced = fence_untrusted(merchant_text)
    if fenced.suspicious:
        turn.record_flag(tool, card.sku, fenced.flags)
    turn.ledger.record_product(card)
    return {
        "sku": card.sku,
        "merchant_text": fenced.text,
        "quarantined": fenced.suspicious,
        "safe_label": f"{card.category} item {card.sku} ({card.unit_label})",
        "category": card.category,
        "unit_label": card.unit_label,
        "unit_price_minor": card.unit_price.minor,
        "unit_price_display": display_amount(card.unit_price),
        "currency": card.unit_price.currency,
        "stock_units": card.stock_units,
        "is_listed": card.is_listed,
        "is_available": card.is_available,
        **card.provenance.to_payload(),
    }


def search_payload(page: SearchPage, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    return {
        "untrusted_data_notice": UNTRUSTED_DATA_NOTICE,
        "query": page.query,
        "locale": page.locale.value,
        "result_count": len(page.hits),
        "items": [product_payload(card, turn, tool=tool) for card in page.hits],
        "allowed_skus": list(page.skus()),
        **page.provenance.to_payload(),
    }


def _quote_payload(quote: BasketQuote, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    turn.ledger.record_quote(quote)
    lines = []
    for line in quote.lines:
        fenced = fence_untrusted(line.name)
        if fenced.suspicious:
            turn.record_flag(tool, line.sku, fenced.flags)
        lines.append(
            {
                "sku": line.sku,
                "merchant_text": fenced.text,
                "quantity": line.quantity,
                "unit_price_minor": line.unit_price.minor,
                "unit_price_display": display_amount(line.unit_price),
                "subtotal_minor": line.subtotal.minor,
                "subtotal_display": display_amount(line.subtotal),
                "tax_bp": line.tax_bp,
                "tax_minor": line.tax.minor,
                "tax_display": display_amount(line.tax),
            }
        )
    payload: dict[str, Any] = {
        "currency": quote.currency,
        "lines": lines,
        "items_subtotal_minor": quote.items_subtotal.minor,
        "items_subtotal_display": display_amount(quote.items_subtotal),
        "items_tax_minor": quote.items_tax.minor,
        "items_tax_display": display_amount(quote.items_tax),
        "delivery_fee_minor": quote.delivery_fee.minor,
        "delivery_fee_display": display_amount(quote.delivery_fee),
        "delivery_tax_minor": quote.delivery_tax.minor,
        "delivery_tax_display": display_amount(quote.delivery_tax),
        "total_minor": quote.total.minor,
        "total_display": display_amount(quote.total),
        "free_delivery_applied": quote.free_delivery_applied,
        "gap_to_free_delivery_minor": quote.gap_to_free_delivery.minor,
        "gap_to_free_delivery_display": display_amount(quote.gap_to_free_delivery),
        "content_hash": quote.content_hash,
        **quote.provenance.to_payload(),
    }
    if quote.free_delivery_threshold is not None:
        payload["free_delivery_threshold_display"] = display_amount(quote.free_delivery_threshold)
    return payload


def basket_payload(view: BasketView, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    turn.ledger.record_basket(view)
    payload: dict[str, Any] = {
        "untrusted_data_notice": UNTRUSTED_DATA_NOTICE,
        "basket_id": view.basket_id,
        "cart_id": view.basket_id,
        "code": view.code.value,
        "code_text": recovery_text(view.code, turn.language),
        "lines": [{"sku": sku, "quantity": quantity} for sku, quantity in view.lines],
        "is_empty": view.is_empty,
        "stale": view.stale,
        "quote": None if view.quote is None else _quote_payload(view.quote, turn, tool=tool),
        "unavailable": [
            {
                "sku": line.sku,
                "requested": line.requested,
                "available_units": line.available_units,
                "listed": line.listed,
            }
            for line in view.unavailable
        ],
        **view.provenance.to_payload(),
    }
    return payload


cart_payload = basket_payload


def approval_payload(card: ApprovalCard, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    turn.ledger.record_approval(card)
    return {
        "untrusted_data_notice": UNTRUSTED_DATA_NOTICE,
        "checkout_id": card.checkout_id,
        "version": card.version,
        "content_hash": card.content_hash,
        "status": card.status.value,
        "approval_happens_on": "trusted buyer surface; this agent cannot approve",
        "total_minor": card.total.minor,
        "total_display": display_amount(card.total),
        "quote": _quote_payload(card.quote, turn, tool=tool),
        "expires_at": None if card.expires_at is None else card.expires_at.isoformat(),
    }


def checkout_payload(view: CheckoutView, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    turn.ledger.record_checkout(view)
    return {
        "untrusted_data_notice": UNTRUSTED_DATA_NOTICE,
        "checkout_id": view.checkout_id,
        "current_version": view.current_version,
        "versions": [approval_payload(card, turn, tool=tool) for card in view.versions],
        "payment": None
        if view.payment is None
        else {
            "attempt_id": view.payment.attempt_id,
            "state": view.payment.state,
            "is_captured": view.payment.state == "CAPTURED",
            "note": "Only CAPTURED is a completed payment. Never claim success otherwise.",
        },
    }


def order_payload(view: OrderView, turn: TurnContext, *, tool: str) -> dict[str, Any]:
    """An order as the agent may speak of it: verified state, exact amounts, refunds.

    ``payment.is_captured`` is the only success flag; ``capture_evidence`` names how the
    platform learned it, so an explanation quotes evidence rather than belief.
    """
    turn.ledger.record_order(view)
    return {
        "untrusted_data_notice": UNTRUSTED_DATA_NOTICE,
        "order_id": view.order_id,
        "checkout_id": view.checkout_id,
        "version": view.version,
        "content_hash": view.content_hash,
        "state": view.state.value,
        "amount_minor": view.amount.minor,
        "amount_display": display_amount(view.amount),
        "currency": view.amount.currency,
        "payment": {
            "attempt_id": view.payment.attempt_id,
            "state": view.payment.state,
            "is_captured": view.payment.is_captured,
            "capture_evidence": view.payment.capture_evidence,
            "note": "Only CAPTURED is a completed payment. Never claim success otherwise.",
        },
        "refunds": [
            {
                "refund_id": refund.refund_id,
                "amount_minor": refund.amount.minor,
                "amount_display": display_amount(refund.amount),
                "state": refund.state,
                "reason": refund.reason,
                "automatic": refund.automatic,
            }
            for refund in view.refunds
        ],
        "quote": None if view.quote is None else _quote_payload(view.quote, turn, tool=tool),
        "policy_receipt_hash": view.policy_receipt_hash,
        "refund_authority": (
            "Refund amounts come only from a resolution plan or a provider record. "
            "This agent cannot issue, promise or compute one."
        ),
    }


def decision_payload(
    decision: KernelDecision, turn: TurnContext, *, currency: str = "INR"
) -> dict[str, Any]:
    turn.ledger.record_decision(decision, currency)
    turn.decisions.append(decision)
    rendered = render_decision(decision, turn.language, currency=currency)
    return {
        "decision_id": str(decision.decision_id),
        "allowed": decision.allowed,
        "code": decision.code.value,
        "code_text": recovery_text(decision.code, turn.language),
        "explanation": decision.explanation,
        "checkout": None
        if decision.checkout is None
        else {
            "checkout_id": str(decision.checkout.checkout_id),
            "version": decision.checkout.version,
            "content_hash": decision.checkout.content_hash,
        },
        "deltas": [
            {
                "field_path": delta.field_path,
                "approved": delta.approved,
                "current": delta.current,
                "reason": delta.reason,
                "reason_text": reason_text(delta.reason, turn.language),
                "approved_display": _display_value(delta.field_path, delta.approved, currency),
                "current_display": _display_value(delta.field_path, delta.current, currency),
            }
            for delta in decision.deltas
        ],
        "next_version": decision.next_version,
        "payment_attempt_id": None
        if decision.payment_attempt_id is None
        else str(decision.payment_attempt_id),
        "payment_status": "not a payment; admission only" if decision.allowed else "no payment",
        "rendered_for_buyer": rendered,
        "include_rendered_verbatim": True,
    }


def _display_value(field_path: str, value: Any, currency: str) -> str:
    if value is None:
        return "—"
    if field_path.endswith("_minor") and isinstance(value, int) and not isinstance(value, bool):
        return display_minor(value, currency)
    return str(value)

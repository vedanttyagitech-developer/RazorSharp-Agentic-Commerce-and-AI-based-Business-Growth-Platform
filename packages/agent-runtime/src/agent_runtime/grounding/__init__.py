"""Grounding, hallucination and prompt-injection defenses (specification 20).

Three lines, in the order a fact travels: the fence (``fence.py``) decides what the model
is told; the payload builders (``payloads.py``) record what a tool actually returned on the
turn ledger (``ledger.py``); the post-check (``postcheck.py``) reads the reply back against
that ledger and removes what no tool said.
"""

from .fence import (
    DATA_BEGIN,
    DATA_END,
    UNTRUSTED_DATA_NOTICE,
    WITHHELD,
    FencedText,
    fence_untrusted,
    sanitize,
    scan,
)
from .ledger import GroundedProduct, GroundingLedger
from .payloads import (
    approval_payload,
    basket_payload,
    cart_payload,
    checkout_payload,
    decision_payload,
    order_payload,
    product_payload,
    search_payload,
)
from .postcheck import (
    ReplyCheck,
    extract_amounts_minor,
    extract_skus,
    extract_stock_counts,
    verify_reply,
)

__all__ = [
    "DATA_BEGIN",
    "DATA_END",
    "UNTRUSTED_DATA_NOTICE",
    "WITHHELD",
    "FencedText",
    "GroundedProduct",
    "GroundingLedger",
    "ReplyCheck",
    "approval_payload",
    "basket_payload",
    "cart_payload",
    "checkout_payload",
    "decision_payload",
    "extract_amounts_minor",
    "extract_skus",
    "extract_stock_counts",
    "fence_untrusted",
    "order_payload",
    "product_payload",
    "sanitize",
    "scan",
    "search_payload",
    "verify_reply",
]

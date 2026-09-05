"""Deterministic buyer-facing text: recovery codes, deltas, denials and money.

Specification 3.17: approval, total, payment and refund speech uses deterministic
templates. The model may add prose around these blocks; it may not replace them.
"""

from .messages import (
    REASON_TEXT,
    RECOVERY_TEXT,
    reason_text,
    recovery_text,
    render_decision,
    render_denial,
    render_fallback,
    render_reasoning_unavailable,
    render_unverified,
)
from .money import display_amount, display_minor

__all__ = [
    "REASON_TEXT",
    "RECOVERY_TEXT",
    "display_amount",
    "display_minor",
    "reason_text",
    "recovery_text",
    "render_decision",
    "render_denial",
    "render_fallback",
    "render_reasoning_unavailable",
    "render_unverified",
]

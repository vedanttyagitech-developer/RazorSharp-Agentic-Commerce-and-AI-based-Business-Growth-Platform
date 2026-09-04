"""Compatibility surface over :mod:`agent_runtime.core.fencing`.

The fence moved to ``core`` (ADR 0004 §1.1) so that the sanitiser, the grounding rules
and the provenance record live together with no runtime import. The names below are the
ones ``grounding/payloads.py`` and the capability factory already use; they are kept so
that a module written against the old surface keeps working while it migrates. New code
imports :data:`~agent_runtime.core.fencing.MERCHANT_DATA_FENCE` directly.

There is exactly one fence implementation. This module defines none of its own.
"""

from __future__ import annotations

from typing import Final

from ..core.fencing import MERCHANT_DATA_FENCE, WITHHELD, FencedText, scan

__all__ = [
    "DATA_BEGIN",
    "DATA_END",
    "UNTRUSTED_DATA_NOTICE",
    "WITHHELD",
    "FencedText",
    "fence_untrusted",
    "sanitize",
    "scan",
]

DATA_BEGIN: Final[str] = MERCHANT_DATA_FENCE.open
DATA_END: Final[str] = MERCHANT_DATA_FENCE.close
UNTRUSTED_DATA_NOTICE: Final[str] = MERCHANT_DATA_FENCE.notice


def sanitize(text: str) -> str:
    """One merchant string, stripped and bounded, without the markers."""
    return MERCHANT_DATA_FENCE.sanitize_text(text, max_chars=400)


def fence_untrusted(text: str) -> FencedText:
    """Fence one merchant string on the buyer surface; withhold it if it reads as an order."""
    return MERCHANT_DATA_FENCE.fence_text(text)

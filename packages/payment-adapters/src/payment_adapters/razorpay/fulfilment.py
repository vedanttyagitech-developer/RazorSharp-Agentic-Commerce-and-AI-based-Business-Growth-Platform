"""The gate between "a payment happened" and "give the buyer the goods".

ADR 0003 D2: the fulfilment vocabulary lives in :mod:`transaction_kernel.evidence`, because
the kernel is the component that decides whether a capture may be applied and must not
import an adapter to name its own decision. This module re-exports it unchanged so that
``payment_adapters.razorpay.fulfilment`` and the package-level re-exports keep working;
the enum values are identical to what this module defined before the move, since
``payments.EVIDENCE_SOURCE`` is compared against them by string.
"""

from __future__ import annotations

from transaction_kernel.evidence import (
    CaptureEvidence,
    may_fulfil,
    requires_release_not_capture,
)

__all__ = ["CaptureEvidence", "may_fulfil", "requires_release_not_capture"]

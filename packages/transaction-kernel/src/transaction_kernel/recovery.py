"""Structured recovery codes, specification 6.7.

Every deterministic service returns one of these. An agent may translate a code into
conversation, but may never override it, invent one, or act on a code it does not
recognise. This is the contract that keeps the model's fluency separated from the
platform's authority: the code carries the decision, the prose carries only the
explanation.

The enum is closed on purpose. A new failure mode gets a new member here, reviewed once,
rather than a free-text reason string that each agent interprets differently.
"""

from __future__ import annotations

from enum import StrEnum


class RecoveryCode(StrEnum):
    """Outcome of a deterministic operation, and what the caller may do next."""

    OK = "OK"

    # --- idempotency and concurrency -------------------------------------
    DUPLICATE_OPERATION = "DUPLICATE_OPERATION"
    CONCURRENT_OPERATION = "CONCURRENT_OPERATION"

    # --- freshness and consent -------------------------------------------
    STALE_CHECKOUT = "STALE_CHECKOUT"
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"

    # --- authority --------------------------------------------------------
    AUTHORITY_REVOKED = "AUTHORITY_REVOKED"
    AUTHORITY_INSUFFICIENT = "AUTHORITY_INSUFFICIENT"

    # --- payment ----------------------------------------------------------
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAYMENT_UNKNOWN = "PAYMENT_UNKNOWN"
    STALE_CAPTURE = "STALE_CAPTURE"

    # --- remedies ---------------------------------------------------------
    REFUND_ALLOWED = "REFUND_ALLOWED"
    REFUND_REVIEW_REQUIRED = "REFUND_REVIEW_REQUIRED"
    RESOLUTION_PLAN_ISSUED = "RESOLUTION_PLAN_ISSUED"
    RESOLUTION_PLAN_EXPIRED = "RESOLUTION_PLAN_EXPIRED"
    RECONCILIATION_IN_PROGRESS = "RECONCILIATION_IN_PROGRESS"

    # --- escalation -------------------------------------------------------
    POLICY_EXCEPTION = "POLICY_EXCEPTION"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"

    # --- operating mode ---------------------------------------------------
    SAFE_MODE_ACTIVE = "SAFE_MODE_ACTIVE"


#: Codes after which the caller may retry the same logical operation, subject to policy.
#: PAYMENT_UNKNOWN is deliberately absent: an unknown outcome is reconciled, never retried.
RETRYABLE: frozenset[RecoveryCode] = frozenset(
    {
        RecoveryCode.CONCURRENT_OPERATION,
        RecoveryCode.PAYMENT_FAILED,
        RecoveryCode.RESERVATION_EXPIRED,
    }
)

#: Codes that require fresh buyer consent before anything else can happen.
NEEDS_REAPPROVAL: frozenset[RecoveryCode] = frozenset(
    {
        RecoveryCode.REAPPROVAL_REQUIRED,
        RecoveryCode.STALE_CHECKOUT,
    }
)

#: Codes that must never be presented to a buyer as a completed money action.
NOT_A_SUCCESS: frozenset[RecoveryCode] = frozenset(
    set(RecoveryCode) - {RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION}
)

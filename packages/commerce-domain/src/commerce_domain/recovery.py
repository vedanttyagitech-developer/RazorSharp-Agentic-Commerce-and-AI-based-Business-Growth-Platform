"""Structured recovery codes, specification 6.7.

Every deterministic service returns one of these. An agent may translate a code into
conversation, but may never override it, invent one, or act on a code it does not
recognise. This is the contract that keeps the model's fluency separated from the
platform's authority: the code carries the decision, the prose carries only the
explanation.

The enum is closed on purpose. A new failure mode gets a new member here, reviewed once,
rather than a free-text reason string that each agent interprets differently.

It lives in the domain package rather than in the kernel because of who has to read it. The
buyer copilot renders every one of these into a sentence, and the voice runtime renders each
into speech; neither decides anything, and neither may be able to reach the code that does.
While this enum sat in the kernel, the only thing stopping a model-facing package from
calling ``admit`` was that nobody had written the line -- the import was already declared.
Nothing about the values changed in the move: ``str()`` on a ``StrEnum`` member returns the
value, never the module, so no hash and no stored row depends on where this file sits.
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
    #: Nothing the buyer approved can still be sold. Distinct from
    #: ``REAPPROVAL_REQUIRED`` because there is nothing to re-approve: that code always
    #: names a superseding version, and this one never can. The caller's next step is to
    #: start again, not to look at a replacement.
    SOLD_OUT = "SOLD_OUT"

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

    # --- upstream dependencies --------------------------------------------
    # A source of authoritative merchant truth could not answer, so the operation stopped
    # before anything was decided. Distinct from every code above it: nothing about the
    # request, the buyer's consent or the platform's own state was found wanting, and the
    # buyer has nothing to fix. Distinct from SAFE_MODE_ACTIVE too -- that is the platform
    # refusing on purpose and clears only when an operator says so, whereas this clears
    # when the upstream comes back.
    CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"

    # --- operating mode ---------------------------------------------------
    SAFE_MODE_ACTIVE = "SAFE_MODE_ACTIVE"


#: Codes after which the caller may retry the same logical operation, subject to policy.
#: PAYMENT_UNKNOWN is deliberately absent: an unknown outcome is reconciled, never retried.
#: CONNECTOR_UNAVAILABLE is present for the opposite reason -- the operation stopped before
#: it decided anything, so there is no ambiguous state for a second attempt to collide with,
#: and the condition clears on its own. SAFE_MODE_ACTIVE stays absent even though it is
#: also a temporary condition, because it clears only when an operator ends the incident
#: and retrying against it is a way of waiting that nobody can see.
RETRYABLE: frozenset[RecoveryCode] = frozenset(
    {
        RecoveryCode.CONCURRENT_OPERATION,
        RecoveryCode.CONNECTOR_UNAVAILABLE,
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

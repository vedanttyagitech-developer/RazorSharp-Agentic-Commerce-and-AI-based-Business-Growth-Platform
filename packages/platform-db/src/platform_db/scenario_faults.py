"""The one statement that consumes a demo fault, in the one place both consumers share.

``scenario_faults`` is a table of single-use demonstration intentions: an operator arms a
row, exactly one consumer takes it, and it is gone. Two processes now consume from it --
the durable worker, before a Razorpay call, and the API, before an agent turn -- and the
correctness of "exactly once" lives entirely in the SQL below. A second copy of that
statement in the second consumer would be a copy that drifts, and the drift would be
invisible: both copies would keep working, and only the property they exist to guarantee
would quietly stop being true. So the statement lives here, in the package both consumers
already depend on, and neither owns it.

This module is deliberately ignorant of *which* faults exist. ``kind`` is a string
because ``scenario_faults.kind`` is a string: the worker's closed set of provider timeouts
and the API's set of reasoning and speech failures are different vocabularies, each
meaningful only to the process that consumes it, and teaching this module both of them
would make it the place where the two have to agree about something they never share.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from .tenancy import require_tenant

__all__ = [
    "ClaimedFault",
    "claim_scenario_fault",
]


@dataclass(frozen=True, slots=True)
class ClaimedFault:
    """One fault that this transaction has just taken, and no other transaction can."""

    fault_id: uuid.UUID
    kind: str
    checkout_id: uuid.UUID | None
    payment_attempt_id: uuid.UUID | None


#: Disarm and return in one statement. The subquery takes the oldest armed row that
#: targets this checkout (or the whole tenant), locked ``FOR UPDATE SKIP LOCKED`` so a
#: concurrent consumer steps over it instead of waiting and then consuming it a second
#: time. Selecting first and updating afterwards would be the same code with a race in
#: the gap.
_CLAIM: Final = text(
    """
    UPDATE scenario_faults AS f
       SET armed = false,
           consumed_at = now(),
           payment_attempt_id = COALESCE(f.payment_attempt_id, :attempt)
     WHERE f.id = (
             SELECT c.id
               FROM scenario_faults AS c
              WHERE c.tenant_id = :tenant
                AND c.armed
                AND c.kind = :kind
                AND (c.checkout_id IS NULL OR c.checkout_id = :checkout)
                AND (c.payment_attempt_id IS NULL OR c.payment_attempt_id = :attempt)
              ORDER BY c.created_at
                FOR UPDATE SKIP LOCKED
              LIMIT 1
           )
    RETURNING f.id, f.kind, f.checkout_id, f.payment_attempt_id
    """
)


def claim_scenario_fault(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    checkout_id: uuid.UUID | None = None,
    payment_attempt_id: uuid.UUID | None = None,
) -> ClaimedFault | None:
    """Consume one armed fault of ``kind``, or return ``None`` when none is armed.

    Needs a role holding UPDATE on ``scenario_faults`` -- the worker or the kernel, never
    the app, which may only arm. The caller commits: a fault consumed in a transaction
    that rolls back is re-armed with it, which keeps the injection and whatever decision
    it caused atomic.

    Refuses a session with no tenant bound. Row-level security would match nothing and
    report "no fault armed" for every call, which is exactly the silent failure this
    apparatus exists to make visible.
    """
    bound = require_tenant(session)
    if bound != tenant_id:
        raise ValueError(f"fault claim names tenant {tenant_id}, transaction is bound to {bound}")
    row = session.execute(
        _CLAIM,
        {
            "tenant": tenant_id,
            "kind": kind,
            "checkout": checkout_id,
            "attempt": payment_attempt_id,
        },
    ).one_or_none()
    if row is None:
        return None
    return ClaimedFault(
        fault_id=row.id,
        kind=row.kind,
        checkout_id=row.checkout_id,
        payment_attempt_id=row.payment_attempt_id,
    )

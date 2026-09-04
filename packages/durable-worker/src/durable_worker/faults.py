"""Worker-side fault injection for the demonstration (ADR 0003 D11).

Step 9 of the demonstration has to be able to show what happens when Razorpay's answer is
lost: the attempt goes ``UNKNOWN``, the checkout goes ``PAYMENT_UNKNOWN``, reconciliation
is enqueued, and no second order is ever created. Waiting for a real timeout is not a
demonstration, so the scenario controller arms a row in ``scenario_faults`` and this
module is where the worker consults it.

Two properties make this apparatus rather than a hole in the payment path.

**A fault fires instead of the provider call, never alongside it.** The claim is a single
``UPDATE ... RETURNING`` that disarms the row, committed in the worker's own transaction
*before* the send would have happened. If it returns a row, no request is sent at all, so
"exactly one provider request per consumed grant" stays literally true and the recorded
``provider_requests`` row carries a transport error rather than a fabricated HTTP status.

**A fault is consumed exactly once.** The candidate is selected ``FOR UPDATE SKIP
LOCKED`` and disarmed in the same statement, so two workers racing on one armed row
produce one injection, not two. An armed fault that is never reached simply stays armed
and is visible to an operator, which is the honest failure direction for demo apparatus.

The rows are never read in the production profile
(:attr:`~durable_worker.settings.WorkerSettings.scenario_faults_enabled`): a fault that
could fire against live credentials would be a way to make a real payment call disappear.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from platform_db import require_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = [
    "ArmedFault",
    "FaultKind",
    "claim_fault",
]


class FaultKind(StrEnum):
    """What the scenario controller may arm. A closed set, matched by exact value.

    Each member names the provider call it replaces, so a reader of a ``scenario_faults``
    row can tell which network call never happened without consulting this module.
    """

    #: ``POST /v1/orders`` never leaves. The attempt becomes ``UNKNOWN`` and reconciles
    #: by receipt -- specification 10.6 and 10.7, and the heart of step 9.
    CREATE_ORDER_TIMEOUT = "CREATE_ORDER_TIMEOUT"

    #: ``POST /v1/payments/{id}/refund`` never leaves. The refund becomes
    #: ``REFUND_UNKNOWN``, which reconciles and never retries blindly.
    REFUND_TIMEOUT = "REFUND_TIMEOUT"

    #: A reconciliation read never leaves, so the bounded-attempts path (ADR D13) can be
    #: shown ending in an escalation rather than in a silent loop.
    RECONCILE_FETCH_TIMEOUT = "RECONCILE_FETCH_TIMEOUT"


@dataclass(frozen=True, slots=True)
class ArmedFault:
    """One fault this worker has just consumed. Returned only to the caller that won it."""

    fault_id: uuid.UUID
    kind: FaultKind
    checkout_id: uuid.UUID | None
    payment_attempt_id: uuid.UUID | None

    @property
    def transport_error(self) -> str:
        """The value recorded in ``provider_requests.transport_error``.

        Names the injection rather than imitating a real ``TransportTimeoutError``: the
        evidence must say that this request was never sent, or the proof chain lies about
        what the platform did.
        """
        return f"ScenarioFault:{self.kind.value}"[:64]


#: Disarm and return in one statement. The subquery takes the oldest armed row that
#: targets this checkout (or the whole tenant), locked ``SKIP LOCKED`` so a concurrent
#: worker steps over it instead of waiting and then consuming it a second time.
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


def claim_fault(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    kind: FaultKind,
    checkout_id: uuid.UUID | None = None,
    payment_attempt_id: uuid.UUID | None = None,
) -> ArmedFault | None:
    """Consume one armed fault of ``kind``, or return ``None``.

    Runs on the **worker** role, which holds UPDATE on ``scenario_faults`` and no write
    privilege on any financial table, so an injection can never be mistaken for a state
    change. The caller commits: a fault consumed in a transaction that rolls back is
    re-armed with it, which keeps the injection and the decision it caused atomic.

    Refuses a session with no tenant bound -- row-level security would match nothing and
    report "no fault armed" for every call, which is the silent failure this apparatus
    exists to make visible.
    """
    bound = require_tenant(session)
    if bound != tenant_id:
        raise ValueError(f"fault claim names tenant {tenant_id}, transaction is bound to {bound}")
    row = session.execute(
        _CLAIM,
        {
            "tenant": tenant_id,
            "kind": kind.value,
            "checkout": checkout_id,
            "attempt": payment_attempt_id,
        },
    ).one_or_none()
    if row is None:
        return None
    return ArmedFault(
        fault_id=row.id,
        kind=FaultKind(row.kind),
        checkout_id=row.checkout_id,
        payment_attempt_id=row.payment_attempt_id,
    )

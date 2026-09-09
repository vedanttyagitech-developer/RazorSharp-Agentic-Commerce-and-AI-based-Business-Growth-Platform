"""Reserve allocation settlement, and the one writer of the simulator's recorded outcome.

Unknown stays held; terminal failure releases once.

``record_simulation_outcome`` is here rather than in the API for the reason ADR 0003 D1
gives: ``payment_attempts`` is a financial table and only this package writes one. Two
callers had the ``UPDATE`` inline -- the admission service and the protected simulator
route -- which is what ``test_capi_boundary`` refuses. The behaviour is unchanged; the
statement moved to the side of the boundary that is allowed to run it.
"""

from __future__ import annotations

import uuid

from commerce_domain import ActorType
from platform_db import require_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import authority
from .audit import append

#: The answers the demo simulator may be told to give.
_SIMULATION_OUTCOMES = frozenset({"captured", "failed", "unknown"})


def record_simulation_outcome(
    session: Session,
    attempt_id: uuid.UUID,
    *,
    outcome: str,
    only_while_held: bool = False,
) -> bool:
    """Record which answer the *simulated* provider will give for this attempt.

    This writes a demo fixture, not provider evidence: nothing here observes a payment, and
    the Action Executor still has to run and apply the outcome through the ordinary payment
    lifecycle. It lives in the Kernel only because the column is on a financial table.

    ``only_while_held`` restricts the write to an attempt whose allocation is still HELD,
    which is what the operator-protected simulator route needs: an attempt that has already
    settled has an answer, and overwriting it would rewrite history rather than choose a
    future. Returns whether a row was actually changed, so a caller can tell "recorded" from
    "too late".
    """
    if outcome not in _SIMULATION_OUTCOMES:
        raise authority.AuthorityError(
            f"{outcome!r} is not a simulated provider outcome; expected one of "
            f"{sorted(_SIMULATION_OUTCOMES)}"
        )
    tenant = require_tenant(session)
    statement = (
        "UPDATE payment_attempts SET reserve_simulation_outcome=:s WHERE tenant_id=:t AND id=:p"
    )
    if only_while_held:
        statement += " AND reserve_allocation='HELD'"
    changed = session.execute(
        text(f"{statement} RETURNING id"), {"s": outcome, "t": tenant, "p": attempt_id}
    ).scalar_one_or_none()
    return changed is not None


def settle_allocation(
    session: Session, attempt_id: uuid.UUID, *, correlation_id: uuid.UUID
) -> None:
    """Derive settlement from persisted financial state, never from a caller's boolean.

    The attempt is the allocation identity. Repeating settlement cannot restore capacity twice.
    Revocation remains revoked even when an unsuccessful debit releases its allocation.
    """
    tenant = require_tenant(session)
    row = session.execute(
        text("SELECT * FROM payment_attempts WHERE tenant_id=:t AND id=:p FOR UPDATE"),
        {"t": tenant, "p": attempt_id},
    ).one()
    if row.reserve_authority_id is None or row.reserve_allocation != "HELD":
        return
    if row.status in ("CAPTURED", "STALE_CAPTURE", "AUTO_REFUND_PENDING"):
        allocation = "SPENT"
    elif row.status == "FAILED":
        allocation = "RELEASED"
        authority.lock_authority(session, row.reserve_authority_id)
        changed = session.execute(
            text(
                "UPDATE delegated_authorities "
                "SET consumed_amount_minor=consumed_amount_minor-:amount, "
                "status=CASE WHEN status='EXHAUSTED' THEN 'ACTIVE' ELSE status END "
                "WHERE tenant_id=:t AND id=:a AND consumed_amount_minor>=:amount RETURNING id"
            ),
            {"t": tenant, "a": row.reserve_authority_id, "amount": row.amount_minor},
        ).scalar_one_or_none()
        if changed is None:
            raise authority.AuthorityError("Reserve allocation accounting mismatch")
    else:
        return
    session.execute(
        text("UPDATE payment_attempts SET reserve_allocation=:s WHERE tenant_id=:t AND id=:p"),
        {"s": allocation, "t": tenant, "p": attempt_id},
    )
    append(
        session,
        tenant=tenant,
        aggregate_type="checkout",
        aggregate_id=row.checkout_id,
        event_type="reserve.allocation_settled",
        actor_type=ActorType.WORKER,
        principal_id="reserve-executor",
        correlation_id=correlation_id,
        payload={
            "payment_attempt_id": str(attempt_id),
            "allocation": allocation,
            "authority_id": str(row.reserve_authority_id),
            "amount_minor": row.amount_minor,
        },
    )

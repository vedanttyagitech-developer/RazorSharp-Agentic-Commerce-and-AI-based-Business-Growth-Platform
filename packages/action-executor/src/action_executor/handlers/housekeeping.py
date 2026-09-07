"""Periodic sweeps. Hygiene, never a correctness authority.

Specification invariant 11 is explicit: reservation validity is checked synchronously
against the database clock, and cleanup workers are not correctness authorities. The same
is true of every sweep here. ``consume_grant`` re-evaluates ``expires_at`` under the row
lock, ``consume_recorded`` refuses a lapsed approval, and the capacity check already
counts an expired hold as holding nothing. If this never ran, no money would move that
should not.

What it buys is a database whose ``status`` columns match reality, and one thing that is
more than cosmetic: a past-due grant still reads ``ISSUED``, and an ``ISSUED`` row holds
the single live slot on its payment attempt, so until the sweep runs that attempt cannot
be re-admitted by anybody. Failing to sweep fails closed -- nothing charges -- but it does
strand the buyer, which is why this is on a timer rather than on a wish.

Two roles, because the tables have two owners: the three kernel sweeps write financial
tables and run as ``commerce_kernel``; reaping exhausted outbox commands writes
``outbox_events`` and runs as ``commerce_worker``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from commerce_domain import uuid7
from durable_work import DeadLetter, reap_exhausted
from platform_db import set_tenant
from transaction_kernel import approvals, grants, reservations

from ..settings import WorkerRuntime

__all__ = ["HousekeepingReport", "run_housekeeping"]


@dataclass(frozen=True, slots=True)
class HousekeepingReport:
    """What one sweep touched, per tenant. Counts, so a log line says whether it mattered."""

    grants_expired: int = 0
    reservations_swept: int = 0
    approvals_expired: int = 0
    commands_reaped: int = 0

    @property
    def touched(self) -> int:
        return (
            self.grants_expired
            + self.reservations_swept
            + self.approvals_expired
            + self.commands_reaped
        )


def run_housekeeping(
    runtime: WorkerRuntime,
    *,
    tenant_id: uuid.UUID,
    on_dead: Callable[[DeadLetter], None] | None = None,
) -> HousekeepingReport:
    """Run every sweep once for one tenant.

    Each role gets its own transaction rather than one shared connection: the kernel
    sweeps and the outbox reap have nothing to be atomic about between them, and running
    the outbox reap under the kernel role would silently prove nothing about the grant
    boundary this deployment relies on.

    ``on_dead`` runs inside the reaping transaction, so a failure to record a burial rolls
    the burial back rather than losing the command quietly.
    """
    correlation_id = uuid7()
    with runtime.kernel_session() as session:
        set_tenant(session, tenant_id)
        expired_grants = grants.expire_stale_grants(session)
        swept = reservations.sweep_expired(session)
        expired_approvals = approvals.expire_stale_approvals(
            session, tenant_id=tenant_id, correlation_id=correlation_id
        )

    with runtime.worker_session() as session:
        set_tenant(session, tenant_id)
        reaped = reap_exhausted(session, policy=runtime.retry_policy, on_dead=on_dead)

    return HousekeepingReport(
        grants_expired=len(expired_grants),
        reservations_swept=swept,
        approvals_expired=expired_approvals,
        commands_reaped=len(reaped),
    )

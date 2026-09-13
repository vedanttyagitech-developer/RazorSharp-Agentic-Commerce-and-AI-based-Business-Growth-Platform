"""Best-effort counters published only after the owning database transaction commits."""

from __future__ import annotations

import uuid
from typing import Any

from platform_observability.instruments import default_registry
from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

_KEY = "kernel_pending_metrics"


def increment(session: Session, tenant: uuid.UUID, name: str, **labels: object) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("business metrics require a transaction")
    pending = session.info.setdefault(_KEY, {})
    pending.setdefault(transaction, []).append((str(tenant), name, labels, None))


def observe(session: Session, tenant: uuid.UUID, name: str, value: float, **labels: object) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("business metrics require a transaction")
    pending = session.info.setdefault(_KEY, {})
    pending.setdefault(transaction, []).append((str(tenant), name, labels, value))


@event.listens_for(Session, "after_commit")
def _commit(session: Session) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info.get(_KEY, {})
    records = pending.pop(transaction, [])
    if transaction is not None and transaction.parent is not None:
        pending.setdefault(transaction.parent, []).extend(records)
        return
    session.info.pop(_KEY, None)
    for tenant, name, labels, value in records:
        metrics = default_registry().for_tenant(tenant)
        if value is None:
            metrics.increment(name, **labels)
        else:
            metrics.observe(name, value, **labels)


@event.listens_for(Session, "after_soft_rollback")
def _rollback(session: Session, transaction: SessionTransaction) -> None:
    pending: dict[Any, Any] = session.info.get(_KEY, {})
    pending.pop(transaction, None)
    if transaction.parent is None:
        session.info.pop(_KEY, None)


@event.listens_for(Session, "after_transaction_end")
def _end(session: Session, transaction: SessionTransaction) -> None:
    # Session.close() also ends a transaction, without necessarily emitting rollback.
    pending = session.info.get(_KEY, {})
    pending.pop(transaction, None)
    if transaction.parent is None:
        session.info.pop(_KEY, None)

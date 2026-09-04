"""Transaction-local tenant context for row-level security.

The failure this module exists to prevent: connection pools reuse connections across
requests. A tenant set with session scope (``SET app.tenant_id``) survives the request
that set it and leaks into whichever tenant borrows that connection next. The leak is
silent, intermittent, and looks like corrupted data rather than an authorization bug.

So the tenant is always set with ``is_local => true``, which binds it to the current
transaction and discards it at COMMIT or ROLLBACK. There is no API here that can set a
session-scoped tenant.

``set_config`` is used rather than ``SET LOCAL`` because ``SET LOCAL`` takes a literal,
which would mean interpolating the tenant into SQL text. ``set_config`` takes a bound
parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

TENANT_SETTING = "app.tenant_id"

_SET_TENANT = text(f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)")
_READ_TENANT = text(f"SELECT current_setting('{TENANT_SETTING}', true)")


class TenantContextError(RuntimeError):
    """The tenant context is missing, malformed, or used outside a transaction."""


def set_tenant(session: Session, tenant_id: uuid.UUID) -> None:
    """Bind ``tenant_id`` to the current transaction.

    Raises if there is no active transaction, because ``is_local`` outside one is a
    silent no-op: the setting would be discarded immediately and every RLS-protected
    query would then return nothing, which reads as "the data vanished" rather than
    "the context was never set".
    """
    if not isinstance(tenant_id, uuid.UUID):
        raise TenantContextError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")
    if not session.in_transaction():
        raise TenantContextError(
            "set_tenant requires an active transaction; a transaction-local setting "
            "outside one is discarded immediately"
        )
    session.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})


def current_tenant(session: Session) -> uuid.UUID | None:
    """The tenant bound to this transaction, or None when unset."""
    raw = session.execute(_READ_TENANT).scalar()
    if raw is None or raw == "":
        return None
    return uuid.UUID(raw)


def require_tenant(session: Session) -> uuid.UUID:
    """The bound tenant, or raise. Use where proceeding without a tenant is a bug."""
    tenant = current_tenant(session)
    if tenant is None:
        raise TenantContextError(
            "no tenant bound to this transaction; RLS would filter every row away"
        )
    return tenant


@contextmanager
def tenant_scope(session: Session, tenant_id: uuid.UUID) -> Iterator[uuid.UUID]:
    """Run a block with ``tenant_id`` bound, restoring the previous tenant on exit.

    Nesting is supported so that a background job processing several tenants on one
    pooled connection cannot leak the outer tenant into the inner block, or the reverse.
    The restore is explicit rather than relying on transaction end, because the block may
    be nested inside a longer transaction that continues afterwards.
    """
    previous = current_tenant(session)
    set_tenant(session, tenant_id)
    try:
        yield tenant_id
    finally:
        if previous is None:
            session.execute(_SET_TENANT, {"tenant_id": None})
        else:
            session.execute(_SET_TENANT, {"tenant_id": str(previous)})

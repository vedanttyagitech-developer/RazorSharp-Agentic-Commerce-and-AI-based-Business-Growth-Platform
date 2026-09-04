"""Row-level security policies and role grants.

Generated as reviewable statements rather than written inline in a migration body, so
that the isolation rule can be read in one place and asserted in tests.

Two details that decide whether this works:

``FORCE ROW LEVEL SECURITY`` — without it, the table owner bypasses every policy.
Migrations create tables as the owner, and in development the application often connects
as that same owner, so a policy without FORCE tests green and protects nothing.

``current_setting('app.tenant_id', true)`` — the ``true`` makes a missing setting return
NULL instead of raising. ``tenant_id = NULL`` is NULL, which is not true, so an unset
context matches no rows. Isolation fails closed: forgetting to set the tenant returns an
empty result, never another tenant's data.
"""

from __future__ import annotations

from .roles import ALL_ROLES, APP, APPEND_ONLY_TABLES, FINANCIAL_TABLES, KERNEL, WORKER
from .schema import RLS_TABLES

# NULLIF is load-bearing. set_config(..., NULL, true) stores the EMPTY STRING, not
# NULL, so resetting the tenant leaves '' behind. Casting '' to uuid raises
# 'invalid input syntax for type uuid', which turns a missing context into a hard
# error on every protected table instead of an empty result. NULLIF maps '' back to
# NULL so the comparison is NULL, matches no rows, and isolation fails closed.
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def create_roles_sql() -> list[str]:
    """Create the roles if absent. NOLOGIN group roles; deployment grants them to users."""
    out: list[str] = []
    for role in ALL_ROLES:
        # S608 is suppressed deliberately: `role` is interpolated from the ALL_ROLES
        # constant in this package and never from a request or database value. SQL
        # identifiers cannot be supplied as bound parameters, so DDL generation is
        # string construction by necessity. Every interpolation in this module must
        # come from a module-level constant for this suppression to stay honest.
        create_role = (
            "DO $$ BEGIN "  # noqa: S608
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS; "
            "END IF; END $$;"
        )
        out.append(create_role)
        # Defensive: a role that already existed might have been created with BYPASSRLS.
        out.append(f"ALTER ROLE {role} NOBYPASSRLS;")
    return out


def enable_rls_sql() -> list[str]:
    out: list[str] = []
    for table in RLS_TABLES:
        out.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        out.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        out.append(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        out.append(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE});"
        )
    return out


def grants_sql() -> list[str]:
    """Least-privilege grants.

    The app role can read everything tenant-scoped and write only non-financial tables.
    The kernel role writes financial state. The worker leases outbox rows and updates
    their delivery status, but cannot touch payment or approval rows directly — it must
    call the kernel, which is the point of specification 5.3 Registry C.
    """
    out: list[str] = ["GRANT USAGE ON SCHEMA public TO " + ", ".join(ALL_ROLES) + ";"]

    # Everyone tenant-scoped may read.
    out.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {APP}, {KERNEL}, {WORKER};")

    # Kernel writes financial state.
    for table in FINANCIAL_TABLES:
        out.append(f"GRANT INSERT, UPDATE ON {table} TO {KERNEL};")

    # App may write only non-financial tables.
    for table in FINANCIAL_TABLES:
        out.append(f"REVOKE INSERT, UPDATE, DELETE ON {table} FROM {APP};")

    # Outbox: kernel enqueues, worker leases and completes.
    out.append(f"GRANT INSERT ON outbox_events TO {KERNEL};")
    out.append(f"GRANT UPDATE ON outbox_events TO {WORKER};")
    out.append(f"REVOKE INSERT, UPDATE, DELETE ON outbox_events FROM {APP};")

    # Audit is append-only for every role, including the kernel. Evidence that can be
    # edited is not evidence.
    for table in APPEND_ONLY_TABLES:
        out.append(f"GRANT INSERT ON {table} TO {KERNEL}, {WORKER};")
        out.append(f"REVOKE UPDATE, DELETE ON {table} FROM {APP}, {KERNEL}, {WORKER};")

    # Nobody deletes financial history through the application roles.
    out.append(f"REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM {APP}, {KERNEL}, {WORKER};")
    return out


def all_statements() -> list[str]:
    return [*create_roles_sql(), *enable_rls_sql(), *grants_sql()]

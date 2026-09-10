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

One detail that decides whether a fresh database can be migrated at all:

Every per-table statement is wrapped in ``IF to_regclass(...) IS NOT NULL``. The core
migration (``c1d2f61e6eb9``) executes this module's *live* output, so on an empty
database it would otherwise reference tables that only later revisions create and fail
with "relation does not exist". The guard makes the generators safe to run at any point
in the migration history, and idempotent, which is also what lets a migration that adds
one table apply :func:`table_statements` for just that table.
"""

from __future__ import annotations

from .roles import (
    ALL_ROLES,
    APP,
    APPEND_ONLY_TABLES,
    APPEND_SCOPE,
    COLUMN_SCOPED_UPDATE,
    FINANCIAL_TABLES,
    KERNEL,
    WORKER,
    WRITE_GRANTS,
)
from .schema import RLS_TABLES, Base
from .schema_service import SERVICE_TABLES

# NULLIF is load-bearing. set_config(..., NULL, true) stores the EMPTY STRING, not
# NULL, so resetting the tenant leaves '' behind. Casting '' to uuid raises
# 'invalid input syntax for type uuid', which turns a missing context into a hard
# error on every protected table instead of an empty result. NULLIF maps '' back to
# NULL so the comparison is NULL, matches no rows, and isolation fails closed.
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"

_APP_ROLES = f"{APP}, {KERNEL}, {WORKER}"


def _guarded(table: str, statement: str) -> str:
    """Run ``statement`` only if ``table`` exists.

    Dollar-quoting keeps the inner statement verbatim, including the single quotes in
    :data:`TENANT_PREDICATE`. ``table`` always comes from a module-level constant; SQL
    identifiers cannot be bound parameters, so DDL generation is string construction.
    """
    return (
        "DO $$ BEGIN "
        f"IF to_regclass('public.{table}') IS NOT NULL THEN "
        f"EXECUTE $q${statement}$q$; "
        "END IF; END $$;"
    )


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


def append_scope_statements(table: str) -> list[str]:
    """Restrictive INSERT policies binding a role to the streams it may author.

    ``AS RESTRICTIVE`` is the whole mechanism and is not a stylistic choice. Policies are
    combined with OR; adding a second permissive policy would *widen* the table, letting a
    row in if either predicate passed. A restrictive policy is ANDed instead, so it can
    only narrow, and this one narrows exactly one role: ``TO`` names it, and PostgreSQL
    applies a policy to the members of the named role, which matters here because the
    login user is granted the group role rather than being it.

    ``FOR INSERT`` takes ``WITH CHECK`` and no ``USING``: there is no existing row to test.
    Reads are untouched, so every role still verifies every stream.
    """
    scopes = APPEND_SCOPE.get(table, {})
    out: list[str] = []
    for role, aggregates in scopes.items():
        policy = f"{role}_append_scope"
        allowed = ", ".join(f"'{value}'" for value in aggregates)
        out.append(_guarded(table, f"DROP POLICY IF EXISTS {policy} ON {table}"))
        out.append(
            _guarded(
                table,
                f"CREATE POLICY {policy} ON {table} AS RESTRICTIVE FOR INSERT "
                f"TO {role} WITH CHECK (aggregate_type IN ({allowed}))",
            )
        )
    return out


def rls_statements(table: str) -> list[str]:
    """ENABLE + FORCE row-level security, tenant_isolation, and any append scope."""
    out = [
        _guarded(table, f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"),
        _guarded(table, f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"),
        _guarded(table, f"DROP POLICY IF EXISTS tenant_isolation ON {table}"),
        _guarded(
            table,
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})",
        ),
    ]
    out.extend(append_scope_statements(table))
    return out


def enable_rls_sql() -> list[str]:
    out: list[str] = []
    for table in RLS_TABLES:
        out.extend(rls_statements(table))
    return out


def grant_statements(table: str) -> list[str]:
    """Least-privilege grants for one table, derived from :mod:`platform_db.roles`.

    Every application role may read; who may write is decided by which list the table
    is in. A table in none of them is read-only for every application role, which is
    the safe default for a table whose writers were never declared. No role ever
    receives DELETE, and the closing REVOKE re-asserts that even if a broad bootstrap
    grant ran earlier.
    """
    out = [_guarded(table, f"GRANT SELECT ON {table} TO {_APP_ROLES}")]
    if table == "verified_authority_proofs":
        out.append(_guarded(table, f"GRANT INSERT ON {table} TO {KERNEL}"))
        out.append(_guarded(table, f"REVOKE INSERT ON {table} FROM {APP}, {WORKER}"))
        out.append(_guarded(table, f"REVOKE UPDATE ON {table} FROM {_APP_ROLES}"))
    elif table in FINANCIAL_TABLES:
        out.append(_guarded(table, f"GRANT INSERT, UPDATE ON {table} TO {KERNEL}"))
        out.append(_guarded(table, f"REVOKE INSERT, UPDATE ON {table} FROM {APP}, {WORKER}"))
    elif table in APPEND_ONLY_TABLES:
        # Append-only for every role, including the kernel. Evidence that can be edited
        # is not evidence.
        out.append(_guarded(table, f"GRANT INSERT ON {table} TO {KERNEL}, {WORKER}"))
        out.append(_guarded(table, f"REVOKE UPDATE ON {table} FROM {_APP_ROLES}"))
    else:
        for role, privileges in WRITE_GRANTS.get(table, {}).items():
            out.append(_guarded(table, f"GRANT {', '.join(privileges)} ON {table} TO {role}"))
        for role, columns in COLUMN_SCOPED_UPDATE.get(table, {}).items():
            # REVOKE first, and unconditionally. A column grant does not replace a
            # table-wide one -- PostgreSQL keeps both and the wider wins -- and a bootstrap
            # script may well have issued the wider one, so narrowing has to remove it.
            out.append(_guarded(table, f"REVOKE UPDATE ON {table} FROM {role}"))
            out.append(_guarded(table, f"GRANT UPDATE ({', '.join(columns)}) ON {table} TO {role}"))
    out.append(_guarded(table, f"REVOKE DELETE ON {table} FROM {_APP_ROLES}"))
    return out


def table_statements(table: str) -> list[str]:
    """Everything a migration must apply after creating ``table``: RLS if it is
    tenant-owned, then its grants. There are no default privileges in this schema, so a
    table that skips this step is invisible to every application role."""
    out = rls_statements(table) if table in RLS_TABLES else []
    out.extend(grant_statements(table))
    return out


def _known_tables() -> tuple[str, ...]:
    # Base.metadata knows every table of both schema modules once schema_service is
    # imported (it is, above). Sorted so the generated script is stable to review.
    return tuple(sorted(Base.metadata.tables))


def grants_sql() -> list[str]:
    """Least-privilege grants for every table the package knows.

    The app role can read everything tenant-scoped and write only non-financial tables.
    The kernel role writes financial state. The worker leases outbox rows and updates
    their delivery status, but cannot touch payment or approval rows directly — it must
    call the kernel, which is the point of specification 5.3 Registry C.
    """
    out: list[str] = ["GRANT USAGE ON SCHEMA public TO " + ", ".join(ALL_ROLES) + ";"]
    # Everyone tenant-scoped may read, including alembic_version, which the ORM does not
    # know about.
    out.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_APP_ROLES};")
    for table in _known_tables():
        out.extend(grant_statements(table))
    # Nobody deletes financial history through the application roles.
    out.append(f"REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM {_APP_ROLES};")
    return out


def all_statements() -> list[str]:
    return [*create_roles_sql(), *enable_rls_sql(), *grants_sql()]


__all__ = [
    "SERVICE_TABLES",
    "TENANT_PREDICATE",
    "all_statements",
    "create_roles_sql",
    "enable_rls_sql",
    "grant_statements",
    "grants_sql",
    "rls_statements",
    "table_statements",
]

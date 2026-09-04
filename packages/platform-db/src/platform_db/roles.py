"""Database roles, specification 21.3.

Least privilege is expressed in the database, not only in Python. The import-boundary
rule ("only transaction-kernel owns financial write repositories") is a lint check that a
determined caller can bypass; a role without UPDATE on ``payment_attempts`` cannot be
bypassed by importing a different module.

No application role receives BYPASSRLS.
"""

from __future__ import annotations

from typing import Final

APP: Final = "commerce_app"  # tenant-scoped reads, non-financial writes
KERNEL: Final = "commerce_kernel"  # the only role that writes financial state
WORKER: Final = "commerce_worker"  # leases outbox work, calls kernel interfaces
MIGRATION: Final = "commerce_migration"
ANALYTICS: Final = "commerce_analytics"

ALL_ROLES: Final[tuple[str, ...]] = (APP, KERNEL, WORKER, MIGRATION, ANALYTICS)

#: Tables only the kernel may write. The app role gets SELECT and nothing more.
FINANCIAL_TABLES: Final[tuple[str, ...]] = (
    "approvals",
    "delegated_authorities",
    "payment_attempts",
    "execution_grants",
    "checkout_versions",
    "policy_at_sale_receipts",
    "reservations",
    "idempotency_records",
)

#: Append-only for every role: no UPDATE, no DELETE. Evidence that can be edited is not
#: evidence. Retention is handled by partition drop under the migration role, not by
#: application deletes.
APPEND_ONLY_TABLES: Final[tuple[str, ...]] = ("audit_events",)

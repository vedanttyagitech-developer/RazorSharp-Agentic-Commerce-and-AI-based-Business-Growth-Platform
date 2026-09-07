"""Database roles, specification 21.3.

Least privilege is expressed in the database, not only in Python. The import-boundary
rule ("only transaction-kernel owns financial write repositories") is a lint check that a
determined caller can bypass; a role without UPDATE on ``payment_attempts`` cannot be
bypassed by importing a different module.

No application role receives BYPASSRLS.

This module is the single description of who may write what. :mod:`platform_db.rls`
turns it into GRANT statements and every migration that adds a table applies those
statements, so a table cannot be added without deciding its writers here first.
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
#: ``orders``, ``provider_requests`` and ``reconciliation_runs`` are evidence of money
#: movement: an order confirmed by the API process under the app role, or a provider
#: request the worker could edit after the fact, would not be evidence.
FINANCIAL_TABLES: Final[tuple[str, ...]] = (
    "approvals",
    "delegated_authorities",
    "payment_attempts",
    "refunds",
    "execution_grants",
    "checkout_versions",
    "policy_at_sale_receipts",
    "reservations",
    "idempotency_records",
    "orders",
    "provider_requests",
    "reconciliation_runs",
)

#: Append-only for every role: no UPDATE, no DELETE. Evidence that can be edited is not
#: evidence. Retention is handled by partition drop under the migration role, not by
#: application deletes.
APPEND_ONLY_TABLES: Final[tuple[str, ...]] = ("audit_events",)

#: Write privileges on the tables that are neither financial nor append-only, as
#: ``table -> role -> privileges``. Every role listed also has SELECT; DELETE is absent
#: everywhere by construction. The reasoning per row:
#:
#: * ``tenants``/``merchants``: onboarding is a demo-profile app action.
#: * ``platform_operating_modes``: the kernel's ``enter_safe_mode`` appends here; without
#:   this grant Safe Mode works only where a bootstrap script widened the kernel.
#: * ``outbox_events``: the kernel enqueues in the admission transaction, the worker
#:   leases and completes. The kernel also holds ``UPDATE``, because
#:   ``POST /v1/ops/outbox/{id}/revive`` returns a buried command to the queue and an API
#:   mutation runs as the kernel role (ADR D1). Declaring only ``INSERT`` here worked
#:   solely where a bootstrap script had widened the kernel to every table; the grants a
#:   migration generates come from this mapping, so a real deployment would have refused
#:   that revive with a permission error.
#: * ``webhook_inbox``: the receiver runs as the kernel (ADR D7); the worker stamps
#:   ``applied_at`` and the apply outcome.
#: * ``scenario_faults``: the controller arms (app), the worker consumes, the kernel may
#:   fill in the attempt it hit.
#: * The remaining heads are API-owned and also writable by the kernel because API
#:   mutations run as the kernel role (ADR D1) and update the head in the same
#:   transaction as the kernel call.
WRITE_GRANTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "tenants": {APP: ("INSERT",)},
    "merchants": {APP: ("INSERT",)},
    "platform_operating_modes": {KERNEL: ("INSERT", "UPDATE")},
    "outbox_events": {KERNEL: ("INSERT", "UPDATE"), WORKER: ("UPDATE",)},
    "api_sessions": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    "carts": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    "checkouts": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    "webhook_inbox": {KERNEL: ("INSERT", "UPDATE"), WORKER: ("UPDATE",)},
    "scenario_faults": {APP: ("INSERT",), KERNEL: ("UPDATE",), WORKER: ("UPDATE",)},
    "scenario_runs": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
}

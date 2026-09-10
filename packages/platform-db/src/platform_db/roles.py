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

#: Which streams a role may append to, as ``table -> role -> aggregate_type values``.
#: A role absent from a table's entry may append to any stream on it.
#:
#: Append-only answers "may this role edit evidence". It does not answer "whose evidence
#: may this role author", and those are different questions with different answers. The
#: hash chain proves that a stream was not edited or reordered; it says nothing about who
#: was entitled to write a row, because the chain is recomputed over whatever is stored and
#: a row appended at the tail hashes correctly. The only authorship signal on the row --
#: ``actor_type`` and ``principal_id`` -- is chosen by the writer, so it proves nothing
#: against a writer who is the problem.
#:
#: The executor holds this credential. It appends exactly one kind of row under it: the
#: dead-letter record on the ``outbox_command`` stream, from one call site. Everything else
#: it writes -- every ``checkout`` and ``payment_attempt`` event -- it writes on a kernel
#: session, because those are kernel decisions that the executor merely reports. Without
#: this scope the worker credential can author an ``admission.allowed`` row on a checkout
#: stream for money nobody authorised, and ``verify_chain`` will call that stream intact,
#: because it is. The restriction costs the executor nothing it does today and closes the
#: gap between "the kernel is the only role that writes financial state" and the evidence
#: about that state.
APPEND_SCOPE: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "audit_events": {WORKER: ("outbox_command",)},
}

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
#: * ``webhook_inbox``: the receiver runs as the kernel (ADR D7), and so does the apply.
#:   The worker held ``UPDATE`` here for a stamping step that has since moved: the row is
#:   locked and stamped by ``transaction_kernel.payments.record_webhook_applied`` inside
#:   ``runtime.kernel_session()``, and no worker session in the executor touches this table
#:   at all. It is not a spare privilege. ``raw_body``, ``signature_verified`` and
#:   ``apply_status`` are re-read from the row when a delivery is applied, so a role that
#:   can edit them can make the kernel act on a webhook the provider never sent.
#: * ``scenario_faults``: the controller arms (app), the worker consumes, the kernel may
#:   fill in the attempt it hit.
#: * The remaining heads are API-owned and also writable by the kernel because API
#:   mutations run as the kernel role (ADR D1) and update the head in the same
#:   transaction as the kernel call.
#: ``table -> role -> columns``. A role here may UPDATE those columns and no others.
#:
#: The executor leases a command and then reports what happened to it. Table-wide UPDATE
#: let it rewrite the command itself: ``payload`` carries the amount a payment will be
#: created for, ``command_type`` decides which handler runs, and ``tenant_id`` decides
#: whose money it is. None of the three is ever written by this package -- the only SETs it
#: issues are ``status``, ``attempts``, ``leased_until`` and ``available_at`` -- so the
#: grant is narrowed to exactly those. A process that is compromised, or merely wrong, then
#: cannot edit the instruction it is about to carry out; PostgreSQL refuses it.
COLUMN_SCOPED_UPDATE: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "outbox_events": {WORKER: ("status", "attempts", "leased_until", "available_at")},
}


WRITE_GRANTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "reserve_passkeys": {KERNEL: ("INSERT", "UPDATE")},
    "reserve_consent_challenges": {KERNEL: ("INSERT", "UPDATE")},
    "tenants": {APP: ("INSERT",)},
    "merchants": {APP: ("INSERT",)},
    "platform_operating_modes": {KERNEL: ("INSERT", "UPDATE")},
    "outbox_events": {KERNEL: ("INSERT", "UPDATE")},
    "api_sessions": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    "carts": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    "checkouts": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    "webhook_inbox": {KERNEL: ("INSERT", "UPDATE")},
    "scenario_faults": {APP: ("INSERT",), KERNEL: ("UPDATE",), WORKER: ("UPDATE",)},
    "scenario_runs": {APP: ("INSERT", "UPDATE"), KERNEL: ("INSERT", "UPDATE")},
    # The buyer's own session opens a case, and the merchant's side will answer it.
    # No kernel write: nothing here moves money, and a financial role holding a grant
    # on it would say otherwise.
    "support_cases": {APP: ("INSERT", "UPDATE")},
    # The merchant proposes, approves, rejects and cancels as the app role: none of that
    # moves money and a financial role holding a grant here would say otherwise. The kernel
    # holds UPDATE and nothing else, for one reason -- carrying an approved action out
    # writes the merchant audit event in the same transaction as the state change, and that
    # append is the kernel's. It may move a row along; it may not create one.
    "merchant_actions": {APP: ("INSERT", "UPDATE"), KERNEL: ("UPDATE",)},
    # The shop's own live state: prices, stock, listing, fees and the running offer.
    #
    # Nothing here moves money, so this is not a financial table and the kernel does not
    # hold it because of what it is -- it holds UPDATE because of when the writes happen.
    # A scenario injection and an executed merchant action each change this state and
    # append their audit event in the same transaction, and that append is the kernel's.
    #
    # APP holds INSERT alone, and that split is the point. Seeding a shop is provisioning,
    # the same act as the `merchants` row the app role already inserts. Changing what a
    # shop charges is not provisioning, and a role that could do it without writing the
    # audit event beside it would be able to move a price with nothing saying why.
    "merchant_state": {APP: ("INSERT",), KERNEL: ("INSERT", "UPDATE")},
    "merchant_sku_state": {APP: ("INSERT",), KERNEL: ("INSERT", "UPDATE")},
    # The inventory ledger. INSERT and nothing else, for either role: a movement that
    # could be edited afterwards would make every balance derived from it an opinion.
    # The app role writes a shop's opening balance because that is provisioning; the
    # kernel writes every movement after it, beside the event that explains it.
    "inventory_movements": {APP: ("INSERT",), KERNEL: ("INSERT",)},
    # INSERT for both, UPDATE for nobody, and the second half is the guarantee.
    #
    # A published version is what a receipt names. A row that could be edited would make
    # every receipt naming it a receipt that says whatever the row says today, so narrowing
    # a term means publishing a new version beside the old one -- and the absent UPDATE is
    # what makes that the only way rather than the polite way. That is the property the
    # tests check and the one the Policy-at-Sale Receipt rests on.
    #
    # Who may INSERT is a smaller question and the answer follows ADR D1: API mutations run
    # as the kernel role, and carrying out an approved policy change is one. An earlier
    # version of this entry granted the app role alone and called kernel authorship a
    # widening; that was wrong twice over. It described a boundary this schema does not
    # draw -- the kernel already writes carts, checkouts and the webhook inbox -- and it
    # made the publication path unrunnable, which is how the mistake was found.
    "merchant_policy_versions": {APP: ("INSERT",), KERNEL: ("INSERT",)},
}

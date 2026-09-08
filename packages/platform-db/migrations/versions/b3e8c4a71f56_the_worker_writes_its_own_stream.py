"""The worker may report on the queue, not testify about a checkout.

Two privileges the executor's role held and never used, both in the same shape as the
``outbox_events`` narrowing in ``a7c31f5d9e02``: a grant kept open by the convention that
this package happens not to write such a statement.

``audit_events``. ``commerce_worker`` held table-wide INSERT, and the only policy on the
table constrained ``tenant_id``. ``aggregate_type`` is an unconstrained string, so the
worker credential could author an ``admission.allowed`` row on a ``checkout`` stream for
money nobody authorised, and ``verify_chain`` would report that stream intact -- correctly,
because the chain proves a stream was not edited or reordered and says nothing about who
was entitled to write a row. The worker appends exactly one kind of row in the whole tree:
the dead-letter record on ``outbox_command``, from one call site in ``action_executor.loop``.
Every ``checkout`` and ``payment_attempt`` event the executor writes, it writes on a kernel
session. So the scope costs it nothing it does today.

``AS RESTRICTIVE`` is the mechanism and not a preference. Policies combine with OR, so a
second permissive policy would widen the table rather than narrow it; a restrictive one is
ANDed. ``TO commerce_worker`` limits it to that role, and PostgreSQL applies a policy to
the *members* of the named role, which is what the deployment does -- the login user is
granted the group role rather than being it.

``webhook_inbox``. ``commerce_worker`` held UPDATE for a stamping step that has since moved
into the kernel: the row is locked and stamped by ``record_webhook_applied`` inside
``runtime.kernel_session()``, and no worker session in the executor touches the table. This
one is not merely untidy. ``raw_body``, ``signature_verified`` and ``apply_status`` are
re-read from the row when a delivery is applied, so a role able to edit them can make the
kernel act on a webhook the provider never sent -- forged state, not just forged evidence.

The RLS statements are replayed from the generator rather than written out here, so the
privileges a deployed database holds and the privileges the code declares cannot drift. The
grant narrowing needs an explicit REVOKE: the generator only emits what a role *should*
have, and PostgreSQL keeps a privilege nobody restated.

Reversible. ``downgrade`` restores both, because a migration that cannot be undone is a
migration nobody can deploy on a Friday.

Revision ID: b3e8c4a71f56
Revises: a7c31f5d9e02
"""

from __future__ import annotations

from alembic import op
from platform_db.rls import append_scope_statements, grant_statements
from platform_db.roles import WORKER

revision = "b3e8c4a71f56"
down_revision = "a7c31f5d9e02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in append_scope_statements("audit_events"):
        op.execute(statement)
    # Order matters only in that the REVOKE must run: `grant_statements` no longer names
    # the worker for this table, so nothing it emits would remove the standing grant.
    op.execute(f"REVOKE UPDATE ON webhook_inbox FROM {WORKER}")
    for statement in grant_statements("webhook_inbox"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS commerce_worker_append_scope ON audit_events")
    op.execute(f"GRANT UPDATE ON webhook_inbox TO {WORKER}")

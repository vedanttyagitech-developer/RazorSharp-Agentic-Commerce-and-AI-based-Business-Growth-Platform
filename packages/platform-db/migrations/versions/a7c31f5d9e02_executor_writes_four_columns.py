"""The executor may report on a command, not rewrite one.

``commerce_worker`` held table-wide UPDATE on ``outbox_events``. The five statements
``durable_work.outbox`` issues set only ``status``, ``attempts``, ``leased_until`` and
``available_at``; the grant also covered ``payload``, which carries the amount a payment
will be created for, ``command_type``, which decides which handler runs, and ``tenant_id``,
which decides whose money it is. The only thing stopping a leasing process from editing the
instruction it was about to carry out was that this package happens never to write such a
statement -- a convention, not a permission.

A column grant does not replace a table-wide one: PostgreSQL keeps both and the wider wins.
So the narrowing has to REVOKE first, which is what ``platform_db.rls.grant_statements``
now emits for this table, and what this migration replays onto an existing database.

Reversible: ``downgrade`` restores the table-wide grant, because a migration that cannot be
undone is a migration nobody can deploy on a Friday.

Revision ID: a7c31f5d9e02
Revises: d5b71e0a92c4
"""

from __future__ import annotations

from alembic import op
from platform_db.rls import grant_statements

revision = "a7c31f5d9e02"
down_revision = "d5b71e0a92c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Replayed from the generator rather than written out here, so the privileges a
    # deployed database holds and the privileges the code declares cannot drift.
    for statement in grant_statements("outbox_events"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("REVOKE UPDATE ON outbox_events FROM commerce_worker")
    op.execute("GRANT UPDATE ON outbox_events TO commerce_worker")

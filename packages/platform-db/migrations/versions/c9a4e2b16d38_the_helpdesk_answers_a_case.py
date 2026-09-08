"""A support case can now be picked up, answered and closed.

``support_cases`` had a status column with four legal values and code that only ever wrote
the first. A buyer opened a case, the row said ``OPEN``, and nothing anywhere could move it,
because there was no merchant-side surface to move it from. The buyer's order screen tells
them a person decides what they are owed; this is the first half of making that true.

Two columns and one index:

* ``handled_by`` -- who on the merchant's side picked it up, as the operator session's own
  actor reference. Deliberately not a foreign key. The merchant staff table does not exist,
  and inventing one to satisfy a constraint would be inventing merchant identity, which is a
  larger decision than a column.
* ``resolution_note`` -- what the person decided, in their words. Free text, never parsed,
  exactly like the buyer's own note.

There is **no amount column**, and that is a decision rather than an omission. A figure
stored here would be a number somebody typed rather than one the kernel resolved, and shown
beside the case it would read as a promise. What is still refundable is asked of the kernel
when it is needed, through the same route the buyer's own screen uses.

The index is the helpdesk's own read: what is still waiting, oldest first. The existing
index leads on ``merchant_id``, which serves "every case for this merchant" and makes the
open-queue page scan all of them. The queue is the read that happens on every page load.

Reversible: ``downgrade`` drops both columns and the index. The rows keep their status, so
a case answered before a rollback stays answered; only the note and the name go.

Revision ID: c9a4e2b16d38
Revises: b3e8c4a71f56
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9a4e2b16d38"
down_revision = "b3e8c4a71f56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("support_cases", sa.Column("handled_by", sa.String(128), nullable=True))
    op.add_column(
        "support_cases",
        sa.Column("resolution_note", sa.String(1000), nullable=False, server_default=sa.text("''")),
    )
    op.create_index(
        "ix_support_cases_tenant_status",
        "support_cases",
        ["tenant_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_support_cases_tenant_status", table_name="support_cases")
    op.drop_column("support_cases", "resolution_note")
    op.drop_column("support_cases", "handled_by")

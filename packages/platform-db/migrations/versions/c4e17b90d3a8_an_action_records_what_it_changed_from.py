"""An action records what it changed *from*, not only what it changed to.

``merchant_actions`` stored ``proposal`` -- the value a change was to produce -- and
nothing about the value it replaced. So the record of a price change read "the price is
now 8395" and never "from 7300", which is half of what a change is. An audit trail that
cannot say what moved is a list of assertions rather than a history.

It also made a revert impossible to build. Restoring a previous value requires knowing the
previous value, and the only place it existed was a ``StateDelta`` the merchant simulator
returns at execution and the service then dropped on the floor.

``applied`` is that delta set, written when the change is carried out and never before: a
draft has changed nothing, so it has nothing to have changed from, and a column filled at
proposal time would be recording a guess about a shop that can move before the approval
lands. Nullable for the same reason, and it stays null on every action that never
succeeded.

Revert is deliberately not an operation on this row. Restoring an old value is a new
action -- proposed, approved against its own digest, executed -- which is the only path
this surface has for changing anything, and an undo that skipped it would be a way to move
the shop that nobody agreed to.

Revision ID: c4e17b90d3a8
Revises: d8b30f1a52c6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4e17b90d3a8"
down_revision = "d8b30f1a52c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "merchant_actions",
        sa.Column("applied", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("merchant_actions", "applied")

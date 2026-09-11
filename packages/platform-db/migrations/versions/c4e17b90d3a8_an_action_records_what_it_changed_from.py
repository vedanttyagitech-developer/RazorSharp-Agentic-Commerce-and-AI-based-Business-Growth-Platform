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


def _has_applied() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == "applied" for c in inspector.get_columns("merchant_actions"))


def upgrade() -> None:
    # WHY THIS IS CONDITIONAL, AND WHAT IT IS WORKING AROUND
    # -----------------------------------------------------
    # ``f7a1d3e08c25`` creates ``merchant_actions`` with ``MerchantAction.__table__.create``
    # -- from the *live* ORM model rather than from DDL frozen at that revision. So on a
    # database that already exists the table was created before this column was declared and
    # this migration adds it; on a database built from nothing today the model already
    # carries ``applied``, the table arrives with it, and this ``ADD COLUMN`` fails with
    # DuplicateColumn. Every existing database was migrated incrementally, so nothing caught
    # it until the first deployment built a schema from scratch.
    #
    # The real fix is that a migration must not create a table from a model that keeps
    # moving. Four others do the same -- ``a2f5e91c7d43``, ``c6e2d901fa74``,
    # ``e91c4d7a2b58``, ``d4b7a1e93c60`` -- and are latent rather than safe: they collide
    # the day a column is added to one of their tables. Freezing all five to explicit DDL is
    # the change worth making; this guard only stops the one that is already broken, and it
    # keeps both paths converging on the same schema.
    if _has_applied():
        return
    op.add_column(
        "merchant_actions",
        sa.Column("applied", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    # Symmetric: on a fresh build this migration added nothing, so it removes nothing.
    if _has_applied():
        op.drop_column("merchant_actions", "applied")

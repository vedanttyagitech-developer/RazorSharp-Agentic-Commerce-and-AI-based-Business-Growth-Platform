"""Persist the buyer payment window independently of provider outcome."""

import sqlalchemy as sa
from alembic import op

revision = "b9d41e627af0"
down_revision = "a8c72e104b93"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "payment_attempts", sa.Column("payment_window_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "payment_attempts", sa.Column("payment_window_closed_at", sa.DateTime(timezone=True))
    )


def downgrade():
    op.drop_column("payment_attempts", "payment_window_closed_at")
    op.drop_column("payment_attempts", "payment_window_expires_at")

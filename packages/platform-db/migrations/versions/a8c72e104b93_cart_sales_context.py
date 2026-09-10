"""Durable, cart-scoped sales preferences and acknowledged add events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a8c72e104b93"
down_revision = "f6a9c2d4e710"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "carts",
        sa.Column(
            "shopping_context", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )


def downgrade():
    op.drop_column("carts", "shopping_context")

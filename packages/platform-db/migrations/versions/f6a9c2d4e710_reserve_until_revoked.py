"""Allow explicitly non-expiring Reserve authority; preserve existing deadlines."""

import sqlalchemy as sa
from alembic import op

revision = "f6a9c2d4e710"
down_revision = "e91c4d7a2b58"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "delegated_authorities",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.create_check_constraint(
        "expiry_or_reserve", "delegated_authorities", "expires_at IS NOT NULL OR kind = 'RESERVE'"
    )


def downgrade():
    # Never invent a deadline or delete an authority to make rollback succeed.
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM delegated_authorities WHERE expires_at IS NULL)")
    ).scalar():
        raise RuntimeError("Cannot downgrade while non-expiring Reserve authorities exist")
    op.drop_constraint("expiry_or_reserve", "delegated_authorities", type_="check")
    op.alter_column(
        "delegated_authorities",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )

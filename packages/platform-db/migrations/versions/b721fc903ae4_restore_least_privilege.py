"""Repair historical bootstrap grants and reassert tenant isolation."""

from alembic import op
from platform_db.rls import grants_sql, rls_statements

revision = "b721fc903ae4"
down_revision = "fa912de43701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in [*rls_statements("carts"), *grants_sql()]:
        op.execute(statement)


def downgrade() -> None:
    # Never restore unsafe privileges when rolling application code back.
    pass

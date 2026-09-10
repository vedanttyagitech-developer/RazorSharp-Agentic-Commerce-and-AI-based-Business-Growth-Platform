"""Durable passkey credentials and single-use Reserve consent challenges."""
from alembic import op
from platform_db.rls import table_statements
from platform_db.schema_service import ReservePasskey, ReserveConsentChallenge
revision = "d7f301a592e8"
down_revision = "c6e2d901fa74"
branch_labels = None
depends_on = None

def upgrade():
    for table in (ReservePasskey.__table__, ReserveConsentChallenge.__table__):
        table.create(bind=op.get_bind())
        for statement in table_statements(table.name):
            op.execute(statement)

def downgrade():
    raise RuntimeError("Do not silently delete buyer consent evidence or enrolled credentials")

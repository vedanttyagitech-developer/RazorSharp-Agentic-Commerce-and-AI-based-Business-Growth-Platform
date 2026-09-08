"""A merchant proposes a change, and somebody agrees to it before it happens.

``merchant_actions`` is the Controller's own table. It holds what a merchant wants to change
about their shop -- a price, a stock level, a listing, an offer -- the digest of the
canonical document that describes it, who proposed it, who approved it, and where it is in
its life.

Not a financial table, and the grants say so. The app role inserts and updates: proposing,
approving, rejecting and cancelling move no money, and a role holding payment credentials
with a write here would be describing this table as something it is not. The kernel role
holds UPDATE alone, for one reason -- carrying an approved action out writes the merchant
audit event in the same transaction as the state change, and that append is the kernel's. It
may move a row along; it may not create one.

Two CHECK constraints, and the second is the one worth reading. The first is the state
vocabulary. The second says an action that got past approval names its approver: the columns
are always written together by today's code, and a constraint is the version of that fact
which survives today's code being replaced. "Who agreed to this" is the question this table
exists to answer years later.

Two indexes for two different reads: the merchant's own worklist by merchant, and the
platform's by state.

Reversible: ``downgrade`` drops the table. Nothing references it, because an action names
its target as a string rather than as a foreign key -- a SKU belongs to the merchant
simulator's memory, not to a table this database has.

Revision ID: f7a1d3e08c25
Revises: e5c76b21a9f4
"""

from __future__ import annotations

from alembic import op
from platform_db.schema_service import MerchantAction
from platform_db.rls import table_statements

revision = "f7a1d3e08c25"
down_revision = "e5c76b21a9f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    MerchantAction.__table__.create(bind=op.get_bind())
    # Replayed from the generator rather than written out here, so the privileges a
    # deployed database holds and the privileges the code declares cannot drift.
    for statement in table_statements("merchant_actions"):
        op.execute(statement)


def downgrade() -> None:
    MerchantAction.__table__.drop(bind=op.get_bind())

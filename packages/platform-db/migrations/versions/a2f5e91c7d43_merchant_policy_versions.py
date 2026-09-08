"""A shop can change what it promises, and an old order keeps what it was sold.

The Policy-at-Sale Receipt has always frozen a merchant's terms onto each order so a later
change cannot narrow them retroactively. Until now the terms were constants in the
simulator: cancellation always allowed before dispatch, refunds always within seven days,
substitution never. The guarantee was real machinery pointed at an event that could not
occur, and a reviewer asking to see it tested got told the merchant cannot edit a policy.

This is the table that lets the event occur. Each row is one complete published version of a
merchant's non-financial terms, and the current version is simply the highest one.

Immutable by grant, not only by convention: the app role holds INSERT and no UPDATE, and the
kernel holds nothing at all. Narrowing a term means publishing a new version beside the old
one. An order that names version three still reads version three, because there is no
statement anybody is permitted to write that would change it.

No "current" pointer column. A pointer is a second source of truth that can disagree with the
rows it points at, and the only thing it would buy is publishing a version without making it
effective -- which is scheduling, and there is no scheduler.

Financial terms are deliberately absent. Delivery charges come from the fee policy and
discounts from the running promotion; both already change and already reach the receipt.
Putting them here as well would give one field two writers.

Reversible: ``downgrade`` drops the table. Receipts already issued keep their frozen terms,
because a receipt stores what it was handed rather than a reference to this table.

Revision ID: a2f5e91c7d43
Revises: f7a1d3e08c25
"""

from __future__ import annotations

from alembic import op
from platform_db.rls import table_statements
from platform_db.schema_service import MerchantPolicyVersion

revision = "a2f5e91c7d43"
down_revision = "f7a1d3e08c25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    MerchantPolicyVersion.__table__.create(bind=op.get_bind())
    for statement in table_statements("merchant_policy_versions"):
        op.execute(statement)


def downgrade() -> None:
    MerchantPolicyVersion.__table__.drop(bind=op.get_bind())

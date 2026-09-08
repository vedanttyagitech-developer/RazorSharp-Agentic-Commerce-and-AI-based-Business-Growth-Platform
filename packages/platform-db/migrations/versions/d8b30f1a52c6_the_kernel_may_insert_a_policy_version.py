"""The role that records an approved change may also write what it changed.

``merchant_policy_versions`` was created with INSERT for the app role alone, on the
reasoning that a financial role authoring a shop's promises would be a widening. That was
wrong in two ways, and the second is how it was found.

It described a boundary this schema does not draw. API mutations run as the kernel role
(ADR 0003 D1), which already writes carts, checkouts, the webhook inbox and scenario runs.
Authorship is not what separates the roles here; the financial *tables* are.

And it made the publication path unrunnable. Carrying out an approved policy change happens
on the merchant action's own execute route, which is a kernel session because the other
action kinds write an audit event. The first test to publish a policy answered "permission
denied".

The guarantee is unchanged and it was never about INSERT. **No role holds UPDATE**, so a
published version cannot be edited, and narrowing a term means publishing a new version
beside the old one. That is what the Policy-at-Sale Receipt rests on and what the tests
check, and this migration does not touch it.

Revision ID: d8b30f1a52c6
Revises: a2f5e91c7d43
"""

from __future__ import annotations

from alembic import op
from platform_db.rls import grant_statements

revision = "d8b30f1a52c6"
down_revision = "a2f5e91c7d43"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in grant_statements("merchant_policy_versions"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("REVOKE INSERT ON merchant_policy_versions FROM commerce_kernel")

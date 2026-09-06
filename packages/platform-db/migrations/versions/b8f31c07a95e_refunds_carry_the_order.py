"""refunds carry the order

Revision ID: b8f31c07a95e
Revises: a4e17c93b5d2

A refund hung off ``payment_attempt_id`` and nothing else, so "the refunds for this order"
was answered by joining through the attempt. That join is correct and it worked -- money
went back the way it came in, which is the attempt -- but it left the order as a leaf that
nothing in the schema pointed at, and the order id is the identifier this system is built
around. An operator holding an order number should reach its refunds without knowing that
an attempt sits between them.

A real foreign key, and deliberately nullable. Every refund the platform admits has an
order -- an order is written at capture and a refund only exists against a capture -- so in
practice this is always filled, and the backfill filled every existing row through the
attempt each already names.

It is nullable for a case the platform has already: a stale capture. Money can be captured
against a version that was invalidated while the payment was in flight, and no order is
written for it -- correctly, because nothing was sold. The platform refunds that capture in
full without anyone asking, and the refund genuinely has no order to name. Requiring one
would mean the kernel could not record money it has already sent back.

So the column is empty exactly where an order does not exist, and never where one does. A
refund with a null here is a real signal -- this money was returned for something that was
never an order -- rather than a gap.

The attempt id stays. It is what the provider's refund is issued against and what the
grant binds to, and dropping it to avoid holding two references would trade a real
guarantee for a tidier row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8f31c07a95e"
down_revision = "a4e17c93b5d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refunds", sa.Column("order_id", sa.UUID(), nullable=True))

    # Through the attempt, which is exactly the join this column exists to remove. Scoped
    # by tenant as well as by attempt: attempt ids are unique, but every statement in this
    # schema names the tenant, and a backfill is not the place to start making exceptions.
    op.execute(
        sa.text(
            "UPDATE refunds r SET order_id = o.id FROM orders o "
            "WHERE o.tenant_id = r.tenant_id AND o.payment_attempt_id = r.payment_attempt_id"
        )
    )

    op.create_foreign_key(
        op.f("fk_refunds_order_id"), "refunds", "orders", ["order_id"], ["id"]
    )
    op.create_index("ix_refunds_tenant_order", "refunds", ["tenant_id", "order_id"], unique=False)
    # Privileges are per table, so the kernel's existing INSERT/UPDATE on refunds covers
    # the new column; nothing to grant.


def downgrade() -> None:
    op.drop_index("ix_refunds_tenant_order", table_name="refunds")
    op.drop_constraint(op.f("fk_refunds_order_id"), "refunds", type_="foreignkey")
    op.drop_column("refunds", "order_id")

"""Persist immutable signed Reserve evidence; unsigned permissions require reauthorization."""

import sqlalchemy as sa
from alembic import op
from platform_db.rls import table_statements
from platform_db.schema import VerifiedAuthorityProof

revision = "c6e2d901fa74"
down_revision = "b9d41e627af0"
branch_labels = None
depends_on = None


def upgrade():
    VerifiedAuthorityProof.__table__.create(bind=op.get_bind())
    for statement in table_statements("verified_authority_proofs"):
        op.execute(statement)
    op.add_column("delegated_authorities", sa.Column("reserve_proof_id", sa.UUID(), nullable=True))
    # Never invent signed consent for legacy permissions. Existing payment evidence and
    # HELD allocations remain intact; revocation blocks new/queued execution only.
    retired = op.get_bind().execute(sa.text(
        "UPDATE delegated_authorities SET status='REVOKED',revocation_epoch=revocation_epoch+1 "
        "WHERE kind='RESERVE' AND status NOT IN ('REVOKED','EXPIRED') "
        "RETURNING id,tenant_id,revocation_epoch"
    )).all()
    # Preserve the existing hash-chain encoding by using its canonical appender.
    from sqlalchemy.orm import Session
    from platform_db import set_tenant
    from commerce_domain import uuid7
    from transaction_kernel.audit import append
    with Session(bind=op.get_bind()) as session, session.begin():
        for row in retired:
            set_tenant(session, row.tenant_id)
            append(session, tenant=row.tenant_id, aggregate_type="authority", aggregate_id=row.id,
                   event_type="reserve.unsigned_permission_retired", actor_type="SYSTEM",
                   principal_id="migration:c6e2d901fa74", correlation_id=uuid7(),
                   payload={"reason": "fresh_signed_authorization_required", "epoch": row.revocation_epoch})
        session.flush()
    op.execute("SELECT set_config('app.tenant_id', '', true)")
    op.create_foreign_key(
        "fk_authority_signed_proof",
        "delegated_authorities",
        "verified_authority_proofs",
        ["tenant_id", "id", "reserve_proof_id"],
        ["tenant_id", "authority_id", "id"],
    )
    op.create_check_constraint(
        "reserve_active_requires_proof",
        "delegated_authorities",
        "kind != 'RESERVE' OR status IN ('REVOKED','EXPIRED','RECONCILING') OR reserve_proof_id IS NOT NULL",
    )


def downgrade():
    raise RuntimeError(
        "Signed financial authorization cannot be silently downgraded to unsigned authority"
    )

"""Delegated-payment Safe Mode tests, specification 10.3.2.

Everything here runs against real PostgreSQL. The invariants under test are about what a
committed row makes impossible -- a revoked grant that cannot be consumed, a refund that
still completes while the kill switch is on, an append-only history that a mode change
cannot rewrite -- and a mocked session can be made to agree with any of those claims
without a single one of them being true.

The tests connect as ``commerce_test_kernel``, a NOSUPERUSER NOBYPASSRLS login role, for
the same reason the isolation suite does: a superuser bypasses row-level security, so the
cross-tenant tests would pass while proving nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

import pytest
from commerce_domain import ActorType, CheckoutRef, Money, RecoveryCode, canonical_hash, uuid7
from platform_db import set_tenant
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session
from transaction_kernel import Operation
from transaction_kernel.grants import (
    GrantBinding,
    GrantRevokedError,
    GrantStatus,
    consume_grant,
    issue_grant,
)
from transaction_kernel.safe_mode import (
    DELEGATED_GRANT_OPERATIONS,
    ENTRY_REASONS,
    EXIT_REASONS,
    NEVER_SWEPT,
    SAFE_MODE_BLOCKED,
    SAFE_MODE_PERMITTED,
    GuardedOperation,
    ModeChangeReason,
    ModeScope,
    OperatingModeName,
    SafeModeBlockedError,
    SafeModeScopeError,
    _coerce_mode,
    assert_permitted,
    banner,
    current_mode,
    enter_safe_mode,
    is_permitted,
    leave_safe_mode,
    resolve_mode,
)

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Seeding, backdating and teardown go through the owner connection: the kernel role has
# no DELETE on any table, and granting it one to make fixtures convenient would erase the
# guarantee that financial history cannot be removed through an application role.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

AMOUNT = Money(39500, "INR")
TTL = 300
OPERATOR = "ops:alice@example.test"
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


# --------------------------------------------------------------------------- fixtures


def _require_db(url: str, *, pool_size: int = 5) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for Safe Mode tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert row.rolsuper is False, "Safe Mode tests must not run as a superuser"
    assert row.rolbypassrls is False, "Safe Mode tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL, pool_size=2)


@dataclass(frozen=True, slots=True)
class Attempt:
    """One payment attempt and the checkout version it belongs to."""

    attempt_id: uuid.UUID
    checkout: CheckoutRef


@dataclass(frozen=True, slots=True)
class Seed:
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    #: Delegated debit path: the grant Safe Mode activation must withdraw.
    debit_a: Attempt
    #: Refund path: the grant Safe Mode activation must leave alone.
    refund_a: Attempt
    #: A grant that is already consumed before activation. Evidence, not a live capability.
    consumed_a: Attempt
    #: An attempt parked in UNKNOWN. Safe Mode must not turn it into FAILED.
    unknown_a: Attempt


def _insert_attempt(conn: object, tenant: uuid.UUID, status: str) -> Attempt:
    """Insert one payment attempt on its own checkout.

    A distinct checkout per attempt is required, not cosmetic: the partial unique index
    ``uq_payment_attempts_one_non_terminal`` permits only one live attempt per checkout.
    """
    attempt_id = uuid7()
    checkout = CheckoutRef(
        checkout_id=uuid7(), version=3, content_hash=canonical_hash({"total_minor": AMOUNT.minor})
    )
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version,"
            " status, amount_minor, currency, receipt) "
            "VALUES (:id, :t, :c, :v, :s, :amt, 'INR', :r)"
        ),
        {
            "id": attempt_id,
            "t": tenant,
            "c": checkout.checkout_id,
            "v": checkout.version,
            "s": status,
            "amt": AMOUNT.minor,
            # The tail of a uuid7 is the random part. Its head is the millisecond
            # timestamp, which four attempts seeded in one transaction share.
            "r": f"rcpt-{attempt_id.hex[-16:]}",
        },
    )
    return Attempt(attempt_id=attempt_id, checkout=checkout)


@pytest.fixture
def seed(admin_engine: Engine) -> Iterator[Seed]:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    with admin_engine.begin() as conn:
        # A known-empty mode history. Several tests assert that an empty history reads as
        # NORMAL, and every precedence test reasons about "the newest record of a scope",
        # so a row left over from an earlier test would silently change the answer.
        conn.execute(text("DELETE FROM platform_operating_modes"))
        for tenant in (tenant_a, tenant_b):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :slug, :name, 'asia-south1')"
                ),
                {"id": tenant, "slug": f"t-{tenant.hex[:8]}", "name": f"t-{tenant.hex[:8]}"},
            )
        # payment_attempts is RLS-protected, so bind the tenant before inserting.
        conn.execute(SET_TENANT, {"t": str(tenant_a)})
        debit_a = _insert_attempt(conn, tenant_a, "CREATED")
        refund_a = _insert_attempt(conn, tenant_a, "CAPTURED")
        consumed_a = _insert_attempt(conn, tenant_a, "CREATED")
        unknown_a = _insert_attempt(conn, tenant_a, "UNKNOWN")

    yield Seed(
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        debit_a=debit_a,
        refund_a=refund_a,
        consumed_a=consumed_a,
        unknown_a=unknown_a,
    )

    with admin_engine.begin() as conn:
        # Mode rows carry a RESTRICT foreign key to tenants, so they go first.
        conn.execute(text("DELETE FROM platform_operating_modes"))
        for tenant in (tenant_a, tenant_b):
            conn.execute(SET_TENANT, {"t": str(tenant)})
            conn.execute(text("DELETE FROM execution_grants WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM payment_attempts WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [tenant_a, tenant_b]}
        )


@pytest.fixture
def open_session(kernel_engine: Engine) -> Iterator[Callable[[], Session]]:
    """Hand out kernel sessions and guarantee they are closed even mid-transaction."""
    opened: list[Session] = []

    def factory() -> Session:
        session = Session(kernel_engine, expire_on_commit=False)
        opened.append(session)
        return session

    yield factory
    for session in opened:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------- helpers


def _enter_global(engine: Engine, reason: ModeChangeReason, actor: str = OPERATOR) -> uuid.UUID:
    """Declare a platform-wide Safe Mode in its own committed transaction."""
    with Session(engine, expire_on_commit=False) as session, session.begin():
        activation = enter_safe_mode(session, tenant=None, reason=reason, actor=actor)
        return activation.record.record_id


def _leave_global(engine: Engine, reason: ModeChangeReason, actor: str = OPERATOR) -> uuid.UUID:
    with Session(engine, expire_on_commit=False) as session, session.begin():
        return leave_safe_mode(session, tenant=None, reason=reason, actor=actor).record_id


def _enter_tenant(
    engine: Engine,
    tenant: uuid.UUID,
    reason: ModeChangeReason,
    actor: str = OPERATOR,
) -> tuple[uuid.UUID, tuple[uuid.UUID, ...]]:
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, tenant)
        activation = enter_safe_mode(session, tenant=tenant, reason=reason, actor=actor)
        return activation.record.record_id, activation.revoked_grant_ids


def _leave_tenant(
    engine: Engine, tenant: uuid.UUID, reason: ModeChangeReason, actor: str = OPERATOR
) -> uuid.UUID:
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, tenant)
        return leave_safe_mode(session, tenant=tenant, reason=reason, actor=actor).record_id


def _backdate_mode(admin_engine: Engine, record_id: uuid.UUID, seconds: int) -> None:
    """Move one mode record's ``changed_at`` into the past, by the database clock.

    Precedence is decided by comparing these timestamps, so the ordering under test has to
    be established on the server. Two commits milliseconds apart would order correctly by
    luck rather than by design, and a test that passes by luck is not a test.
    """
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE platform_operating_modes "
                "SET changed_at = now() - make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": seconds, "id": record_id},
        )


def _align_mode_clocks(admin_engine: Engine, *record_ids: uuid.UUID) -> None:
    """Give several records exactly the same ``changed_at``, to test the tie-break."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE platform_operating_modes SET changed_at = now() WHERE id = ANY(:ids)"),
            {"ids": list(record_ids)},
        )


def _issue_committed(
    engine: Engine,
    tenant: uuid.UUID,
    attempt: Attempt,
    operation: Operation,
) -> uuid.UUID:
    """Issue one grant in its own committed transaction and return its id."""
    with Session(engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, tenant)
        grant = issue_grant(
            session,
            tenant=tenant,
            checkout_ref=attempt.checkout,
            payment_attempt_id=attempt.attempt_id,
            operation=operation,
            amount=AMOUNT,
            kernel_decision_id=uuid7(),
            ttl_seconds=TTL,
        )
        grant_id: uuid.UUID = grant.id
        return grant_id


def _binding(tenant: uuid.UUID, attempt: Attempt, operation: Operation) -> GrantBinding:
    return GrantBinding(
        tenant_id=tenant,
        checkout=attempt.checkout,
        payment_attempt_id=attempt.attempt_id,
        operation=operation,
        amount=AMOUNT,
    )


def _grant_row(engine: Engine, tenant: uuid.UUID, grant_id: uuid.UUID) -> tuple[str, object]:
    """Read a grant's status and consumed_at on a fresh connection, not through the ORM."""
    with Session(engine) as session, session.begin():
        set_tenant(session, tenant)
        row = session.execute(
            text("SELECT status, consumed_at FROM execution_grants WHERE id = :id"),
            {"id": grant_id},
        ).one()
        return row.status, row.consumed_at


def _payment_status(engine: Engine, tenant: uuid.UUID, attempt_id: uuid.UUID) -> str:
    with Session(engine) as session, session.begin():
        set_tenant(session, tenant)
        status: str = session.execute(
            text("SELECT status FROM payment_attempts WHERE id = :id"), {"id": attempt_id}
        ).scalar_one()
        return status


def _mode_history(engine: Engine, tenant: uuid.UUID | None) -> list[tuple[str, str, str]]:
    """Every mode record of one scope, oldest first: (mode, reason_code, actor)."""
    clause = "tenant_id IS NULL" if tenant is None else "tenant_id = :t"
    with Session(engine) as session, session.begin():
        rows = session.execute(
            text(
                f"SELECT mode, reason_code, actor FROM platform_operating_modes "  # noqa: S608
                f"WHERE {clause} ORDER BY changed_at, id"
            ),
            {} if tenant is None else {"t": tenant},
        ).all()
        return [(row.mode, row.reason_code, row.actor) for row in rows]


# ------------------------------------------------------- classification (pure, no I/O)


def test_classification_partitions_the_guarded_operations() -> None:
    """Every guarded operation is classified exactly once.

    This is the review-time guard behind the runtime default. ``is_permitted`` consults
    the allowlist, so an operation nobody classified is blocked while Safe Mode is on --
    fail-closed, which is right for a money-moving path and wrong for a buyer-protective
    one. This test makes the omission impossible to merge either way.
    """
    assert SAFE_MODE_BLOCKED.isdisjoint(SAFE_MODE_PERMITTED)
    assert set(GuardedOperation) == SAFE_MODE_BLOCKED | SAFE_MODE_PERMITTED


def test_buyer_protective_operations_are_never_in_the_blocklist() -> None:
    """The kill switch may not reach refunds, reconciliation, support or reads.

    Stated separately from the partition test because it is the substantive rule rather
    than a structural one: a Safe Mode that also stops refunds harms exactly the buyer it
    was thrown to protect.
    """
    protective = {
        GuardedOperation.REFUND_EXECUTE,
        GuardedOperation.RECONCILIATION,
        GuardedOperation.ORDER_TRACKING,
        GuardedOperation.SUPPORT_ACTION,
        GuardedOperation.HUMAN_PRESENT_CHECKOUT,
        GuardedOperation.AUTHORITY_REVOKE,
        GuardedOperation.AUDIT_APPEND,
        GuardedOperation.READ,
    }
    assert protective <= SAFE_MODE_PERMITTED
    assert protective.isdisjoint(SAFE_MODE_BLOCKED)


def test_entry_and_exit_reasons_are_disjoint_and_total() -> None:
    """No reason can serve as both a cause for stopping and a cause for resuming."""
    assert ENTRY_REASONS.isdisjoint(EXIT_REASONS)
    assert set(ModeChangeReason) == ENTRY_REASONS | EXIT_REASONS


def test_refund_grants_are_never_swept_by_activation() -> None:
    """The activation sweep may not include the refund operation.

    Enforced on the constant as well as on behaviour: a future edit that adds
    ``REFUND_EXECUTE`` here would cancel refunds the buyer is already owed.
    """
    assert Operation.REFUND_EXECUTE not in DELEGATED_GRANT_OPERATIONS
    assert Operation.RESERVE_DEBIT in DELEGATED_GRANT_OPERATIONS


def test_unrecognized_stored_mode_reads_as_safe_mode() -> None:
    """A mode value the code does not recognize is treated as the switch being on.

    The check constraint makes this unreachable through the schema, which is why it is
    tested at the helper. The failure is asymmetric: reading a corrupt value as NORMAL
    re-opens delegated debits during an incident, while reading it as SAFE_MODE costs one
    investigation.
    """
    assert _coerce_mode("NORMAL") is OperatingModeName.NORMAL
    assert _coerce_mode("SAFE_MODE") is OperatingModeName.SAFE_MODE
    assert _coerce_mode("PARTIALLY_DEGRADED") is OperatingModeName.SAFE_MODE
    assert _coerce_mode("") is OperatingModeName.SAFE_MODE
    assert _coerce_mode("normal") is OperatingModeName.SAFE_MODE


# --------------------------------------------------------------- what the switch stops


def test_empty_history_is_normal_and_permits_everything(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """A platform that has never declared an incident is not in one."""
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        resolution = resolve_mode(session, seed.tenant_a)
        assert resolution.mode is OperatingModeName.NORMAL
        assert resolution.decided_by is None
        assert banner(resolution) is None
        for operation in GuardedOperation:
            assert is_permitted(session, seed.tenant_a, operation) == (True, RecoveryCode.OK)


@pytest.mark.parametrize("operation", sorted(SAFE_MODE_BLOCKED))
def test_safe_mode_blocks_machine_initiated_money_movement(
    seed: Seed,
    kernel_engine: Engine,
    open_session: Callable[[], Session],
    operation: GuardedOperation,
) -> None:
    """Every delegated / autonomous path is refused with SAFE_MODE_ACTIVE."""
    _enter_global(kernel_engine, ModeChangeReason.KEY_COMPROMISE_EVIDENCE)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        assert is_permitted(session, seed.tenant_a, operation) == (
            False,
            RecoveryCode.SAFE_MODE_ACTIVE,
        )


@pytest.mark.parametrize("operation", sorted(SAFE_MODE_PERMITTED))
def test_safe_mode_keeps_buyer_protective_paths_available(
    seed: Seed,
    kernel_engine: Engine,
    open_session: Callable[[], Session],
    operation: GuardedOperation,
) -> None:
    """Refunds, reconciliation, tracking, support, human-present checkout and reads survive.

    This is the test that would fail if Safe Mode were implemented as a blanket denial.
    A kill switch that also blocks refunds harms the buyer it is meant to protect.
    """
    _enter_global(kernel_engine, ModeChangeReason.PROVIDER_INCIDENT_DECLARED)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        assert is_permitted(session, seed.tenant_a, operation) == (True, RecoveryCode.OK)


def test_assert_permitted_raises_carrying_the_deciding_record(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """The fail-closed form stops the caller and shows which record stopped it."""
    _enter_global(kernel_engine, ModeChangeReason.ABNORMAL_DUPLICATE_ATTEMPTS)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        # Permitted operations return quietly; only the blocked one raises.
        assert_permitted(session, seed.tenant_a, GuardedOperation.REFUND_EXECUTE)
        with pytest.raises(SafeModeBlockedError) as caught:
            assert_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT)

    assert caught.value.code is RecoveryCode.SAFE_MODE_ACTIVE
    resolution = caught.value.resolution
    assert resolution.scope is ModeScope.GLOBAL
    assert resolution.reason_code == ModeChangeReason.ABNORMAL_DUPLICATE_ATTEMPTS.value
    assert resolution.actor == OPERATOR


def test_is_permitted_refuses_an_unclassified_operation_name(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """A bare string is refused rather than matched by StrEnum coincidence.

    ``GuardedOperation`` is a StrEnum, so ``"READ"`` compares and hashes equal to
    ``GuardedOperation.READ``. Without the explicit type check, a caller inventing an
    operation name would be granted whatever the coincidence produced.
    """
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(ValueError, match="GuardedOperation"):
            is_permitted(session, seed.tenant_a, "READ")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="GuardedOperation"):
            is_permitted(session, seed.tenant_a, "DELEGATED_DEBIT")  # type: ignore[arg-type]


# --------------------------------------------------------------- grants and evidence


def test_activation_revokes_the_unused_delegated_grant(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """An unused Reserve Pay grant cannot be spent after activation."""
    grant_id = _issue_committed(kernel_engine, seed.tenant_a, seed.debit_a, Operation.RESERVE_DEBIT)

    _record_id, revoked = _enter_tenant(
        kernel_engine, seed.tenant_a, ModeChangeReason.KEY_COMPROMISE_EVIDENCE
    )
    assert revoked == (grant_id,)

    status, _consumed_at = _grant_row(kernel_engine, seed.tenant_a, grant_id)
    assert status == GrantStatus.REVOKED

    # The status column is not the guarantee; refusal at consumption is.
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(GrantRevokedError) as caught:
            consume_grant(
                session, grant_id, _binding(seed.tenant_a, seed.debit_a, Operation.RESERVE_DEBIT)
            )
    assert caught.value.code is RecoveryCode.AUTHORITY_REVOKED


def test_activation_leaves_a_refund_grant_consumable(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """A refund already admitted still completes while the switch is on.

    The single most important thing Safe Mode must not break: a buyer owed money during
    an incident is the buyer least able to wait for the incident to end.
    """
    grant_id = _issue_committed(
        kernel_engine, seed.tenant_a, seed.refund_a, Operation.REFUND_EXECUTE
    )
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.PROVIDER_INCIDENT_DECLARED)

    status, _consumed_at = _grant_row(kernel_engine, seed.tenant_a, grant_id)
    assert status == GrantStatus.ISSUED

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        consumed = consume_grant(
            session, grant_id, _binding(seed.tenant_a, seed.refund_a, Operation.REFUND_EXECUTE)
        )
        assert consumed.status == GrantStatus.CONSUMED


def test_the_sweep_cannot_be_pointed_at_refund_grants_by_its_caller(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """The no-refund-sweep rule has to hold against the API, not just the default argument.

    ``enter_safe_mode`` takes a ``revoke_operations`` override. Without a guard, one call
    passing ``{REFUND_EXECUTE}`` revoked a live refund grant and the refund was then
    refused at ``consume_grant`` -- the exact harm the asymmetry exists to prevent, reached
    through the front door. The refusal happens before the mode record is written, so a
    refused activation leaves no history claiming it happened.
    """
    grant_id = _issue_committed(
        kernel_engine, seed.tenant_a, seed.refund_a, Operation.REFUND_EXECUTE
    )

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(ValueError, match="may not withdraw"):
            enter_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
                actor=OPERATOR,
                revoke_operations={Operation.REFUND_EXECUTE, Operation.RESERVE_DEBIT},
            )
        assert _mode_history_in(session, seed.tenant_a) == []

    assert Operation.REFUND_EXECUTE in NEVER_SWEPT
    status, _consumed_at = _grant_row(kernel_engine, seed.tenant_a, grant_id)
    assert status == GrantStatus.ISSUED


def test_a_global_activation_sweeps_nothing_and_stops_the_debit_at_admission(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """The module's central claim: the sweep is cleanup, the enforcement point is admission.

    A transaction with no tenant bound cannot see any tenant's grants under RLS, so a
    platform-wide activation revokes nothing and an already-issued delegated grant stays
    ``ISSUED`` and technically consumable. That is only safe because admission asks
    ``is_permitted`` before it consumes anything, which is what this pins: the grant
    survives, and the debit is refused anyway.
    """
    grant_id = _issue_committed(kernel_engine, seed.tenant_a, seed.debit_a, Operation.RESERVE_DEBIT)

    with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
        activation = enter_safe_mode(
            session,
            tenant=None,
            reason=ModeChangeReason.KEY_COMPROMISE_EVIDENCE,
            actor=OPERATOR,
        )
    assert activation.revoked_grant_ids == ()

    status, _consumed_at = _grant_row(kernel_engine, seed.tenant_a, grant_id)
    assert status == GrantStatus.ISSUED

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
            False,
            RecoveryCode.SAFE_MODE_ACTIVE,
        )
        with pytest.raises(SafeModeBlockedError):
            assert_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT)


def test_activation_does_not_erase_evidence_of_a_consumed_grant(
    seed: Seed, kernel_engine: Engine
) -> None:
    """A grant already spent keeps its CONSUMED status and its consumed_at.

    Rewriting it to REVOKED would make the record claim an operation was withdrawn when
    it was in fact sent to the provider, which is the opposite of what an audit needs
    after an incident.
    """
    grant_id = _issue_committed(
        kernel_engine, seed.tenant_a, seed.consumed_a, Operation.RESERVE_DEBIT
    )
    with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, seed.tenant_a)
        consume_grant(
            session, grant_id, _binding(seed.tenant_a, seed.consumed_a, Operation.RESERVE_DEBIT)
        )
    before_status, before_consumed_at = _grant_row(kernel_engine, seed.tenant_a, grant_id)
    assert before_status == GrantStatus.CONSUMED
    assert before_consumed_at is not None

    _record_id, revoked = _enter_tenant(
        kernel_engine, seed.tenant_a, ModeChangeReason.SIGNATURE_VERIFICATION_ANOMALY
    )
    assert grant_id not in revoked

    after_status, after_consumed_at = _grant_row(kernel_engine, seed.tenant_a, grant_id)
    assert after_status == GrantStatus.CONSUMED
    assert after_consumed_at == before_consumed_at


def test_activation_does_not_convert_an_unknown_payment_to_failed(
    seed: Seed, kernel_engine: Engine
) -> None:
    """An UNKNOWN outcome stays UNKNOWN, and reconciliation stays permitted.

    A provider request timing out during an incident is not evidence that the buyer's
    money stayed put. Marking it FAILED would tell the buyer their payment did not go
    through while the charge sits on their statement.
    """
    assert _payment_status(kernel_engine, seed.tenant_a, seed.unknown_a.attempt_id) == "UNKNOWN"

    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.UNRESOLVED_RECONCILIATION_BACKLOG)

    assert _payment_status(kernel_engine, seed.tenant_a, seed.unknown_a.attempt_id) == "UNKNOWN"
    with Session(kernel_engine) as session, session.begin():
        set_tenant(session, seed.tenant_a)
        assert is_permitted(session, seed.tenant_a, GuardedOperation.RECONCILIATION) == (
            True,
            RecoveryCode.OK,
        )


def test_leaving_does_not_resurrect_revoked_grants(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """Standing down re-opens admission, not the capabilities the incident killed.

    A grant minted before an incident was authorized under conditions the incident
    invalidated. The delegated debit must be re-admitted, which re-checks the
    reservation, the approval and the authority epoch from scratch.
    """
    grant_id = _issue_committed(kernel_engine, seed.tenant_a, seed.debit_a, Operation.RESERVE_DEBIT)
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.KEY_COMPROMISE_EVIDENCE)
    _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.INCIDENT_RESOLVED)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        # Admission is open again ...
        assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
            True,
            RecoveryCode.OK,
        )
        # ... but the withdrawn grant is still dead.
        with pytest.raises(GrantRevokedError):
            consume_grant(
                session, grant_id, _binding(seed.tenant_a, seed.debit_a, Operation.RESERVE_DEBIT)
            )


def test_mode_history_is_append_only(seed: Seed, kernel_engine: Engine) -> None:
    """Entering and leaving append records; neither rewrites its predecessor.

    The history is the audit trail for the kill switch. A mode change implemented as an
    UPDATE would leave no evidence of who stopped payments, when, or why.
    """
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.PROVIDER_INCIDENT_DECLARED)
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.KEY_COMPROMISE_EVIDENCE, "ops:bo")
    _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.INCIDENT_RESOLVED)

    assert _mode_history(kernel_engine, seed.tenant_a) == [
        ("SAFE_MODE", ModeChangeReason.PROVIDER_INCIDENT_DECLARED.value, OPERATOR),
        ("SAFE_MODE", ModeChangeReason.KEY_COMPROMISE_EVIDENCE.value, "ops:bo"),
        ("NORMAL", ModeChangeReason.INCIDENT_RESOLVED.value, OPERATOR),
    ]


def test_changed_at_is_written_by_the_database_clock(seed: Seed, kernel_engine: Engine) -> None:
    """The record's timestamp is the server's, not the pod's.

    Precedence between a global and a per-tenant record is decided by comparing exactly
    these timestamps. An application-supplied one would let a pod with a skewed clock
    backdate a platform-wide activation so that it silently lost to the stale tenant
    record it was thrown to override.
    """
    with Session(kernel_engine, expire_on_commit=False) as session, session.begin():
        set_tenant(session, seed.tenant_a)
        record = enter_safe_mode(
            session,
            tenant=seed.tenant_a,
            reason=ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
            actor=OPERATOR,
        ).record
        server_now = session.execute(text("SELECT now()")).scalar_one()

    # now() is the transaction timestamp, so an insert in this transaction that took its
    # default from the server clock matches it exactly.
    assert record.changed_at == server_now
    assert isinstance(record.changed_at, datetime)
    assert record.changed_at.tzinfo is not None


# ------------------------------------------------------------------ scope precedence


def test_tenant_safe_mode_overrides_a_global_normal(
    seed: Seed, kernel_engine: Engine, admin_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """A tenant may stop its own delegated payments while the platform runs normally."""
    global_id = _leave_global(kernel_engine, ModeChangeReason.OPERATOR_STOOD_DOWN)
    _backdate_mode(admin_engine, global_id, 100)
    tenant_id, _revoked = _enter_tenant(
        kernel_engine, seed.tenant_a, ModeChangeReason.ABNORMAL_DUPLICATE_ATTEMPTS
    )
    _backdate_mode(admin_engine, tenant_id, 10)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        resolution = resolve_mode(session, seed.tenant_a)
        assert resolution.mode is OperatingModeName.SAFE_MODE
        assert resolution.scope is ModeScope.TENANT
        assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
            False,
            RecoveryCode.SAFE_MODE_ACTIVE,
        )


def test_a_later_tenant_normal_exempts_that_tenant_from_a_global_safe_mode(
    seed: Seed, kernel_engine: Engine, admin_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """An operator may deliberately exempt one tenant from a platform incident.

    The exemption is an audited per-tenant action taken *after* the global declaration,
    which is what distinguishes it from a stale row silently vetoing a kill switch.
    """
    global_id = _enter_global(kernel_engine, ModeChangeReason.PROVIDER_INCIDENT_DECLARED)
    _backdate_mode(admin_engine, global_id, 100)
    tenant_id = _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.TENANT_EXEMPTED)
    _backdate_mode(admin_engine, tenant_id, 10)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        resolution = resolve_mode(session, seed.tenant_a)
        assert resolution.mode is OperatingModeName.NORMAL
        assert resolution.scope is ModeScope.TENANT
        # Both records are carried, so the exemption is legible next to what it overrode.
        assert resolution.global_record is not None
        assert resolution.global_record.mode is OperatingModeName.SAFE_MODE
        assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
            True,
            RecoveryCode.OK,
        )


def test_a_later_global_safe_mode_covers_a_tenant_with_a_stale_normal(
    seed: Seed, kernel_engine: Engine, admin_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """The kill switch is not vetoed by a per-tenant NORMAL written before it.

    Without this rule a tenant exempted during last month's incident would keep charging
    through every platform incident afterwards, silently, until somebody noticed.
    """
    tenant_id = _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.TENANT_EXEMPTED)
    _backdate_mode(admin_engine, tenant_id, 100)
    global_id = _enter_global(kernel_engine, ModeChangeReason.KEY_COMPROMISE_EVIDENCE)
    _backdate_mode(admin_engine, global_id, 10)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        resolution = resolve_mode(session, seed.tenant_a)
        assert resolution.mode is OperatingModeName.SAFE_MODE
        assert resolution.scope is ModeScope.GLOBAL
        assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
            False,
            RecoveryCode.SAFE_MODE_ACTIVE,
        )
        # The kill switch still may not reach a refund.
        assert is_permitted(session, seed.tenant_a, GuardedOperation.REFUND_EXECUTE) == (
            True,
            RecoveryCode.OK,
        )


def test_a_global_stand_down_does_not_lift_a_tenants_own_safe_mode(
    seed: Seed, kernel_engine: Engine, admin_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """De-escalation does not propagate downward.

    The platform's incident ending is not evidence that this tenant's incident ended. A
    tenant leaves Safe Mode only by its own audited action.
    """
    tenant_id, _revoked = _enter_tenant(
        kernel_engine, seed.tenant_a, ModeChangeReason.SIGNATURE_VERIFICATION_ANOMALY
    )
    _backdate_mode(admin_engine, tenant_id, 100)
    global_id = _leave_global(kernel_engine, ModeChangeReason.INCIDENT_RESOLVED)
    _backdate_mode(admin_engine, global_id, 10)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        resolution = resolve_mode(session, seed.tenant_a)
        assert resolution.mode is OperatingModeName.SAFE_MODE
        assert resolution.scope is ModeScope.TENANT
        assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
            False,
            RecoveryCode.SAFE_MODE_ACTIVE,
        )


def test_the_tenant_record_wins_a_tie(
    seed: Seed, kernel_engine: Engine, admin_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """At equal timestamps the more specific scope decides.

    Records written in one transaction share ``now()`` exactly, so the tie is reachable
    rather than theoretical, and ``>`` rather than ``>=`` in the override clause is what
    makes the specific scope win it.
    """
    global_id = _enter_global(kernel_engine, ModeChangeReason.PROVIDER_INCIDENT_DECLARED)
    tenant_id = _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.TENANT_EXEMPTED)
    _align_mode_clocks(admin_engine, global_id, tenant_id)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        resolution = resolve_mode(session, seed.tenant_a)
        assert resolution.mode is OperatingModeName.NORMAL
        assert resolution.scope is ModeScope.TENANT


def test_a_tie_inside_one_scope_resolves_to_safe_mode(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """Two records of one scope written in one transaction share ``changed_at`` exactly.

    Insert order cannot be recovered from the ids: ``uuid7`` is a millisecond timestamp
    plus ten random bytes with no intra-millisecond counter, so two ids minted
    microseconds apart sort backwards about half the time. Ordering by ``id DESC`` alone
    therefore made a thrown kill switch read as ``NORMAL`` on a coin flip -- observed at
    3 of 12 attempts. The tie is broken by safety instead: ``SAFE_MODE`` wins.

    Looped, because a regression to the coin flip would pass a single attempt half the
    time. Each iteration's pair is strictly newer than the last, so the newest pair always
    decides.
    """
    for _ in range(12):
        with Session(kernel_engine) as session, session.begin():
            set_tenant(session, seed.tenant_a)
            leave_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.OPERATOR_STOOD_DOWN,
                actor=OPERATOR,
            )
            enter_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.KEY_COMPROMISE_EVIDENCE,
                actor=OPERATOR,
            )

        session = open_session()
        with session.begin():
            set_tenant(session, seed.tenant_a)
            assert current_mode(session, seed.tenant_a) is OperatingModeName.SAFE_MODE
            assert is_permitted(session, seed.tenant_a, GuardedOperation.DELEGATED_DEBIT) == (
                False,
                RecoveryCode.SAFE_MODE_ACTIVE,
            )


def test_a_stand_down_shares_no_transaction_with_the_entry_it_lifts(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """The other half of the tie rule, stated so nobody mistakes it for a bug.

    Entering and leaving in one transaction leaves the scope in Safe Mode, because the two
    records are indistinguishable by time and the tie is resolved fail-closed. The
    stand-down has to be its own transaction, and then it works.
    """
    with Session(kernel_engine) as session, session.begin():
        set_tenant(session, seed.tenant_a)
        enter_safe_mode(
            session,
            tenant=seed.tenant_a,
            reason=ModeChangeReason.PROVIDER_INCIDENT_DECLARED,
            actor=OPERATOR,
        )
        leave_safe_mode(
            session,
            tenant=seed.tenant_a,
            reason=ModeChangeReason.INCIDENT_RESOLVED,
            actor=OPERATOR,
        )

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        assert current_mode(session, seed.tenant_a) is OperatingModeName.SAFE_MODE

    _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.INCIDENT_RESOLVED)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        assert current_mode(session, seed.tenant_a) is OperatingModeName.NORMAL


def test_a_tenants_safe_mode_does_not_reach_another_tenant(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """Per-tenant Safe Mode is per tenant."""
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.KEY_COMPROMISE_EVIDENCE)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_b)
        assert current_mode(session, seed.tenant_b) is OperatingModeName.NORMAL
        assert is_permitted(session, seed.tenant_b, GuardedOperation.DELEGATED_DEBIT) == (
            True,
            RecoveryCode.OK,
        )


def test_global_safe_mode_applies_to_a_tenant_with_no_record_of_its_own(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    _enter_global(kernel_engine, ModeChangeReason.PROVIDER_INCIDENT_DECLARED)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_b)
        resolution = resolve_mode(session, seed.tenant_b)
        assert resolution.mode is OperatingModeName.SAFE_MODE
        assert resolution.scope is ModeScope.GLOBAL
        assert resolution.tenant_record is None


def test_both_scopes_are_read_in_exactly_one_statement(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """Precedence is computed over one snapshot, so it is computed on a state that existed.

    Under READ COMMITTED each statement takes its own snapshot. Reading the global record
    and the tenant record separately could observe a global activation that the tenant read
    did not, and precedence computed across two snapshots is precedence computed on a state
    that never existed. Splitting the ``DISTINCT ON`` into two selects passed every other
    test in this file, so the property is pinned here directly: one statement, one snapshot.
    """
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.KEY_COMPROMISE_EVIDENCE)

    statements: list[str] = []

    def record_statement(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if "platform_operating_modes" in statement:
            statements.append(statement)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        event.listen(kernel_engine, "before_cursor_execute", record_statement)
        try:
            resolution = resolve_mode(session, seed.tenant_a)
        finally:
            event.remove(kernel_engine, "before_cursor_execute", record_statement)

    assert resolution.mode is OperatingModeName.SAFE_MODE
    assert resolution.scope is ModeScope.TENANT
    assert len(statements) == 1, statements


# ------------------------------------------------------------- authority and attribution


def test_an_agent_cannot_enter_or_leave_safe_mode(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """Specification 10.3.2: the LLM cannot enter or leave Safe Mode.

    Structural, not authentication: it stops the ordinary accident of an agent-facing
    tool wired straight through to the kill switch.
    """
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(ValueError, match="AGENT"):
            enter_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.KEY_COMPROMISE_EVIDENCE,
                actor="agent:shopper",
                actor_type=ActorType.AGENT,
            )
        with pytest.raises(ValueError, match="AGENT"):
            leave_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.INCIDENT_RESOLVED,
                actor="agent:shopper",
                actor_type=ActorType.AGENT,
            )
        assert _mode_history_in(session, seed.tenant_a) == []


def _mode_history_in(session: Session, tenant: uuid.UUID) -> list[str]:
    rows = session.execute(
        text("SELECT mode FROM platform_operating_modes WHERE tenant_id = :t"), {"t": tenant}
    ).all()
    return [row.mode for row in rows]


def test_an_unattributed_mode_change_is_refused(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """A blank actor is not the audited administrative action the specification requires."""
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        for blank in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="actor is required"):
                enter_safe_mode(
                    session,
                    tenant=seed.tenant_a,
                    reason=ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
                    actor=blank,
                )


def test_entering_requires_an_entry_reason_and_leaving_an_exit_reason(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """The history may not record a stand-down caused by key-compromise evidence.

    A record that reads as an incident causing a resumption is worse than no record,
    because a reviewer will believe it.
    """
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(ValueError, match="not an entry reason"):
            enter_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.INCIDENT_RESOLVED,
                actor=OPERATOR,
            )
        with pytest.raises(ValueError, match="not an exit reason"):
            leave_safe_mode(
                session,
                tenant=seed.tenant_a,
                reason=ModeChangeReason.KEY_COMPROMISE_EVIDENCE,
                actor=OPERATOR,
            )


def test_a_tenant_bound_transaction_cannot_flip_the_platform_switch(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """The global switch is unreachable from a tenant-scoped request path.

    ``platform_operating_modes`` is outside row-level security because its tenant is
    nullable, so this check is the only thing between one merchant's traffic and a
    platform-wide stop.
    """
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(SafeModeScopeError, match="no tenant bound"):
            enter_safe_mode(
                session,
                tenant=None,
                reason=ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
                actor=OPERATOR,
            )
        assert _mode_history_in(session, seed.tenant_a) == []


def test_a_tenant_mode_change_must_match_the_bound_tenant(
    seed: Seed, open_session: Callable[[], Session]
) -> None:
    """One tenant's transaction cannot stop another tenant's payments."""
    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(SafeModeScopeError):
            enter_safe_mode(
                session,
                tenant=seed.tenant_b,
                reason=ModeChangeReason.KEY_COMPROMISE_EVIDENCE,
                actor=OPERATOR,
            )


def test_reading_another_tenants_mode_is_refused(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """A tenant-bound transaction may ask about itself or the platform, nothing else."""
    _enter_tenant(kernel_engine, seed.tenant_b, ModeChangeReason.KEY_COMPROMISE_EVIDENCE)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        with pytest.raises(SafeModeScopeError):
            resolve_mode(session, seed.tenant_b)
        with pytest.raises(SafeModeScopeError):
            is_permitted(session, seed.tenant_b, GuardedOperation.DELEGATED_DEBIT)
        # The platform scope stays readable, and tenant B's incident has not leaked into it.
        assert current_mode(session, None) is OperatingModeName.NORMAL


# ------------------------------------------------------------------------------ banner


def test_banner_carries_the_reason_actor_and_scope(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """Specification 10.3.2 requires a visible banner with a reason code and an actor.

    Structured fields only. Nothing here is a sentence, so no surface -- and no model --
    can restate the platform's payment posture in words of its own.
    """
    _enter_tenant(
        kernel_engine, seed.tenant_a, ModeChangeReason.PROVIDER_INCIDENT_DECLARED, "ops:carol"
    )

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        rendered = banner(resolve_mode(session, seed.tenant_a))

    assert rendered is not None
    assert rendered.scope is ModeScope.TENANT
    assert rendered.tenant_id == seed.tenant_a
    assert rendered.reason_code == ModeChangeReason.PROVIDER_INCIDENT_DECLARED.value
    assert rendered.actor == "ops:carol"
    assert rendered.since is not None
    assert GuardedOperation.DELEGATED_DEBIT in rendered.blocked
    assert GuardedOperation.REFUND_EXECUTE in rendered.still_available
    assert GuardedOperation.HUMAN_PRESENT_CHECKOUT in rendered.still_available
    assert set(rendered.blocked).isdisjoint(rendered.still_available)


def test_banner_is_absent_under_normal(
    seed: Seed, kernel_engine: Engine, open_session: Callable[[], Session]
) -> None:
    """No incident, no banner -- including after a stand-down."""
    _enter_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.OPERATOR_DECLARED_INCIDENT)
    _leave_tenant(kernel_engine, seed.tenant_a, ModeChangeReason.INCIDENT_RESOLVED)

    session = open_session()
    with session.begin():
        set_tenant(session, seed.tenant_a)
        assert banner(resolve_mode(session, seed.tenant_a)) is None

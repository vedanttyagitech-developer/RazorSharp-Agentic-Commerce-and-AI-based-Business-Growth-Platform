"""Hash-chained append-only audit tests, specification 26.2.

These run against real PostgreSQL as ``commerce_test_kernel`` -- a NOSUPERUSER,
NOBYPASSRLS login role. Nothing here would be meaningful on SQLite: the invariants are
about a server clock, an advisory lock, a unique constraint under contention, row-level
security, and a grant set that denies UPDATE and DELETE on the evidence table.

The four invariants under test, and the failure each one prevents:

1. ``self_hash`` chains each event to its predecessor. Without the chain, a row can be
   edited, removed or moved and the remaining rows still verify, so the audit proves
   nothing about what happened.
2. ``verify_chain`` names the FIRST broken link. All three tamper modes are exercised --
   an edited payload, a deleted middle row, and a reordered pair whose hashes were
   recomputed by the tamperer. The third is the one that fails if only per-row hashing
   were implemented and the chain were decorative.
3. ``seq`` is gapless and strictly increasing, including under real contention. Two
   events claiming one position in a stream would make the order of a money movement
   arguable.
4. The audit row is written in the caller's transaction. Evidence that can commit while
   the state change rolls back (or the reverse) is worse than no evidence, because it
   is believed.

``audit_events`` has no UPDATE or DELETE grant for any application role -- asserted
below rather than assumed -- so every tamper here is applied through the owner
connection, which is the realistic threat: an operator, a restored backup, a replica.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from platform_db import TenantContextError, set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel.audit import (
    ENVELOPE_SCHEMA,
    FIRST_SEQ,
    AuditConcurrencyError,
    AuditContentError,
    AuditEventView,
    AuditTenantError,
    AuditUsageError,
    BreakKind,
    append,
    compute_self_hash,
    envelope_of,
    event_envelope,
    head,
    read_stream,
    verify_chain,
)
from transaction_kernel.contracts import ActorType
from transaction_kernel.recovery import RecoveryCode

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Seeding, tampering and teardown run as the owner. Application roles deliberately have
# no UPDATE or DELETE on audit_events, which is exactly the guarantee under test, so a
# tamper simulated through an application role would prove the opposite of what it looks
# like it proves.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

AGGREGATE = "CHECKOUT"


def _require_db(url: str, *, pool_size: int = 1) -> Engine:
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=0)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for audit tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    """Six connections: the contention tests need genuinely separate sessions."""
    engine = _require_db(KERNEL_URL, pool_size=6)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    # A superuser bypasses row-level security unconditionally, so a suite run as one
    # would pass while proving nothing about the tenant scoping of a stream read.
    assert row.rolsuper is False, "audit tests must not run as a superuser"
    assert row.rolbypassrls is False, "audit tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL, pool_size=2)


@pytest.fixture
def sessions(kernel_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=kernel_engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    s = sessions()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def make_tenant(admin_engine: Engine) -> Iterator[Callable[[], uuid.UUID]]:
    """Factory for tenants, all removed afterwards along with their audit rows."""
    created: list[uuid.UUID] = []

    def _make() -> uuid.UUID:
        tenant_id = uuid.uuid4()
        slug = f"au-{tenant_id.hex[:8]}"
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :slug, :name, 'asia-south1')"
                ),
                {"id": tenant_id, "slug": slug, "name": slug},
            )
        created.append(tenant_id)
        return tenant_id

    yield _make

    with admin_engine.begin() as conn:
        for tenant_id in created:
            # audit_events is RLS-protected and forced even for the owner, so the delete
            # needs the tenant bound or it matches nothing and the tenant delete then
            # fails on the foreign key.
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
        conn.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": created})


@pytest.fixture
def tenant(make_tenant: Callable[[], uuid.UUID]) -> uuid.UUID:
    return make_tenant()


# ------------------------------------------------------------------------------ helpers


def emit(
    session: Session,
    tenant_id: uuid.UUID,
    aggregate_id: uuid.UUID,
    *,
    event_type: str = "CHECKOUT_QUOTED",
    payload: dict[str, Any] | None = None,
    aggregate_type: str = AGGREGATE,
    actor_type: ActorType = ActorType.AGENT,
    principal_id: str | None = "agent/shopper-1",
    correlation_id: uuid.UUID | None = None,
) -> AuditEventView:
    """Append one event with the boilerplate filled in."""
    return append(
        session,
        tenant=tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor_type=actor_type,
        principal_id=principal_id,
        payload=payload if payload is not None else {"note": "quoted"},
        correlation_id=correlation_id or uuid7(),
    )


def emit_committed(
    sessions: sessionmaker[Session],
    tenant_id: uuid.UUID,
    aggregate_id: uuid.UUID,
    count: int,
) -> list[AuditEventView]:
    """Commit ``count`` events onto one stream, one transaction each."""
    written: list[AuditEventView] = []
    for index in range(count):
        s = sessions()
        try:
            with s.begin():
                set_tenant(s, tenant_id)
                written.append(
                    emit(
                        s,
                        tenant_id,
                        aggregate_id,
                        event_type=f"STEP_{index + 1}",
                        payload={"step": index + 1},
                    )
                )
        finally:
            s.close()
    return written


def as_admin(admin: Engine, tenant_id: uuid.UUID, statement: str, params: dict[str, Any]) -> None:
    """Run one statement through the owner connection with the tenant bound.

    This is how every tamper below is applied: audit_events denies UPDATE and DELETE to
    every application role, so the realistic attacker is someone holding owner
    credentials, a backup, or a replica.
    """
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(text(statement), params)


def rows_for(admin: Engine, tenant_id: uuid.UUID, aggregate_id: uuid.UUID) -> int:
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return int(
            conn.execute(
                text("SELECT count(*) FROM audit_events WHERE aggregate_id = :a"),
                {"a": aggregate_id},
            ).scalar_one()
        )


def verify(session: Session, tenant_id: uuid.UUID, aggregate_id: uuid.UUID) -> Any:
    return verify_chain(
        session, tenant=tenant_id, aggregate_type=AGGREGATE, aggregate_id=aggregate_id
    )


# ------------------------------------------------------------------- 1. the chain itself


class TestChainConstruction:
    def test_first_event_opens_at_seq_one_with_no_predecessor(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        aggregate_id = uuid7()
        with session.begin():
            set_tenant(session, tenant)
            event = emit(session, tenant, aggregate_id)

        assert event.seq == FIRST_SEQ == 1
        # A genesis event that carried a prev_hash would let a stream be spliced onto
        # somebody else's history and still verify.
        assert event.prev_hash is None
        assert event.self_hash

    def test_each_event_chains_to_its_predecessor(
        self, sessions: sessionmaker[Session], session: Session, tenant: uuid.UUID
    ) -> None:
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 4)

        assert [e.seq for e in written] == [1, 2, 3, 4]
        for earlier, later in zip(written[:-1], written[1:], strict=True):
            assert later.prev_hash == earlier.self_hash

        with session.begin():
            set_tenant(session, tenant)
            stream = read_stream(
                session, tenant=tenant, aggregate_type=AGGREGATE, aggregate_id=aggregate_id
            )
        assert [e.seq for e in stream] == [1, 2, 3, 4]
        assert [e.self_hash for e in stream] == [e.self_hash for e in written]

    def test_self_hash_is_reproducible_from_the_stored_row_alone(
        self, sessions: sessionmaker[Session], session: Session, tenant: uuid.UUID
    ) -> None:
        """An independent verifier must reach the stored hash without this module's state.

        Recomputed here straight from ``commerce_domain.canonical_hash`` over the
        envelope, so a change to how the hash is derived cannot be hidden behind an
        equally changed helper.
        """
        aggregate_id = uuid7()
        emit_committed(sessions, tenant, aggregate_id, 3)

        with session.begin():
            set_tenant(session, tenant)
            stream = read_stream(
                session, tenant=tenant, aggregate_type=AGGREGATE, aggregate_id=aggregate_id
            )

        for event in stream:
            assert canonical_hash(envelope_of(event)) == event.self_hash
        assert stream[0].payload == {"step": 1}

    def test_streams_are_independent_per_aggregate_and_per_type(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """seq is per (tenant, aggregate_type, aggregate_id), not global.

        The same uuid used as both a CHECKOUT and a PAYMENT_ATTEMPT id must open two
        separate chains; sharing one would make either stream unverifiable in isolation.
        """
        shared_id = uuid7()
        other_id = uuid7()
        with session.begin():
            set_tenant(session, tenant)
            a = emit(session, tenant, shared_id, aggregate_type="CHECKOUT")
            b = emit(session, tenant, shared_id, aggregate_type="PAYMENT_ATTEMPT")
            c = emit(session, tenant, other_id, aggregate_type="CHECKOUT")

        assert (a.seq, b.seq, c.seq) == (1, 1, 1)
        assert a.self_hash != b.self_hash, "aggregate_type must be inside the hash"

    def test_streams_are_scoped_to_one_tenant(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        make_tenant: Callable[[], uuid.UUID],
    ) -> None:
        """Two tenants using the same aggregate id keep separate chains.

        Row-level security is what enforces it; this test exists because a stream read
        that leaked across tenants would also chain across them, and one tenant's
        appends would then invalidate another's evidence.
        """
        tenant_a = make_tenant()
        tenant_b = make_tenant()
        shared_id = uuid7()

        emit_committed(sessions, tenant_a, shared_id, 2)
        emit_committed(sessions, tenant_b, shared_id, 1)

        with session.begin():
            set_tenant(session, tenant_a)
            stream_a = read_stream(
                session, tenant=tenant_a, aggregate_type=AGGREGATE, aggregate_id=shared_id
            )
        with session.begin():
            set_tenant(session, tenant_b)
            stream_b = read_stream(
                session, tenant=tenant_b, aggregate_type=AGGREGATE, aggregate_id=shared_id
            )

        assert [e.seq for e in stream_a] == [1, 2]
        assert [e.seq for e in stream_b] == [1]
        assert stream_b[0].prev_hash is None, "tenant B's stream must not chain onto A's"
        assert {e.event_id for e in stream_a}.isdisjoint({e.event_id for e in stream_b})

    def test_head_reports_the_newest_event(
        self, sessions: sessionmaker[Session], session: Session, tenant: uuid.UUID
    ) -> None:
        aggregate_id = uuid7()
        with session.begin():
            set_tenant(session, tenant)
            assert (
                head(session, tenant=tenant, aggregate_type=AGGREGATE, aggregate_id=aggregate_id)
                is None
            )

        written = emit_committed(sessions, tenant, aggregate_id, 3)
        with session.begin():
            set_tenant(session, tenant)
            current = head(
                session, tenant=tenant, aggregate_type=AGGREGATE, aggregate_id=aggregate_id
            )
        assert current is not None
        assert (current.seq, current.self_hash) == (3, written[-1].self_hash)


class TestEnvelopeCoverage:
    """Every meaningful column must move the hash. A field outside the envelope could be
    edited in place and the row would still verify."""

    @staticmethod
    def _base() -> dict[str, Any]:
        return {
            "tenant_id": uuid.UUID(int=1),
            "aggregate_type": "CHECKOUT",
            "aggregate_id": uuid.UUID(int=2),
            "seq": 4,
            "event_type": "PAYMENT_AUTHORIZED",
            "actor_type": str(ActorType.SYSTEM),
            "actor_id": "kernel",
            "principal_id": "agent/shopper-1",
            "payload": {"amount_minor": 129900, "currency": "INR"},
            "prev_hash": "cGxhY2Vob2xkZXItcHJldmlvdXMtaGFzaA",
            "correlation_id": uuid.UUID(int=3),
            "occurred_at_ms": 1_788_472_597_586,
        }

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("tenant_id", uuid.UUID(int=99)),
            ("aggregate_type", "PAYMENT_ATTEMPT"),
            ("aggregate_id", uuid.UUID(int=99)),
            ("seq", 5),
            ("event_type", "PAYMENT_CAPTURED"),
            ("actor_type", str(ActorType.BUYER)),
            ("actor_id", "someone-else"),
            ("principal_id", "agent/shopper-2"),
            ("payload", {"amount_minor": 129901, "currency": "INR"}),
            ("prev_hash", "YW5vdGhlci1wcmV2aW91cy1oYXNoLXZhbHVl"),
            ("correlation_id", uuid.UUID(int=99)),
            ("occurred_at_ms", 1_788_472_597_587),
        ],
    )
    def test_changing_any_hashed_field_changes_the_hash(self, field: str, value: Any) -> None:
        base = self._base()
        altered = {**base, field: value}
        assert compute_self_hash(event_envelope(**base)) != compute_self_hash(
            event_envelope(**altered)
        ), f"{field} is not covered by the hash, so it could be edited undetectably"

    def test_a_null_field_and_an_absent_field_cannot_hash_alike(self) -> None:
        """``principal_id=None`` is recorded as a null key, never by omitting the key.

        If the key were dropped when null, an event attributed to nobody and an event
        whose attribution was deleted would produce identical bytes.
        """
        envelope = event_envelope(**{**self._base(), "principal_id": None})
        assert "principal_id" in envelope
        assert envelope["principal_id"] is None
        assert envelope["schema"] == ENVELOPE_SCHEMA

        # The claim in the name, asserted rather than implied: the two shapes must not
        # produce the same bytes. Structure alone would not prove it -- a canonicaliser
        # that dropped nulls on the way to the digest would still pass the checks above.
        without_the_key = {k: v for k, v in envelope.items() if k != "principal_id"}
        assert compute_self_hash(envelope) != compute_self_hash(without_the_key)

    def test_the_hash_does_not_depend_on_key_order(self) -> None:
        """JCS sorts keys, so two dicts built in different orders must agree.

        Without this, a verifier that rebuilt the payload in a different order would
        report honest evidence as tampered.
        """
        base = self._base()
        forward = {**base, "payload": {"amount_minor": 129900, "currency": "INR"}}
        reversed_payload = {**base, "payload": {"currency": "INR", "amount_minor": 129900}}
        assert compute_self_hash(event_envelope(**forward)) == compute_self_hash(
            event_envelope(**reversed_payload)
        )


# ------------------------------------------------------------------- 2. tamper detection


class TestVerifyChain:
    def test_an_untampered_stream_verifies(
        self, sessions: sessionmaker[Session], session: Session, tenant: uuid.UUID
    ) -> None:
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 5)

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.intact is True
        assert result.first_break is None
        assert result.code is RecoveryCode.OK
        assert (result.length, result.events_verified) == (5, 5)
        assert (result.head_seq, result.head_hash) == (5, written[-1].self_hash)

    def test_an_empty_stream_is_vacuously_intact_and_says_so(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, uuid7())

        assert result.empty is True
        assert result.intact is True
        assert result.head_hash is None

    def test_an_edited_payload_is_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Tamper mode 1: a field is rewritten in place and the stored hash is left alone.

        This is the change a database operator makes to soften what an event says. It
        must be caught at the exact row, with the earlier events still reported verified.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 4)

        as_admin(
            admin_engine,
            tenant,
            "UPDATE audit_events SET payload = CAST(:p AS jsonb) WHERE id = :i",
            {
                "p": json.dumps({"step": 2, "note": "adjusted after the fact"}),
                "i": written[1].event_id,
            },
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.intact is False
        assert result.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.SELF_HASH_MISMATCH
        assert result.first_break.at_seq == 2
        assert result.first_break.event_id == written[1].event_id
        assert result.first_break.found == written[1].self_hash
        # Everything before the edit is still trustworthy, and the head to anchor
        # against is the last event that verified.
        assert result.events_verified == 1
        assert result.head_seq == 1

    def test_editing_the_stored_hash_to_match_the_edit_is_still_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """The tamperer knows the algorithm and recomputes the row's own hash.

        Per-row hashing alone would accept this. The chain is what refuses it: the next
        event still names the ORIGINAL hash as its predecessor.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 3)

        forged_payload = {"step": 2, "note": "adjusted after the fact"}
        forged = compute_self_hash(envelope_of(replace(written[1], payload=forged_payload)))
        as_admin(
            admin_engine,
            tenant,
            "UPDATE audit_events SET payload = CAST(:p AS jsonb), self_hash = :h WHERE id = :i",
            {"p": json.dumps(forged_payload), "h": forged, "i": written[1].event_id},
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.intact is False
        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.PREV_HASH_MISMATCH
        assert result.first_break.at_seq == 3
        assert result.first_break.expected == forged
        assert result.first_break.found == written[1].self_hash
        assert result.events_verified == 2

    def test_a_deleted_middle_row_is_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Tamper mode 2: an inconvenient event is removed, leaving a gap in seq."""
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 4)

        as_admin(
            admin_engine,
            tenant,
            "DELETE FROM audit_events WHERE id = :i",
            {"i": written[1].event_id},
        )
        assert rows_for(admin_engine, tenant, aggregate_id) == 3

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.intact is False
        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.SEQ_GAP
        assert result.first_break.at_seq == 2
        assert (result.first_break.expected, result.first_break.found) == ("2", "3")
        assert result.events_verified == 1
        assert result.length == 3

    def test_a_deleted_opening_row_is_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Removing the head of a stream is reported distinctly from a middle gap.

        The distinction matters to an investigator: a stream that never had a genesis
        event and one whose genesis was deleted look the same to a naive length check.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 3)

        as_admin(
            admin_engine,
            tenant,
            "DELETE FROM audit_events WHERE id = :i",
            {"i": written[0].event_id},
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.SEQ_NOT_FIRST
        assert result.first_break.at_seq == 1
        assert result.events_verified == 0
        assert result.head_hash is None

    def test_a_reordered_pair_is_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Tamper mode 3: two events exchange content, and BOTH hashes are recomputed.

        This is the strongest of the three. Every row still hashes to its own stored
        hash, so per-row integrity is intact; only the link between them is wrong. A
        chain that stored ``prev_hash`` but never compared it would pass this test, which
        is precisely why it is here.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 4)
        second, third = written[1], written[2]

        swapped_second = replace(second, payload=dict(third.payload))
        swapped_third = replace(third, payload=dict(second.payload))
        hash_second = compute_self_hash(envelope_of(swapped_second))
        hash_third = compute_self_hash(envelope_of(swapped_third))

        for event, payload, forged in (
            (second, third.payload, hash_second),
            (third, second.payload, hash_third),
        ):
            as_admin(
                admin_engine,
                tenant,
                "UPDATE audit_events SET payload = CAST(:p AS jsonb), self_hash = :h WHERE id = :i",
                {"p": json.dumps(dict(payload)), "h": forged, "i": event.event_id},
            )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)
            stream = read_stream(
                session, tenant=tenant, aggregate_type=AGGREGATE, aggregate_id=aggregate_id
            )

        # Each tampered row is self-consistent: only the chain gives them away.
        for event in stream[1:3]:
            assert compute_self_hash(envelope_of(event)) == event.self_hash

        assert result.intact is False
        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.PREV_HASH_MISMATCH
        assert result.first_break.at_seq == 3
        assert result.events_verified == 2

    def test_a_forged_genesis_link_is_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Event 1 must have no predecessor.

        Allowing a prev_hash there would let a stream be re-rooted onto a fabricated
        history that no longer exists to contradict it.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 2)

        invented = canonical_hash({"fabricated": "history"})
        forged = compute_self_hash(envelope_of(replace(written[0], prev_hash=invented)))
        as_admin(
            admin_engine,
            tenant,
            "UPDATE audit_events SET prev_hash = :p, self_hash = :h WHERE id = :i",
            {"p": invented, "h": forged, "i": written[0].event_id},
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.GENESIS_PREV_HASH
        assert result.first_break.at_seq == 1
        assert result.first_break.found == invented
        assert result.events_verified == 0

    def test_a_relocated_event_does_not_verify_in_another_stream(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Moving a genuine event to another aggregate breaks it.

        The stream identity is inside the hash, so an event cannot be lifted out of the
        chain that gives it meaning and dropped into one where it reads better.
        """
        source_id = uuid7()
        target_id = uuid7()
        written = emit_committed(sessions, tenant, source_id, 1)
        emit_committed(sessions, tenant, target_id, 1)

        as_admin(
            admin_engine,
            tenant,
            "UPDATE audit_events SET aggregate_id = :a, seq = 2, prev_hash = :p WHERE id = :i",
            {
                "a": target_id,
                "p": read_only_hash(admin_engine, tenant, target_id, 1),
                "i": written[0].event_id,
            },
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, target_id)

        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.SELF_HASH_MISMATCH
        assert result.first_break.at_seq == 2

    def test_a_severed_link_on_a_later_event_is_detected(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """Nulling a later event's prev_hash is a broken link, not a forged genesis.

        Only position 1 may have no predecessor. Erasing the link elsewhere is how a
        tamperer detaches the tail of a stream from the part they rewrote, so it must be
        reported at the event that lost its link, not tolerated as a second genesis.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 3)

        forged = compute_self_hash(envelope_of(replace(written[1], prev_hash=None)))
        as_admin(
            admin_engine,
            tenant,
            "UPDATE audit_events SET prev_hash = NULL, self_hash = :h WHERE id = :i",
            {"h": forged, "i": written[1].event_id},
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.intact is False
        assert result.first_break is not None
        assert result.first_break.kind is BreakKind.PREV_HASH_MISMATCH
        assert result.first_break.at_seq == 2
        assert result.first_break.expected == written[0].self_hash
        assert result.first_break.found is None
        assert result.events_verified == 1

    def test_a_truncated_tail_is_not_detectable_from_the_stream_alone(
        self,
        sessions: sessionmaker[Session],
        session: Session,
        tenant: uuid.UUID,
        admin_engine: Engine,
    ) -> None:
        """An honest limit, asserted so it cannot quietly be claimed otherwise.

        Deleting the newest events leaves a chain that is internally consistent. Nothing
        inside a self-contained hash chain can detect this; the remedy is an anchor kept
        outside the database, which is why ``verify_chain`` reports the head it reached.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 4)

        as_admin(
            admin_engine,
            tenant,
            "DELETE FROM audit_events WHERE id = :i",
            {"i": written[3].event_id},
        )

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)

        assert result.intact is True, "a truncated tail leaves a self-consistent chain"
        # What makes it detectable is the head moving backwards against a published
        # anchor: an operator holding the pre-truncation head sees the difference.
        assert result.head_seq == 3
        assert result.head_hash == written[2].self_hash
        assert result.head_hash != written[3].self_hash


def read_only_hash(admin: Engine, tenant_id: uuid.UUID, aggregate_id: uuid.UUID, seq: int) -> str:
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return str(
            conn.execute(
                text("SELECT self_hash FROM audit_events WHERE aggregate_id = :a AND seq = :s"),
                {"a": aggregate_id, "s": seq},
            ).scalar_one()
        )


# ----------------------------------------------------------- 3. seq under real contention


class TestSequenceIntegrity:
    def test_seq_is_gapless_and_strictly_increasing(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        aggregate_id = uuid7()
        with session.begin():
            set_tenant(session, tenant)
            written = [
                emit(session, tenant, aggregate_id, event_type=f"S{i}", payload={"i": i})
                for i in range(1, 7)
            ]
        assert [e.seq for e in written] == [1, 2, 3, 4, 5, 6]

    def test_a_duplicate_seq_is_refused_by_the_database(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        """The unique constraint is the backstop under the advisory lock.

        Asserted directly because the whole no-duplicate-position guarantee rests on it:
        if the constraint were ever dropped from the schema, the contention test below
        could still pass by luck while this one could not.
        """
        aggregate_id = uuid7()
        written = emit_committed(sessions, tenant, aggregate_id, 1)
        original = written[0]

        with pytest.raises(IntegrityError) as exc:
            as_admin(
                admin_engine,
                tenant,
                "INSERT INTO audit_events (id, tenant_id, aggregate_type, aggregate_id, seq,"
                " event_type, actor_type, payload, self_hash, correlation_id)"
                " VALUES (:id, :t, :at, :a, :s, 'FORGED', 'SYSTEM', '{}'::jsonb, :h, :c)",
                {
                    "id": uuid7(),
                    "t": tenant,
                    "at": AGGREGATE,
                    "a": aggregate_id,
                    "s": original.seq,
                    "h": "x" * 43,
                    "c": uuid7(),
                },
            )
        assert getattr(exc.value.orig, "sqlstate", None) == "23505"

    def test_contending_appends_block_rather_than_race(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        """Real contention, forced rather than hoped for.

        B's append is issued while A's transaction still holds the stream lock, so B must
        wait inside PostgreSQL -- which the elapsed-time assertion checks. Without the
        exclusion both would read the same tail, compute the same seq and the same
        prev_hash, and one of them would be lost or the pair would claim one position.
        """
        aggregate_id = uuid7()
        a_appended = threading.Event()
        b_started = threading.Event()
        seqs: dict[str, int] = {}
        prev: dict[str, str | None] = {}
        elapsed: dict[str, float] = {}

        def run_a() -> None:
            s = sessions()
            try:
                with s.begin():
                    set_tenant(s, tenant)
                    event = emit(s, tenant, aggregate_id, event_type="A", payload={"who": "a"})
                    seqs["a"], prev["a"] = event.seq, event.prev_hash
                    a_appended.set()
                    b_started.wait(timeout=5)
                    # Hold the lock long enough that B is provably blocked, not merely
                    # scheduled later.
                    time.sleep(0.4)
            finally:
                s.close()

        def run_b() -> None:
            s = sessions()
            try:
                a_appended.wait(timeout=5)
                with s.begin():
                    set_tenant(s, tenant)
                    b_started.set()
                    started = time.monotonic()
                    event = emit(s, tenant, aggregate_id, event_type="B", payload={"who": "b"})
                    elapsed["b"] = time.monotonic() - started
                    seqs["b"], prev["b"] = event.seq, event.prev_hash
            finally:
                s.close()

        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "an appender deadlocked"

        assert seqs == {"a": 1, "b": 2}
        assert prev["a"] is None
        assert elapsed["b"] >= 0.3, "B must have waited on A's lock, not raced past it"
        # B chained onto A's committed row, which it could only have read after A's
        # transaction ended.
        assert rows_for(admin_engine, tenant, aggregate_id) == 2

    def test_a_blind_tail_read_raises_rather_than_forging_a_position(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        """The backstop under the advisory lock, exercised through ``append`` itself.

        The advisory lock serializes appenders but cannot make a snapshot-stable
        transaction see a row committed after its snapshot was taken, which is the one
        condition that can defeat it. Reproduced here with two real sessions and no
        patching: A freezes a REPEATABLE READ snapshot, B commits seq 1, and A's tail
        read is then blind to it and computes seq 1 as well.

        The unique constraint must refuse that insert and this module must report it as
        ``CONCURRENT_OPERATION`` from the closed enum -- never retry it silently, which
        would let two events claim one position, and never let it through, which would
        put a second genesis in the stream.
        """
        aggregate_id = uuid7()

        blind = sessions()
        try:
            blind.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            set_tenant(blind, tenant)
            # Establish the snapshot before the competing commit exists.
            blind.execute(text("SELECT 1")).scalar_one()

            emit_committed(sessions, tenant, aggregate_id, 1)

            with pytest.raises(AuditConcurrencyError) as exc:
                emit(blind, tenant, aggregate_id, event_type="BLIND", payload={"who": "blind"})

            assert exc.value.code is RecoveryCode.CONCURRENT_OPERATION
            # The failure was contained in a SAVEPOINT, so the caller's transaction is
            # still live and can roll back the state change this event was recording --
            # rather than having been aborted from under it by the driver.
            assert blind.in_transaction()
            blind.rollback()
        finally:
            blind.close()

        # Exactly one row at seq 1: the refused append left nothing behind.
        assert rows_for(admin_engine, tenant, aggregate_id) == 1

    def test_concurrent_appenders_never_duplicate_a_position(
        self, sessions: sessionmaker[Session], session: Session, tenant: uuid.UUID
    ) -> None:
        """Five unsynchronised threads on one stream produce exactly seq 1..5.

        No forced interleaving here: the threads are free to collide however the
        scheduler arranges it. A duplicate position, a gap, or a broken link would all
        fail this.
        """
        aggregate_id = uuid7()
        results: list[int] = []
        failures: list[Exception] = []
        lock = threading.Lock()
        start = threading.Barrier(5)

        def worker(index: int) -> None:
            s = sessions()
            try:
                start.wait(timeout=10)
                with s.begin():
                    set_tenant(s, tenant)
                    event = emit(
                        s, tenant, aggregate_id, event_type=f"W{index}", payload={"w": index}
                    )
                with lock:
                    results.append(event.seq)
            except Exception as exc:  # reported, never swallowed
                with lock:
                    failures.append(exc)
            finally:
                s.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not failures, f"an append failed under contention: {failures!r}"
        assert sorted(results) == [1, 2, 3, 4, 5], f"positions collided or skipped: {results}"

        with session.begin():
            set_tenant(session, tenant)
            result = verify(session, tenant, aggregate_id)
        assert result.intact is True
        assert result.events_verified == 5


# ------------------------------------------------------- 4. same transaction as the change


class TestSameTransaction:
    def test_append_does_not_commit_on_its_own(
        self, session: Session, tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        """Observed from a second connection: nothing is durable until the caller commits.

        If append committed on its own, an audit row would survive a state change that
        rolled back -- evidence for a payment that never happened.
        """
        aggregate_id = uuid7()
        session.begin()
        set_tenant(session, tenant)
        emit(session, tenant, aggregate_id)

        assert rows_for(admin_engine, tenant, aggregate_id) == 0, "append committed by itself"

        session.commit()
        assert rows_for(admin_engine, tenant, aggregate_id) == 1

    def test_a_rolled_back_state_change_takes_its_evidence_with_it(
        self, session: Session, tenant: uuid.UUID, admin_engine: Engine
    ) -> None:
        """The other direction: no orphaned evidence for work that was undone."""
        aggregate_id = uuid7()
        session.begin()
        set_tenant(session, tenant)
        emit(session, tenant, aggregate_id)
        emit(session, tenant, aggregate_id, event_type="SECOND")
        session.rollback()

        assert rows_for(admin_engine, tenant, aggregate_id) == 0

        # And the stream is genuinely empty afterwards, not merely invisible: a later
        # append opens at seq 1 rather than continuing from a phantom predecessor.
        with session.begin():
            set_tenant(session, tenant)
            event = emit(session, tenant, aggregate_id)
        assert (event.seq, event.prev_hash) == (1, None)

    def test_append_refuses_a_session_with_no_transaction(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID
    ) -> None:
        """Without an open transaction the evidence would land in a unit of work of its
        own, free to commit while the state change rolled back."""
        s = sessions()
        try:
            with pytest.raises(AuditUsageError, match="active transaction"):
                emit(s, tenant, uuid7())
        finally:
            s.rollback()
            s.close()

    def test_no_application_role_may_edit_or_delete_evidence(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID
    ) -> None:
        """The premise every tamper test above relies on: the grants really are missing.

        If UPDATE or DELETE were ever granted to the kernel role, tampering would stop
        being an operator-credential problem and become an application-bug problem, and
        the hash chain would be the only thing left standing between a bug and a rewritten
        financial record.
        """
        for statement in (
            "UPDATE audit_events SET event_type = 'REWRITTEN' WHERE tenant_id = :t",
            "DELETE FROM audit_events WHERE tenant_id = :t",
        ):
            s = sessions()
            try:
                with pytest.raises(ProgrammingError) as exc:
                    with s.begin():
                        set_tenant(s, tenant)
                        s.execute(text(statement), {"t": tenant})
                # 42501 is insufficient_privilege: refused by the grant, not by a policy
                # that a future migration might loosen by accident.
                assert getattr(exc.value.orig, "sqlstate", None) == "42501"
            finally:
                s.rollback()
                s.close()


class TestDatabaseClock:
    def test_occurred_at_is_the_database_transaction_clock(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Not the application clock. A pod skewed by a minute must not be able to stamp
        evidence with a time the database never saw, because a back-dated event is the
        cheapest way to fabricate an alibi."""
        with session.begin():
            set_tenant(session, tenant)
            event = emit(session, tenant, uuid7())
            db_now = session.execute(text("SELECT date_trunc('milliseconds', now())")).scalar_one()

        assert event.occurred_at == db_now
        # And the millisecond value inside the hash is exactly that instant, so the
        # envelope can be rebuilt from the stored timestamp without a rounding step.
        assert event.occurred_at == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            milliseconds=event.occurred_at_ms
        )

    def test_events_of_one_transaction_share_the_transaction_timestamp(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """One atomic state change is one instant; ``seq`` carries the order, not the
        clock. Deriving order from timestamps would make same-transaction events
        arbitrarily ordered."""
        aggregate_id = uuid7()
        with session.begin():
            set_tenant(session, tenant)
            first = emit(session, tenant, aggregate_id, event_type="ONE")
            second = emit(session, tenant, aggregate_id, event_type="TWO")

        assert first.occurred_at == second.occurred_at
        assert second.seq == first.seq + 1
        assert second.prev_hash == first.self_hash


# ------------------------------------------------------------------- payload and refusals


class TestPayload:
    def test_a_rich_payload_survives_the_jsonb_round_trip_and_still_verifies(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """The stored payload must hash to the same bytes after PostgreSQL has held it.

        JSONB normalises what it stores. If the round trip changed a value -- a large
        integer, a supplementary-plane character, an empty container -- honest evidence
        would fail its own hash check and be indistinguishable from tampered evidence.
        """
        aggregate_id = uuid7()
        payload = {
            "amount": Money(129900, "INR"),
            "grant_id": uuid.UUID(int=7),
            "actor_type": ActorType.OPERATOR,
            "lines": [{"sku": "SKU-1", "quantity": 2}, {"sku": "SKU-2", "quantity": 1}],
            "large_minor": 2**70,
            "negative": -1,
            "flag": True,
            "missing": None,
            "empty_object": {},
            "empty_list": [],
            "unicode": 'साड़ी 𝄞 "quoted"\n',
        }
        with session.begin():
            set_tenant(session, tenant)
            written = emit(session, tenant, aggregate_id, payload=payload)

        with session.begin():
            set_tenant(session, tenant)
            stream = read_stream(
                session, tenant=tenant, aggregate_type=AGGREGATE, aggregate_id=aggregate_id
            )
            result = verify(session, tenant, aggregate_id)

        stored = stream[0].payload
        assert stored["amount"] == {"currency": "INR", "minor": 129900}
        assert stored["grant_id"] == str(uuid.UUID(int=7))
        assert stored["actor_type"] == "OPERATOR"
        assert stored["large_minor"] == 2**70
        assert stored["unicode"] == 'साड़ी 𝄞 "quoted"\n'
        assert stored["empty_object"] == {}
        assert stored["empty_list"] == []
        assert stored["missing"] is None
        assert compute_self_hash(envelope_of(stream[0])) == written.self_hash
        assert result.intact is True

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            (1.5, "float"),
            (Decimal("1.50"), "Decimal"),
            (datetime(2026, 9, 4, tzinfo=UTC), "datetime"),
            (b"signature-bytes", "bytes"),
            ({1, 2}, "set"),
        ],
    )
    def test_unhashable_payload_values_are_refused(
        self, session: Session, tenant: uuid.UUID, value: Any, why: str
    ) -> None:
        """Each of these either cannot be canonicalized or round-trips as a different
        value, which would make an honest event fail its own hash check later."""
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError):
                emit(session, tenant, uuid7(), payload={"field": value})

    def test_a_float_nested_deep_in_the_payload_is_still_refused(
        self, session: Session, tenant: uuid.UUID
    ) -> None:
        """Money that escaped the Money type is the realistic way a float arrives."""
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError, match="integer minor units"):
                emit(
                    session,
                    tenant,
                    uuid7(),
                    payload={"order": {"lines": [{"price": 1299.0}]}},
                )

    def test_a_non_string_payload_key_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        """JSONB would coerce it silently, and the stored key would no longer be the key
        that was hashed."""
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError, match="non-string payload key"):
                emit(session, tenant, uuid7(), payload={"outer": {7: "seven"}})

    def test_a_non_mapping_payload_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError, match="payload must be a mapping"):
                append(
                    session,
                    tenant=tenant,
                    aggregate_type=AGGREGATE,
                    aggregate_id=uuid7(),
                    event_type="BAD",
                    actor_type=ActorType.SYSTEM,
                    principal_id=None,
                    payload=[{"not": "a mapping"}],  # type: ignore[arg-type]
                    correlation_id=uuid7(),
                )


class TestRefusals:
    def test_append_refuses_without_a_bound_tenant(
        self, sessions: sessionmaker[Session], tenant: uuid.UUID
    ) -> None:
        """Row-level security would otherwise reject the insert with a policy error that
        names no tenant, and a stream read would return silence that reads as 'no
        history'."""
        s = sessions()
        try:
            with s.begin():
                with pytest.raises(TenantContextError):
                    emit(s, tenant, uuid7())
        finally:
            s.rollback()
            s.close()

    def test_append_refuses_a_tenant_other_than_the_bound_one(
        self, session: Session, tenant: uuid.UUID, make_tenant: Callable[[], uuid.UUID]
    ) -> None:
        """An event belongs in the stream of its own tenant or nowhere."""
        other = make_tenant()
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditTenantError, match="bound to"):
                emit(session, other, uuid7())

    def test_verify_chain_refuses_a_tenant_other_than_the_bound_one(
        self, session: Session, tenant: uuid.UUID, make_tenant: Callable[[], uuid.UUID]
    ) -> None:
        """Silence from row-level security must not be reported as a clean chain."""
        other = make_tenant()
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditTenantError):
                verify(session, other, uuid7())

    def test_head_refuses_a_tenant_other_than_the_bound_one(
        self, session: Session, tenant: uuid.UUID, make_tenant: Callable[[], uuid.UUID]
    ) -> None:
        """``head`` is the value an operator publishes to an external anchor.

        Row-level security would hand back ``None`` for another tenant's stream, and a
        ``None`` head is indistinguishable from an empty one -- so an operator anchoring
        the wrong tenant would record "no history" as though it were a fact about the
        stream rather than about their own binding.
        """
        other = make_tenant()
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditTenantError):
                head(session, tenant=other, aggregate_type=AGGREGATE, aggregate_id=uuid7())

    def test_an_unknown_actor_type_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        """Attribution is half of what the record is for, so an unrecognised actor label
        never enters the evidence."""
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError, match="not a known ActorType"):
                append(
                    session,
                    tenant=tenant,
                    aggregate_type=AGGREGATE,
                    aggregate_id=uuid7(),
                    event_type="SOMETHING",
                    actor_type="MODEL",
                    principal_id=None,
                    payload={},
                    correlation_id=uuid7(),
                )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("aggregate_type", "A" * 49),
            ("event_type", "E" * 65),
        ],
    )
    def test_a_value_wider_than_its_column_is_refused_by_name(
        self, session: Session, tenant: uuid.UUID, field: str, value: str
    ) -> None:
        """Refused in Python, not by the driver: a driver error would already have
        aborted the caller's transaction, taking the state change with it and leaving
        an operator with 'value too long for character varying' as the whole story."""
        kwargs: dict[str, Any] = {
            "tenant": tenant,
            "aggregate_type": AGGREGATE,
            "aggregate_id": uuid7(),
            "event_type": "OK_EVENT",
            "actor_type": ActorType.SYSTEM,
            "principal_id": None,
            "payload": {},
            "correlation_id": uuid7(),
        }
        kwargs[field] = value
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError, match=field):
                append(session, **kwargs)

    def test_a_missing_correlation_id_is_refused(self, session: Session, tenant: uuid.UUID) -> None:
        """It is the only thread a dispute is reconstructed along across services."""
        with session.begin():
            set_tenant(session, tenant)
            with pytest.raises(AuditContentError, match="correlation_id"):
                append(
                    session,
                    tenant=tenant,
                    aggregate_type=AGGREGATE,
                    aggregate_id=uuid7(),
                    event_type="SOMETHING",
                    actor_type=ActorType.SYSTEM,
                    principal_id=None,
                    payload={},
                    correlation_id=None,  # type: ignore[arg-type]
                )

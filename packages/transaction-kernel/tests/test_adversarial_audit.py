"""Adversarial audit: try to fork the chain, and try to edit it without being caught.

``test_audit.py`` already proves that a single edit, a deleted row and a reordered pair
are detected. This file asks two harder questions.

**Can the chain be forked?** Sixteen sessions appending to one stream at once, and appends
interleaved with savepoint rollbacks, which is the shape a denial inside a larger
transaction actually has. A fork -- two events claiming one position, or a gap where a
rolled-back append used to be -- would make the stream unverifiable ever after.

**Can it be edited without detection?** The answer is yes, and this file says so out loud,
because a verifier whose limits are undocumented is worse than one whose limits are known.
An attacker holding database-owner credentials can rewrite a payload *and every hash from
that point to the head*, and :func:`verify_chain` then reports the stream intact. That is a
property of every self-contained hash chain, not a bug in this one, and the module says so.
What this file adds is the proof that it is true here, and the proof that the published
head moves when it happens -- which is what makes an external anchor the actual control
rather than a suggestion.
"""

from __future__ import annotations

import uuid

import pytest
from admission_support import SET_TENANT, Fixture
from adversarial_support import race
from commerce_domain import uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import audit
from transaction_kernel.audit import (
    AuditEventView,
    BreakKind,
    compute_self_hash,
    envelope_of,
)
from transaction_kernel.contracts import ActorType

pytestmark = pytest.mark.db

AGGREGATE = "adversarial_audit"


# ------------------------------------------------------------------------- helpers


def _append(
    kernel_engine: Engine, fixture: Fixture, stream: uuid.UUID, event_type: str, **payload: object
) -> AuditEventView:
    session = Session(kernel_engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            return audit.append(
                session,
                tenant=fixture.tenant_id,
                aggregate_type=AGGREGATE,
                aggregate_id=stream,
                event_type=event_type,
                actor_type=ActorType.SYSTEM,
                principal_id="adversary-suite",
                payload=dict(payload),
                correlation_id=fixture.correlation_id,
            )
    finally:
        session.close()


def _read(engine: Engine, fixture: Fixture, stream: uuid.UUID) -> list[AuditEventView]:
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            return list(
                audit.read_stream(
                    session,
                    tenant=fixture.tenant_id,
                    aggregate_type=AGGREGATE,
                    aggregate_id=stream,
                )
            )
    finally:
        session.close()


def _verify(engine: Engine, fixture: Fixture, stream: uuid.UUID):
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            return audit.verify_chain(
                session,
                tenant=fixture.tenant_id,
                aggregate_type=AGGREGATE,
                aggregate_id=stream,
            )
    finally:
        session.close()


def _head(engine: Engine, fixture: Fixture, stream: uuid.UUID):
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            return audit.head(
                session,
                tenant=fixture.tenant_id,
                aggregate_type=AGGREGATE,
                aggregate_id=stream,
            )
    finally:
        session.close()


@pytest.fixture
def stream(adm_kernel_engine: Engine, admissible: Fixture) -> uuid.UUID:
    """A five-event stream, written the way production writes one."""
    aggregate_id = uuid7()
    for index in range(5):
        _append(
            adm_kernel_engine,
            admissible,
            aggregate_id,
            f"step.{index}",
            index=index,
            note=f"event {index}",
        )
    return aggregate_id


# ------------------------------------------------------------------- forks and gaps


class TestTheChainCannotBeForked:
    def test_sixteen_concurrent_appends_produce_one_gapless_stream(
        self, adm_kernel_engine, admissible
    ):
        """Sixteen positions, sixteen events, one chain.

        The advisory lock is what makes ``prev_hash`` the *committed* tail rather than
        whatever was visible a moment ago. If it failed, the losers would either duplicate
        a position (refused by the unique constraint, so a raise) or chain onto a
        predecessor that is not the one before them (a fork that verifies row by row and
        not as a stream).
        """
        aggregate_id = uuid7()

        def body(session: Session, index: int) -> int:
            return audit.append(
                session,
                tenant=admissible.tenant_id,
                aggregate_type=AGGREGATE,
                aggregate_id=aggregate_id,
                event_type="concurrent.append",
                actor_type=ActorType.SYSTEM,
                principal_id=f"worker-{index}",
                payload={"worker": index},
                correlation_id=admissible.correlation_id,
            ).seq

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=16)
        crashed = [o for o in outcomes if not o.ok]
        assert not crashed, (
            "an append crashed rather than queueing behind the advisory lock: "
            f"{type(crashed[0].error).__name__}: {crashed[0].error}"
        )
        positions = sorted(o.value for o in outcomes)
        assert positions == list(range(1, 17)), f"positions were not 1..16: {positions}"

        verification = _verify(adm_kernel_engine, admissible, aggregate_id)
        assert verification.intact, verification.first_break
        assert verification.length == 16
        assert verification.events_verified == 16

    def test_a_rolled_back_append_leaves_no_hole(self, adm_kernel_engine, admissible, stream):
        """The denial-inside-a-larger-transaction shape.

        A caller opens a savepoint, appends, then abandons the savepoint. The position it
        allocated must be free again: a gap would make every later event unverifiable, and
        a chain that breaks whenever a transaction is retried is not evidence.
        """
        before = _head(adm_kernel_engine, admissible, stream)
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            session.begin()
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            savepoint = session.begin_nested()
            audit.append(
                session,
                tenant=admissible.tenant_id,
                aggregate_type=AGGREGATE,
                aggregate_id=stream,
                event_type="abandoned",
                actor_type=ActorType.SYSTEM,
                principal_id="adversary-suite",
                payload={"note": "this must not survive"},
                correlation_id=admissible.correlation_id,
            )
            savepoint.rollback()
            session.commit()
        finally:
            session.close()

        assert _head(adm_kernel_engine, admissible, stream) == before

        after = _append(adm_kernel_engine, admissible, stream, "step.next", index=99)
        assert after.seq == before.seq + 1, "the abandoned append consumed a position"
        verification = _verify(adm_kernel_engine, admissible, stream)
        assert verification.intact, verification.first_break

    def test_appends_to_two_streams_do_not_serialise_against_each_other(
        self, adm_kernel_engine, admissible
    ):
        """The lock is per stream, so a busy checkout cannot stall an unrelated one.

        Correctness does not depend on this, but a global lock would make the audit the
        bottleneck of every money path, and a later refactor to one is worth catching.
        """
        streams = [uuid7() for _ in range(8)]

        def body(session: Session, index: int) -> int:
            return audit.append(
                session,
                tenant=admissible.tenant_id,
                aggregate_type=AGGREGATE,
                aggregate_id=streams[index],
                event_type="parallel",
                actor_type=ActorType.SYSTEM,
                principal_id=f"worker-{index}",
                payload={"worker": index},
                correlation_id=admissible.correlation_id,
            ).seq

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=8)
        assert all(o.ok for o in outcomes), [o.error for o in outcomes if not o.ok]
        assert {o.value for o in outcomes} == {1}, "each stream must have opened at 1"


# ----------------------------------------------------------------------- tampering


class TestSingleEditsAreCaught:
    def test_an_event_moved_to_another_tenants_stream_does_not_verify_there(
        self, adm_kernel_engine, adm_admin_engine, admissible, stream
    ):
        """``tenant_id`` is inside the hash, so a wholesale relocation fails on arrival.

        Without it, a valid event could be lifted from one tenant and dropped into
        another's history -- an audit trail that says a customer authorised something they
        never saw.
        """
        stranger = uuid.uuid4()
        with adm_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :s, :n, 'asia-south1')"
                ),
                {
                    "id": stranger,
                    "s": f"adv-{stranger.hex[:8]}",
                    "n": f"adv-{stranger.hex[:8]}",
                },
            )
        try:
            events = _read(adm_kernel_engine, admissible, stream)
            with adm_admin_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                conn.execute(
                    text(
                        "UPDATE audit_events SET tenant_id = :new, seq = 1, prev_hash = NULL "
                        "WHERE id = :i"
                    ),
                    {"new": stranger, "i": events[2].event_id},
                )

            foreign = Fixture(
                tenant_id=stranger,
                merchant_id=admissible.merchant_id,
                checkout=admissible.checkout,
                principal=admissible.principal,
                correlation_id=admissible.correlation_id,
            )
            verification = _verify(adm_admin_engine, foreign, stream)
            assert not verification.intact
            assert verification.first_break.kind is BreakKind.SELF_HASH_MISMATCH

            # And the stream it was taken from now has a hole where it used to be.
            origin = _verify(adm_kernel_engine, admissible, stream)
            assert not origin.intact
            assert origin.first_break.kind is BreakKind.SEQ_GAP
        finally:
            with adm_admin_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(stranger)})
                conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": stranger})
                conn.execute(SET_TENANT, {"t": None})
                conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": stranger})

    def test_a_backdated_event_is_caught(
        self, adm_kernel_engine, adm_admin_engine, admissible, stream
    ):
        """A timestamp is an alibi, so it is chained like everything else."""
        events = _read(adm_kernel_engine, admissible, stream)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "UPDATE audit_events SET occurred_at = occurred_at - interval '1 day' "
                    "WHERE id = :i"
                ),
                {"i": events[1].event_id},
            )
        verification = _verify(adm_kernel_engine, admissible, stream)
        assert not verification.intact
        assert verification.first_break.kind is BreakKind.SELF_HASH_MISMATCH
        assert verification.first_break.at_seq == events[1].seq

    def test_sub_millisecond_precision_smuggled_into_a_timestamp_is_caught(
        self, adm_kernel_engine, adm_admin_engine, admissible, stream
    ):
        """The stored value and the hashed value are the same number, to the millisecond.

        Adding microseconds is the subtlest edit available -- it survives every human
        reading of the row -- so the verifier must fail closed on it.
        """
        events = _read(adm_kernel_engine, admissible, stream)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "UPDATE audit_events SET occurred_at = occurred_at + interval '1 microsecond' "
                    "WHERE id = :i"
                ),
                {"i": events[3].event_id},
            )
        verification = _verify(adm_kernel_engine, admissible, stream)
        assert not verification.intact
        assert verification.first_break.kind is BreakKind.SELF_HASH_MISMATCH

    def test_a_rewritten_attribution_is_caught(
        self, adm_kernel_engine, adm_admin_engine, admissible, stream
    ):
        """Who did it is half of what an audit is for."""
        events = _read(adm_kernel_engine, admissible, stream)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text("UPDATE audit_events SET principal_id = 'somebody-else' WHERE id = :i"),
                {"i": events[2].event_id},
            )
        verification = _verify(adm_kernel_engine, admissible, stream)
        assert not verification.intact
        assert verification.first_break.kind is BreakKind.SELF_HASH_MISMATCH

    def test_the_application_role_cannot_make_the_edit_at_all(
        self, adm_kernel_engine, admissible, stream
    ):
        """Grants are the first line; the hash chain is what survives their loss."""
        events = _read(adm_kernel_engine, admissible, stream)
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(Exception) as caught:
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                session.execute(
                    text("UPDATE audit_events SET payload = '{}'::jsonb WHERE id = :i"),
                    {"i": events[0].event_id},
                )
        finally:
            session.close()
        assert "permission denied" in str(caught.value).lower(), caught.value


class TestWhatCannotBeDetected:
    """The honest half. Both of these are properties of a self-contained hash chain."""

    def test_a_full_reforge_from_the_edit_to_the_head_verifies_clean(
        self, adm_kernel_engine, adm_admin_engine, admissible, stream
    ):
        """An owner who rewrites every hash after the edit is not caught by the verifier.

        This is the finding to read alongside the README's "tamper-evident" claim. The
        chain makes tampering *expensive and total* -- one edited field forces a rewrite
        of every row from there to the head, which cannot be done by an application role
        at all and cannot be done quietly by anyone -- but it does not make it impossible.
        The control that closes this is the published head, asserted in the next test.
        """
        before_head = _head(adm_kernel_engine, admissible, stream)
        events = _read(adm_kernel_engine, admissible, stream)
        target = events[1]

        # Each step commits before the next reads, because a re-forge has to see its own
        # earlier edits -- which is exactly the position a real attacker is in.
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "UPDATE audit_events SET payload = "
                    '\'{"index": 1, "note": "forged"}\'::jsonb WHERE id = :i'
                ),
                {"i": target.event_id},
            )

        previous_hash: str | None = None
        for event in _read(adm_admin_engine, admissible, stream):
            if event.seq < target.seq:
                previous_hash = event.self_hash
                continue
            if previous_hash is not None:
                with adm_admin_engine.begin() as conn:
                    conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                    conn.execute(
                        text("UPDATE audit_events SET prev_hash = :p WHERE id = :i"),
                        {"p": previous_hash, "i": event.event_id},
                    )
            refreshed = next(
                e for e in _read(adm_admin_engine, admissible, stream) if e.seq == event.seq
            )
            recomputed = compute_self_hash(envelope_of(refreshed))
            with adm_admin_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                conn.execute(
                    text("UPDATE audit_events SET self_hash = :h WHERE id = :i"),
                    {"h": recomputed, "i": event.event_id},
                )
            previous_hash = recomputed

        verification = _verify(adm_kernel_engine, admissible, stream)
        assert verification.intact, (
            "the re-forge was detected, which would be a stronger property than a "
            f"self-contained chain can have -- re-read this test: {verification.first_break}"
        )
        assert _read(adm_kernel_engine, admissible, stream)[1].payload["note"] == "forged"

        after_head = _head(adm_kernel_engine, admissible, stream)
        assert after_head is not None and before_head is not None
        assert after_head.seq == before_head.seq
        assert after_head.self_hash != before_head.self_hash, (
            "the head hash did not move, so even an external anchor would not catch this"
        )

    def test_a_truncated_tail_verifies_clean_but_moves_the_published_head(
        self, adm_kernel_engine, adm_admin_engine, admissible, stream
    ):
        """Deleting the newest events leaves nothing behind that expects them.

        Same limit, different act, and the same control: ``head`` is what an operator
        publishes outside the database so that the loss is visible by comparison.
        """
        before = _head(adm_kernel_engine, admissible, stream)
        assert before is not None and before.seq == 5

        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "DELETE FROM audit_events WHERE aggregate_type = :a AND aggregate_id = :i "
                    "AND seq > 3"
                ),
                {"a": AGGREGATE, "i": stream},
            )

        verification = _verify(adm_kernel_engine, admissible, stream)
        assert verification.intact, "a truncated tail should verify clean; it did not"
        assert verification.length == 3

        after = _head(adm_kernel_engine, admissible, stream)
        assert after is not None
        assert (after.seq, after.self_hash) != (before.seq, before.self_hash), (
            "the head is unchanged after truncation, so the anchor control would not work"
        )


class TestTheVerifierDoesNotCryWolf:
    def test_an_awkward_but_legal_payload_still_verifies(self, adm_kernel_engine, admissible):
        """A false SELF_HASH_MISMATCH would be as damaging as a missed real one.

        The payload goes through JSONB, which normalises key order and whitespace, so the
        keys below are chosen to sort differently under JSONB's ordering (length first)
        than under the canonical hash's (lexicographic), and the values exercise deep
        nesting, empty containers, unicode, and integers past 64 bits.
        """
        aggregate_id = uuid7()
        payload = {
            "zzz": 1,
            "a": {"nested": {"deeper": [1, 2, {"x": []}]}},
            "aa": "",
            "aaa": None,
            "b": "अन्तर्राष्ट्रीयकरण 🙂",
            "big": 2**80,
            "empty_map": {},
            "empty_list": [],
            "negative": -(2**70),
        }
        written = _append(adm_kernel_engine, admissible, aggregate_id, "awkward", **payload)
        _append(adm_kernel_engine, admissible, aggregate_id, "after", note="follows")

        verification = _verify(adm_kernel_engine, admissible, aggregate_id)
        assert verification.intact, verification.first_break

        stored = _read(adm_kernel_engine, admissible, aggregate_id)[0]
        assert stored.payload == payload, "the payload did not survive the JSONB round trip"
        assert stored.self_hash == written.self_hash

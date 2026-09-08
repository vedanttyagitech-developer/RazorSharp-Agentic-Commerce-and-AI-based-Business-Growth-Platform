"""Adversarial admission: single-winner under contention the existing suite does not apply.

``test_admission.py`` proves the property with two threads on one checkout version. That
is the shape a reviewer thinks of first, and it is the shape the reservation lock already
serialises perfectly. This file goes after the cases that shape does not reach:

* **width** -- twelve contenders rather than two, so a bug that only shows up when a
  thread is descheduled between the lock and the insert has room to appear;
* **repetition with varied interleaving** -- the same race run many times with staggered
  starts, because a race won identically every time was probably not a race;
* **a different collision entirely** -- the partial unique index is keyed on
  ``(tenant_id, checkout_id)`` and says nothing about the version, so two *different*
  versions of one checkout collide on it. That path never touches the reservation lock
  the two-thread test relies on, and it is where admission was found to raise instead of
  deny (ADR 0003 D4(b)).

Every claim here is made against real PostgreSQL sessions in real threads. A stub cannot
lose a race it was not asked to run.
"""

from __future__ import annotations

import uuid

import pytest
from admission_support import APPROVED_TOTAL, SET_TENANT, Fixture, StubMerchant
from adversarial_support import add_approved_version, count, race, scalar
from commerce_domain import Money, uuid7
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from transaction_kernel import audit
from transaction_kernel.admission import AdmissionRequest, admit
from transaction_kernel.contracts import CheckoutRef, KernelDecision, Operation
from transaction_kernel.recovery import RecoveryCode

pytestmark = pytest.mark.db

#: Codes a loser may legitimately carry. Each one tells the caller a competing operation
#: exists and that current state must be read; none of them may be shown to a buyer as a
#: completed purchase. RESERVATION_EXPIRED is absent on purpose: the hold was spent by the
#: winner, not lapsed, and telling a caller to re-reserve would invite a second attempt.
LOSER_CODES = frozenset({RecoveryCode.DUPLICATE_OPERATION, RecoveryCode.CONCURRENT_OPERATION})


def _request(
    fixture: Fixture,
    checkout: CheckoutRef,
    *,
    approval_id: uuid.UUID | None = None,
    **overrides: object,
) -> AdmissionRequest:
    base: dict[str, object] = {
        "tenant_id": fixture.tenant_id,
        "merchant_id": fixture.merchant_id,
        "checkout": checkout,
        "amount": APPROVED_TOTAL,
        "operation": Operation.PAYMENT_CREATE_ORDER,
        "idempotency_key": f"idem-{uuid7().hex[:16]}",
        "principal": fixture.principal,
        "correlation_id": fixture.correlation_id,
        "approval_id": approval_id if approval_id is not None else fixture.approval_id,
    }
    base.update(overrides)
    return AdmissionRequest(**base)  # type: ignore[arg-type]


def _live_approval(session: Session, fixture: Fixture, checkout: CheckoutRef) -> uuid.UUID:
    """The RECORDED approval on this version, read in the caller's own transaction.

    Admission reads the approval it is handed, and these tests admit several versions of
    one checkout, so the id cannot be a constant taken from the fixture.
    """
    found = session.execute(
        text(
            "SELECT id FROM approvals WHERE tenant_id = :t AND checkout_id = :c "
            "AND checkout_version = :v AND status = 'RECORDED'"
        ),
        {"t": fixture.tenant_id, "c": checkout.checkout_id, "v": checkout.version},
    ).scalar_one()
    return uuid.UUID(str(found))


def _admit_once(session: Session, fixture: Fixture, checkout: CheckoutRef) -> KernelDecision:
    return admit(
        session,
        _request(fixture, checkout, approval_id=_live_approval(session, fixture, checkout)),
        StubMerchant(checkout.checkout_id),
    )


def _attempts(engine: Engine, fixture: Fixture) -> int:
    return count(
        engine,
        fixture.tenant_id,
        "SELECT count(*) FROM payment_attempts WHERE checkout_id = :c",
        c=fixture.checkout.checkout_id,
    )


def _live_attempts(engine: Engine, fixture: Fixture) -> int:
    return count(
        engine,
        fixture.tenant_id,
        "SELECT count(*) FROM payment_attempts WHERE checkout_id = :c AND status IN "
        "('CREATED','SUBMITTED','AUTHORIZED','UNKNOWN','RECONCILING')",
        c=fixture.checkout.checkout_id,
    )


def _grants(engine: Engine, fixture: Fixture) -> int:
    return count(
        engine,
        fixture.tenant_id,
        "SELECT count(*) FROM execution_grants WHERE checkout_id = :c",
        c=fixture.checkout.checkout_id,
    )


class TestWideContention:
    """One checkout, twelve contenders, one winner."""

    def test_twelve_concurrent_admissions_produce_exactly_one_attempt(
        self, adm_kernel_engine, admissible, adm_admin_engine
    ):
        outcomes = race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: _admit_once(session, admissible, admissible.checkout),
            workers=12,
        )

        crashed = [o for o in outcomes if not o.ok]
        assert not crashed, (
            "admission raised instead of returning a decision: "
            f"{[(o.index, type(o.error).__name__, str(o.error)[:160]) for o in crashed]}"
        )
        decisions = [o.value for o in outcomes]
        winners = [d for d in decisions if d.allowed]
        assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"

        losers = [d for d in decisions if not d.allowed]
        assert len(losers) == 11
        offenders = [d.code for d in losers if d.code not in LOSER_CODES]
        assert not offenders, f"losers carried codes that do not describe a race: {offenders}"

        assert _attempts(adm_admin_engine, admissible) == 1, "a second payment attempt exists"
        assert _grants(adm_admin_engine, admissible) == 1, "a second execution grant exists"

    def test_no_loser_receives_a_grant_or_an_attempt_id(self, adm_kernel_engine, admissible):
        """A denial that carried a grant id would be a capability handed to a refused caller."""
        outcomes = race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: _admit_once(session, admissible, admissible.checkout),
            workers=8,
        )
        for outcome in outcomes:
            assert outcome.ok, outcome.error
            decision = outcome.value
            if decision.allowed:
                continue
            assert decision.grant_id is None, "a denied admission handed back a grant"
            assert decision.payment_attempt_id is None, "a denied admission named an attempt"

    def test_every_contender_leaves_exactly_one_audit_event(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """Twelve decisions, twelve events, and a chain that still verifies.

        The audit row is written inside the deciding transaction, so a lost race must
        leave evidence of the refusal rather than nothing at all -- and twelve concurrent
        appends to one stream must not fork it.
        """
        workers = 12

        def _chain():
            session = Session(adm_admin_engine, expire_on_commit=False)
            try:
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                    return audit.verify_chain(
                        session,
                        tenant=admissible.tenant_id,
                        aggregate_type="checkout",
                        aggregate_id=admissible.checkout.checkout_id,
                    )
            finally:
                session.close()

        # The fixture's own approval is on this stream too, so the count is measured as a
        # delta rather than as a total.
        before = _chain().length
        race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: _admit_once(session, admissible, admissible.checkout),
            workers=workers,
        )
        verification = _chain()

        assert verification.length - before == workers, (
            f"{workers} admissions wrote {verification.length} events; a decision without "
            "an event is a money action with no evidence"
        )
        assert verification.intact, verification.first_break
        assert verification.head_seq == verification.length, "the stream is not gapless"

    @pytest.mark.slow
    def test_the_race_is_won_once_in_every_round(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """Six rounds, six versions, one win each, whatever the interleaving.

        A single round proves the property held once. Repeating it against a freshly
        approved version each time is what distinguishes a guarantee from a lucky
        schedule, and running it after the previous round's attempt has been retired is
        the shape a policy-safe retry actually has: a confirmed failure releases the
        attempt (``PAYMENT_FAILED -> EXECUTION_PENDING``) and re-admission mints a new
        single-use grant rather than reusing the old one.
        """
        for round_index in range(6):
            # Retire the previous round's winner, the way a confirmed provider failure
            # would. FAILED is terminal, so it leaves the one-non-terminal index free
            # without weakening any invariant this test is about.
            with adm_admin_engine.begin() as conn:
                conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                conn.execute(
                    text(
                        "UPDATE payment_attempts SET status = 'FAILED' "
                        "WHERE checkout_id = :c AND status = 'CREATED'"
                    ),
                    {"c": admissible.checkout.checkout_id},
                )

            version = admissible.checkout.version + 1 + round_index
            ref = add_approved_version(
                adm_admin_engine, adm_kernel_engine, admissible, version=version
            )
            outcomes = race(
                adm_kernel_engine,
                admissible.tenant_id,
                lambda session, _, ref=ref: _admit_once(session, admissible, ref),
                workers=6,
            )
            crashed = [o for o in outcomes if not o.ok]
            assert not crashed, f"round {round_index} crashed: {crashed[0].error!r}"
            winners = [o.value for o in outcomes if o.value.allowed]
            assert len(winners) == 1, f"round {round_index} produced {len(winners)} winners"
            losers = [o.value for o in outcomes if not o.value.allowed]
            assert all(d.code in LOSER_CODES for d in losers), [d.code for d in losers]

            # Exactly one live attempt at every moment, across every round: the invariant
            # the partial unique index exists to hold.
            assert _live_attempts(adm_admin_engine, admissible) == 1
            assert _attempts(adm_admin_engine, admissible) == round_index + 1


class TestSecondAttemptPerCheckout:
    """The collision the version lock does not serialise.

    ``uq_payment_attempts_one_non_terminal`` is keyed on ``(tenant_id, checkout_id)`` and
    does not mention the version. Two versions of one checkout therefore contend on it
    while locking two different ``checkout_versions`` rows -- so nothing upstream of the
    INSERT serialises them, and the INSERT itself is the whole guarantee.

    ADR 0003 D4(b) says that collision must become an audited ``CONCURRENT_OPERATION``
    denial and that the library must never call ``session.rollback()``. Before the fix
    these four tests failed: admission raised ``IntegrityError`` out of the caller's
    ``with session.begin()`` after tearing that transaction down from underneath it.
    """

    def test_a_second_live_attempt_is_denied_rather_than_raised(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            first = _admit_once(session, admissible, admissible.checkout)
        session.close()
        assert first.allowed

        later = add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                second = _admit_once(session, admissible, later)
        finally:
            session.close()

        assert not second.allowed
        assert second.code is RecoveryCode.CONCURRENT_OPERATION, (
            "a live attempt on the same checkout must be reported as a concurrent "
            "operation, never as a duplicate a caller could show a buyer as complete"
        )
        assert second.grant_id is None
        assert _live_attempts(adm_admin_engine, admissible) == 1

    def test_the_denial_commits_with_the_callers_transaction(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """The caller's transaction survives the refusal and its own writes commit.

        This is the half of D4(b) that a returned code alone does not prove. A library
        that calls ``session.rollback()`` discards work the caller did before it was
        called; the marker row below is that work.
        """
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            _admit_once(session, admissible, admissible.checkout)
        session.close()

        later = add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )
        marker = uuid7()
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                audit.append(
                    session,
                    tenant=admissible.tenant_id,
                    aggregate_type="adversarial_marker",
                    aggregate_id=marker,
                    event_type="written.before.the.denial",
                    actor_type=admissible.principal.actor_type,
                    principal_id=admissible.principal.principal_id,
                    payload={"note": "must survive the refusal"},
                    correlation_id=admissible.correlation_id,
                )
                decision = _admit_once(session, admissible, later)
                assert not decision.allowed
                assert session.in_transaction(), "the kernel rolled back the caller's transaction"
        finally:
            session.close()

        survived = count(
            adm_admin_engine,
            admissible.tenant_id,
            "SELECT count(*) FROM audit_events WHERE aggregate_id = :m",
            m=marker,
        )
        assert survived == 1, (
            "the caller's own write was discarded; the kernel rolled back a transaction "
            "it does not own (ADR 0003 D4(b))"
        )

    def test_the_denial_is_audited(self, adm_kernel_engine, adm_admin_engine, admissible):
        """A refusal with no evidence cannot be explained to a buyer afterwards."""
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            _admit_once(session, admissible, admissible.checkout)
        session.close()

        later = add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                _admit_once(session, admissible, later)
        finally:
            session.close()

        denials = count(
            adm_admin_engine,
            admissible.tenant_id,
            "SELECT count(*) FROM audit_events WHERE aggregate_id = :c AND event_type = "
            "'admission.denied' AND payload->>'explanation' = "
            "'a_live_payment_attempt_already_exists_for_this_checkout'",
            c=admissible.checkout.checkout_id,
        )
        assert denials == 1

    def test_no_second_grant_is_minted_for_the_refused_version(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            _admit_once(session, admissible, admissible.checkout)
        session.close()

        later = add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                _admit_once(session, admissible, later)
        finally:
            session.close()

        assert _grants(adm_admin_engine, admissible) == 1

    def test_the_refused_versions_hold_is_not_spent(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """A denial must leave the later version admissible once the live attempt ends.

        If the refusal consumed version N+1's reservation on the way past, the buyer would
        be left holding an approved version that can never be admitted, and the stock
        would be released to somebody else while their money was still in play.
        """
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            _admit_once(session, admissible, admissible.checkout)
        session.close()

        later = add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                _admit_once(session, admissible, later)
        finally:
            session.close()

        status = scalar(
            adm_admin_engine,
            admissible.tenant_id,
            "SELECT status FROM reservations WHERE checkout_id = :c AND checkout_version = :v",
            c=later.checkout_id,
            v=later.version,
        )
        assert status == "ACTIVE", f"the refused version's hold is {status}, not ACTIVE"


class TestUnexpectedIntegrityFailuresStillRaise:
    """The refusal is narrow on purpose.

    Only ``uq_payment_attempts_one_non_terminal`` means "another attempt won". Every other
    integrity failure is a fault, and converting one into ``CONCURRENT_OPERATION`` would
    tell the caller to go and read a competing attempt that does not exist -- and would
    hide a broken foreign key or a row-level-security refusal behind a business outcome.
    """

    def test_a_duplicate_receipt_is_not_reported_as_a_lost_race(
        self, adm_kernel_engine, adm_admin_engine, admissible, monkeypatch
    ):
        import transaction_kernel.admission as admission_module

        # Pin the attempt id so the receipt admission will derive is known in advance.
        # ``receipt`` is UNIQUE per tenant, so planting that exact value on an unrelated
        # terminal attempt makes the next admission collide on a constraint that has
        # nothing to do with concurrency.
        fixed = uuid.UUID(int=0x5EC0)
        receipt = f"rcpt_{fixed.hex[:24]}"
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO payment_attempts (id, tenant_id, checkout_id, "
                    "checkout_version, status, amount_minor, currency, receipt) "
                    "VALUES (:id, :t, :c, 1, 'FAILED', 100, 'INR', :rcpt)"
                ),
                {
                    "id": uuid7(),
                    "t": admissible.tenant_id,
                    "c": uuid7(),
                    "rcpt": receipt,
                },
            )

        monkeypatch.setattr(admission_module, "uuid7", lambda: fixed)
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(IntegrityError) as caught:
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                _admit_once(session, admissible, admissible.checkout)
        finally:
            session.close()

        assert "receipt_unique_per_tenant" in str(caught.value), (
            "a duplicate receipt must surface as itself; reporting it as a lost race "
            f"would send the caller looking for an attempt that does not exist: {caught.value}"
        )
        assert _attempts(adm_admin_engine, admissible) == 0, (
            "the raise aborted the transaction, so no attempt may have survived on this "
            "checkout; the planted row that caused it belongs to another one"
        )


class TestDenialsDoNotMoveMoney:
    def test_a_stale_approval_racing_a_winner_creates_no_second_attempt(
        self, adm_kernel_engine, adm_admin_engine, admissible
    ):
        """Half the contenders quote the approved total, half quote a changed one.

        The changed-total contenders must be refused with ``REAPPROVAL_REQUIRED`` and must
        not create an attempt; the honest ones must still produce exactly one winner
        between them. Mixing the two is the realistic shape of a price change landing
        mid-flight.
        """

        def body(session: Session, index: int) -> KernelDecision:
            merchant = StubMerchant(admissible.checkout.checkout_id)
            if index % 2:
                merchant.total = Money(APPROVED_TOTAL.minor + 5500, "INR")
            return admit(session, _request(admissible, admissible.checkout), merchant)

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=8)
        crashed = [o for o in outcomes if not o.ok]
        assert not crashed, f"a contender crashed: {crashed[0].error!r}"

        decisions = [o.value for o in outcomes]
        assert len([d for d in decisions if d.allowed]) <= 1
        assert _attempts(adm_admin_engine, admissible) <= 1
        assert _grants(adm_admin_engine, admissible) <= 1

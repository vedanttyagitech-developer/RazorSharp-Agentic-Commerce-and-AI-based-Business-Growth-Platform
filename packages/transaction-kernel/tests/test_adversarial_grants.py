"""Adversarial Execution Grants: the single use, attacked from every side.

``test_grants.py`` proves the property with two contending sessions. This file widens the
contention, and then goes after the cases two well-behaved workers never produce:

* **a grant consumed twice concurrently** by ten workers, not two;
* **a grant reused after the attempt it authorised failed** -- the shape a naive retry
  loop has;
* **a grant from one checkout presented against another**, field by field, which is what a
  compromised agent would actually try;
* **a grant whose window closed between issue and use**, judged on the database clock;
* **revocation racing consumption**, where the dangerous outcome is not "the wrong one
  won" but "both did".

Every grant here is minted by the kernel's own admission, so what is attacked is the
capability production issues, not a hand-built row that agrees with the test.
"""

from __future__ import annotations

import uuid

import pytest
from admission_support import APPROVED_TOTAL, SET_TENANT, Fixture, StubMerchant
from adversarial_support import add_approved_version, race, scalar
from commerce_domain import Money, canonical_hash, uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel.admission import AdmissionRequest, admit
from transaction_kernel.contracts import CheckoutRef, Operation
from transaction_kernel.grants import (
    GrantAlreadyConsumedError,
    GrantBinding,
    GrantBindingMismatchError,
    GrantExpiredError,
    GrantLinkConflictError,
    GrantNotFoundError,
    GrantReplacementRefusedError,
    GrantRevokedError,
    GrantStatus,
    consume_grant,
    issue_grant,
    link_command,
    revoke_grant,
)
from transaction_kernel.recovery import RecoveryCode

pytestmark = pytest.mark.db


# ------------------------------------------------------------------------- helpers


class Admitted:
    """One real admission: its checkout ref, its attempt and its grant."""

    __slots__ = ("attempt_id", "checkout", "grant_id")

    def __init__(self, checkout: CheckoutRef, attempt_id: uuid.UUID, grant_id: uuid.UUID) -> None:
        self.checkout = checkout
        self.attempt_id = attempt_id
        self.grant_id = grant_id

    def binding(
        self, tenant_id: uuid.UUID, *, amount: Money = APPROVED_TOTAL, **overrides: object
    ) -> GrantBinding:
        """The command the worker intends to send. Built from the *command*, never from
        the grant row, so an override below is a genuine substitution attempt."""
        fields: dict[str, object] = {
            "tenant_id": tenant_id,
            "checkout": self.checkout,
            "payment_attempt_id": self.attempt_id,
            "operation": Operation.PAYMENT_CREATE_ORDER,
            "amount": amount,
        }
        fields.update(overrides)
        return GrantBinding(**fields)  # type: ignore[arg-type]


def _admit(
    kernel_engine: Engine, fixture: Fixture, checkout: CheckoutRef | None = None
) -> Admitted:
    ref = checkout or fixture.checkout
    session = Session(kernel_engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            decision = admit(
                session,
                AdmissionRequest(
                    tenant_id=fixture.tenant_id,
                    merchant_id=fixture.merchant_id,
                    checkout=ref,
                    amount=APPROVED_TOTAL,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    idempotency_key=f"idem-{uuid7().hex[:16]}",
                    principal=fixture.principal,
                    correlation_id=fixture.correlation_id,
                    approval_id=uuid7(),
                ),
                StubMerchant(ref.checkout_id),
            )
    finally:
        session.close()
    assert decision.allowed, f"fixture admission was refused: {decision.code}"
    assert decision.grant_id is not None
    assert decision.payment_attempt_id is not None
    return Admitted(ref, decision.payment_attempt_id, decision.grant_id)


def _grant_row(admin_engine: Engine, tenant_id: uuid.UUID, grant_id: uuid.UUID, column: str):
    # S608: ``column`` is a literal supplied by this module, never request data; a SQL
    # identifier cannot be passed as a bound parameter.
    return scalar(
        admin_engine,
        tenant_id,
        f"SELECT {column} FROM execution_grants WHERE id = :g",  # noqa: S608
        g=grant_id,
    )


@pytest.fixture
def admitted(adm_kernel_engine: Engine, admissible: Fixture) -> Admitted:
    return _admit(adm_kernel_engine, admissible)


# ------------------------------------------------------------------- single use


class TestConcurrentConsumption:
    def test_ten_workers_handed_one_grant_consume_it_once(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """Exactly one provider call may follow. The other nine must be told to read
        state, not to send the command again."""
        outcomes = race(
            adm_kernel_engine,
            admissible.tenant_id,
            lambda session, _: consume_grant(
                session, admitted.grant_id, admitted.binding(admissible.tenant_id)
            ),
            workers=10,
        )

        winners = [o for o in outcomes if o.ok]
        assert len(winners) == 1, f"{len(winners)} workers consumed one grant"

        losers = [o for o in outcomes if not o.ok]
        wrong = [o for o in losers if not isinstance(o.error, GrantAlreadyConsumedError)]
        assert not wrong, (
            "a loser was refused for a reason other than 'already consumed': "
            f"{[(type(o.error).__name__, str(o.error)[:120]) for o in wrong]}"
        )
        for loser in losers:
            assert loser.error.code is RecoveryCode.CONCURRENT_OPERATION, (
                "a consumed grant proves the operation was started, never that it "
                "succeeded; DUPLICATE_OPERATION would let an agent announce a completed "
                "payment on the strength of somebody else's in-flight call"
            )

        assert _grant_row(adm_admin_engine, admissible.tenant_id, admitted.grant_id, "status") == (
            GrantStatus.CONSUMED.value
        )
        assert (
            _grant_row(adm_admin_engine, admissible.tenant_id, admitted.grant_id, "consumed_at")
            is not None
        )

    def test_a_second_consumption_after_the_first_committed_is_refused(
        self, adm_kernel_engine, admissible, admitted
    ):
        binding = admitted.binding(admissible.tenant_id)
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            consume_grant(session, admitted.grant_id, binding)
        session.close()

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(GrantAlreadyConsumedError):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consume_grant(session, admitted.grant_id, binding)
        finally:
            session.close()

    def test_a_worker_that_rolls_back_leaves_the_grant_usable(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """Consumption is only real if the transaction that spent it committed.

        A worker that consumes and then dies before its commit must leave the grant
        ISSUED: the provider was never called, so refusing the retry would strand a
        legitimate payment behind a capability that cannot be re-minted.
        """
        binding = admitted.binding(admissible.tenant_id)
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            session.begin()
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            consume_grant(session, admitted.grant_id, binding)
            session.rollback()
        finally:
            session.close()

        assert (
            _grant_row(adm_admin_engine, admissible.tenant_id, admitted.grant_id, "status")
            == GrantStatus.ISSUED.value
        )

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consumed = consume_grant(session, admitted.grant_id, binding)
        finally:
            session.close()
        assert consumed.status == GrantStatus.CONSUMED


class TestReuseAfterTheAttemptFailed:
    def test_a_consumed_grant_is_not_revived_by_the_attempt_failing(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """A confirmed provider failure retires the attempt. It does not un-spend the
        grant: the same single use must not authorise a second command."""
        binding = admitted.binding(admissible.tenant_id)
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            consume_grant(session, admitted.grant_id, binding)
        session.close()

        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text("UPDATE payment_attempts SET status = 'FAILED' WHERE id = :i"),
                {"i": admitted.attempt_id},
            )

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(GrantAlreadyConsumedError):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consume_grant(session, admitted.grant_id, binding)
        finally:
            session.close()

    def test_no_replacement_grant_is_minted_for_the_failed_attempt(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """Specification 10.3.1: an uncertain outcome is reconciled, never re-granted.

        A retry is a *new* payment attempt with a grant of its own -- proven here by
        admitting the next version once the failed attempt has freed the index.
        """
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            consume_grant(session, admitted.grant_id, admitted.binding(admissible.tenant_id))
        session.close()

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(GrantReplacementRefusedError):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                issue_grant(
                    session,
                    tenant=admissible.tenant_id,
                    checkout_ref=admitted.checkout,
                    payment_attempt_id=admitted.attempt_id,
                    operation=Operation.PAYMENT_CREATE_ORDER,
                    amount=APPROVED_TOTAL,
                    kernel_decision_id=uuid7(),
                    ttl_seconds=300,
                )
        finally:
            session.close()

        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text("UPDATE payment_attempts SET status = 'FAILED' WHERE id = :i"),
                {"i": admitted.attempt_id},
            )
        retry = add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )
        second = _admit(adm_kernel_engine, admissible, retry)
        assert second.grant_id != admitted.grant_id
        assert second.attempt_id != admitted.attempt_id


class TestSubstitutedCommands:
    """A grant is bound byte for byte. Each of these is one field moved."""

    @pytest.fixture
    def other_checkout(self, adm_admin_engine, adm_kernel_engine, admissible) -> CheckoutRef:
        return add_approved_version(
            adm_admin_engine,
            adm_kernel_engine,
            admissible,
            version=admissible.checkout.version + 1,
        )

    def _refused(self, kernel_engine, tenant_id, grant_id, binding) -> GrantBindingMismatchError:
        session = Session(kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(GrantBindingMismatchError) as caught:
                session.execute(SET_TENANT, {"t": str(tenant_id)})
                consume_grant(session, grant_id, binding)
        finally:
            session.close()
        return caught.value

    def test_a_grant_for_one_checkout_does_not_authorise_another(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted, other_checkout
    ):
        error = self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(admissible.tenant_id, checkout=other_checkout),
        )
        moved = {delta.field_path for delta in error.deltas}
        assert {"checkout_version", "content_hash"} <= moved, moved
        assert (
            _grant_row(adm_admin_engine, admissible.tenant_id, admitted.grant_id, "status")
            == GrantStatus.ISSUED.value
        ), "a refused substitution must not spend the grant"

    def test_a_raised_amount_is_refused(self, adm_kernel_engine, admissible, admitted):
        error = self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(admissible.tenant_id, amount=Money(APPROVED_TOTAL.minor * 10, "INR")),
        )
        assert {d.field_path for d in error.deltas} == {"amount_minor"}

    def test_the_same_number_in_another_currency_is_refused(
        self, adm_kernel_engine, admissible, admitted
    ):
        """39500 INR and 39500 USD differ by two orders of magnitude in real money."""
        error = self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(admissible.tenant_id, amount=Money(APPROVED_TOTAL.minor, "USD")),
        )
        assert {d.field_path for d in error.deltas} == {"currency"}

    def test_a_payment_grant_does_not_authorise_a_refund(
        self, adm_kernel_engine, admissible, admitted
    ):
        error = self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(admissible.tenant_id, operation=Operation.REFUND_EXECUTE),
        )
        assert "operation" in {d.field_path for d in error.deltas}

    def test_a_forged_content_hash_is_refused(self, adm_kernel_engine, admissible, admitted):
        """The hash binds the bytes the buyer approved; a matching id and version with
        different content is exactly the substitution the hash exists to catch."""
        forged = CheckoutRef(
            admitted.checkout.checkout_id,
            admitted.checkout.version,
            canonical_hash({"total_minor": 1}),
        )
        error = self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(admissible.tenant_id, checkout=forged),
        )
        assert {d.field_path for d in error.deltas} == {"content_hash"}

    def test_every_moved_field_is_reported_not_only_the_first(
        self, adm_kernel_engine, admissible, admitted, other_checkout
    ):
        """The audit record must show the full shape of the attempted substitution."""
        error = self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(
                admissible.tenant_id,
                checkout=other_checkout,
                amount=Money(1, "USD"),
                payment_attempt_id=uuid7(),
            ),
        )
        moved = {delta.field_path for delta in error.deltas}
        assert {
            "checkout_version",
            "content_hash",
            "payment_attempt_id",
            "amount_minor",
            "currency",
        } <= moved, moved

    def test_the_grant_still_works_for_the_command_it_was_issued_for(
        self, adm_kernel_engine, admissible, admitted
    ):
        """Every refusal above must have left the capability intact for its rightful use."""
        self._refused(
            adm_kernel_engine,
            admissible.tenant_id,
            admitted.grant_id,
            admitted.binding(admissible.tenant_id, amount=Money(1, "INR")),
        )
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consumed = consume_grant(
                    session, admitted.grant_id, admitted.binding(admissible.tenant_id)
                )
        finally:
            session.close()
        assert consumed.status == GrantStatus.CONSUMED


class TestExpiryBetweenIssueAndUse:
    def test_a_window_that_closed_refuses_even_while_the_row_reads_issued(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """No sweeper has run, so ``status`` is still ISSUED. ``expires_at`` decides.

        The elapsed time is simulated by moving ``expires_at`` into the past rather than
        by sleeping, which makes the test deterministic; the comparison the kernel makes
        is still ``now() >= expires_at`` evaluated by PostgreSQL, not by this process.
        """
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "UPDATE execution_grants SET expires_at = now() - interval '1 second' "
                    "WHERE id = :g"
                ),
                {"g": admitted.grant_id},
            )
        assert (
            _grant_row(adm_admin_engine, admissible.tenant_id, admitted.grant_id, "status")
            == GrantStatus.ISSUED.value
        )

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(GrantExpiredError) as caught:
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consume_grant(session, admitted.grant_id, admitted.binding(admissible.tenant_id))
        finally:
            session.close()
        assert caught.value.code is RecoveryCode.RESERVATION_EXPIRED

    def test_an_expired_grant_that_was_already_consumed_reports_consumption(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """Order matters, and it is the difference between one charge and two.

        Reporting expiry first would tell the caller to re-admit an operation whose
        provider request may already be in flight.
        """
        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            consume_grant(session, admitted.grant_id, admitted.binding(admissible.tenant_id))
        session.close()

        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "UPDATE execution_grants SET expires_at = now() - interval '1 hour' "
                    "WHERE id = :g"
                ),
                {"g": admitted.grant_id},
            )

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(GrantAlreadyConsumedError):
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consume_grant(session, admitted.grant_id, admitted.binding(admissible.tenant_id))
        finally:
            session.close()

    def test_a_lapsed_reservation_does_not_by_itself_stop_consumption(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """Documented behaviour, asserted so a change to it is deliberate.

        ``consume_grant`` does not re-read the reservation: the hold was checked and spent
        inside the admission transaction, and the grant's own TTL (at most fifteen
        minutes) is the window that governs execution afterwards. Expiring the hold under
        a consumed reservation therefore changes nothing here.

        This is safe only because the reservation is *consumed* at admission rather than
        left ACTIVE. If a future change lets a grant outlive an unconsumed hold, this test
        is the one that should start failing.
        """
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            conn.execute(
                text(
                    "UPDATE reservations SET expires_at = now() - interval '1 hour' "
                    "WHERE checkout_id = :c AND checkout_version = :v"
                ),
                {
                    "c": admitted.checkout.checkout_id,
                    "v": admitted.checkout.version,
                },
            )
            status = conn.execute(
                text(
                    "SELECT status FROM reservations WHERE checkout_id = :c "
                    "AND checkout_version = :v"
                ),
                {"c": admitted.checkout.checkout_id, "v": admitted.checkout.version},
            ).scalar()
        assert status == "CONSUMED", "admission must have spent the hold, not left it live"

        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin():
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consumed = consume_grant(
                    session, admitted.grant_id, admitted.binding(admissible.tenant_id)
                )
        finally:
            session.close()
        assert consumed.status == GrantStatus.CONSUMED


class TestRevocationRacingConsumption:
    def test_a_grant_never_ends_up_both_consumed_and_revoked(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """Safe Mode's hand against the worker's. Either may win; both may not.

        A grant that read CONSUMED *and* REVOKED would mean the evidence claims an
        operation was withdrawn while the provider request was in flight.
        """
        binding = admitted.binding(admissible.tenant_id)

        def body(session: Session, index: int) -> str:
            if index % 2:
                return f"revoked:{revoke_grant(session, admitted.grant_id).status}"
            return f"consumed:{consume_grant(session, admitted.grant_id, binding).status}"

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=8)
        for outcome in outcomes:
            if outcome.ok:
                continue
            assert isinstance(outcome.error, GrantAlreadyConsumedError | GrantRevokedError), (
                f"unexpected refusal: {type(outcome.error).__name__}: {outcome.error}"
            )

        final = _grant_row(adm_admin_engine, admissible.tenant_id, admitted.grant_id, "status")
        assert final in {GrantStatus.CONSUMED.value, GrantStatus.REVOKED.value}
        consumed_at = _grant_row(
            adm_admin_engine, admissible.tenant_id, admitted.grant_id, "consumed_at"
        )
        if final == GrantStatus.REVOKED.value:
            assert consumed_at is None, (
                "the grant is REVOKED yet carries a consumption time; a provider request "
                "may have been sent under a grant the evidence says was withdrawn"
            )


class TestOneCommandCarriesOneGrant:
    def test_two_commands_racing_for_one_grant_leave_one_link(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        command_ids = [uuid7() for _ in range(6)]

        def body(session: Session, index: int):
            return link_command(
                session,
                tenant_id=admissible.tenant_id,
                grant_id=admitted.grant_id,
                outbox_command_id=command_ids[index],
                correlation_id=admissible.correlation_id,
            )

        outcomes = race(adm_kernel_engine, admissible.tenant_id, body, workers=6)
        winners = [o for o in outcomes if o.ok]
        assert len(winners) == 1, f"{len(winners)} commands claimed one grant"
        for loser in (o for o in outcomes if not o.ok):
            assert isinstance(loser.error, GrantLinkConflictError), loser.error

        linked = _grant_row(
            adm_admin_engine, admissible.tenant_id, admitted.grant_id, "outbox_command_id"
        )
        assert linked == command_ids[winners[0].index]


class TestTenantConfinement:
    def test_another_tenants_grant_is_indistinguishable_from_one_that_never_existed(
        self, adm_kernel_engine, adm_admin_engine, admissible, admitted
    ):
        """A grant id is a capability. A probe must not learn that it names something."""
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
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                with session.begin(), pytest.raises(GrantNotFoundError) as real:
                    session.execute(SET_TENANT, {"t": str(stranger)})
                    consume_grant(session, admitted.grant_id, admitted.binding(stranger))
            finally:
                session.close()

            invented = uuid7()
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                with session.begin(), pytest.raises(GrantNotFoundError) as fictional:
                    session.execute(SET_TENANT, {"t": str(stranger)})
                    consume_grant(session, invented, admitted.binding(stranger))
            finally:
                session.close()

            assert str(real.value).replace(str(admitted.grant_id), "X") == str(
                fictional.value
            ).replace(str(invented), "X"), (
                "the two refusals differ, so the message distinguishes a real grant "
                "belonging to somebody else from an id that names nothing"
            )
        finally:
            with adm_admin_engine.begin() as conn:
                conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": stranger})

    def test_a_grant_cannot_be_consumed_under_a_binding_naming_another_tenant(
        self, adm_kernel_engine, admissible, admitted
    ):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with session.begin(), pytest.raises(Exception) as caught:
                session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                consume_grant(session, admitted.grant_id, admitted.binding(uuid.uuid4()))
        finally:
            session.close()
        assert caught.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

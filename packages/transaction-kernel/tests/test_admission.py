"""Admission integration tests, specification 10.3.

Every concurrency claim here is proven with real contending PostgreSQL sessions. Mocking
a lock proves that the mock works; the guarantees this kernel makes come from the
database, so the database has to be in the test.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

import pytest
from admission_support import APPROVED_TOTAL, Fixture, StubMerchant
from commerce_domain import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Money,
    RecoveryCode,
    canonical_hash,
    uuid7,
)
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel.admission import AdmissionError, AdmissionRequest, admit
from transaction_kernel.contracts import Operation

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


def _request(fx: Fixture, **overrides) -> AdmissionRequest:
    base = {
        "tenant_id": fx.tenant_id,
        "merchant_id": fx.merchant_id,
        "checkout": fx.checkout,
        "amount": APPROVED_TOTAL,
        "operation": Operation.PAYMENT_CREATE_ORDER,
        "idempotency_key": f"idem-{uuid7().hex[:16]}",
        "principal": fx.principal,
        "correlation_id": fx.correlation_id,
        "approval_id": fx.approval_id,
    }
    base.update(overrides)
    return AdmissionRequest(**base)


def _run(factory: Callable[[], Session], fx: Fixture, merchant, **overrides):
    session = factory()
    with session.begin():
        session.execute(SET_TENANT, {"t": str(fx.tenant_id)})
        return admit(session, _request(fx, **overrides), merchant)


class TestHappyPath:
    def test_admits_and_issues_exactly_one_grant(
        self, kernel_session_factory, admissible, merchant
    ):
        decision = _run(kernel_session_factory, admissible, merchant)
        assert decision.allowed
        assert decision.code is RecoveryCode.OK
        # The constructor already refuses an allowed decision without a grant; assert the
        # value is present so a future relaxation of that invariant is caught here too.
        assert decision.grant_id is not None
        assert decision.payment_attempt_id is not None

    def test_creates_one_attempt_and_one_grant_row(
        self, kernel_session_factory, admissible, merchant
    ):
        decision = _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            attempts = session.execute(
                text("SELECT count(*) FROM payment_attempts WHERE checkout_id = :c"),
                {"c": admissible.checkout.checkout_id},
            ).scalar()
            issued = session.execute(
                text("SELECT count(*) FROM execution_grants WHERE payment_attempt_id = :a"),
                {"a": decision.payment_attempt_id},
            ).scalar()
        assert attempts == 1
        assert issued == 1

    def test_writes_audit_in_the_same_transaction(
        self, kernel_session_factory, admissible, merchant
    ):
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            kinds = (
                session.execute(
                    text(
                        "SELECT event_type FROM audit_events WHERE aggregate_id = :c ORDER BY seq"
                    ),
                    {"c": admissible.checkout.checkout_id},
                )
                .scalars()
                .all()
            )
        assert "admission.allowed" in kinds


class TestSingleWinner:
    def test_two_concurrent_admissions_produce_exactly_one_attempt(
        self, kernel_session_factory, admissible, merchant, adm_kernel_engine
    ):
        """The core claim: a logical purchase executes at most once.

        Two threads open real transactions and race. The partial unique index permits one
        non-terminal attempt per checkout, so one commits and the other must fail rather
        than create a second charge.
        """
        results: list[object] = []
        barrier = threading.Barrier(2)

        def attempt() -> None:
            session = Session(adm_kernel_engine, expire_on_commit=False)
            try:
                barrier.wait(timeout=10)
                with session.begin():
                    session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
                    results.append(
                        admit(
                            session,
                            _request(admissible),
                            StubMerchant(admissible.checkout.checkout_id),
                        )
                    )
            except Exception as exc:  # losing the race is a normal outcome
                results.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        allowed = [r for r in results if getattr(r, "allowed", False)]
        assert len(allowed) == 1, f"expected exactly one winner, got {results}"

        # The loser must lose *cleanly*. A structured recovery code means the caller can
        # read current state and explain it; an unhandled exception would mean the race
        # was survived by accident rather than handled by design.
        losers = [r for r in results if r not in allowed]
        assert len(losers) == 1
        loser = losers[0]
        assert not isinstance(loser, BaseException), (
            f"loser crashed instead of being denied: {loser!r}"
        )
        assert loser.code in {
            RecoveryCode.DUPLICATE_OPERATION,
            RecoveryCode.CONCURRENT_OPERATION,
        }, f"loser returned {loser.code}, which does not tell the caller a race occurred"

        session = Session(adm_kernel_engine, expire_on_commit=False)
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            attempts = session.execute(
                text("SELECT count(*) FROM payment_attempts WHERE checkout_id = :c"),
                {"c": admissible.checkout.checkout_id},
            ).scalar()
        session.close()
        assert attempts == 1, "a second payment attempt was created for one checkout"


class TestStaleApproval:
    def test_changed_total_denies_and_supersedes_the_version(
        self, kernel_session_factory, admissible, merchant
    ):
        """The demonstration's headline moment, specification 10.3 step 11."""
        merchant.total = Money(39500 + 5500, "INR")  # a fee changed underneath the approval
        decision = _run(kernel_session_factory, admissible, merchant)

        assert not decision.allowed
        assert decision.code is RecoveryCode.REAPPROVAL_REQUIRED
        assert decision.next_version == admissible.checkout.version + 1
        assert any(d.field_path == "total" for d in decision.deltas)
        delta = next(d for d in decision.deltas if d.field_path == "total")
        assert delta.approved == 39500
        assert delta.current == 45000

    def test_denial_creates_no_payment_attempt(self, kernel_session_factory, admissible, merchant):
        """No Razorpay order may exist before the buyer approves the new version."""
        merchant.total = Money(45000, "INR")
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            attempts = session.execute(
                text("SELECT count(*) FROM payment_attempts WHERE checkout_id = :c"),
                {"c": admissible.checkout.checkout_id},
            ).scalar()
        assert attempts == 0

    def test_version_n_is_invalidated_and_n_plus_one_awaits_approval(
        self, kernel_session_factory, admissible, merchant
    ):
        merchant.total = Money(45000, "INR")
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            rows = dict(
                session.execute(
                    text(
                        "SELECT version, status FROM checkout_versions "
                        "WHERE checkout_id = :c ORDER BY version"
                    ),
                    {"c": admissible.checkout.checkout_id},
                ).all()
            )
        assert rows[admissible.checkout.version] == "INVALIDATED"
        assert rows[admissible.checkout.version + 1] == "APPROVAL_REQUIRED"

    def test_invalidated_version_cannot_be_admitted_afterwards(
        self, kernel_session_factory, admissible, merchant
    ):
        """Version N never returns to APPROVED, even if merchant state reverts."""
        merchant.total = Money(45000, "INR")
        _run(kernel_session_factory, admissible, merchant)
        merchant.total = APPROVED_TOTAL  # the fee change is reverted
        again = _run(kernel_session_factory, admissible, merchant)
        assert not again.allowed
        assert again.code is RecoveryCode.STALE_CHECKOUT

    def test_unavailable_item_denies(self, kernel_session_factory, admissible, merchant):
        merchant.available = False
        decision = _run(kernel_session_factory, admissible, merchant)
        assert not decision.allowed
        assert decision.code is RecoveryCode.REAPPROVAL_REQUIRED

    def test_a_partial_sellout_still_offers_a_successor(
        self, kernel_session_factory, admissible, merchant
    ):
        """Some lines gone, some left: the buyer gets a version N+1 to decide about."""
        merchant.available = False
        merchant.total = Money(20000, "INR")
        decision = _run(kernel_session_factory, admissible, merchant)
        assert decision.code is RecoveryCode.REAPPROVAL_REQUIRED
        assert decision.next_version == admissible.checkout.version + 1


class TestTheApprovalIsRead:
    """The module's first promise, made true.

    ``admit`` used to check ``approval_id`` for presence and never read it. A caller
    holding a checkout could name any id at all -- one belonging to another version, one
    already spent, one that never existed -- and admission would write a payment attempt,
    issue a grant and consume the reservation before anything looked. Each test here names
    one way of being wrong, and each must be refused before any of that happens.
    """

    def _refused(self, kernel_session_factory, admissible, merchant, **overrides):
        decision = _run(kernel_session_factory, admissible, merchant, **overrides)
        assert not decision.allowed
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.grant_id is None
        assert decision.payment_attempt_id is None
        return decision

    def test_an_invented_approval_id_is_refused(self, kernel_session_factory, admissible, merchant):
        decision = self._refused(kernel_session_factory, admissible, merchant, approval_id=uuid7())
        assert decision.explanation == "approval_not_found"

    def test_an_approval_for_a_different_amount_is_refused(
        self, kernel_session_factory, admissible, merchant
    ):
        """The buyer agreed to a number. Admission must be for that number."""
        merchant.total = Money(APPROVED_TOTAL.minor + 1, "INR")
        decision = _run(
            kernel_session_factory,
            admissible,
            merchant,
            amount=Money(APPROVED_TOTAL.minor + 1, "INR"),
        )
        assert not decision.allowed
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        assert decision.explanation == "approval_amount_does_not_match"

    def test_an_approval_for_a_different_operation_is_refused(
        self, kernel_session_factory, admissible, merchant
    ):
        """Consent to buy is not consent to refund."""
        decision = self._refused(
            kernel_session_factory,
            admissible,
            merchant,
            operation=Operation.REFUND_EXECUTE,
        )
        assert decision.explanation == "approval_authorises_a_different_operation"

    def test_an_approval_already_spent_is_refused(
        self, kernel_session_factory, admissible, merchant
    ):
        """The first admission spends it; the second finds it CONSUMED."""
        first = _run(kernel_session_factory, admissible, merchant)
        assert first.allowed
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            session.execute(
                text("UPDATE approvals SET status = 'CONSUMED' WHERE tenant_id = :t AND id = :i"),
                {"t": admissible.tenant_id, "i": admissible.approval_id},
            )
        decision = _run(kernel_session_factory, admissible, merchant)
        assert not decision.allowed
        # Not AUTHORITY_INSUFFICIENT. Whoever spent it was entitled to, and so was this
        # caller; what happened is that an attempt is already running. ADR 0003 D9 wants
        # the buyer pointed at that attempt rather than told they were not allowed.
        assert decision.code is RecoveryCode.CONCURRENT_OPERATION
        assert decision.explanation == "another_attempt_won"
        assert decision.grant_id is None
        assert decision.payment_attempt_id is None

    def test_a_lapsed_approval_is_refused(self, kernel_session_factory, admissible, merchant):
        """Judged by the database clock, not by a sweep having run."""
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            session.execute(
                text(
                    "UPDATE approvals SET expires_at = now() - interval '1 second' "
                    "WHERE tenant_id = :t AND id = :i"
                ),
                {"t": admissible.tenant_id, "i": admissible.approval_id},
            )
        decision = self._refused(kernel_session_factory, admissible, merchant)
        assert decision.explanation == "approval_expired"

    def test_the_refusal_is_audited(self, kernel_session_factory, admissible, merchant):
        """A refused money action is still a money action and leaves evidence."""
        self._refused(kernel_session_factory, admissible, merchant, approval_id=uuid7())
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            payloads = (
                session.execute(
                    text(
                        "SELECT payload FROM audit_events WHERE aggregate_type = 'checkout' "
                        "AND aggregate_id = :c AND event_type = 'admission.denied'"
                    ),
                    {"c": admissible.checkout.checkout_id},
                )
                .scalars()
                .all()
            )
        assert [p["explanation"] for p in payloads] == ["approval_not_found"]


class TestTheVersionMustBeApproved:
    def test_a_version_still_awaiting_a_decision_cannot_pay(
        self, kernel_session_factory, admissible, merchant
    ):
        """The status column was selected and never read."""
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            session.execute(
                text(
                    "UPDATE checkout_versions SET status = 'APPROVAL_REQUIRED' "
                    "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
                ),
                {
                    "t": admissible.tenant_id,
                    "c": admissible.checkout.checkout_id,
                    "v": admissible.checkout.version,
                },
            )
        decision = _run(kernel_session_factory, admissible, merchant)
        assert not decision.allowed
        assert decision.code is RecoveryCode.STALE_CHECKOUT
        assert decision.explanation == "version_is_approval_required_not_approved"


class TestTotalSellout:
    """Every approved line has gone.

    There is nothing to re-price, so there is nothing to approve. The kernel must say so
    in its own code, hand the stock back, and leave the refusal on the record -- the last
    of those being the one a rollback used to destroy.
    """

    def test_it_is_sold_out_and_names_no_successor(
        self, kernel_session_factory, admissible, merchant
    ):
        merchant.sell_out_everything()
        decision = _run(kernel_session_factory, admissible, merchant)
        assert not decision.allowed
        assert decision.code is RecoveryCode.SOLD_OUT
        # The whole point of a separate code: REAPPROVAL_REQUIRED promises a version to
        # approve, and there is none. Offering version N+1 here would be an empty cart
        # dressed as a purchase.
        assert decision.next_version is None

    def test_it_creates_no_attempt_and_no_grant(self, kernel_session_factory, admissible, merchant):
        """What licenses the storefront's words "you were not charged"."""
        merchant.sell_out_everything()
        decision = _run(kernel_session_factory, admissible, merchant)
        assert decision.grant_id is None
        assert decision.payment_attempt_id is None
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            attempts = session.execute(
                text("SELECT count(*) FROM payment_attempts WHERE checkout_id = :c"),
                {"c": admissible.checkout.checkout_id},
            ).scalar()
        assert attempts == 0

    def test_it_invalidates_the_approved_version(
        self, kernel_session_factory, admissible, merchant
    ):
        merchant.sell_out_everything()
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            row = session.execute(
                text(
                    "SELECT status, invalidated_at FROM checkout_versions "
                    "WHERE checkout_id = :c AND version = :v"
                ),
                {"c": admissible.checkout.checkout_id, "v": admissible.checkout.version},
            ).one()
        assert row.status == "INVALIDATED"
        assert row.invalidated_at is not None

    def test_it_writes_no_successor_version(self, kernel_session_factory, admissible, merchant):
        merchant.sell_out_everything()
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            later = session.execute(
                text(
                    "SELECT count(*) FROM checkout_versions WHERE checkout_id = :c AND version > :v"
                ),
                {"c": admissible.checkout.checkout_id, "v": admissible.checkout.version},
            ).scalar()
        assert later == 0

    def test_it_gives_the_stock_back(self, kernel_session_factory, admissible, merchant):
        """An invalidated version holding stock is a leak with no owner left to clear it."""
        merchant.sell_out_everything()
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            statuses = (
                session.execute(
                    text(
                        "SELECT status FROM reservations "
                        "WHERE checkout_id = :c AND checkout_version = :v"
                    ),
                    {"c": admissible.checkout.checkout_id, "v": admissible.checkout.version},
                )
                .scalars()
                .all()
            )
        assert statuses, "the fixture is expected to hold a reservation"
        assert set(statuses) == {"RELEASED"}

    def test_the_denial_survives_the_transaction(
        self, kernel_session_factory, admissible, merchant
    ):
        """The regression this class exists for.

        The refusal used to be written and then rolled back with the transaction that
        raised on the malformed replacement document, so a sold-out cart left no evidence
        at all. A platform that cannot explain what it refused has not refused well.
        """
        merchant.sell_out_everything()
        _run(kernel_session_factory, admissible, merchant)
        session = kernel_session_factory()
        with session.begin():
            session.execute(SET_TENANT, {"t": str(admissible.tenant_id)})
            events = session.execute(
                text(
                    "SELECT event_type, payload FROM audit_events "
                    "WHERE aggregate_type = 'checkout' AND aggregate_id = :c "
                    "ORDER BY seq"
                ),
                {"c": admissible.checkout.checkout_id},
            ).all()
        denials = [e for e in events if e.event_type == "admission.denied"]
        assert len(denials) == 1
        assert denials[0].payload["code"] == "SOLD_OUT"
        assert denials[0].payload["next_version"] is None


class TestFreshnessAndBinding:
    def test_wrong_content_hash_is_refused(self, kernel_session_factory, admissible, merchant):
        """An approval quoting bytes the store does not hold is refused, not reconciled."""
        forged = CheckoutRef(
            admissible.checkout.checkout_id,
            admissible.checkout.version,
            canonical_hash({"total_minor": 1}),
        )
        decision = _run(kernel_session_factory, admissible, merchant, checkout=forged)
        assert not decision.allowed
        assert decision.code is RecoveryCode.STALE_CHECKOUT

    def test_unknown_version_is_refused(self, kernel_session_factory, admissible, merchant):
        ghost = CheckoutRef(admissible.checkout.checkout_id, 99, admissible.checkout.content_hash)
        decision = _run(kernel_session_factory, admissible, merchant, checkout=ghost)
        assert not decision.allowed
        assert decision.code is RecoveryCode.STALE_CHECKOUT


class TestRequestValidation:
    def test_both_approval_and_proof_is_refused(self, admissible):
        from transaction_kernel.contracts import VerifiedAuthorityProof

        proof = VerifiedAuthorityProof(
            protocol="UCP",
            protocol_version="2026-08-25",
            issuer="i",
            subject="s",
            key_id="k",
            algorithm="ES256",
            mandate_ref="m",
            tenant_id=admissible.tenant_id,
            merchant_id=admissible.merchant_id,
            checkout=admissible.checkout,
            amount=APPROVED_TOTAL,
            action="pay",
            expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            authority_epoch=0,
            verification_receipt_id="r",
            correlation_id=uuid7(),
        )
        with pytest.raises(AdmissionError, match="exactly one of"):
            _request(admissible, proof=proof)

    def _proof(self, admissible, **overrides):
        from datetime import UTC, datetime

        from transaction_kernel.contracts import VerifiedAuthorityProof

        fields = {
            "protocol": "UCP",
            "protocol_version": "2026-08-25",
            "issuer": "i",
            "subject": "s",
            "key_id": "k",
            "algorithm": "ES256",
            "mandate_ref": "m",
            "tenant_id": admissible.tenant_id,
            "merchant_id": admissible.merchant_id,
            "checkout": admissible.checkout,
            "amount": APPROVED_TOTAL,
            "action": "pay",
            "expires_at": datetime.now(UTC),
            "authority_epoch": 0,
            "verification_receipt_id": "r",
            "correlation_id": uuid7(),
        }
        fields.update(overrides)
        return VerifiedAuthorityProof(**fields)

    def test_a_mandate_that_names_a_different_amount_is_refused(self, admissible):
        """A proof authorises the action it names and no other.

        `VerifiedAuthorityProof` promises the kernel "independently re-checks every field
        against locked rows", and for the mandate itself that never happened:
        `request.proof` was read once in the whole module, to pick a Safe Mode gate. Every
        later step reads the *request* -- the amount against merchant state, the content
        hash against the approved version -- so a gateway presenting a mandate for one
        amount and a request for another would have been admitted against the request and
        evidenced against the mandate.
        """
        cheaper = Money(APPROVED_TOTAL.minor - 1, APPROVED_TOTAL.currency)
        with pytest.raises(AdmissionError, match="mandate's amount"):
            _request(admissible, approval_id=None, proof=self._proof(admissible, amount=cheaper))

    def test_a_mandate_for_another_checkout_is_refused(self, admissible):
        other = CheckoutRef(uuid7(), 1, admissible.checkout.content_hash)
        with pytest.raises(AdmissionError, match="mandate's checkout"):
            _request(admissible, approval_id=None, proof=self._proof(admissible, checkout=other))

    def test_a_mandate_matching_the_request_is_accepted(self, admissible):
        """The binding must not refuse the consistent case it exists to allow."""
        request = _request(admissible, approval_id=None, proof=self._proof(admissible))
        assert request.proof is not None

    def test_neither_approval_nor_proof_is_refused(self, admissible):
        with pytest.raises(AdmissionError, match="exactly one of"):
            _request(admissible, approval_id=None)

    def test_principal_from_another_tenant_is_refused(self, admissible):
        """Tenant comes from the authenticated session, never from the request body."""
        foreign = AgentPrincipal(
            principal_id="p-foreign",
            tenant_id=uuid.uuid4(),
            actor_type=ActorType.AGENT,
        )
        with pytest.raises(AdmissionError, match="does not match request tenant"):
            _request(admissible, principal=foreign)

    def test_non_positive_amount_is_refused(self, admissible):
        with pytest.raises(AdmissionError, match="positive amount"):
            _request(admissible, amount=Money(0, "INR"))

    def test_agent_without_submit_capability_is_denied(
        self, kernel_session_factory, admissible, merchant
    ):
        """An agent may only submit; the capability is checked, not assumed."""
        weak = AgentPrincipal(
            principal_id="p-weak",
            tenant_id=admissible.tenant_id,
            actor_type=ActorType.AGENT,
            capabilities=frozenset({"catalog.search"}),
        )
        decision = _run(kernel_session_factory, admissible, merchant, principal=weak)
        assert not decision.allowed
        assert decision.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    def test_admit_outside_a_transaction_raises(self, adm_kernel_engine, admissible, merchant):
        session = Session(adm_kernel_engine, expire_on_commit=False)
        try:
            with pytest.raises(AdmissionError, match="inside a transaction"):
                admit(session, _request(admissible), merchant)
        finally:
            session.close()

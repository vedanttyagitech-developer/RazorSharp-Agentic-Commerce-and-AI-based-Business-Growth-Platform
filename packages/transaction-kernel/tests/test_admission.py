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
from commerce_domain import Money, canonical_hash, uuid7
from conftest import APPROVED_TOTAL, Fixture, StubMerchant
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel.admission import AdmissionError, AdmissionRequest, admit
from transaction_kernel.contracts import ActorType, AgentPrincipal, CheckoutRef, Operation
from transaction_kernel.recovery import RecoveryCode

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
        "approval_id": uuid7(),
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

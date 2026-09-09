"""The protocol-neutral core, specification 13.1, 13.3 and 29.5.

Two properties in this file are worth more than the rest of the package put together,
because everything else is built on the assumption that they hold.

The first is the **capability ceiling**. An external protocol caller must never hold a
consent capability -- approve, reject, cancel, request a refund -- because consent is not
delegable to the thing that proposed the purchase. ``TestCapabilityCeiling`` asserts it
three ways: that the ceiling excludes them, that a misconfigured tenant granting one has it
silently dropped rather than honoured, and that the resulting principal is refused outright
if it somehow holds one anyway.

The second is that the **intent vocabulary cannot express consent or execution**. There is
no ``APPROVE`` and no ``PAY`` member, so no downstream bug can honour a request that was
never constructible. ``TestIntentVocabulary`` asserts over the enum itself rather than over
behaviour, since the property is an absence.

The evidence and replay suites need a real PostgreSQL. A replay guard asserted against a
fake proves only that the fake behaves; the thing being claimed is that two concurrent
claims on one nonce cannot both win, and that is a statement about a unique index.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from commerce_domain import ActorType, AgentPrincipal, sha256_b64url, uuid7
from commerce_protocols.core import (
    CONSENT_CAPABILITIES,
    DEFAULT_MAX_REQUEST_AGE,
    PINS,
    PROTOCOL_CAPABILITIES,
    AuthenticatedCaller,
    AuthenticationRejected,
    ClaimBoundary,
    EvidenceStage,
    IntentKind,
    Protocol,
    ProtocolIntent,
    ReplayRejected,
    assert_fresh,
    assert_never_consents,
    claim_nonce,
    database_now,
    evidence,
    fingerprint,
    nonce_key,
    open_interaction,
    principal_for,
    require_pin,
)
from commerce_protocols.core.evidence import AGGREGATE_TYPE
from commerce_protocols.core.pins import UnsupportedVersionError
from sqlalchemy import text
from sqlalchemy.orm import Session


def _caller(
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    granted: frozenset[str] = PROTOCOL_CAPABILITIES,
) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        protocol=Protocol.ACP,
        client_id="external-buyer-1",
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        authenticated_by="http-message-signature",
        correlation_id=uuid7(),
        granted=granted,
    )


# ---- the pinned matrix


class TestPins:
    def test_every_protocol_names_a_pinned_version_and_a_claim_boundary(self) -> None:
        assert set(PINS) == {Protocol.UCP, Protocol.AP2, Protocol.ACP, Protocol.MCP}
        for pin in PINS.values():
            assert pin.version
            assert pin.source_ref
            assert pin.disclaimer

    def test_the_ap2_pin_is_the_commit_the_specification_names(self) -> None:
        assert PINS[Protocol.AP2].source_ref == "b4587ac1d055888a73b4b21750973cffba961793"
        assert PINS[Protocol.AP2].version == "v0.2.0"

    def test_the_acp_boundary_refuses_the_claim_that_matters(self) -> None:
        """Specification 16.2: compatible interface is not live in Instant Checkout."""
        pin = PINS[Protocol.ACP]
        assert pin.boundary is ClaimBoundary.COMPATIBLE_INTERFACE
        assert "ChatGPT Instant Checkout" in pin.disclaimer

    def test_a_version_we_have_no_fixtures_for_is_refused(self) -> None:
        with pytest.raises(UnsupportedVersionError) as caught:
            require_pin(Protocol.UCP, "2027-01-01")
        assert caught.value.announced == "2027-01-01"
        assert caught.value.supported == "2026-08-25"

    def test_an_absent_version_is_refused_rather_than_defaulted(self) -> None:
        """A caller that states no contract has agreed to none.

        Defaulting would make a future version change a silent behaviour change for
        somebody's integration.
        """
        with pytest.raises(UnsupportedVersionError) as caught:
            require_pin(Protocol.ACP, None)
        assert caught.value.announced == "<absent>"

    def test_the_pinned_version_is_accepted(self) -> None:
        assert require_pin(Protocol.ACP, "2026-04-17").protocol is Protocol.ACP


# ---- the capability ceiling


class TestCapabilityCeiling:
    def test_the_ceiling_contains_no_consent_capability(self) -> None:
        """Specification 5.3, Registry A against Registry B. The whole architecture."""
        assert frozenset() == PROTOCOL_CAPABILITIES & CONSENT_CAPABILITIES
        assert "checkout.approve" not in PROTOCOL_CAPABILITIES
        assert "checkout.reject" not in PROTOCOL_CAPABILITIES
        assert "checkout.cancel" not in PROTOCOL_CAPABILITIES
        assert "refund.request" not in PROTOCOL_CAPABILITIES

    def test_a_tenant_granting_approve_has_it_silently_dropped(self) -> None:
        """Intersection, not validation, and the difference is the failure mode.

        Refusing every request from a misconfigured client would take a working
        integration down until somebody noticed. Dropping the capability keeps the client
        working for everything it should be able to do, and the one thing it should never
        have been able to do simply is not there.
        """
        tenant_id, merchant_id = uuid.uuid4(), uuid7()
        overreaching = _caller(
            tenant_id,
            merchant_id,
            granted=PROTOCOL_CAPABILITIES | {"checkout.approve", "refund.request"},
        )
        principal = principal_for(overreaching)

        assert not principal.can("checkout.approve")
        assert not principal.can("refund.request")
        assert principal.can("checkout.submit_approved")
        assert principal.capabilities <= PROTOCOL_CAPABILITIES

    def test_a_principal_holding_consent_is_refused_outright(self) -> None:
        """Unreachable through principal_for, checked anyway.

        The cost of the check is nothing; the cost of being wrong is an external party
        consenting on a human's behalf.
        """
        forged = AgentPrincipal(
            principal_id="protocol:acp:attacker",
            tenant_id=uuid.uuid4(),
            actor_type=ActorType.PROTOCOL,
            capabilities=frozenset({"catalogue.read", "checkout.approve"}),
        )
        with pytest.raises(AuthenticationRejected) as caught:
            assert_never_consents(forged)
        assert caught.value.reason == "protocol_principal_holds_consent_capability"
        assert caught.value.details["capabilities"] == ["checkout.approve"]

    def test_a_conforming_principal_passes_the_same_check(self) -> None:
        assert_never_consents(principal_for(_caller(uuid.uuid4(), uuid7())))

    def test_a_protocol_caller_is_audited_as_protocol_not_as_agent(self) -> None:
        """A reviewer must be able to tell an external platform from our own copilot.

        The two have very different blast radii and the audit row is the only place that
        difference is recorded.
        """
        principal = principal_for(_caller(uuid.uuid4(), uuid7()))
        assert principal.actor_type is ActorType.PROTOCOL
        assert principal.actor_type is not ActorType.AGENT

    def test_the_external_client_is_recorded_in_the_delegation_chain(self) -> None:
        principal = principal_for(_caller(uuid.uuid4(), uuid7()))
        assert principal.delegation_chain == ("client:external-buyer-1",)
        assert principal.principal_id == "protocol:acp:external-buyer-1"

    def test_a_derived_sub_agent_can_never_exceed_the_ceiling(self) -> None:
        """The kernel's own subset_for rule, applied to a protocol principal."""
        principal = principal_for(_caller(uuid.uuid4(), uuid7()))
        with pytest.raises(ValueError, match="would gain capabilities"):
            principal.subset_for("child", frozenset({"checkout.approve"}))


# ---- the intent vocabulary


class TestIntentVocabulary:
    def test_there_is_no_intent_that_expresses_consent(self) -> None:
        """An absence, asserted over the enum. A caller cannot form the request at all."""
        names = {member.name for member in IntentKind}
        assert "APPROVE" not in names
        assert "REJECT" not in names
        assert "CONSENT" not in names

    def test_there_is_no_intent_that_executes_money_movement(self) -> None:
        """Only PROPOSE_*, which is a request for a human decision."""
        names = {member.name for member in IntentKind}
        assert "PAY" not in names
        assert "REFUND" not in names
        assert "CANCEL" not in names
        assert "PROPOSE_REFUND" in names
        assert "PROPOSE_CANCELLATION" in names

    def test_only_submitting_an_approved_version_can_reach_the_kernel(self) -> None:
        tenant_id, merchant_id = uuid.uuid4(), uuid7()
        caller = _caller(tenant_id, merchant_id)
        pin = PINS[Protocol.ACP]

        moving = [
            kind
            for kind in IntentKind
            if ProtocolIntent(kind=kind, caller=caller, pin=pin, correlation_id=uuid7()).moves_money
        ]
        assert moving == [IntentKind.SUBMIT_APPROVED]

    def test_proposals_are_marked_as_proposals(self) -> None:
        tenant_id, merchant_id = uuid.uuid4(), uuid7()
        caller = _caller(tenant_id, merchant_id)
        pin = PINS[Protocol.ACP]
        refund = ProtocolIntent(
            kind=IntentKind.PROPOSE_REFUND, caller=caller, pin=pin, correlation_id=uuid7()
        )
        assert refund.is_proposal
        assert not refund.moves_money


# ---- evidence, specification 13.3


class TestFingerprint:
    def test_a_fingerprint_records_the_artifact_without_recording_it(self) -> None:
        """Specification 28: never display credentials or full payment signatures."""
        secret = "sig_" + "a" * 200
        print_ = fingerprint(secret)

        assert print_.digest == sha256_b64url(secret.encode())
        assert print_.length == len(secret)
        assert len(print_.preview) == 8
        assert secret not in print_.digest
        assert secret not in str(print_.as_payload())

    def test_two_fingerprints_of_the_same_bytes_agree(self) -> None:
        """The point of the digest: confirm a stored fixture matches what arrived."""
        assert fingerprint("abc").digest == fingerprint(b"abc").digest

    def test_a_truncated_artifact_is_distinguishable(self) -> None:
        assert fingerprint("abcdefgh").length != fingerprint("abcdefg").length


@pytest.mark.db
class TestEvidenceChain:
    def test_every_stage_lands_on_one_gapless_hash_chain(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """13.3: a reviewer must be able to reconstruct the whole interaction.

        Written through the kernel's audit chain, so the sequence is gapless and each row's
        hash covers the previous one -- deleting a stage or reordering two breaks it.
        """
        tenant_id, merchant_id = cp_tenant
        interaction = open_interaction(
            pin=PINS[Protocol.ACP],
            tenant_id=tenant_id,
            principal=principal_for(_caller(tenant_id, merchant_id)),
            correlation_id=uuid7(),
        )

        interaction.record_received(
            cp_session,
            endpoint="POST /v1/acp/checkout_sessions",
            announced_version="2026-04-17",
            body={"items": [{"id": "TEA-BEV-001", "quantity": 2}]},
            headers_seen={"content-type": "application/json"},
            credential=fingerprint("sig_secret_value"),
        )
        interaction.record(cp_session, EvidenceStage.AUTHENTICATED, client_id="external-buyer-1")
        interaction.record(cp_session, EvidenceStage.VALIDATED)
        interaction.record(cp_session, EvidenceStage.MAPPED, intent="BUILD_BASKET")
        cp_session.flush()

        rows = cp_session.execute(
            text(
                "SELECT seq, event_type, prev_hash, self_hash, payload "
                "FROM audit_events WHERE tenant_id = :t AND aggregate_type = :a "
                "AND aggregate_id = :i ORDER BY seq"
            ),
            {"t": tenant_id, "a": AGGREGATE_TYPE, "i": interaction.interaction_id},
        ).all()

        assert [row.seq for row in rows] == [1, 2, 3, 4]
        assert rows[0].prev_hash is None
        # Deliberately not strict: rows[1:] is one shorter, which is the point of the pairing.
        for earlier, later in zip(rows, rows[1:], strict=False):
            assert later.prev_hash == earlier.self_hash

    def test_the_pinned_version_travels_on_every_row_not_just_the_first(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A row read in isolation must say which contract the platform believed it spoke."""
        tenant_id, merchant_id = cp_tenant
        interaction = open_interaction(
            pin=PINS[Protocol.UCP],
            tenant_id=tenant_id,
            principal=principal_for(_caller(tenant_id, merchant_id)),
            correlation_id=uuid7(),
        )
        interaction.record(cp_session, EvidenceStage.VALIDATED)
        interaction.record(cp_session, EvidenceStage.MAPPED)
        cp_session.flush()

        rows = cp_session.execute(
            text(
                "SELECT payload FROM audit_events WHERE tenant_id = :t "
                "AND aggregate_id = :i ORDER BY seq"
            ),
            {"t": tenant_id, "i": interaction.interaction_id},
        ).all()
        assert len(rows) == 2
        for row in rows:
            assert row.payload["protocol"] == "UCP"
            assert row.payload["protocol_version"] == "2026-08-25"

    def test_the_request_body_is_kept_verbatim_but_the_credential_is_not(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A redacted ask is not evidence of anything; a recorded secret is a leak."""
        tenant_id, merchant_id = cp_tenant
        interaction = open_interaction(
            pin=PINS[Protocol.ACP],
            tenant_id=tenant_id,
            principal=principal_for(_caller(tenant_id, merchant_id)),
            correlation_id=uuid7(),
        )
        # A stand-in bearer token. The assertion below is precisely that it does not
        # survive into the evidence row, so it has to look like the real thing.
        secret = "Bearer super-secret-token-value"  # noqa: S105 - fixture, not a credential
        interaction.record_received(
            cp_session,
            endpoint="POST /v1/acp/checkout_sessions",
            announced_version="2026-04-17",
            body={"items": [{"id": "TEA-BEV-001", "quantity": 2}]},
            headers_seen={"content-type": "application/json"},
            credential=fingerprint(secret),
        )
        cp_session.flush()

        payload = cp_session.execute(
            text("SELECT payload FROM audit_events WHERE tenant_id = :t AND aggregate_id = :i"),
            {"t": tenant_id, "i": interaction.interaction_id},
        ).scalar_one()

        assert payload["request"]["items"][0]["id"] == "TEA-BEV-001"
        assert secret not in str(payload)
        assert payload["credential"]["digest"] == sha256_b64url(secret.encode())

    def test_a_rejection_names_the_stage_that_refused_it(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        interaction = open_interaction(
            pin=PINS[Protocol.ACP],
            tenant_id=tenant_id,
            principal=principal_for(_caller(tenant_id, merchant_id)),
            correlation_id=uuid7(),
        )
        interaction.record_rejection(
            cp_session,
            stage=EvidenceStage.VERIFIED,
            reason="signature_did_not_verify",
            code="AUTHORITY_INSUFFICIENT",
        )
        cp_session.flush()

        payload = cp_session.execute(
            text("SELECT payload FROM audit_events WHERE tenant_id = :t AND aggregate_id = :i"),
            {"t": tenant_id, "i": interaction.interaction_id},
        ).scalar_one()
        assert payload["stage"] == "REJECTED"
        assert payload["failed_at"] == "VERIFIED"
        assert payload["reason"] == "signature_did_not_verify"

    def test_an_interaction_that_records_nothing_leaves_no_trace(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Opening a stream is not an event. A request that never arrived must not appear to."""
        tenant_id, merchant_id = cp_tenant
        interaction = open_interaction(
            pin=PINS[Protocol.ACP],
            tenant_id=tenant_id,
            principal=principal_for(_caller(tenant_id, merchant_id)),
            correlation_id=uuid7(),
        )
        cp_session.flush()
        count = cp_session.execute(
            text("SELECT count(*) FROM audit_events WHERE tenant_id = :t AND aggregate_id = :i"),
            {"t": tenant_id, "i": interaction.interaction_id},
        ).scalar_one()
        assert count == 0


# ---- replay and freshness, specification 16.3


@pytest.mark.db
class TestReplayGuard:
    def test_a_nonce_can_be_claimed_once_and_never_again(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        nonce = uuid7().hex
        claim_nonce(
            cp_session,
            protocol=Protocol.ACP,
            client_id="external-buyer-1",
            nonce=nonce,
            request_digest="digest-1",
        )
        with pytest.raises(ReplayRejected) as caught:
            claim_nonce(
                cp_session,
                protocol=Protocol.ACP,
                client_id="external-buyer-1",
                nonce=nonce,
                request_digest="digest-1",
            )
        assert caught.value.reason == "nonce_already_presented"

    def test_a_replay_is_refused_rather_than_answered_with_a_stored_response(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The difference between a nonce guard and idempotency, and it matters.

        Idempotency answers a repeat with what happened last time. A nonce guard must not:
        that would hand an attacker replaying a captured request the original's result.
        The code is POLICY_EXCEPTION, never DUPLICATE_OPERATION, because
        DUPLICATE_OPERATION is a code a caller may present to a buyer as a completed
        money action and nothing completed here.
        """
        from commerce_domain import RecoveryCode

        nonce = uuid7().hex
        claim_nonce(
            cp_session,
            protocol=Protocol.ACP,
            client_id="c",
            nonce=nonce,
            request_digest="d",
        )
        with pytest.raises(ReplayRejected) as caught:
            claim_nonce(
                cp_session, protocol=Protocol.ACP, client_id="c", nonce=nonce, request_digest="d"
            )
        assert caught.value.code is RecoveryCode.POLICY_EXCEPTION
        assert caught.value.code is not RecoveryCode.DUPLICATE_OPERATION

    def test_a_replayed_nonce_with_a_changed_body_is_still_a_replay(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Somebody probing, rather than a retrying proxy. Both are refused."""
        nonce = uuid7().hex
        claim_nonce(
            cp_session, protocol=Protocol.ACP, client_id="c", nonce=nonce, request_digest="one"
        )
        with pytest.raises(ReplayRejected):
            claim_nonce(
                cp_session, protocol=Protocol.ACP, client_id="c", nonce=nonce, request_digest="two"
            )

    def test_one_client_cannot_burn_another_clients_nonce(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The key is scoped by client, so a guessed nonce is not a denial-of-service."""
        nonce = uuid7().hex
        claim_nonce(
            cp_session, protocol=Protocol.ACP, client_id="attacker", nonce=nonce, request_digest="d"
        )
        claim_nonce(
            cp_session, protocol=Protocol.ACP, client_id="victim", nonce=nonce, request_digest="d"
        )

    def test_the_same_nonce_under_two_protocols_does_not_collide(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        nonce = uuid7().hex
        claim_nonce(
            cp_session, protocol=Protocol.ACP, client_id="c", nonce=nonce, request_digest="d"
        )
        claim_nonce(
            cp_session, protocol=Protocol.UCP, client_id="c", nonce=nonce, request_digest="d"
        )

    def test_an_absent_nonce_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        with pytest.raises(ReplayRejected) as caught:
            claim_nonce(
                cp_session, protocol=Protocol.ACP, client_id="c", nonce="", request_digest="d"
            )
        assert caught.value.reason == "nonce_absent"

    def test_an_over_long_nonce_is_refused_rather_than_truncated(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A truncated nonce silently collides with every nonce sharing its prefix."""
        with pytest.raises(ReplayRejected) as caught:
            claim_nonce(
                cp_session,
                protocol=Protocol.ACP,
                client_id="c",
                nonce="n" * 500,
                request_digest="d",
            )
        assert caught.value.reason == "nonce_too_long"

    def test_the_key_is_scoped_by_protocol_and_client(self) -> None:
        assert nonce_key(Protocol.ACP, "c1", "n") != nonce_key(Protocol.ACP, "c2", "n")
        assert nonce_key(Protocol.ACP, "c", "n") != nonce_key(Protocol.UCP, "c", "n")


@pytest.mark.db
class TestFreshnessWindow:
    def test_a_recent_request_is_accepted(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        assert_fresh(cp_session, timestamp=database_now(cp_session))

    def test_a_request_older_than_the_window_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        stale = database_now(cp_session) - DEFAULT_MAX_REQUEST_AGE - timedelta(seconds=1)
        with pytest.raises(ReplayRejected) as caught:
            assert_fresh(cp_session, timestamp=stale)
        assert caught.value.reason == "request_outside_freshness_window"

    def test_a_request_dated_in_the_future_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Not pedantry: a future-dated request outlives any bounded nonce store."""
        ahead = database_now(cp_session) + timedelta(hours=1)
        with pytest.raises(ReplayRejected) as caught:
            assert_fresh(cp_session, timestamp=ahead)
        assert caught.value.reason == "request_timestamp_in_the_future"

    def test_a_naive_timestamp_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        from datetime import datetime

        with pytest.raises(ReplayRejected) as caught:
            assert_fresh(cp_session, timestamp=datetime(2026, 9, 5, 12, 0, 0))  # noqa: DTZ001
        assert caught.value.reason == "request_timestamp_is_naive"

    def test_a_protocol_may_tighten_the_window_but_never_widen_it(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Specification 16.3's ceiling. A per-protocol override is not an opt-out."""
        assert_fresh(cp_session, timestamp=database_now(cp_session), max_age=timedelta(seconds=30))
        with pytest.raises(ValueError, match="exceeds the specification 16.3 ceiling"):
            assert_fresh(
                cp_session,
                timestamp=database_now(cp_session),
                max_age=DEFAULT_MAX_REQUEST_AGE + timedelta(seconds=1),
            )


# ------------------------------------------------- secrets never enter the chain


class TestABodyNeverCarriesASecretIntoTheChain:
    """The chain cannot be edited, so a secret that gets in is in for good.

    An ACP completion body may carry a payment instrument. Nothing in this platform reads
    one -- `_complete` takes `content_hash` and nothing else, and payment happens at the
    provider's own gateway (specification 2.4) -- but the RECEIVED row is written before
    any body check, so a refused body was stored as faithfully as an accepted one. It then
    came back in cleartext from the Protocol Inspector to any session in the tenant, and
    because each row's hash covers its predecessor it could not be redacted afterwards
    without breaking the stream from that point to the head.

    These tests are about what `screen_secrets` leaves behind: enough to prove an
    instrument arrived, and none of the instrument.
    """

    PAN = "4111111111111111"
    CVC = "737"

    def _body(self) -> dict[str, object]:
        return {
            "content_hash": "Eovyh1PTM9",
            "checkout_version": 1,
            "total": {"amount_minor": 5750, "currency": "INR"},
            "payment": {
                "type": "card",
                "number": self.PAN,
                "cvc": self.CVC,
                "token": "tok_secret_from_the_external_platform",
            },
        }

    def test_no_part_of_a_card_survives_screening(self) -> None:
        """Not the number, not the CVC, not the token -- and not a prefix of any of them.

        The prefix matters and is why this does not use `fingerprint`: that helper keeps
        `MAX_FINGERPRINT_PREVIEW` characters in cleartext beside the digest, which is right
        for a signature and wrong for a card. Eight characters of a sixteen-digit PAN is
        half the PAN.
        """
        rendered = json.dumps(evidence.screen_secrets(self._body()))

        assert self.PAN not in rendered
        assert self.CVC not in rendered
        assert "tok_secret_from_the_external_platform" not in rendered
        for length in range(4, len(self.PAN)):
            assert self.PAN[:length] not in rendered, (
                f"the first {length} digits of the card survived screening"
            )

    def test_the_ask_is_still_legible_and_the_instrument_still_provable(self) -> None:
        """A screen that lost the shape of the request would defeat the evidence rule.

        Everything that is not a secret is untouched, so a reviewer can still read what was
        asked for. The instrument is replaced by a digest and a length, which is enough to
        say an instrument arrived, to correlate two rows that carried the same one, and to
        catch truncation -- and not enough to reconstruct it.
        """
        screened = evidence.screen_secrets(self._body())

        assert screened["content_hash"] == "Eovyh1PTM9"
        assert screened["checkout_version"] == 1
        assert screened["total"] == {"amount_minor": 5750, "currency": "INR"}

        payment = screened["payment"]
        assert payment["withheld"] is True
        assert payment["algorithm"] == "SHA-256"
        assert payment["length"] > 0
        assert payment["digest"]
        assert "preview" not in payment, "a preview is what leaks the first half of a card"

    def test_the_same_instrument_twice_is_recognisable_as_the_same(self) -> None:
        """Correlation is the one thing the digest has to keep, and a different card must differ."""
        first = evidence.screen_secrets(self._body())["payment"]["digest"]
        again = evidence.screen_secrets(self._body())["payment"]["digest"]
        other = dict(self._body())
        other["payment"] = {**other["payment"], "number": "4242424242424242"}  # type: ignore[dict-item]
        different = evidence.screen_secrets(other)["payment"]["digest"]

        assert first == again
        assert first != different

    def test_a_secret_nested_anywhere_is_still_caught(self) -> None:
        """Depth is not a hiding place, and neither is a list.

        A screen that only looked at the top level would be defeated by one more level of
        object, which is exactly the shape a protocol change tends to take.
        """
        rendered = json.dumps(
            evidence.screen_secrets(
                {
                    "outer": {"inner": {"cvc": self.CVC}},
                    "attempts": [{"card_number": self.PAN}, {"note": "fine"}],
                }
            )
        )

        assert self.CVC not in rendered
        assert self.PAN not in rendered
        assert "fine" in rendered, "screening removed something that was not a secret"

    def test_screening_is_applied_where_every_surface_passes_through(self) -> None:
        """ACP and MCP both record through `record_received`, so the screen lives there.

        Asserted at the call site rather than per surface: a protocol added later is
        covered by having been written, not by somebody remembering to screen it.
        """
        source = Path(evidence.__file__).read_text(encoding="utf-8")
        received = source.split("def record_received", 1)[1]
        body_arg = received.split("self.record(", 1)[1]
        assert "request=screen_secrets(" in body_arg, (
            "record_received no longer screens the body it stores; every protocol surface "
            "records through here, so this is the one place that has to do it"
        )

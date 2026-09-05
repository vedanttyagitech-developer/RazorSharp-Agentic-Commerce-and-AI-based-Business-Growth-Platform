"""UCP 2026-08-25, specification 14 and 29.5.

The assertion this file exists for is specification 29.5's last line:

    UCP Standard Checkout handoff uses ``requires_escalation + continue_url``;
    ``complete_in_progress`` is accepted only for valid asynchronous/Action cases.

``TestCompleteCallSemantics`` is that sentence, exhaustively. ``TestEscalationMessage``
covers the trap specification 14.3 calls out by name -- ``requires_buyer_review`` is the
message severity, not its type and not its code. ``TestContinuation`` proves the three
properties the handoff URL has to have, and proves them separately, because they come from
three different mechanisms and any one of them failing silently would leave a live URL in
the world.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from ap2.sdk.generated.types.checkout import Status
from ap2.sdk.generated.types.message_error import Severity
from commerce_domain import Money, uuid7
from commerce_protocols.ap2.signing import InProcessSigner, KeyRing
from commerce_protocols.core.errors import (
    MandateRejected,
    ReplayRejected,
    SchemaRejected,
    StateRejected,
)
from commerce_protocols.core.identity import AuthenticatedCaller
from commerce_protocols.core.intent import IntentKind
from commerce_protocols.core.pins import Protocol, UnsupportedVersionError
from commerce_protocols.core.replay import database_now
from commerce_protocols.ucp import (
    CODE_NO_HEADLESS_PAYMENT_PATH,
    MAX_CONTINUATION_TTL,
    SEVERITIES,
    AsyncGround,
    BusinessProfile,
    Completed,
    CompletionContext,
    Escalation,
    Incomplete,
    InProgress,
    consume_continuation,
    decide_completion,
    escalate_for_razorpay_handoff,
    escalation_message,
    intent_for,
    issue_continuation,
    line_items_from,
    message_payload,
    outcome_payload,
    profile_document,
    recoverable_message,
    total_of,
)
from commerce_protocols.ucp.lifecycle import UCP_INTENT_BY_OPERATION
from jwcrypto.jwk import JWK
from sqlalchemy.orm import Session

UCP_VERSION = "2026-08-25"
CONTINUE_URL = "https://demo.invalid/checkouts/abc/continue?continuation=x"


def _checkout(total_minor: int = 45990) -> dict[str, Any]:
    return {
        "id": "chk_ucp_1",
        "currency": "INR",
        "status": "ready_for_complete",
        "line_items": [
            {
                "id": "li_1",
                "item": {"id": "TEA-BEV-001", "title": "Masala chai", "price": 19900},
                "quantity": 2,
                "totals": [{"type": "subtotal", "amount": 39800}],
            },
            {
                "id": "li_2",
                "item": {"id": "SUG-GRO-014", "title": "Sugar", "price": 6500},
                "quantity": 1,
                "totals": [{"type": "subtotal", "amount": 6500}],
            },
        ],
        "totals": [{"type": "subtotal", "amount": 46300}, {"type": "total", "amount": total_minor}],
        "links": [],
    }


def _caller(tenant_id: uuid.UUID, merchant_id: uuid.UUID) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        protocol=Protocol.UCP,
        client_id="platform-under-test",
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        authenticated_by="test",
        correlation_id=uuid7(),
    )


def _context(**overrides: Any) -> CompletionContext:
    base: dict[str, Any] = {
        "checkout_id": uuid7(),
        "version": 1,
        "negotiated_payment_action": False,
        "asynchronous_work_in_flight": False,
    }
    base.update(overrides)
    return CompletionContext(**base)


# ---- specification 29.5's headline assertion


class TestCompleteCallSemantics:
    def test_the_razorpay_path_escalates_and_never_claims_progress(self) -> None:
        """The default for this deployment, and it must be the default.

        No negotiated Action, no asynchronous work, no capture: Razorpay test mode has no
        headless charge path, so the only honest answer is to get a human.
        """
        outcome = decide_completion(_context(), continue_url=CONTINUE_URL)

        assert isinstance(outcome, Escalation)
        assert outcome.status is Status.requires_escalation
        assert outcome.status is not Status.complete_in_progress
        assert outcome.continue_url == CONTINUE_URL

    def test_complete_in_progress_requires_a_negotiated_action_or_real_async_work(self) -> None:
        """Specification 14.3's two legitimate grounds, and only those two."""
        negotiated = decide_completion(
            _context(negotiated_payment_action=True), continue_url=CONTINUE_URL
        )
        assert isinstance(negotiated, InProgress)
        assert negotiated.ground is AsyncGround.NEGOTIATED_ACTION

        asynchronous = decide_completion(
            _context(asynchronous_work_in_flight=True), continue_url=CONTINUE_URL
        )
        assert isinstance(asynchronous, InProgress)
        assert asynchronous.ground is AsyncGround.ASYNCHRONOUS_PROCESSING

    def test_in_progress_cannot_be_constructed_without_naming_its_ground(self) -> None:
        """The status is misused by people who have not decided which case they are in.

        Requiring the ground makes that decision explicit at every construction site rather
        than defaulting to whichever reading is convenient.
        """
        with pytest.raises(TypeError):
            InProgress()  # type: ignore[call-arg]

    def test_completed_requires_a_captured_order(self) -> None:
        """Specification 14.3 step 9: completed only after verified capture."""
        outcome = decide_completion(_context(captured_order_id="ord_9"), continue_url=CONTINUE_URL)
        assert isinstance(outcome, Completed)
        assert outcome.order_id == "ord_9"

    def test_capture_wins_over_every_other_condition(self) -> None:
        """A captured payment is terminal truth; nothing downgrades it to an escalation."""
        outcome = decide_completion(
            _context(
                captured_order_id="ord_9",
                negotiated_payment_action=True,
                asynchronous_work_in_flight=True,
                blocking_messages=(recoverable_message(code="x_stale", content="stale"),),
            ),
            continue_url=CONTINUE_URL,
        )
        assert isinstance(outcome, Completed)

    def test_a_blocking_condition_is_incomplete_not_an_escalation(self) -> None:
        """A caller who can fix it themselves must not be sent to find a human."""
        outcome = decide_completion(
            _context(
                blocking_messages=(
                    recoverable_message(code="checkout_version_superseded", content="re-read"),
                )
            ),
            continue_url=CONTINUE_URL,
        )
        assert isinstance(outcome, Incomplete)
        assert outcome.status is Status.incomplete

    def test_an_escalation_without_a_continue_url_cannot_be_built(self) -> None:
        """Telling a client to get a human without saying where is not an escalation."""
        with pytest.raises(SchemaRejected) as caught:
            Escalation(continue_url="", messages=(escalation_message(code="c", content="x"),))
        assert caught.value.reason == "escalation_without_continue_url"

    def test_an_escalation_without_a_message_cannot_be_built(self) -> None:
        with pytest.raises(SchemaRejected) as caught:
            Escalation(continue_url=CONTINUE_URL, messages=())
        assert caught.value.reason == "escalation_without_a_structured_message"

    def test_the_response_body_carries_the_status_of_the_outcome_it_was_built_from(self) -> None:
        """A body cannot claim one status while carrying another's fields."""
        body = outcome_payload(escalate_for_razorpay_handoff(CONTINUE_URL))
        assert body["status"] == "requires_escalation"
        assert body["continue_url"] == CONTINUE_URL
        assert len(body["messages"]) == 1

        completed = outcome_payload(Completed(order_id="ord_1"))
        assert completed["status"] == "completed"
        assert "continue_url" not in completed


# ---- the requires_buyer_review trap


class TestEscalationMessage:
    def test_requires_buyer_review_is_the_severity_and_not_the_type_or_code(self) -> None:
        """Specification 14.3 spells this out, which means people get it wrong.

        Three fields, three different statements. The type says what kind of message this
        is, the severity says what to do, and the code says why.
        """
        escalation = escalate_for_razorpay_handoff(CONTINUE_URL)
        message = escalation.messages[0]

        assert message.severity is Severity.requires_buyer_review
        assert message.type == "error"
        assert message.code == CODE_NO_HEADLESS_PAYMENT_PATH
        assert message.code != "requires_buyer_review"
        assert message.type != "requires_buyer_review"

    def test_a_severity_used_as_a_code_is_refused(self) -> None:
        """The exact malformed message the specification warns against."""
        for severity in SEVERITIES:
            with pytest.raises(ValueError, match="severity, not an application code"):
                escalation_message(code=severity, content="x")

    def test_the_four_severities_come_from_the_pinned_schema(self) -> None:
        """Imported rather than redeclared, so a schema change is an import error."""
        assert {
            "recoverable",
            "requires_buyer_input",
            "requires_buyer_review",
            "unrecoverable",
        } == SEVERITIES

    def test_a_recoverable_condition_does_not_use_the_escalation_severity(self) -> None:
        """Sending a buyer to a trusted surface for something their agent could fix is a bug."""
        message = recoverable_message(code="checkout_version_superseded", content="re-read it")
        assert message.severity is Severity.recoverable

    def test_content_type_is_normalised_to_the_string_form(self) -> None:
        """The generated model's default is a raw string while a parsed value is an enum.

        Without normalising, a round trip through JSON changes the serialised shape of a
        message nobody edited.
        """
        payload = message_payload(escalation_message(code="c_x", content="x"))
        assert payload["content_type"] == "plain"
        assert isinstance(payload["content_type"], str)

    def test_the_message_names_the_json_path_it_is_about(self) -> None:
        payload = message_payload(escalate_for_razorpay_handoff(CONTINUE_URL).messages[0])
        assert payload["path"] == "$.status"


# ---- the trusted continuation


@pytest.mark.db
class TestContinuation:
    def test_a_freshly_issued_continuation_is_accepted_exactly_once(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        cp_platform_key: JWK,
    ) -> None:
        """Single use is the property a signature cannot provide.

        Verifying a token changes nothing, so replay has to be stopped by a write that can
        only succeed once. The second presentation of a perfectly valid token is refused.
        """
        tenant_id, _ = cp_tenant
        signer = InProcessSigner.from_jwk(cp_platform_key)
        ring = KeyRing.of(signer)
        checkout_id = uuid7()

        url, claims = issue_continuation(
            tenant_id=tenant_id,
            checkout_id=checkout_id,
            version=3,
            content_hash="hash-3",
            external_checkout_id="chk_ucp_1",
            signer=signer,
            base_url="https://demo.invalid",
            now=database_now(cp_session),
        )
        token = url.split("continuation=")[1]

        accepted = consume_continuation(
            cp_session,
            token,
            ring,
            tenant_id=tenant_id,
            current_version=3,
            current_content_hash="hash-3",
        )
        assert accepted.nonce == claims.nonce
        assert accepted.version == 3

        with pytest.raises(ReplayRejected) as caught:
            consume_continuation(
                cp_session,
                token,
                ring,
                tenant_id=tenant_id,
                current_version=3,
                current_content_hash="hash-3",
            )
        assert caught.value.reason == "nonce_already_presented"

    def test_a_continuation_for_a_superseded_version_is_refused(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        cp_platform_key: JWK,
    ) -> None:
        """Specification 14.3 step 4: the buyer reviews the *same* version and hash.

        Without this the buyer would be asked to consent to a total the escalation was not
        issued for, which is the exact hole the version/hash discipline exists to close.
        """
        tenant_id, _ = cp_tenant
        signer = InProcessSigner.from_jwk(cp_platform_key)
        ring = KeyRing.of(signer)

        url, _ = issue_continuation(
            tenant_id=tenant_id,
            checkout_id=uuid7(),
            version=1,
            content_hash="hash-1",
            external_checkout_id="chk_ucp_1",
            signer=signer,
            base_url="https://demo.invalid",
            now=database_now(cp_session),
        )
        token = url.split("continuation=")[1]

        with pytest.raises(StateRejected) as caught:
            consume_continuation(
                cp_session,
                token,
                ring,
                tenant_id=tenant_id,
                current_version=2,
                current_content_hash="hash-2",
            )
        assert caught.value.reason == "continuation_describes_a_superseded_version"

    def test_a_continuation_minted_for_another_tenant_is_refused_before_it_is_spent(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        cp_platform_key: JWK,
    ) -> None:
        """A cross-tenant token must not be able to burn a nonce in this tenant's records."""
        tenant_id, _ = cp_tenant
        signer = InProcessSigner.from_jwk(cp_platform_key)
        ring = KeyRing.of(signer)

        url, claims = issue_continuation(
            tenant_id=uuid.uuid4(),
            checkout_id=uuid7(),
            version=1,
            content_hash="hash-1",
            external_checkout_id="chk_ucp_1",
            signer=signer,
            base_url="https://demo.invalid",
            now=database_now(cp_session),
        )
        token = url.split("continuation=")[1]

        with pytest.raises(StateRejected) as caught:
            consume_continuation(
                cp_session,
                token,
                ring,
                tenant_id=tenant_id,
                current_version=1,
                current_content_hash="hash-1",
            )
        assert caught.value.reason == "continuation_belongs_to_another_tenant"

        # The nonce was never claimed, so a legitimate token carrying it would still work.
        from commerce_protocols.core.replay import claim_nonce

        claim_nonce(
            cp_session,
            protocol=Protocol.UCP,
            client_id="continuation",
            nonce=claims.nonce,
            request_digest="hash-1",
        )

    def test_an_expired_continuation_is_refused(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        cp_platform_key: JWK,
    ) -> None:
        tenant_id, _ = cp_tenant
        signer = InProcessSigner.from_jwk(cp_platform_key)
        ring = KeyRing.of(signer)

        url, _ = issue_continuation(
            tenant_id=tenant_id,
            checkout_id=uuid7(),
            version=1,
            content_hash="hash-1",
            external_checkout_id="chk_ucp_1",
            signer=signer,
            base_url="https://demo.invalid",
            now=database_now(cp_session) - timedelta(hours=1),
            ttl=timedelta(minutes=10),
        )
        token = url.split("continuation=")[1]

        with pytest.raises(MandateRejected) as caught:
            consume_continuation(
                cp_session,
                token,
                ring,
                tenant_id=tenant_id,
                current_version=1,
                current_content_hash="hash-1",
            )
        assert caught.value.reason == "continuation_expired"

    def test_a_forged_continuation_is_refused(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        cp_platform_key: JWK,
        cp_foreign_key: JWK,
    ) -> None:
        """A mintable continue_url would route a buyer to an attacker's chosen checkout."""
        tenant_id, _ = cp_tenant
        attacker = InProcessSigner.from_jwk(cp_foreign_key)
        ring = KeyRing.of(InProcessSigner.from_jwk(cp_platform_key))

        url, _ = issue_continuation(
            tenant_id=tenant_id,
            checkout_id=uuid7(),
            version=1,
            content_hash="hash-1",
            external_checkout_id="chk_ucp_1",
            signer=attacker,
            base_url="https://demo.invalid",
            now=database_now(cp_session),
        )
        token = url.split("continuation=")[1]

        from commerce_protocols.core.errors import SignatureRejected

        with pytest.raises(SignatureRejected):
            consume_continuation(
                cp_session,
                token,
                ring,
                tenant_id=tenant_id,
                current_version=1,
                current_content_hash="hash-1",
            )


class TestContinuationTtl:
    def test_a_ttl_above_the_ceiling_is_refused(self, cp_platform_key: JWK) -> None:
        """A configurable TTL with no ceiling is a configurable security property."""
        from datetime import UTC, datetime

        signer = InProcessSigner.from_jwk(cp_platform_key)
        with pytest.raises(ValueError, match="exceeds the"):
            issue_continuation(
                tenant_id=uuid.uuid4(),
                checkout_id=uuid7(),
                version=1,
                content_hash="h",
                external_checkout_id="chk",
                signer=signer,
                base_url="https://demo.invalid",
                now=datetime.now(tz=UTC),
                ttl=MAX_CONTINUATION_TTL + timedelta(seconds=1),
            )


# ---- lifecycle mapping


class TestLifecycleMapping:
    def test_every_ucp_operation_maps_to_an_intent_that_cannot_consent(self) -> None:
        """No UCP operation may become an approval, a payment or an executed refund."""
        forbidden = {IntentKind.REQUEST_APPROVAL}
        mapped = set(UCP_INTENT_BY_OPERATION.values())
        assert not (mapped & forbidden)
        assert IntentKind.PROPOSE_REFUND in mapped
        assert IntentKind.PROPOSE_CANCELLATION in mapped

    def test_the_only_money_moving_intent_is_submitting_something_already_approved(self) -> None:
        moving = {k for k, v in UCP_INTENT_BY_OPERATION.items() if v is IntentKind.SUBMIT_APPROVED}
        assert moving == {"complete_checkout"}

    def test_an_unpinned_version_is_refused(
        self, cp_tenant_ids: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant_ids
        with pytest.raises(UnsupportedVersionError):
            intent_for(
                "get_checkout",
                _caller(tenant_id, merchant_id),
                announced_version="2027-01-01",
                correlation_id=uuid7(),
            )

    def test_an_absent_version_is_refused_rather_than_defaulted(
        self, cp_tenant_ids: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A caller that names no contract has not agreed to one."""
        tenant_id, merchant_id = cp_tenant_ids
        with pytest.raises(UnsupportedVersionError):
            intent_for(
                "get_checkout",
                _caller(tenant_id, merchant_id),
                announced_version=None,
                correlation_id=uuid7(),
            )

    def test_an_unimplemented_operation_is_refused_by_name(
        self, cp_tenant_ids: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Not silently mapped to something adjacent."""
        tenant_id, merchant_id = cp_tenant_ids
        with pytest.raises(SchemaRejected) as caught:
            intent_for(
                "execute_payment",
                _caller(tenant_id, merchant_id),
                announced_version=UCP_VERSION,
                correlation_id=uuid7(),
            )
        assert caught.value.reason == "ucp_operation_not_implemented"

    def test_a_valid_operation_becomes_a_typed_intent(
        self, cp_tenant_ids: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant_ids
        intent = intent_for(
            "complete_checkout",
            _caller(tenant_id, merchant_id),
            announced_version=UCP_VERSION,
            correlation_id=uuid7(),
            amount=Money(45990, "INR"),
        )
        assert intent.kind is IntentKind.SUBMIT_APPROVED
        assert intent.moves_money
        assert intent.pin.version == UCP_VERSION
        assert intent.amount == Money(45990, "INR")


class TestAmountsAreIntegerMinorUnits:
    def test_the_total_is_read_from_the_total_component_not_summed(self) -> None:
        """Summing the components would substitute our arithmetic for the merchant's."""
        assert total_of(_checkout(total_minor=45990)) == Money(45990, "INR")

    @pytest.mark.parametrize("bad", [459.90, "45990", None, True])
    def test_a_non_integer_total_is_refused_rather_than_coerced(self, bad: Any) -> None:
        """``True`` is in the list because bool subclasses int and JSON true becomes True."""
        checkout = _checkout()
        checkout["totals"] = [{"type": "total", "amount": bad}]
        with pytest.raises(SchemaRejected) as caught:
            total_of(checkout)
        assert caught.value.reason == "amount_is_not_an_integer_minor_unit"

    def test_a_checkout_with_no_total_component_is_refused(self) -> None:
        checkout = _checkout()
        checkout["totals"] = [{"type": "subtotal", "amount": 100}]
        with pytest.raises(SchemaRejected) as caught:
            total_of(checkout)
        assert caught.value.reason == "checkout_has_no_total_component"

    def test_line_items_flatten_to_sku_and_quantity(self) -> None:
        assert line_items_from(_checkout()) == {"TEA-BEV-001": 2, "SUG-GRO-014": 1}

    def test_a_duplicate_sku_is_refused_rather_than_summed(self) -> None:
        """Two lines for one SKU is ambiguous, and guessing decides for the buyer."""
        checkout = _checkout()
        checkout["line_items"].append(checkout["line_items"][0])
        with pytest.raises(SchemaRejected) as caught:
            line_items_from(checkout)
        assert caught.value.reason == "line_items_name_the_same_sku_twice"

    def test_a_zero_quantity_line_is_refused(self) -> None:
        checkout = _checkout()
        checkout["line_items"][0]["quantity"] = 0
        with pytest.raises(SchemaRejected) as caught:
            line_items_from(checkout)
        assert caught.value.reason == "line_item_quantity_below_one"


# ---- the published profile


class TestBusinessProfile:
    def test_the_published_profile_never_contains_a_private_key(self, cp_merchant_key: JWK) -> None:
        """Asserted over the serialised bytes. A structural check is not enough here."""
        import json

        signer = InProcessSigner.from_jwk(cp_merchant_key)
        profile = BusinessProfile(
            profile_version=1,
            subject_id="mrc_demo",
            display_name="Demo Kirana",
            website="https://demo.invalid",
            ring=KeyRing.of(signer),
        )
        serialised = json.dumps(profile_document(profile))
        assert '"d"' not in serialised
        assert "PRIVATE" not in serialised.upper()

    def test_the_profile_publishes_the_pinned_version_and_its_disclaimer(
        self, cp_merchant_key: JWK
    ) -> None:
        """Specification 13.2: implementing UCP is not availability inside Gemini."""
        signer = InProcessSigner.from_jwk(cp_merchant_key)
        document = profile_document(
            BusinessProfile(
                profile_version=2,
                subject_id="mrc_demo",
                display_name="Demo Kirana",
                website="https://demo.invalid",
                ring=KeyRing.of(signer),
            )
        )
        assert document["protocol_version"] == UCP_VERSION
        assert document["profile_version"] == 2
        assert "Gemini" in document["conformance"]["disclaimer"]

    def test_the_profile_does_not_advertise_headless_payment_completion(
        self, cp_merchant_key: JWK
    ) -> None:
        """A profile is a promise. Advertising what 14.3 says we cannot do would be a lie."""
        signer = InProcessSigner.from_jwk(cp_merchant_key)
        document = profile_document(
            BusinessProfile(
                profile_version=1,
                subject_id="mrc_demo",
                display_name="Demo Kirana",
                website="https://demo.invalid",
                ring=KeyRing.of(signer),
            )
        )
        capabilities = document["capabilities"]
        assert "checkout.complete.requires_escalation" in capabilities
        assert not any(c == "checkout.complete" for c in capabilities)
        assert not any("payment.execute" in c for c in capabilities)

    def test_a_rotated_key_stays_published_so_old_evidence_still_verifies(
        self, cp_merchant_key: JWK, cp_foreign_key: JWK
    ) -> None:
        """Specification 14.1: rotate without silently invalidating stored evidence."""
        old = InProcessSigner.from_jwk(cp_merchant_key)
        new = InProcessSigner.from_jwk(cp_foreign_key)
        document = profile_document(
            BusinessProfile(
                profile_version=3,
                subject_id="mrc_demo",
                display_name="Demo Kirana",
                website="https://demo.invalid",
                ring=KeyRing.of(old, new),
            )
        )
        published = {key["kid"] for key in document["jwks"]["keys"]}
        assert published == {old.kid, new.kid}

    def test_a_profile_with_no_keys_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no verification keys"):
            BusinessProfile(
                profile_version=1,
                subject_id="mrc_demo",
                display_name="Demo Kirana",
                website="https://demo.invalid",
                ring=KeyRing(keys={}),
            )


@pytest.fixture
def cp_tenant_ids() -> tuple[uuid.UUID, uuid.UUID]:
    """Identifiers only. The mapping tests touch no database."""
    return uuid.uuid4(), uuid7()

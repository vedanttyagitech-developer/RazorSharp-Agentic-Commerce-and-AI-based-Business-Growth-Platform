"""Webhook admission, deduplication and state folding, specification 11.3.

Razorpay documents duplicate delivery as expected and does not guarantee ordering, so
this file treats both as normal traffic. The invariants under test:

* an unverified event cannot claim a deduplication key -- otherwise a forged body could
  suppress the genuine one that follows;
* a replayed event is marked duplicate and changes state at most once;
* ``CAPTURED`` never regresses to ``AUTHORIZED``, whatever order events arrive in;
* ``refund.processed`` is never guessed to be a full refund.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    API_KEY_MATERIAL,
    WEBHOOK_KEY_MATERIAL,
    payment_entity,
    sign_body,
    webhook_body,
)
from payment_adapters.razorpay import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    InMemoryInboxStore,
    RazorpayConfig,
    RazorpayWebhookEvent,
    UnmappableEventError,
    WebhookInbox,
    apply_event,
    dedup_key_for,
    parse_event,
    payment_state_for_event,
)
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.states import PaymentState

EVENT_ID = "evt_29QQoUBi66xm2f"


def delivery(
    body: dict[str, object],
    *,
    event_id: str | None = EVENT_ID,
    secret: str = WEBHOOK_KEY_MATERIAL,
) -> tuple[bytes, dict[str, str]]:
    """Serialize a body once and sign those exact bytes, as the provider would."""
    raw = json.dumps(body).encode("utf-8")
    headers = {SIGNATURE_HEADER: sign_body(raw, secret)}
    if event_id is not None:
        headers[EVENT_ID_HEADER] = event_id
    return raw, headers


class Ledger:
    """A minimal stand-in for the payment attempt row, to count applications."""

    def __init__(self, state: PaymentState = PaymentState.SUBMITTED) -> None:
        self.state = state
        self.applications = 0

    def process(
        self, inbox: WebhookInbox, raw: bytes, headers: dict[str, str], cfg: RazorpayConfig
    ) -> None:
        admission = inbox.admit(raw_body=raw, headers=headers, config=cfg)
        if not admission.accepted or admission.event is None:
            return
        self.applications += 1
        self.state = apply_event(self.state, admission.event)


# ------------------------------------------------------------------ signature gating


def test_an_unverified_event_is_refused_and_stores_nothing(config: RazorpayConfig) -> None:
    store = InMemoryInboxStore()
    inbox = WebhookInbox(store)
    raw, headers = delivery(webhook_body("payment.captured", payment=payment_entity()))
    headers[SIGNATURE_HEADER] = "00" * 32

    admission = inbox.admit(raw_body=raw, headers=headers, config=config)

    assert not admission.accepted
    assert admission.code is RecoveryCode.AUTHORITY_INSUFFICIENT
    assert admission.dedup_key is None
    assert len(store) == 0
    assert not admission.should_acknowledge


def test_a_forged_event_cannot_pre_claim_a_key_and_suppress_the_real_one(
    config: RazorpayConfig,
) -> None:
    """The reason verification must precede the inbox claim.

    The endpoint is public. If an unverified delivery could claim a key, an attacker who
    guesses or observes an event id could take it, and the genuine signed webhook would
    then arrive, be seen as a duplicate, and be discarded -- so a capture would never be
    applied. Verifying first makes the inbox claimable only by the holder of the secret.
    """
    store = InMemoryInboxStore()
    inbox = WebhookInbox(store)
    body = webhook_body("payment.captured", payment=payment_entity())
    raw, headers = delivery(body)

    forged_headers = dict(headers)
    forged_headers[SIGNATURE_HEADER] = sign_body(raw, "attacker-guessed-material")
    assert not inbox.admit(raw_body=raw, headers=forged_headers, config=config).accepted

    genuine = inbox.admit(raw_body=raw, headers=headers, config=config)
    assert genuine.accepted, "the forgery must not have consumed the genuine event's key"
    assert genuine.code is RecoveryCode.OK


def test_a_missing_signature_header_is_refused(config: RazorpayConfig) -> None:
    inbox = WebhookInbox(InMemoryInboxStore())
    raw = b'{"event":"payment.captured"}'
    admission = inbox.admit(raw_body=raw, headers={}, config=config)
    assert admission.code is RecoveryCode.AUTHORITY_INSUFFICIENT


# --------------------------------------------------------------------- deduplication


def test_the_first_delivery_is_accepted_and_the_replay_is_a_duplicate(
    config: RazorpayConfig,
) -> None:
    """Duplicate delivery is expected per Razorpay's own documentation."""
    inbox = WebhookInbox(InMemoryInboxStore())
    raw, headers = delivery(webhook_body("payment.captured", payment=payment_entity()))

    first = inbox.admit(raw_body=raw, headers=headers, config=config)
    second = inbox.admit(raw_body=raw, headers=headers, config=config)

    assert first.accepted and first.code is RecoveryCode.OK
    assert not second.accepted
    assert second.code is RecoveryCode.DUPLICATE_OPERATION
    assert second.is_duplicate
    assert first.dedup_key == second.dedup_key
    # Both are acknowledged quickly; redelivering an event we hold helps nobody.
    assert first.should_acknowledge and second.should_acknowledge


def test_a_replayed_event_changes_state_at_most_once(config: RazorpayConfig) -> None:
    """The invariant the inbox exists for.

    Five deliveries of one capture must move the attempt exactly once. If the dedup key
    or the claim were removed, ``applications`` would be five.
    """
    inbox = WebhookInbox(InMemoryInboxStore())
    ledger = Ledger(PaymentState.SUBMITTED)
    raw, headers = delivery(webhook_body("payment.captured", payment=payment_entity()))

    for _ in range(5):
        ledger.process(inbox, raw, headers, config)

    assert ledger.applications == 1
    assert ledger.state is PaymentState.CAPTURED


def test_a_replay_after_a_restart_still_deduplicates_on_the_stored_key(
    config: RazorpayConfig,
) -> None:
    """A new inbox over the same store still refuses the replay: state lives in the store."""
    store = InMemoryInboxStore()
    raw, headers = delivery(webhook_body("payment.captured", payment=payment_entity()))

    assert WebhookInbox(store).admit(raw_body=raw, headers=headers, config=config).accepted
    assert not WebhookInbox(store).admit(raw_body=raw, headers=headers, config=config).accepted


def test_the_inbox_record_is_stored_before_any_processing(config: RazorpayConfig) -> None:
    """Specification 11.3: durable receipt first, business processing afterwards."""
    store = InMemoryInboxStore()
    inbox = WebhookInbox(store)
    raw, headers = delivery(webhook_body("payment.captured", payment=payment_entity()))

    admission = inbox.admit(raw_body=raw, headers=headers, config=config)
    assert admission.dedup_key is not None
    record = store.get(admission.dedup_key)

    assert record is not None
    assert record.event_type == "payment.captured"
    assert record.provider_event_id == EVENT_ID
    assert record.payment_id == "pay_29QQoUBi66xm2f"
    assert record.order_id == "order_9A33XWu170gUtm"
    assert record.body_digest


# ------------------------------------------------------------------ dedup key tiers


def test_the_header_is_the_primary_key() -> None:
    key = dedup_key_for({EVENT_ID_HEADER: EVENT_ID}, b'{"event":"payment.captured"}')
    assert key == f"evt:{EVENT_ID}"


def test_the_header_lookup_is_case_insensitive() -> None:
    """Frameworks normalise header casing differently.

    A case-sensitive lookup would use the header on one deployment and fall through to a
    fingerprint on another, so the same event could deduplicate under two keys and be
    applied twice.
    """
    raw = b'{"event":"payment.captured"}'
    assert dedup_key_for({"X-Razorpay-Event-Id": EVENT_ID}, raw) == f"evt:{EVENT_ID}"
    assert dedup_key_for({"X-RAZORPAY-EVENT-ID": EVENT_ID}, raw) == f"evt:{EVENT_ID}"


def test_a_blank_header_falls_through_to_the_fingerprint() -> None:
    """A whitespace-only header is not an identifier and must not become one."""
    raw = b'{"event":"payment.captured"}'
    assert dedup_key_for({EVENT_ID_HEADER: "   "}, raw).startswith("jcs:")


def test_the_fallback_fingerprint_is_deterministic_and_semantic() -> None:
    """Documented fallback: canonical hash of the parsed body.

    Canonicalization rather than raw-byte hashing, so a redelivery that is semantically
    identical but formatted differently still deduplicates.
    """
    first = dedup_key_for({}, b'{"event":"payment.captured","created_at":1767225600}')
    second = dedup_key_for({}, b'{"created_at": 1767225600,  "event": "payment.captured"}')
    assert first == second
    assert first.startswith("jcs:")


def test_different_events_get_different_fingerprints() -> None:
    a = dedup_key_for({}, b'{"event":"payment.captured","created_at":1}')
    b = dedup_key_for({}, b'{"event":"payment.captured","created_at":2}')
    assert a != b


def test_a_non_json_body_still_yields_a_key() -> None:
    """A webhook is never dropped for want of a fingerprint."""
    key = dedup_key_for({}, b"<html>not json</html>")
    assert key.startswith("raw:")


def test_a_body_containing_a_float_degrades_to_the_raw_tier() -> None:
    """The platform's canonicalization profile refuses floats, by design.

    That refusal must not become a 500 that makes Razorpay retry the same event forever,
    so the fingerprint degrades to hashing the bytes instead.
    """
    key = dedup_key_for({}, b'{"event":"payment.captured","tax_rate":0.18}')
    assert key.startswith("raw:")


def test_the_tier_namespaces_cannot_collide() -> None:
    """A crafted event id must not be able to impersonate a fingerprint.

    Without the ``evt:`` prefix, an attacker could send an event whose id is the exact
    fingerprint string of a real event and pre-empt its key.
    """
    raw = b'{"event":"payment.captured"}'
    fingerprint = dedup_key_for({}, raw)
    impersonation = dedup_key_for({EVENT_ID_HEADER: fingerprint}, raw)
    assert impersonation != fingerprint
    assert impersonation == f"evt:{fingerprint}"


# ------------------------------------------------------------------ ordering and state


def test_captured_never_regresses_to_authorized(config: RazorpayConfig) -> None:
    """Specification 11.3, and the reason the two states are kept distinct.

    Razorpay does not guarantee ordering, so an ``authorized`` event arriving after a
    ``captured`` one is routine -- not an error, and not a reason to rewind the money.
    """
    inbox = WebhookInbox(InMemoryInboxStore())
    ledger = Ledger(PaymentState.SUBMITTED)

    captured = delivery(
        webhook_body("payment.captured", payment=payment_entity()), event_id="evt_captured"
    )
    authorized = delivery(
        webhook_body("payment.authorized", payment=payment_entity(status="authorized")),
        event_id="evt_authorized",
    )

    ledger.process(inbox, *captured, config)
    assert ledger.state is PaymentState.CAPTURED

    ledger.process(inbox, *authorized, config)
    assert ledger.state is PaymentState.CAPTURED, "a late authorization must not rewind a capture"


def test_in_order_delivery_reaches_the_same_place_as_out_of_order(
    config: RazorpayConfig,
) -> None:
    """Ordering must not change the destination, only the path."""
    events = [
        ("evt_a", webhook_body("payment.authorized", payment=payment_entity(status="authorized"))),
        ("evt_c", webhook_body("payment.captured", payment=payment_entity())),
    ]

    forward = Ledger(PaymentState.SUBMITTED)
    inbox_a = WebhookInbox(InMemoryInboxStore())
    for event_id, body in events:
        forward.process(inbox_a, *delivery(body, event_id=event_id), config)

    reverse = Ledger(PaymentState.SUBMITTED)
    inbox_b = WebhookInbox(InMemoryInboxStore())
    for event_id, body in reversed(events):
        reverse.process(inbox_b, *delivery(body, event_id=event_id), config)

    assert forward.state is reverse.state is PaymentState.CAPTURED


def test_a_stale_failed_event_does_not_undo_a_capture() -> None:
    event = parse_event(webhook_body("payment.failed", payment=payment_entity(status="failed")))
    assert apply_event(PaymentState.CAPTURED, event) is PaymentState.CAPTURED


def test_an_event_cannot_resolve_an_unknown_attempt() -> None:
    """Specification 10.7: only a verified provider query resolves uncertainty.

    An inbound ``captured`` on an ``UNKNOWN`` attempt moves it to ``RECONCILING``, not to
    ``CAPTURED``. A webhook is not a substitute for looking.
    """
    event = parse_event(webhook_body("payment.captured", payment=payment_entity()))
    assert apply_event(PaymentState.UNKNOWN, event) is PaymentState.RECONCILING


def test_an_unrecognised_event_type_changes_nothing() -> None:
    """New Razorpay events appear over time; guessing at one would move money state."""
    event = parse_event(webhook_body("payment.dispute.created", payment=payment_entity()))
    assert payment_state_for_event(event.event_type) is None
    assert apply_event(PaymentState.CAPTURED, event) is PaymentState.CAPTURED


def test_order_paid_maps_to_captured() -> None:
    event = parse_event(webhook_body("order.paid", payment=payment_entity()))
    assert apply_event(PaymentState.SUBMITTED, event) is PaymentState.CAPTURED


# ------------------------------------------------------------------- refund ambiguity


def test_refund_processed_is_refused_without_the_full_or_partial_fact() -> None:
    """Guessing ``REFUNDED`` strands the remainder the buyer is still owed.

    ``REFUNDED`` is terminal in the payment lifecycle, so a partial refund recorded as a
    full one permanently blocks the rest of the money from ever being returned.
    """
    with pytest.raises(UnmappableEventError, match="refund_covers_full_capture"):
        payment_state_for_event("refund.processed")


def test_a_partial_refund_maps_to_partially_refunded() -> None:
    assert (
        payment_state_for_event("refund.processed", refund_covers_full_capture=False)
        is PaymentState.PARTIALLY_REFUNDED
    )


def test_a_full_refund_maps_to_refunded() -> None:
    assert (
        payment_state_for_event("refund.processed", refund_covers_full_capture=True)
        is PaymentState.REFUNDED
    )


def test_the_full_or_partial_fact_is_read_from_the_payment_entity() -> None:
    """Razorpay ships the payment alongside a refund event; use it rather than guessing."""
    partial = parse_event(
        webhook_body(
            "refund.processed",
            payment=payment_entity(amount=39500, amount_refunded=10000),
            refund={
                "id": "rfnd_1",
                "payment_id": "pay_29QQoUBi66xm2f",
                "amount": 10000,
                "currency": "INR",
                "status": "processed",
            },
        )
    )
    assert partial.refund_covers_full_capture is False
    assert apply_event(PaymentState.REFUND_PENDING, partial) is PaymentState.PARTIALLY_REFUNDED

    full = parse_event(
        webhook_body(
            "refund.processed",
            payment=payment_entity(amount=39500, amount_refunded=39500),
            refund={
                "id": "rfnd_2",
                "payment_id": "pay_29QQoUBi66xm2f",
                "amount": 39500,
                "currency": "INR",
                "status": "processed",
            },
        )
    )
    assert full.refund_covers_full_capture is True
    assert apply_event(PaymentState.REFUND_PENDING, full) is PaymentState.REFUNDED


def test_an_explicit_argument_overrides_a_stale_payload() -> None:
    """The local ledger is authoritative when it disagrees with the delivered entity."""
    event = parse_event(
        webhook_body(
            "refund.processed",
            payment=payment_entity(amount=39500, amount_refunded=39500),
            refund={
                "id": "rfnd_3",
                "payment_id": "pay_x",
                "amount": 100,
                "currency": "INR",
                "status": "processed",
            },
        )
    )
    applied = apply_event(PaymentState.REFUND_PENDING, event, refund_covers_full_capture=False)
    assert applied is PaymentState.PARTIALLY_REFUNDED


def test_a_refund_event_without_a_payment_entity_still_refuses_to_guess() -> None:
    event = parse_event(
        webhook_body(
            "refund.processed",
            refund={
                "id": "rfnd_4",
                "payment_id": "pay_x",
                "amount": 100,
                "currency": "INR",
                "status": "processed",
            },
        )
    )
    assert event.refund_covers_full_capture is None
    with pytest.raises(UnmappableEventError):
        apply_event(PaymentState.REFUND_PENDING, event)


def test_a_refund_unknown_attempt_is_not_resolved_by_an_event() -> None:
    """Specification 10.6: a refund whose outcome is unknown reconciles, never retries.

    An inbound ``refund.processed`` must not let the platform conclude the refund landed;
    it moves to ``RECONCILING`` so the provider's own refund list is consulted first.
    """
    event = parse_event(
        webhook_body(
            "refund.processed",
            payment=payment_entity(amount=39500, amount_refunded=39500),
            refund={
                "id": "rfnd_5",
                "payment_id": "pay_x",
                "amount": 39500,
                "currency": "INR",
                "status": "processed",
            },
        )
    )
    assert apply_event(PaymentState.REFUND_UNKNOWN, event) is PaymentState.RECONCILING


# ------------------------------------------------------------------------- parsing


def test_parsing_never_raises_on_a_malformed_payload() -> None:
    """Evidence that something arrived is worth more than a clean data model."""
    for body in (
        {},
        {"event": 42},
        {"event": "payment.captured", "payload": "not-a-dict"},
        {"event": "payment.captured", "payload": {"payment": {"entity": None}}},
        {"event": "payment.captured", "payload": {"payment": {"entity": {"amount": "lots"}}}},
    ):
        event = parse_event(body)
        assert isinstance(event, RazorpayWebhookEvent)


def test_an_unsupported_currency_is_data_not_a_crash() -> None:
    event = parse_event(webhook_body("payment.captured", payment=payment_entity(currency="XYZ")))
    assert event.payment_amount is None
    assert event.event_type == "payment.captured"


def test_a_float_amount_is_refused_rather_than_rounded() -> None:
    """Razorpay sends integer minor units; a float means a mangled payload."""
    body = webhook_body("payment.captured", payment=payment_entity())
    body["payload"]["payment"]["entity"]["amount"] = 395.0  # type: ignore[index]
    assert parse_event(body).payment_amount is None


def test_an_unparseable_body_is_still_recorded(config: RazorpayConfig) -> None:
    """A signed delivery we cannot read is still evidence, and still deduplicates."""
    store = InMemoryInboxStore()
    inbox = WebhookInbox(store)
    raw = b"<html>gateway error</html>"
    headers = {SIGNATURE_HEADER: sign_body(raw, WEBHOOK_KEY_MATERIAL)}

    first = inbox.admit(raw_body=raw, headers=headers, config=config)
    second = inbox.admit(raw_body=raw, headers=headers, config=config)

    assert first.accepted
    assert first.event is not None and first.event.event_type == ""
    assert second.is_duplicate
    assert len(store) == 1


def test_the_api_secret_does_not_verify_a_webhook(config: RazorpayConfig) -> None:
    """Specification 11.5, exercised through the inbox rather than the primitive."""
    inbox = WebhookInbox(InMemoryInboxStore())
    body = webhook_body("payment.captured", payment=payment_entity())
    raw, headers = delivery(body, secret=API_KEY_MATERIAL)
    assert not inbox.admit(raw_body=raw, headers=headers, config=config).accepted


# --------------------------------------------------------------------- body types


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview])
def test_every_buffer_type_the_verifier_accepts_is_admissible(
    config: RazorpayConfig, wrap: object
) -> None:
    """ASGI and WSGI stacks hand back bytes, bytearray and memoryview interchangeably.

    ``verify_webhook_signature`` accepts all three, so ``admit`` must too. A ``TypeError``
    here would be an uncaught 500 on an endpoint that had already verified the delivery,
    and Razorpay would retry the same event forever.
    """
    inbox = WebhookInbox(InMemoryInboxStore())
    raw, headers = delivery(webhook_body("payment.captured", payment=payment_entity()))

    admission = inbox.admit(raw_body=wrap(raw), headers=headers, config=config)  # type: ignore[operator]

    assert admission.accepted
    assert admission.event is not None
    assert admission.event.event_type == "payment.captured"


def test_the_same_body_deduplicates_across_buffer_types(config: RazorpayConfig) -> None:
    """The digest and the dedup key must not depend on how the framework wrapped it."""
    inbox = WebhookInbox(InMemoryInboxStore())
    raw, headers = delivery(
        webhook_body("payment.captured", payment=payment_entity()), event_id=None
    )

    first = inbox.admit(raw_body=raw, headers=headers, config=config)
    second = inbox.admit(raw_body=memoryview(raw), headers=headers, config=config)

    assert first.accepted
    assert second.is_duplicate
    assert first.dedup_key == second.dedup_key

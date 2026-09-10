import pytest
from voice_runtime.consent import CardUnavailableError
from voice_runtime.gateway.agent_client import checkout_guidance
from voice_runtime.wire.frames import parse_client_frame


def test_client_success_claim_cannot_invent_payment():
    text, amounts = checkout_guidance({"state": "PAYMENT_UNKNOWN"}, "success")
    assert "not confirmed" in text
    assert not amounts


def test_bill_amount_is_read_from_server():
    text, amounts = checkout_guidance(
        {"state": "APPROVAL_REQUIRED", "approval_card": {"amount_minor": 5750, "currency": "INR"}},
        "review",
    )
    assert "57.50 rupees" in text
    assert amounts == {5750}


def test_confirmed_order_overrides_client_stage():
    text, _ = checkout_guidance({"state": "COMPLETED", "order_id": "actual"}, "review")
    assert "order is placed" in text


@pytest.mark.parametrize("amount", [True, "5750", -1])
def test_invalid_bill_is_not_spoken(amount):
    with pytest.raises(CardUnavailableError):
        checkout_guidance(
            {
                "state": "APPROVAL_REQUIRED",
                "approval_card": {"amount_minor": amount, "currency": "INR"},
            },
            "review",
        )


def test_guidance_frame_cannot_supply_amount_or_outcome():
    with pytest.raises(ValueError):
        parse_client_frame('{"type":"checkout_guidance","amount_minor":1}')


@pytest.mark.asyncio
async def test_guidance_speaks_without_opening_consent_and_routes_back_to_shopping():
    import asyncio

    from test_voice_pipeline import wait_until
    from voice_runtime.clock import FakeClock
    from voice_runtime.pipeline import VoicePipeline
    from voice_runtime.testing import MemoryTransport, an_identity
    from voice_runtime.tts.synth import FakeSynthesizer
    from voice_runtime.turn import FakeTurnHandler

    class Reader:
        async def read_guidance(self, checkout_id, stage, version=None, locale="en-IN"):
            return "Your bill is ready. Choose your payment method.", frozenset()

    transport = MemoryTransport()
    handler = FakeTurnHandler()
    pipeline = VoicePipeline(
        transport=transport,
        stt_factory=None,
        synthesizer=FakeSynthesizer(),
        turn_handler=handler,
        identity=an_identity(),
        clock=FakeClock(),
        card_reader=Reader(),
    )
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text(
            {"type": "checkout_guidance", "checkout_id": "00000000-0000-0000-0000-000000000001"}
        )
        await wait_until(lambda: bool(transport.frames("speech_end")))
        transport.push_text({"type": "text_input", "text": "pay with Reserve Pay"})
        await wait_until(lambda: bool(transport.frames("transcript_final")))
        assert not handler.calls
        assert not transport.frames("consent_recognised")
        transport.push_text({"type": "checkout_guidance", "checkout_id": None})
        transport.push_text({"type": "text_input", "text": "Show me bread"})
        await wait_until(lambda: bool(handler.calls))
        assert len(handler.calls) == 1
    finally:
        transport.end()
        await task


def test_changed_version_requires_a_new_review_before_spoken_amount():
    message, amounts = checkout_guidance(
        {
            "state": "APPROVAL_REQUIRED",
            "approval_card": {"amount_minor": 9999, "currency": "INR", "version": 2},
        },
        "review",
        1,
    )
    assert "bill has changed" in message
    assert not amounts


@pytest.mark.parametrize("stage", ["review", "reserve-review"])
def test_hindi_bill_uses_exact_server_amount(stage):
    text, amounts = checkout_guidance(
        {
            "state": "APPROVAL_REQUIRED",
            "approval_card": {"version": 1, "amount_minor": 5750, "currency": "INR"},
        },
        stage,
        1,
        "hi-IN",
    )
    assert "57.50 रुपये" in text
    assert amounts == {5750}
    assert "सिमुलेशन" in text


@pytest.mark.parametrize(
    "state,stage,phrase",
    [
        ("PAYMENT_UNKNOWN", "success", "अभी पुष्टि नहीं"),
        ("EXPIRED", "review", "पूरा नहीं"),
        ("AWAITING_PAYMENT", "manual", "ओटीपी न बताएं"),
    ],
)
def test_hindi_guidance_preserves_financial_truth(state, stage, phrase):
    text, amounts = checkout_guidance({"state": state}, stage, locale="hi-IN")
    assert phrase in text
    assert not amounts


def test_hindi_changed_bill_never_reads_old_amount():
    text, amounts = checkout_guidance(
        {
            "state": "APPROVAL_REQUIRED",
            "approval_card": {"version": 2, "amount_minor": 9999, "currency": "INR"},
        },
        "review",
        1,
        "hi-IN",
    )
    assert "बिल बदल गया" in text
    assert "99.99" not in text
    assert not amounts


@pytest.mark.parametrize("locale", ["en-IN", "hi-IN"])
def test_bill_details_include_tax_and_only_server_values(locale):
    payload = {
        "state": "APPROVAL_REQUIRED",
        "approval_card": {
            "amount_minor": 5750,
            "currency": "INR",
            "version": 1,
            "quote": {
                "items_subtotal_minor": 5000,
                "items_tax_minor": 250,
                "delivery_fee_minor": 600,
                "delivery_tax_minor": 0,
                "discount_minor": 100,
            },
        },
    }
    message, amounts = checkout_guidance(payload, "bill-details", 1, locale)
    assert "57.50" in message and "2.50" in message
    assert amounts == {5750, 5000, 250, 600, 100}
    payload["approval_card"]["quote"]["items_tax_minor"] = 0
    with pytest.raises(CardUnavailableError):
        checkout_guidance(payload, "bill-details", 1, locale)


def test_bill_details_frame_cannot_accept_client_price():
    frame = parse_client_frame('{"type":"checkout_guidance","stage":"bill-details"}')
    assert frame.stage == "bill-details"
    with pytest.raises(ValueError):
        parse_client_frame('{"type":"checkout_guidance","stage":"bill-details","tax":0}')

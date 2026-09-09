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
        async def read_guidance(self, checkout_id, stage, version=None):
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

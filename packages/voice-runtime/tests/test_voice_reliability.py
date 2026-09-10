import asyncio

import pytest
from voice_runtime.playback import PlaybackLedger
from voice_runtime.tts.fallback import FallbackSynthesizer
from voice_runtime.tts.guard import SpeechGuard
from voice_runtime.tts.synth import VoiceSpec
from voice_runtime.tts.templates import Locale


@pytest.mark.parametrize(
    "text",
    [
        "Shall we review checkout?",
        "क्या आपको कुछ और चाहिए या checkout की तरफ चलें?",
        "Aur kuch chahiye ya checkout karein?",
    ],
)
def test_review_invitation_is_not_a_financial_outcome(text):
    assert not SpeechGuard().check(text, deterministic=False).refused


@pytest.mark.parametrize(
    "text",
    [
        "Shall we review checkout and payment is confirmed?",
        "checkout karein payment successful",
        "Your payment is complete.",
        "Shall we review checkout for ₹500?",
    ],
)
def test_review_invitation_does_not_whitelist_outcomes_or_amounts(text):
    assert SpeechGuard().check(text, deterministic=False).refused


def test_unacknowledged_playback_expires_and_late_ack_cannot_revive_it():
    ledger = PlaybackLedger(history_limit=2)
    for i in range(5):
        row = ledger.announce(i, now=0)
        ledger.started(row.utterance_id, 1)
        ledger.sent(row.utterance_id)
    fresh = ledger.announce(6, now=299)
    assert len(ledger.expire(300)) == 5
    assert not ledger.audible
    assert not ledger.started(1, 301)
    assert not ledger.end(5)
    assert ledger.started(fresh.utterance_id, 301)
    assert len(ledger._entries) == 3  # two historical, one live


@pytest.mark.asyncio
async def test_first_audio_timeout_closes_primary_and_uses_fallback():
    closed = []

    class Stalled:
        async def stream(self, text, voice):
            try:
                yield b""
                await asyncio.Event().wait()
            finally:
                closed.append(True)

    class Backup:
        async def stream(self, text, voice):
            yield b"backup"

    chain = FallbackSynthesizer(Stalled(), Backup(), first_audio_timeout_s=0.01)
    result = [
        chunk
        async for chunk in chain.stream("Advice", VoiceSpec(locale=Locale.EN_IN, name="Aoede"))
    ]
    assert result == [b"backup"]
    assert closed == [True]


@pytest.mark.asyncio
async def test_typing_works_while_recognizer_is_connecting():
    from test_voice_pipeline import build, wait_until
    from voice_runtime.stt.fakes import FakeSttFactory
    from voice_runtime.testing import MemoryTransport

    transport = MemoryTransport()
    pipeline = build(transport=transport, factory=FakeSttFactory(open_delay_s=0.3))
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "text_input", "text": "Show milk"})
        await wait_until(lambda: bool(transport.frames("turn_closed")), timeout=0.2)
        assert [f["state"] for f in transport.frames("recognition_state")] == ["connecting"]
        await wait_until(
            lambda: any(f["state"] == "ready" for f in transport.frames("recognition_state"))
        )
    finally:
        transport.end()
        await task

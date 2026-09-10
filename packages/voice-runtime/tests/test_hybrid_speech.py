from dataclasses import replace

import pytest
from voice_runtime.providers.hybrid import HybridSpeechSynthesizer, route_reply
from voice_runtime.tts.synth import voice_for
from voice_runtime.tts.templates import Locale

ADVICE = (
    "For everyday comfort, I recommend breathable cotton "
    "because it feels softer during warm weather. "
    "Compared with heavier fabric, it is easier to layer and more comfortable for a long commute. "
    "Consider whether easy washing or a structured look matters more to you."
)
VOICE = replace(voice_for(Locale.EN_IN), exact_wording=False)


@pytest.mark.parametrize(
    "text",
    [
        "Found milk.",
        "Added milk to your cart.",
        "Your payment is pending.",
        "आपका भुगतान पूरा हुआ।",
        "Do packet cart mein add hue.",
        ADVICE + " It costs ₹200.",
        ADVICE + " Your order is confirmed.",
    ],
)
def test_facts_and_short_replies_stay_literal(text):
    assert route_reply(text, VOICE) == "chirp"


def test_only_nonnumeric_advice_uses_native_audio():
    assert route_reply(ADVICE, VOICE) == "gemini"
    assert route_reply(ADVICE, replace(VOICE, exact_wording=True)) == "chirp"


class Stub:
    supports_streaming = True

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = 0
        self.closed = False

    async def stream(self, text, voice):
        self.calls += 1
        try:
            if self.fail == "before":
                raise RuntimeError("unavailable")
            yield b"\x01\x00"
            if self.fail == "after":
                raise RuntimeError("lost")
        finally:
            self.closed = True

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_fallback_before_audio_only():
    chirp, gemini = Stub(), Stub("before")
    hybrid = HybridSpeechSynthesizer("test", chirp=chirp, gemini=gemini)
    assert await hybrid.synthesize(ADVICE, VOICE) == b"\x01\x00"
    assert chirp.calls == 1 and gemini.calls == 1
    assert hybrid.consume_degradation() == "chirp"


@pytest.mark.asyncio
async def test_partial_audio_never_replays():
    chirp, gemini = Stub(), Stub("after")
    hybrid = HybridSpeechSynthesizer("test", chirp=chirp, gemini=gemini)
    with pytest.raises(RuntimeError):
        await hybrid.synthesize(ADVICE, VOICE)
    assert chirp.calls == 0


@pytest.mark.asyncio
async def test_interruption_closes_selected_stream():
    chirp, gemini = Stub(), Stub()
    hybrid = HybridSpeechSynthesizer("test", chirp=chirp, gemini=gemini)
    stream = hybrid.stream(ADVICE, VOICE)
    await anext(stream)
    await stream.aclose()
    assert gemini.closed and chirp.calls == 0


@pytest.mark.asyncio
async def test_financial_chirp_failure_does_not_fallback_to_generative_audio():
    chirp, gemini = Stub("before"), Stub()
    hybrid = HybridSpeechSynthesizer("test", chirp=chirp, gemini=gemini)
    with pytest.raises(RuntimeError):
        await hybrid.synthesize("Payment failed.", VOICE)
    assert gemini.calls == 0

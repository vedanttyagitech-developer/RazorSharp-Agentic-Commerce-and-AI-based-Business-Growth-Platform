"""The preferred voice is preferred on every utterance, not merely on the first one.

RazorAI is one voice: Gemini TTS with ``Aoede``, in both languages and in both registers,
so that a greeting and an amount are the same person speaking. A fallback model stands
behind it so an outage is a different voice rather than silence -- and that is the whole
risk this file exists for. A fallback that *stuck* would mean one failed request quietly
moved the rest of the conversation into a second voice, and nothing would say so: the
degradation is announced once, so the sentence after it would simply sound different.

Three properties, and the first is the one with teeth:

* the chain retries from the top on every utterance, so a blip costs one sentence;
* the substitution is named once, so the buyer is told which voice went away rather than
  that speech failed;
* how often each model spoke is counted and reported, so "mostly the preferred one" is a
  number somebody can check instead of an intention.

No network. The synthesisers here are stand-ins whose only job is to fail on demand, which
is the one behaviour the real ones cannot be asked for.
"""

from __future__ import annotations

import pytest
from voice_runtime.tts.fallback import FallbackSynthesizer
from voice_runtime.tts.synth import VoiceSpec
from voice_runtime.tts.templates import Locale

SYNTHESIZER_NAMES = ("primary-test", "fallback-test")
VOICE = VoiceSpec(locale=Locale.EN_IN, name="en-IN")


class Scripted:
    """Speaks, or fails, exactly as told. Records every call so order can be asserted."""

    def __init__(self, label: str, fail_on: set[int] | None = None) -> None:
        self.label = label
        self.fail_on = fail_on or set()
        self.calls = 0

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError(f"{self.label} is unavailable")
        return f"{self.label}:{text}".encode()


async def _speak(chain: FallbackSynthesizer, text: str) -> bytes:
    return await chain.synthesize(text, VOICE)


@pytest.mark.anyio
async def test_the_chain_returns_to_the_preferred_model() -> None:
    """One failure moves one sentence. The next sentence tries the preferred model again.

    This is the property that makes a fallback a fallback rather than a switch. If it were
    sticky, a single blip on utterance two would leave every later utterance in the other
    voice for the rest of the session -- audible, unexplained after the first frame, and
    invisible to every test that only checks the substitution happened at all.
    """
    preferred = Scripted("3.1", fail_on={2})
    behind = Scripted("2.5")
    chain = FallbackSynthesizer(preferred, behind, names=SYNTHESIZER_NAMES)

    assert await _speak(chain, "one") == b"3.1:one"
    assert await _speak(chain, "two") == b"2.5:two"
    # The third utterance is the assertion: back to the preferred model, unprompted.
    assert await _speak(chain, "three") == b"3.1:three"
    assert await _speak(chain, "four") == b"3.1:four"

    assert preferred.calls == 4, "the preferred model was not tried on every utterance"
    assert behind.calls == 1, "the fallback spoke more than the one sentence it was needed for"


@pytest.mark.anyio
async def test_the_substitution_is_named_once_and_then_forgotten() -> None:
    """The buyer is told which voice went away, and is not told again for the same event."""
    chain = FallbackSynthesizer(
        Scripted("3.1", fail_on={1}), Scripted("2.5"), names=SYNTHESIZER_NAMES
    )

    await _speak(chain, "one")
    assert chain.consume_degradation() == "fallback-test"
    assert chain.consume_degradation() is None

    # And a sentence the preferred model spoke reports nothing at all: a degradation frame
    # for an utterance that was not degraded would teach the buyer to ignore them.
    await _speak(chain, "two")
    assert chain.consume_degradation() is None


@pytest.mark.anyio
async def test_how_often_each_model_spoke_is_counted() -> None:
    """ "Mostly the preferred model" has to be checkable, or it is only a hope.

    The counter existed before this test and was surfaced nowhere; ``/v1/voice/metrics``
    now reports it, seeded at zero for every name in the chain so a fallback that has never
    spoken reads as zero rather than as an absent key.
    """
    chain = FallbackSynthesizer(
        Scripted("3.1", fail_on={2}), Scripted("2.5"), names=SYNTHESIZER_NAMES
    )
    for text in ("one", "two", "three", "four", "five"):
        await _speak(chain, text)

    assert chain.spoke == {"primary-test": 4, "fallback-test": 1}
    assert set(chain.spoke) == set(SYNTHESIZER_NAMES), "a name in the chain is not counted"


@pytest.mark.anyio
async def test_every_model_failing_raises_rather_than_returning_silence() -> None:
    """Empty audio would be indistinguishable from a voice that simply said nothing.

    The gateway catches this and sends a degradation frame; what must not happen is a
    chunk of zero bytes travelling the wire as though it were speech.
    """
    chain = FallbackSynthesizer(
        Scripted("3.1", fail_on={1}), Scripted("2.5", fail_on={1}), names=SYNTHESIZER_NAMES
    )
    with pytest.raises(RuntimeError, match="every synthesizer failed"):
        await _speak(chain, "one")

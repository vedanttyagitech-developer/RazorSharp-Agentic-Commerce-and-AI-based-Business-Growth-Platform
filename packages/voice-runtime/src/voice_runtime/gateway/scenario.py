"""Demo apparatus for the speech half of specification 30's "STT/TTS failure" row.

The gateway holds no database, no principal and no capability, and this module does not
give it any. What it adds is a synthesizer wrapper with one bit of state, armed only when
the trusted server says -- in the response to the turn the gateway just sent -- that it
has consumed an armed ``TTS_FAILURE`` from ``scenario_faults``. The server did the
claiming, the disarming and the audit; the gateway is told the outcome and makes one
phrase of synthesis raise.

WHY A WRAPPER AND NOT A BRANCH IN THE PIPELINE
----------------------------------------------
:mod:`voice_runtime.pipeline` is not touched by any of this, and that is the strongest
property here. A failure raised from ``synthesize`` is caught where a real Chirp or Gemini
failure is caught -- ``Speaker.speak``'s ``except Exception`` -- so the frames the buyer's
browser receives are produced by the shipped code path, byte for byte: the reply text is
already on screen (19.1 puts text before speech, always), a ``Degradation`` frame with
``kind="tts_failed"`` follows, and that frame carries ``transaction_state_changed: False``
on the wire. A demo branch that *resembled* that path would prove nothing about the path
that ships.

WHY IT CANNOT BE ARMED IN PRODUCTION
-------------------------------------
There is nothing here to arm. The wrapper's only ``arm`` caller is the turn handler, and
it calls only when the response carries the header; the header is only ever set by an API
whose ``scenario_routes_enabled`` is true, and in production the routes that arm the row
do not exist at all. A gateway pointed at a production API is inert not because it checks
a flag but because nothing will ever tell it to fire.
"""

from __future__ import annotations

from ..tts.synth import SpeechSynthesizer, VoiceSpec

__all__ = ["SCENARIO_TTS_FAILURE", "OneShotFailingSynthesizer"]

#: The one fault name this gateway acts on. Any other value in the header -- a kind meant
#: for a different consumer, or a malformed one -- arms nothing, so a header cannot be a
#: way to make speech fail by accident.
SCENARIO_TTS_FAILURE = "TTS_FAILURE"


class OneShotFailingSynthesizer:
    """Wraps a synthesizer and fails exactly one phrase, once, after being armed.

    Single-use twice over, and both gates are load-bearing. The database row was already
    disarmed by the API's claim, so a second turn cannot arm this again. And ``_armed`` is
    cleared *before* the exception is raised, so a single reply cannot lose more than one
    phrase to it: :class:`~voice_runtime.tts.synth.Speaker` launches look-ahead synthesis
    tasks concurrently, and an armed flag that survived the raise would fail every phrase
    in flight rather than the one the operator asked for.

    A multi-phrase reply therefore loses its first phrase and then stops -- the pipeline
    breaks out of the utterance loop on ``tts_failed``. That is the shipped behaviour of a
    real synthesis failure, not a special case for the demonstration, and it is what the
    buyer would see if Chirp went down mid-sentence.
    """

    def __init__(self, inner: SpeechSynthesizer) -> None:
        self._inner = inner
        self._armed = False

    @property
    def armed(self) -> bool:
        return self._armed

    def arm_for(self, kind: str) -> None:
        """Arm, but only for the one fault this object is the consumer of.

        The turn handler hands over every name the server's header carried, because the
        handler has no business knowing the vocabulary. The filter belongs here, where the
        consumer is: a kind meant for a different consumer, or a header somebody
        malformed, arms nothing rather than making speech fail for a reason nobody asked
        for.
        """
        if kind == SCENARIO_TTS_FAILURE:
            self._armed = True

    async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
        if self._armed:
            self._armed = False
            raise RuntimeError(f"ScenarioFault:{SCENARIO_TTS_FAILURE}")
        return await self._inner.synthesize(text, voice)

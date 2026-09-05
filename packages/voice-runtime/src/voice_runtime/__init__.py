"""voice-runtime: the split realtime voice pipeline (specification section 19).

    mic -> Gemini Transcribe Live (STT) -> text agent -> deterministic template or
    guarded text -> Chirp 3 HD (TTS) -> speaker

Native audio, ADK ``run_live`` and ADK tool confirmation are not used on any path (19.1).
Every I/O sits behind a Protocol with a fake, so the 19.14 properties are unit-tested
without audio or network.
"""

from .clock import Clock, FakeClock, MonotonicClock
from .pipeline import Incoming, PipelineMetrics, TransportClosedError, VoicePipeline, VoiceTransport
from .turn import FakeTurnHandler, TurnHandler, TurnReply

__all__ = [
    "Clock",
    "FakeClock",
    "FakeTurnHandler",
    "Incoming",
    "MonotonicClock",
    "PipelineMetrics",
    "TransportClosedError",
    "TurnHandler",
    "TurnReply",
    "VoicePipeline",
    "VoiceTransport",
]

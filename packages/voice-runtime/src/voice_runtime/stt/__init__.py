"""Speech-to-text side of the split pipeline (specification 19.3 to 19.6, 19.8).

The gateway owns the microphone stream. The Transcribe Live connection is rotated before
the provider limit, the queue in front of it is bounded by freshness, the echo gate feeds
silence while the assistant speaks, and transcript text is replaced rather than appended.
"""

from .echo_gate import EchoGate
from .events import (
    LiveSttFactory,
    LiveSttSession,
    SttActivity,
    SttConnectionLostError,
    SttEvent,
    SttFinal,
    SttGoAway,
    SttInterim,
    SttResumption,
    SttSetupComplete,
)
from .fakes import FakeSttFactory, FakeSttSession
from .queue import FreshAudioQueue, QueuedFrame
from .session import SttListener, SttMetrics, TranscribeSession
from .transcript import FreshnessStamp, TranscriptState, TranscriptTurn, apply_stream_text

__all__ = [
    "EchoGate",
    "FakeSttFactory",
    "FakeSttSession",
    "FreshAudioQueue",
    "FreshnessStamp",
    "LiveSttFactory",
    "LiveSttSession",
    "QueuedFrame",
    "SttActivity",
    "SttConnectionLostError",
    "SttEvent",
    "SttFinal",
    "SttGoAway",
    "SttInterim",
    "SttListener",
    "SttMetrics",
    "SttResumption",
    "SttSetupComplete",
    "TranscribeSession",
    "TranscriptState",
    "TranscriptTurn",
    "apply_stream_text",
]

"""PCM16 helpers: the format assertions and the one resampling the pipeline needs.

The recognizer takes **16 kHz** PCM16 little-endian mono and the synthesiser returns
**24 kHz**. That asymmetry is intentional (specification 19.2) and is not to be "fixed",
but a test that feeds synthesised speech back into the recognizer has to cross it, so the
conversion lives here, once, with the reasoning attached.

Everything operates on ``bytes`` of signed 16-bit little-endian samples. Sample values are
clamped, never wrapped: a wrapped sample is a loud click, and a loud click in front of a
voice-activity detector reads as the start of speech.
"""

from __future__ import annotations

import array
import math
from typing import Final

from .constants import BYTES_PER_SAMPLE, INPUT_SAMPLE_RATE_HZ, MIC_FRAME_BYTES

__all__ = [
    "AudioFormatError",
    "assert_pcm16",
    "frames_of",
    "pcm16_rms",
    "resample_pcm16",
    "silence",
]

_INT16_MIN: Final[int] = -32768
_INT16_MAX: Final[int] = 32767


class AudioFormatError(ValueError):
    """The bytes are not PCM16 little-endian mono. Asserted on ingress, never assumed."""


def assert_pcm16(pcm: bytes) -> bytes:
    """Return ``pcm`` if it is a whole number of 16-bit samples; raise otherwise."""
    if len(pcm) % BYTES_PER_SAMPLE:
        raise AudioFormatError(
            f"PCM16 is two bytes per sample; got {len(pcm)} bytes, which is not a whole sample"
        )
    return pcm


def silence(byte_length: int) -> bytes:
    """Digital silence of exactly ``byte_length`` bytes -- what the echo gate substitutes."""
    return bytes(byte_length)


def _samples(pcm: bytes) -> array.array[int]:
    values = array.array("h")
    values.frombytes(assert_pcm16(pcm))
    if _BIG_ENDIAN:  # pragma: no cover - x86 and Apple silicon are both little-endian
        values.byteswap()
    return values


def _to_bytes(values: array.array[int]) -> bytes:
    if _BIG_ENDIAN:  # pragma: no cover - see above
        values = values[:]
        values.byteswap()
    return values.tobytes()


_BIG_ENDIAN: Final[bool] = array.array("h", [1]).tobytes()[0] == 0


def resample_pcm16(pcm: bytes, *, from_hz: int, to_hz: int) -> bytes:
    """Resample by linear interpolation between neighbouring samples.

    Linear interpolation is the honest choice here rather than a windowed filter: the one
    conversion this package performs is 24 kHz down to 16 kHz, where everything above
    8 kHz is already near-silent in speech, and the result is checked the only way that
    means anything -- by feeding it to the real recognizer and reading the transcript. A
    filter that is never verified against the recognizer is decoration.
    """
    if from_hz <= 0 or to_hz <= 0:
        raise ValueError("sample rates are positive")
    source = _samples(pcm)
    if from_hz == to_hz or not source:
        return _to_bytes(source)
    out_len = max(1, math.floor(len(source) * to_hz / from_hz))
    step = from_hz / to_hz
    out = array.array("h", bytes(out_len * BYTES_PER_SAMPLE))
    last = len(source) - 1
    for i in range(out_len):
        position = i * step
        left = int(position)
        if left >= last:
            out[i] = source[last]
            continue
        fraction = position - left
        value = source[left] + (source[left + 1] - source[left]) * fraction
        out[i] = max(_INT16_MIN, min(_INT16_MAX, int(round(value))))
    return _to_bytes(out)


def pcm16_rms(pcm: bytes) -> float:
    """Root-mean-square level in 0..1, the same scale the client's barge-in uses (19.7)."""
    source = _samples(pcm)
    if not source:
        return 0.0
    total = sum(sample * sample for sample in source)
    return math.sqrt(total / len(source)) / -_INT16_MIN


def frames_of(pcm: bytes, frame_bytes: int = MIC_FRAME_BYTES) -> list[bytes]:
    """Split into fixed-size frames, padding the last with silence.

    The recognizer is fed a steady cadence of equal frames whether the buyer is speaking
    or not, so a short final frame would be the one irregularity in the stream.
    """
    if frame_bytes <= 0 or frame_bytes % BYTES_PER_SAMPLE:
        raise ValueError("a frame is a positive whole number of 16-bit samples")
    assert_pcm16(pcm)
    frames = [pcm[at : at + frame_bytes] for at in range(0, len(pcm), frame_bytes)]
    if frames and len(frames[-1]) < frame_bytes:
        frames[-1] = frames[-1] + silence(frame_bytes - len(frames[-1]))
    return frames


def duration_s(pcm: bytes, sample_rate_hz: int = INPUT_SAMPLE_RATE_HZ) -> float:
    """How long ``pcm`` plays for, at ``sample_rate_hz``."""
    return len(pcm) / (BYTES_PER_SAMPLE * sample_rate_hz)

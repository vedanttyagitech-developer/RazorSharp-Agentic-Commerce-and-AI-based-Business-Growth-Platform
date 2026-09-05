"""RIFF/WAVE header handling for synthesised audio.

Cloud TTS ``LINEAR16`` output is PCM16 inside a WAV container. The client is sent raw
PCM16 at the advertised rate, so the container is stripped. The header is parsed, not
assumed to be 44 bytes: an optional chunk (``LIST``, ``fact``) before ``data`` would
otherwise be played as a click of noise before every sentence.
"""

from __future__ import annotations

import struct


def strip_wav_header(audio: bytes) -> bytes:
    """Return the PCM payload of a WAV file, or the input unchanged if it is not RIFF."""
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio
    offset = 12
    while offset + 8 <= len(audio):
        chunk_id = audio[offset : offset + 4]
        (chunk_size,) = struct.unpack_from("<I", audio, offset + 4)
        body = offset + 8
        if chunk_id == b"data":
            return audio[body : body + chunk_size]
        offset = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned
    return b""


def wav_header(pcm_length: int, *, sample_rate_hz: int, channels: int = 1) -> bytes:
    """A canonical 44-byte PCM16 header; used by tests and the live harness."""
    bits = 16
    byte_rate = sample_rate_hz * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + pcm_length)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate_hz, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", pcm_length)
    )

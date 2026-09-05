"""Every voice tunable in one place, with what it protects (specification 19.15, ADR D13).

None of these values are universal. Each is re-measured per deployment, particularly the
echo tail, which is a property of the listener's speakers and not of this code. The table
in specification 19.15 is the source of record; this module is its executable copy.
"""

from __future__ import annotations

from typing import Final

# --- audio format (19.2, 19.15) ------------------------------------------------------
#: Recognizer contract: Transcribe Live consumes 16 kHz PCM16 little-endian mono.
INPUT_SAMPLE_RATE_HZ: Final[int] = 16_000
#: Model output rate. Differs from the input rate on purpose; it is not to be "fixed".
OUTPUT_SAMPLE_RATE_HZ: Final[int] = 24_000
#: PCM16 mono: two bytes per sample. Asserted on ingress, never assumed.
BYTES_PER_SAMPLE: Final[int] = 2
#: The sample rate travels in the MIME string, not in a config field (19.2).
INPUT_MIME_TYPE: Final[str] = f"audio/pcm;rate={INPUT_SAMPLE_RATE_HZ}"
OUTPUT_MIME_TYPE: Final[str] = f"audio/pcm;rate={OUTPUT_SAMPLE_RATE_HZ}"

# --- microphone framing (19.15) ------------------------------------------------------
#: ~100 ms frames: latency against syscall overhead.
MIC_FRAME_MS: Final[int] = 100
#: Bytes in one nominal microphone frame at the input contract.
MIC_FRAME_BYTES: Final[int] = INPUT_SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * MIC_FRAME_MS // 1000
#: Largest binary frame the gateway accepts from a client (one second of audio).
MAX_CLIENT_AUDIO_FRAME_BYTES: Final[int] = INPUT_SAMPLE_RATE_HZ * BYTES_PER_SAMPLE

# --- audio queue (19.4) --------------------------------------------------------------
#: Freshness bound on queued microphone audio. Specification range is 3-5 s; a 30 s buffer
#: replays half a minute of stale speech into a live commerce conversation after a long
#: reconnect, which is worse than losing it.
MAX_FRESH_AUDIO_AGE_S: Final[float] = 4.0
#: Capacity bound: the upper end of the freshness window expressed in nominal frames, so
#: memory is bounded even if the clock is never consulted. Eviction is oldest-first.
QUEUE_MAX_FRAMES: Final[int] = int(5.0 * 1000 / MIC_FRAME_MS)
#: Text older than this never reaches the agent (19.4 applied to transcripts).
TRANSCRIPT_FRESHNESS_S: Final[float] = MAX_FRESH_AUDIO_AGE_S

# --- stream lifecycle (19.3) ---------------------------------------------------------
#: Documented provider limit for one Transcribe Live stream.
PROVIDER_STREAM_LIMIT_S: Final[float] = 600.0
#: Rotate before the provider limit with a safety margin: mid-utterance disconnection.
STREAM_ROTATION_MARGIN_S: Final[float] = 540.0
#: A hung connect must not block startup: warn and continue with the retry loop underneath.
CONNECT_TIMEOUT_S: Final[float] = 20.0
#: Reconnect backoff, exponential from the start to the ceiling: no hot-looping.
RECONNECT_BACKOFF_START_S: Final[float] = 0.5
RECONNECT_BACKOFF_MAX_S: Final[float] = 10.0
#: Reconnect attempts are bounded and surfaced; after this the session degrades to text.
MAX_RECONNECT_ATTEMPTS: Final[int] = 5
#: How long the previous connection may keep delivering results after the writer switched.
ROTATION_DRAIN_S: Final[float] = 2.0

# --- echo gate and barge-in (19.6, 19.7) ---------------------------------------------
#: Echo tail measured from the client's playback end: speaker ring-out being transcribed.
ECHO_TAIL_S: Final[float] = 0.6
#: If the client never reports playback end, the gate stays engaged for at most this long
#: after the server finished sending, then releases with a visible degradation (19.12).
ECHO_GATE_MAX_HOLD_S: Final[float] = 30.0
#: Client-side barge-in detection thresholds, advertised to the client at session start.
BARGE_IN_LEVEL_RMS: Final[float] = 0.08
BARGE_IN_SUSTAIN_S: Final[float] = 0.3
#: Client scheduling lead for playback chunks: underrun protection.
PLAYBACK_LEAD_S: Final[float] = 0.03

# --- tickets and origin (19.15, 24.2) ------------------------------------------------
#: Single-use, short-lived, tenant- and session-bound.
VOICE_TICKET_TTL_S: Final[float] = 60.0
VOICE_TICKET_BYTES: Final[int] = 32

# --- model pins (19.2) ---------------------------------------------------------------
#: Realtime STT. The 3.5 transcribe models serve from ``global`` only; a regional endpoint
#: returns 404, so the location is pinned here and deliberately ignores
#: ``GOOGLE_CLOUD_LOCATION``.
TRANSCRIBE_MODEL: Final[str] = "gemini-3.5-transcribe-live-preview"
TRANSCRIBE_LOCATION: Final[str] = "global"
#: Transactional TTS: Cloud TTS Chirp 3 HD, keyed by locale.
TRANSACTIONAL_VOICES: Final[dict[str, str]] = {
    "en-IN": "en-IN-Chirp3-HD-Kore",
    "hi-IN": "hi-IN-Chirp3-HD-Kore",
}
#: Digital silence: what the echo gate substitutes for a microphone frame (19.6).
SILENCE_BYTE: Final[int] = 0

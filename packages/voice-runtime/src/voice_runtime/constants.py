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
#: End-to-end budget for a settled transcript: how old the audio behind it already was,
#: plus however long the turn then waited behind another turn. It is deliberately LARGER
#: than the audio window rather than equal to it -- the queue already refuses to send
#: audio older than MAX_FRESH_AUDIO_AGE_S, so a window equal to it could never be
#: exceeded and the check would be decoration. What this bounds is the case the queue
#: cannot see: a turn that sat in line while a previous turn ran long, and now describes
#: something the buyer has moved on from.
TRANSCRIPT_FRESHNESS_S: Final[float] = 12.0

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
#: Results arriving from it inside this window ARE dispatched: that is what draining means.
ROTATION_DRAIN_S: Final[float] = 2.0
#: How long a hypothesis carried across a rotation seam stays joinable. Beyond this the
#: prefix is dropped rather than prepended to whatever the buyer says next. Without the
#: bound, an utterance whose final was lost at the seam welds itself onto the buyer's next,
#: unrelated sentence and two intents reach the agent as one message.
PREFIX_CARRY_MAX_S: Final[float] = 5.0

#: Longest phrase handed to the synthesiser in one call. Not a limit on what is SAID --
#: nothing is dropped -- only on how much is synthesised before the first sample can play.
#: Measured on this deployment: a 350-character sentence took 21 s to synthesise, and the
#: same sentence cut at its commas started playing in about 3. Whole sentences shorter
#: than this are never cut. See ``tts/tokenizer.py::split_for_synthesis``.
MAX_SYNTHESIS_CHARS: Final[int] = 110
#: Phrases synthesised ahead of the one being sent. Synthesis is slower than playback on
#: this deployment, so running them strictly in turn leaves the buyer in silence for the
#: sum of the two rather than the larger. Two is enough to hide it; more just holds unsent
#: audio in memory that a barge-in will throw away.
SYNTHESIS_LOOKAHEAD: Final[int] = 2

# --- echo gate and barge-in (19.6, 19.7) ---------------------------------------------
#: Echo tail, measured from the CLIENT's playback end: speaker ring-out being transcribed.
#:
#: Measured against the real recognizer rather than guessed. Feeding it the last N
#: milliseconds of an utterance, it returns nothing at all up to 500 ms and starts
#: transcribing at 700 ms:
#:
#:     100 / 200 / 300 / 500 ms -> (nothing)
#:     700 ms  -> "please"
#:     1000 ms -> "milk please"
#:     1500 ms -> "liters of milk, please."
#:
#: So a leak has to exceed roughly 700 ms before it can be heard at all, and this tail
#: sits below that with margin. What the tail actually has to cover is the client's
#: playback-ended report reaching the server plus the speakers' physical ring-out; both
#: are far under 500 ms on any ordinary setup. The anchor is the part that mattered --
#: measured from the server's last byte instead, the client can still be holding seconds
#: of queued audio (see ADR 0006 section 4.2).
ECHO_TAIL_S: Final[float] = 0.6
#: The shortest leak the recognizer will transcribe, from the measurement above. Kept as a
#: constant so the relationship between it and the tail is asserted rather than remembered.
RECOGNIZABLE_LEAK_S: Final[float] = 0.7
#: If the client never reports playback end, the gate stays engaged for at most this long
#: after the server finished sending, then releases with a visible degradation (19.12).
ECHO_GATE_MAX_HOLD_S: Final[float] = 30.0
#: Client-side barge-in detection thresholds, advertised to the client at session start.
BARGE_IN_LEVEL_RMS: Final[float] = 0.08
BARGE_IN_SUSTAIN_S: Final[float] = 0.3
#: Client scheduling lead for playback chunks: underrun protection.
PLAYBACK_LEAD_S: Final[float] = 0.03

# --- spoken consent (19.11) ----------------------------------------------------------
#: How long, after the gateway has finished SENDING the approval reading, a spoken yes or
#: no counts against the card that was read. Anchored on send-completion because that is
#: the last instant the server knows about; the client's playback report never extends
#: it, so a slow or hostile client cannot stretch consent, only lose it.
#:
#: Ten seconds is a sum, not a feeling: the client may still hold SYNTHESIS_LOOKAHEAD
#: phrases of unplayed audio at send-complete (about 2-3 s), the echo gate keeps the
#: microphone silent until the client's playback_ended plus ECHO_TAIL_S, a person takes
#: about a second to answer, "yes" takes half a second to say, the recognizer's
#: endpointing needs 1.5 s of silence (measured, tests/test_voice_real_audio.py), and the
#: final arrives about 0.7 s after that. Roughly seven seconds; ten leaves margin while
#: still meaning "just after the amount was read".
CONSENT_WINDOW_S: Final[float] = 10.0

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
#:
#: Hindi is Sulafat and English is Kore, which is a deliberate pair rather than a
#: preference. Both are FEMALE Chirp 3 HD voices at 24 kHz, so they are interchangeable
#: on every axis the pipeline cares about, and the same thirty star voices exist in both
#: locales -- holding one name across both was available and was not chosen.
#:
#: The pair is a product choice and is not defended here on pace. An earlier version of
#: this comment claimed Kore reads Hindi "a quarter faster" than Sulafat; that came from
#: one sentence synthesised once per voice, and it does not survive repetition. Chirp 3 HD
#: is not deterministic -- the same text, voice and rate varies by up to 16% run to run --
#: and over three sentences at five runs each the two voices differ by about 2% in median
#: duration, with overlapping ranges. Whatever separates them, it is not measurable speed.
#:
#: What DOES control pace is SPEAKING_RATE below, which is measured and does hold up.
#:
#: Hinglish maps here too (``gateway.agent_client._LOCALE_FOR_LANGUAGE``): romanised Hindi
#: is SPOKEN as Hindi. That is verified rather than assumed -- synthesising "Mujhe do
#: packet doodh chahiye, kitna hoga?" with this voice and reading it back through the real
#: recognizer returns "मुझे दो पैकेट दूध चाहिए, कितना होगा?", so Chirp does pronounce Latin-script
#: Hindi as Hindi and the mapping is sound.
TRANSACTIONAL_VOICES: Final[dict[str, str]] = {
    "en-IN": "en-IN-Chirp3-HD-Kore",
    "hi-IN": "hi-IN-Chirp3-HD-Sulafat",
}
#: How fast transactional speech is spoken, as Cloud TTS's multiplier on the voice's own
#: pace. Below 1.0 because Chirp 3 HD's default is brisk: measured through this package's
#: own code path, English transactional lines ran 172-186 wpm and Hindi 182-198, against
#: roughly 150 wpm for unhurried conversational speech. At 0.85 both land in the 155-170
#: band, which is the pace of somebody reading you a total rather than reciting one.
#:
#: That Chirp 3 HD honours this at all was measured, not assumed -- some of its voices
#: ignore AudioConfig prosody fields. Duration scales as almost exactly the inverse of the
#: rate (0.8 -> 1.244x, 1.25 -> 0.794x against 1.25 and 0.80 expected). Steps smaller than
#: about 0.1 disappear into the model's own run-to-run variance, which is why this is not
#: tuned more finely than it is measured.
#:
#: One rate for both locales: words per minute is not comparable across languages, and at
#: the same rate the two are already within one band of each other by characters per
#: second. The Gemini TTS fallback has no equivalent knob and speaks at its own pace; that
#: substitution is already surfaced as a degradation (19.12), so it is not a silent one.
SPEAKING_RATE: Final[float] = 0.85
#: Digital silence: what the echo gate substitutes for a microphone frame (19.6).
SILENCE_BYTE: Final[int] = 0

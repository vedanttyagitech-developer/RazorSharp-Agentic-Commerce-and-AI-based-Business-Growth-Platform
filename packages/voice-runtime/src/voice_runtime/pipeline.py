"""The split-pipeline loop for one voice connection (19.1).

    mic frames -> echo gate -> fresh queue -> Transcribe Live -> final transcript
      -> TurnHandler (text) -> template or guarded text -> TTS chunks -> client

Everything that touches Google is behind a Protocol, so this loop runs unchanged against
fakes. The transport is a Protocol too, so the gateway's FastAPI WebSocket is one adapter
and the tests' in-memory transport is another.

Two generations live here and must not be confused: the STT connection generation (owned
by ``TranscribeSession``, bumped on rotation) and the speech generation (owned by
``SpeechGeneration``, bumped on barge-in). Wire frames name each explicitly.

SPOKEN CONSENT, AND WHAT THIS LOOP DOES NOT DO WITH IT
------------------------------------------------------
A ``read_card`` frame makes this loop read an approval card from the trusted server, speak
it from a template, and open a consent window when the last byte has been SENT. A settled
voice transcript inside that window is matched against a closed lexicon
(:mod:`voice_runtime.consent`) and the verdict is reported as a frame. That is the whole
of it: the loop never sends an approval. The storefront receives ``consent_recognised``,
compares it to the card it is displaying, and presses its own Approve button, so the
request that reaches the kernel is the button's request with the button's bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from commerce_domain import Money
from pydantic import ValidationError

from .clock import Clock
from .consent import CardReader, CardUnavailableError, ConsentTracker
from .constants import (
    BYTES_PER_SAMPLE,
    CONNECT_TIMEOUT_S,
    CONSENT_WINDOW_S,
    MAX_CLIENT_AUDIO_FRAME_BYTES,
    MAX_RECONNECT_ATTEMPTS,
    RECONNECT_BACKOFF_START_S,
    STREAM_ROTATION_MARGIN_S,
)
from .identity import VoiceIdentity
from .stt.events import LiveSttFactory
from .stt.session import TranscribeSession
from .stt.transcript import FreshnessStamp, TranscriptTurn
from .tts.plain import plain_for_speech
from .tts.synth import Speaker, SpeakResult, SpeechChunk, SpeechGeneration, SpeechSynthesizer
from .tts.templates import Locale, render_consent_reading, render_decision, render_decision_card
from .turn import TurnHandler
from .wire.frames import (
    AgentReply,
    BargeIn,
    CardRead,
    ConsentClosed,
    ConsentClosedReason,
    ConsentDeclined,
    ConsentListening,
    ConsentRecognised,
    ConsentUnrecognised,
    Degradation,
    DegradationKind,
    ErrorFrame,
    Interrupted,
    PlaybackEnded,
    ReadCard,
    ServerFrame,
    SessionReady,
    SpeechChunkHeader,
    SpeechEnd,
    SpeechStart,
    TextInput,
    TranscriptFinal,
    TranscriptPartial,
    parse_client_frame,
)

log = logging.getLogger(__name__)


class TransportClosedError(Exception):
    """The client went away. Not an error."""


@dataclass(frozen=True, slots=True)
class Incoming:
    """One WebSocket message: exactly one of ``text`` or ``data`` is set."""

    text: str | None = None
    data: bytes | None = None


class VoiceTransport(Protocol):
    async def send_frame(self, frame: ServerFrame) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def receive(self) -> Incoming:
        """Next message; raises ``TransportClosedError`` when the socket ends."""
        ...


@dataclass(slots=True)
class PipelineMetrics:
    """Surfaced counters (19.13)."""

    audio_frames_in: int = 0
    invalid_frames: int = 0
    turns: int = 0
    stale_turns_rejected: int = 0
    barge_ins: int = 0
    degradations: int = 0
    stale_playback_reports: int = 0
    card_reads: int = 0


@dataclass(frozen=True, slots=True)
class SpokenOutcome:
    """What ``_speak_utterances`` actually put on the wire, for callers that must know.

    A consent window may open only after a reading was sent in full: no chunks, a
    cancellation or a synthesis failure each mean the buyer did not hear the amount.
    """

    chunks: int
    cancelled: bool
    tts_failed: bool

    @property
    def complete(self) -> bool:
        return self.chunks > 0 and not self.cancelled and not self.tts_failed


class VoicePipeline:
    """Runs one connection until the transport closes."""

    def __init__(
        self,
        *,
        transport: VoiceTransport,
        stt_factory: LiveSttFactory | None,
        synthesizer: SpeechSynthesizer,
        turn_handler: TurnHandler,
        identity: VoiceIdentity,
        clock: Clock,
        locale: Locale = Locale.EN_IN,
        card_reader: CardReader | None = None,
        consent_window_s: float = CONSENT_WINDOW_S,
        rotation_margin_s: float = STREAM_ROTATION_MARGIN_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        backoff_start_s: float = RECONNECT_BACKOFF_START_S,
        max_reconnect_attempts: int = MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        self._transport = transport
        self._stt_factory = stt_factory
        self._turn_handler = turn_handler
        self._identity = identity
        self._session_id = identity.session_id
        self._clock = clock
        self._locale = locale
        self._card_reader = card_reader
        #: At most one consent window at a time, on the same clock as every other
        #: freshness decision here. Public so a test can read its counters.
        self.consent = ConsentTracker(clock=clock, window_s=consent_window_s)
        self._stt_options = {
            "rotation_margin_s": rotation_margin_s,
            "connect_timeout_s": connect_timeout_s,
            "backoff_start_s": backoff_start_s,
            "max_reconnect_attempts": max_reconnect_attempts,
        }
        self.speech_generation = SpeechGeneration()
        self.speaker = Speaker(
            synthesizer=synthesizer, sink=self, generation=self.speech_generation
        )
        self.stt: TranscribeSession | None = None
        self.metrics = PipelineMetrics()
        self._turn_lock = asyncio.Lock()
        #: Serialises every write to the transport. See :meth:`send_chunk`.
        self._send_lock = asyncio.Lock()
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._text_turn_seq = 0

    # ---- lifecycle -------------------------------------------------------------------

    async def run(self) -> None:
        await self._send(SessionReady(session_id=self._session_id))
        if self._stt_factory is None:
            await self._degrade(
                "stt_unavailable", "Voice recognition is not configured. You can type instead."
            )
        else:
            self.stt = TranscribeSession(
                factory=self._stt_factory,
                listener=self,
                clock=self._clock,
                rotation_margin_s=float(self._stt_options["rotation_margin_s"]),
                connect_timeout_s=float(self._stt_options["connect_timeout_s"]),
                backoff_start_s=float(self._stt_options["backoff_start_s"]),
                max_reconnect_attempts=int(self._stt_options["max_reconnect_attempts"]),
            )
            await self.stt.start()
        try:
            while True:
                message = await self._transport.receive()
                if message.data is not None:
                    await self._on_audio(message.data)
                elif message.text is not None:
                    await self._on_text_frame(message.text)
        except TransportClosedError:
            pass
        finally:
            self.speech_generation.bump()
            pending = list(self._turn_tasks)
            for task in pending:
                task.cancel()
            # gather(return_exceptions=True) collects each turn task's outcome instead of
            # re-raising the first one, so a failed turn cannot mask the cleanup of the
            # rest, and no child exception is left un-retrieved.
            #
            # The suppression around it is deliberate and was arrived at the hard way.
            # This is the ASGI boundary: run() IS the websocket handler, and a client
            # going away cancels it, after which every await here re-raises immediately.
            # Letting that propagate was tried -- it turns an ordinary disconnect into an
            # exception out of the handler and breaks the close path. The cost is that an
            # outer task.cancel() on the pipeline sees it complete rather than cancel;
            # nothing in this system does that, and cleanup finishing matters more.
            with contextlib.suppress(asyncio.CancelledError):
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if self.stt is not None:
                    await self.stt.stop()

    # ---- inbound ---------------------------------------------------------------------

    async def _on_audio(self, pcm: bytes) -> None:
        """PCM16 LE mono is asserted on ingress, never assumed (19.2)."""
        if not pcm or len(pcm) % BYTES_PER_SAMPLE or len(pcm) > MAX_CLIENT_AUDIO_FRAME_BYTES:
            self.metrics.invalid_frames += 1
            await self._send(
                ErrorFrame(
                    code="invalid_audio_frame",
                    message=(
                        "audio frames are PCM16 little-endian mono at 16 kHz: non-empty, "
                        f"even length, at most {MAX_CLIENT_AUDIO_FRAME_BYTES} bytes"
                    ),
                )
            )
            return
        self.metrics.audio_frames_in += 1
        if self.stt is None:
            return
        self.stt.feed(pcm)
        if self.stt.echo_gate.consume_hold_expired():
            await self._degrade(
                "echo_gate_uncertain",
                "Playback completion was never reported; listening resumed. If the "
                "assistant mishears itself, type instead.",
            )

    async def _on_text_frame(self, raw: str) -> None:
        try:
            frame = parse_client_frame(raw)
        except ValidationError as exc:
            self.metrics.invalid_frames += 1
            await self._send(
                ErrorFrame(code="invalid_frame", message=str(exc.errors()[0].get("msg", "invalid")))
            )
            return
        match frame:
            case TextInput(text=text):
                self._text_turn_seq += 1
                turn = TranscriptTurn(
                    turn_id=-self._text_turn_seq,  # negative: never collides with voice turns
                    text=text,
                    is_final=True,
                    stamp=FreshnessStamp(
                        observed_at=self._clock.now(),
                        generation=self.stt.generation if self.stt is not None else 0,
                    ),
                    source="text",
                )
                await self._send(
                    TranscriptFinal(
                        text=text,
                        turn_id=turn.turn_id,
                        stt_generation=turn.generation,
                        age_ms=0,
                        stale=False,
                        source="text",
                    )
                )
                self._schedule_turn(turn)
            case ReadCard():
                self._spawn(self._run_read_card(frame), name=f"voice-read-card-{frame.version}")
            case BargeIn():
                await self._interrupt()
            case PlaybackEnded(speech_generation=reported):
                # The frame names a generation and the server must check it. A stale or
                # forged report -- from a barged-into generation, a buggy client, or a
                # hostile one -- would release the echo gate while the assistant is still
                # audible. The client can be holding seconds of queued audio at that
                # moment, so the recognizer would transcribe the assistant's own sentence
                # and it would settle as a FINAL transcript and be sent as buyer intent.
                if reported != self.speech_generation.current:
                    self.metrics.stale_playback_reports += 1
                    log.info(
                        "ignoring playback_ended for generation %d; current is %d",
                        reported,
                        self.speech_generation.current,
                    )
                elif self.stt is not None:
                    self.stt.echo_gate.on_client_playback_ended()
            case _:
                pass

    async def _interrupt(self) -> None:
        """Client acted locally first (19.7); the server bumps the generation (19.8)."""
        self.metrics.barge_ins += 1
        generation = self.speech_generation.bump()
        if self.stt is not None:
            self.stt.echo_gate.on_barge_in()
        await self._send(Interrupted(speech_generation=generation))
        # A buyer who spoke over the reading had not heard the whole amount, so whatever
        # they said -- including a yes -- is not consent to it. The client raises a
        # barge-in while it still has queued audio, which is exactly the case where the
        # server has finished sending and the speakers have not finished saying.
        await self._close_consent("barge_in")

    async def _close_consent(self, reason: ConsentClosedReason) -> None:
        """Close the open window, if there is one, and say so on the wire."""
        window = self.consent.close(reason)
        if window is not None:
            await self._send(ConsentClosed(consent_id=window.consent_id, reason=reason))

    # ---- SttListener -----------------------------------------------------------------

    async def on_partial(self, turn: TranscriptTurn) -> None:
        await self._send(
            TranscriptPartial(
                text=turn.text,
                turn_id=turn.turn_id,
                stt_generation=turn.generation,
                age_ms=int(turn.stamp.age(self._clock.now()) * 1000),
            )
        )

    async def on_final(self, turn: TranscriptTurn) -> None:
        assert self.stt is not None
        fresh = self.stt.transcript.is_fresh(turn)
        await self._send(
            TranscriptFinal(
                text=turn.text,
                turn_id=turn.turn_id,
                stt_generation=turn.generation,
                age_ms=int(turn.stamp.age(self._clock.now()) * 1000),
                stale=not fresh,
            )
        )
        if not fresh:
            # Stale text never reaches the agent (19.4). The buyer sees it marked stale
            # and can repeat themselves; they cannot un-hear a stale answer.
            self.metrics.stale_turns_rejected += 1
            return
        # Consent is consulted HERE and only here: on a settled transcript that came off
        # the buyer's microphone. Typed input, the agent's replies and the gateway's own
        # readings have no path to it. With no window open this is a no-op and the turn
        # goes to the agent as it always did, where a "yes" is a sentence like any other.
        observed = self.consent.observe(turn)
        window = observed.window
        match observed.kind:
            case "no_window":
                self._schedule_turn(turn)
            case "recognised":
                assert window is not None
                await self._send(
                    ConsentRecognised(
                        consent_id=window.consent_id,
                        checkout_id=window.card.checkout_id,
                        version=window.card.version,
                        content_hash=window.card.content_hash,
                        amount_minor=window.card.amount_minor,
                        currency=window.card.currency,
                        heard=turn.text,
                        turn_id=turn.turn_id,
                        stt_generation=turn.generation,
                        offset_ms=int((turn.stamp.observed_at - window.opened_at) * 1000),
                    )
                )
                await self._send(ConsentClosed(consent_id=window.consent_id, reason="recognised"))
            case "declined":
                assert window is not None
                await self._send(ConsentDeclined(consent_id=window.consent_id, heard=turn.text))
                await self._send(ConsentClosed(consent_id=window.consent_id, reason="declined"))
            case "unrecognised":
                assert window is not None
                await self._send(
                    ConsentUnrecognised(
                        consent_id=window.consent_id, text=turn.text, reason="not_in_lexicon"
                    )
                )
            case "began_before_reading":
                assert window is not None
                await self._send(
                    ConsentUnrecognised(
                        consent_id=window.consent_id,
                        text=turn.text,
                        reason="began_before_reading_ended",
                    )
                )
            case "expired":
                assert window is not None
                await self._send(ConsentClosed(consent_id=window.consent_id, reason="expired"))
                # A late yes or no was an answer to the reading and is not forwarded as
                # a question; anything else the buyer said is still theirs to ask.
                if observed.verdict == "none":
                    self._schedule_turn(turn)

    async def on_activity(self, started: bool) -> None:
        log.debug("STT activity %s for session %s", "start" if started else "end", self._session_id)

    async def on_rotated(self, generation: int) -> None:
        log.info("STT rotated to generation %d for session %s", generation, self._session_id)

    async def on_stt_degraded(self, kind: str, detail: str) -> None:
        messages: dict[str, str] = {
            "stt_connection_lost": "Voice connection lost; reconnecting. You can type meanwhile.",
            "stt_rotation_failed": "Voice stream rotation failed; reconnecting. You can type.",
            "stt_unavailable": "Voice recognition is unavailable. Please type instead.",
        }
        log.warning("STT degraded (%s) for session %s: %s", kind, self._session_id, detail)
        if kind in ("stt_connection_lost", "stt_rotation_failed", "stt_unavailable"):
            await self._degrade(kind, messages[kind])  # type: ignore[arg-type]

    # ---- turns -----------------------------------------------------------------------

    def _schedule_turn(self, turn: TranscriptTurn) -> None:
        self._spawn(self._run_turn(turn), name=f"voice-turn-{turn.turn_id}")

    def _spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> None:
        """Run ``coro`` as a task this pipeline owns and cancels when the socket ends."""
        task = asyncio.create_task(coro, name=name)
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_turn(self, turn: TranscriptTurn) -> None:
        async with self._turn_lock:
            # Re-checked HERE, after the lock, because this is the only place a turn can
            # have waited. It was fresh when the recognizer settled it; it may have spent
            # the intervening time queued behind a turn that ran long, and answering a
            # question the buyer has since abandoned is worse than not answering it (19.4).
            if self.stt is not None and not self.stt.transcript.is_fresh(turn):
                self.metrics.stale_turns_rejected += 1
                await self._degrade(
                    "stale_turn_dropped",
                    "That took too long to come back, so I did not act on it. "
                    "Please say it again if you still want it.",
                )
                return
            self.metrics.turns += 1
            generation = self.speech_generation.current
            try:
                reply = await self._turn_handler.handle_turn(turn, self._identity)
            except Exception:
                log.exception("turn handler failed for turn %d", turn.turn_id)
                await self._degrade(
                    "reasoning_failed",
                    "The assistant could not process that. Search and checkout still work "
                    "from the screen; no approval, payment or refund state changed.",
                )
                return
            if not self.speech_generation.is_current(generation):
                return  # barge-in arrived while reasoning: say nothing

            utterances: list[AgentReply] = []
            if reply.decision_card is not None:
                # Server-authored, from the platform's own decision card. Spoken first: a
                # buyer hears the settled fact before any commentary about it.
                card = render_decision_card(reply.decision_card, locale=reply.locale)
                utterances.append(
                    AgentReply(
                        text=card.text,
                        deterministic=True,
                        locale=str(card.locale),
                        turn_id=turn.turn_id,
                        speech_generation=generation,
                        template_id=card.template_id,
                        template_version=card.template_version,
                        fields=dict(card.fields),
                    )
                )
            if reply.decision is not None:
                rendered = render_decision(
                    reply.decision,
                    locale=reply.locale,
                    amount=reply.amount,
                    previous_amount=reply.previous_amount,
                )
                utterances.append(
                    AgentReply(
                        text=rendered.text,
                        deterministic=True,
                        locale=str(rendered.locale),
                        turn_id=turn.turn_id,
                        speech_generation=generation,
                        template_id=rendered.template_id,
                        template_version=rendered.template_version,
                        fields=dict(rendered.fields),
                    )
                )
            if reply.text:
                utterances.append(
                    AgentReply(
                        text=reply.text,
                        deterministic=False,
                        locale=str(reply.locale),
                        turn_id=turn.turn_id,
                        speech_generation=generation,
                        offer=None if reply.offer is None else dict(reply.offer),
                    )
                )
            # Text exists before speech, always (19.1): every reply is on screen first.
            for utterance in utterances:
                await self._send(utterance)
            if not utterances:
                return

            if self.stt is not None:
                self.stt.echo_gate.start_speaking()
            try:
                await self._speak_utterances(utterances, generation, reply.grounded_amounts_minor)
            finally:
                # Whatever happened, the gate must end up released or on its bounded
                # hold. Left engaged with no send-complete recorded, ECHO_GATE_MAX_HOLD_S
                # can never fire and every microphone frame becomes silence forever.
                if self.stt is not None and self.stt.echo_gate.speaking:
                    self.stt.echo_gate.on_server_send_complete()

    # ---- the approval card, read aloud (19.11) ------------------------------------------

    async def _run_read_card(self, request: ReadCard) -> None:
        """Read one card from the trusted server, speak it, then listen for a word.

        Under the turn lock, so a reading never overlaps a reply. The order inside is the
        order that makes consent mean something: the card is read from the server (never
        from the frame), the text is on the wire before any audio, the window opens only
        once every byte of the reading has been sent, and a reading that was cut short,
        failed to synthesise or produced nothing opens no window at all.
        """
        async with self._turn_lock:
            # A new reading retires the old window before anything is spoken, so no open
            # window ever refers to a card other than the last one read.
            await self._close_consent("superseded")
            if self._card_reader is None or self.stt is None or self.stt.degraded:
                await self._degrade(
                    "card_unavailable",
                    "Voice approval is not available on this connection, so a spoken yes "
                    "could not be heard. Nothing was read aloud. The button works.",
                )
                return
            checkout_id = str(request.checkout_id)
            try:
                card = await self._card_reader.read_card(checkout_id, request.version)
            except CardUnavailableError as exc:
                await self._degrade(
                    "card_unavailable",
                    f"The approval card could not be read: {exc}. Nothing was read aloud "
                    "and no consent window opened. The button works.",
                )
                return
            self.metrics.card_reads += 1
            generation = self.speech_generation.current
            locale = Locale(request.locale)
            reading = render_consent_reading(
                checkout_id=card.checkout_id,
                version=card.version,
                content_hash=card.content_hash,
                amount=Money(minor=card.amount_minor, currency=card.currency),
                locale=locale,
            )
            # Negative, like typed turns: a reading is server-originated and must never
            # share an id with a voice turn.
            self._text_turn_seq += 1
            utterance = AgentReply(
                text=reading.text,
                deterministic=True,
                locale=str(reading.locale),
                turn_id=-self._text_turn_seq,
                speech_generation=generation,
                template_id=reading.template_id,
                template_version=reading.template_version,
                fields=dict(reading.fields),
            )
            # Text before speech (19.1), then the binding fields, then the audio.
            await self._send(utterance)
            await self._send(
                CardRead(
                    checkout_id=card.checkout_id,
                    version=card.version,
                    content_hash=card.content_hash,
                    amount_minor=card.amount_minor,
                    currency=card.currency,
                    locale=str(reading.locale),
                    template_id=reading.template_id,
                    template_version=reading.template_version,
                    speech_generation=generation,
                )
            )
            self.stt.echo_gate.start_speaking()
            try:
                spoken = await self._speak_utterances(
                    [utterance], generation, frozenset({card.amount_minor})
                )
            finally:
                if self.stt.echo_gate.speaking:
                    self.stt.echo_gate.on_server_send_complete()
            if not spoken.complete or not self.speech_generation.is_current(generation):
                # Cancelled or failed readings already produced their own frame
                # (``interrupted`` or ``tts_failed``). A reading that produced no audio
                # with no failure is the one case nothing else would have named.
                if spoken.chunks == 0 and not spoken.tts_failed and not spoken.cancelled:
                    await self._degrade(
                        "card_unavailable",
                        "The reading produced no speech, so no consent window opened. "
                        "The button works.",
                    )
                return
            # The utterance that was in flight when the reading ended began before the
            # buyer had heard the amount; it is named so its final cannot count.
            excluded = self.stt.transcript.turn_id if self.stt.transcript.held else None
            window = self.consent.open(
                consent_id=str(uuid.uuid7()),
                card=card,
                speech_generation=generation,
                excluded_turn_id=excluded,
            )
            await self._send(
                ConsentListening(
                    consent_id=window.consent_id,
                    closes_in_s=self.consent.window_s,
                    speech_generation=generation,
                )
            )
            self._spawn(
                self._expire_consent(window.consent_id), name=f"voice-consent-{window.consent_id}"
            )

    async def _expire_consent(self, consent_id: str) -> None:
        """Make a window that lapsed unanswered visible (19.12): silence is never the end."""
        await asyncio.sleep(self.consent.window_s)
        window = self.consent.close_if(consent_id, "expired")
        if window is not None:
            await self._send(ConsentClosed(consent_id=consent_id, reason="expired"))

    async def _speak_utterances(
        self,
        utterances: list[AgentReply],
        generation: int,
        grounded_amounts_minor: frozenset[int],
    ) -> SpokenOutcome:
        """Synthesise and send each utterance. The caller owns the echo gate's lifetime."""
        await self._send(SpeechStart(speech_generation=generation))
        chunks = 0
        cancelled = False
        tts_failed = False
        for utterance in utterances:
            # The screen gets the model's Markdown; the voice gets the words. Stripping the
            # markup here, after the frame was sent, keeps the two in step on every amount
            # and spares the buyer a recital of asterisks.
            result: SpeakResult = await self.speaker.speak(
                plain_for_speech(utterance.text),
                locale=Locale(utterance.locale),
                deterministic=utterance.deterministic,
                generation=generation,
                grounded_amounts_minor=grounded_amounts_minor,
            )
            if result.refused.refused_any and not utterance.deterministic:
                # A refusal is silence where a sentence would have been, so it has to
                # be visible: the buyer reads the text on screen and is told the
                # assistant would not say it aloud (19.12).
                await self._degrade(
                    "speech_guard_refused",
                    "Some of that reply is shown on screen but not spoken aloud: "
                    "amounts and payment outcomes are only spoken when the server "
                    "confirmed them.",
                )
            chunks += result.chunks_sent
            if result.tts_failed:
                tts_failed = True
                await self._degrade(
                    "tts_failed",
                    "Speech synthesis failed. The exact text is shown on screen.",
                )
                break
            if result.cancelled:
                cancelled = True
                break
        if self.stt is not None:
            self.stt.echo_gate.on_server_send_complete()
            if chunks == 0:
                # Nothing was sent, so no playback will ever end: release now.
                self.stt.echo_gate.on_client_playback_ended()
        await self._send(
            SpeechEnd(speech_generation=generation, chunks=chunks, cancelled=cancelled)
        )
        return SpokenOutcome(chunks=chunks, cancelled=cancelled, tts_failed=tts_failed)

    # ---- SpeechSink ------------------------------------------------------------------

    async def send_chunk(self, chunk: SpeechChunk) -> None:
        """One header, then exactly its binary frame, with nothing in between.

        The client sizes its next read from ``byte_length``, so anything that slipped
        between the two would be read as audio. Three independent task trees write to this
        socket -- the receive loop, the recognizer's listener callbacks and the turn task
        -- and a real socket write suspends, so the pair is taken under the same lock every
        other write uses. The in-memory transport used by most tests never suspends, which
        is exactly why this could not be caught there.
        """
        async with self._send_lock:
            await self._transport.send_frame(
                SpeechChunkHeader(
                    seq=chunk.seq,
                    speech_generation=chunk.generation,
                    text=chunk.text,
                    sample_rate_hz=chunk.sample_rate_hz,
                    byte_length=len(chunk.pcm),
                    deterministic=chunk.deterministic,
                )
            )
            await self._transport.send_audio(chunk.pcm)

    # ---- degradation -----------------------------------------------------------------

    async def _send(self, frame: ServerFrame) -> None:
        """Every frame this pipeline writes goes through here, under one lock.

        Serialising the writes is what lets :meth:`send_chunk` guarantee that a
        ``speech_chunk`` header is followed by its own binary frame and nothing else.
        """
        async with self._send_lock:
            await self._transport.send_frame(frame)

    async def _degrade(self, kind: DegradationKind, message: str) -> None:
        """Silent degradation is a defect (19.12): every degraded path is a frame."""
        self.metrics.degradations += 1
        log.warning("voice degradation %s for session %s: %s", kind, self._session_id, message)
        await self._send(Degradation(kind=kind, message=message))

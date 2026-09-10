"""TranscribeSession: one recognition stream that outlives the utterance (19.3, 19.8).

The commerce session never rotates. Only the Google connection does, before the provider's
10-minute stream limit, make-before-break: the next connection is open and receiving
before the frame writer switches and the previous connection is drained and closed.

A generation counter increments on every rotation and reconnect. Every loop re-checks the
generation after every await, and a result arriving from an old generation is dropped
before it can reach a listener. That is what makes a rotation invisible to the buyer and
keeps a stale connection from settling a turn the new one is already transcribing.

Degradation is visible (19.12): when reconnects are exhausted the listener is told, and
the caller keeps text input working. Callback exceptions are logged at ``exception``,
never swallowed at ``debug`` (19.13).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final, Protocol

from ..clock import Clock
from ..constants import (
    CONNECT_TIMEOUT_S,
    MAX_RECONNECT_ATTEMPTS,
    RECONNECT_BACKOFF_MAX_S,
    RECONNECT_BACKOFF_START_S,
    ROTATION_DRAIN_S,
    STREAM_ROTATION_MARGIN_S,
)
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
from .queue import FreshAudioQueue
from .transcript import TranscriptState, TranscriptTurn

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

#: How often the rotation watcher re-reads the clock while waiting for the margin. It is a
#: poll, not a timer, because the deadline is measured on the injected :class:`Clock`: a
#: test advances a ``FakeClock`` and gets the rotation deterministically, instead of racing
#: a wall-clock timer. In production the tick only decides how late a rotation may be, and
#: a quarter of a second against a nine-minute margin costs nothing.
ROTATION_TICK_S: Final[float] = 0.25


class SttListener(Protocol):
    """What the pipeline hears from the recognizer. Every method may be async."""

    async def on_partial(self, turn: TranscriptTurn) -> None: ...

    async def on_final(self, turn: TranscriptTurn) -> None: ...

    async def on_activity(self, started: bool) -> None: ...

    async def on_rotated(self, generation: int) -> None: ...

    async def on_stt_degraded(self, kind: str, detail: str) -> None:
        """``kind`` is one of ``stt_connection_lost``, ``stt_rotation_failed``,
        ``stt_unavailable`` (19.12 table)."""
        ...


@dataclass(slots=True)
class SttMetrics:
    """Surfaced counters (19.13)."""

    rotations: int = 0
    reconnects: int = 0
    frames_sent: int = 0
    stale_results_dropped: int = 0
    go_aways: int = 0
    connect_failures: int = 0
    frames_dropped_at_seam: int = 0


class TranscribeSession:
    """Owns the queue, the echo gate, the transcript state and the connection generation."""

    def __init__(
        self,
        *,
        factory: LiveSttFactory,
        listener: SttListener,
        clock: Clock,
        queue: FreshAudioQueue | None = None,
        echo_gate: EchoGate | None = None,
        transcript: TranscriptState | None = None,
        rotation_margin_s: float = STREAM_ROTATION_MARGIN_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        backoff_start_s: float = RECONNECT_BACKOFF_START_S,
        backoff_max_s: float = RECONNECT_BACKOFF_MAX_S,
        max_reconnect_attempts: int = MAX_RECONNECT_ATTEMPTS,
        rotation_drain_s: float = ROTATION_DRAIN_S,
        rotation_tick_s: float = ROTATION_TICK_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._factory = factory
        self._listener = listener
        self._clock = clock
        self.queue = queue if queue is not None else FreshAudioQueue(clock=clock)
        self.echo_gate = echo_gate if echo_gate is not None else EchoGate(clock=clock)
        self.transcript = transcript if transcript is not None else TranscriptState(clock=clock)
        self._rotation_margin_s = rotation_margin_s
        self._connect_timeout_s = connect_timeout_s
        self._backoff_start_s = backoff_start_s
        self._backoff_max_s = backoff_max_s
        self._max_reconnect_attempts = max_reconnect_attempts
        self._rotation_drain_s = rotation_drain_s
        self._rotation_tick_s = rotation_tick_s
        # ``sleep`` is the BACKOFF seam only. The rotation deadline is measured on the
        # clock: one seam serving both meant a test that made backoff instant also made
        # the rotation timer instant, and the session rotated in a hot loop.
        self._sleep = sleep

        self.metrics = SttMetrics()
        self._generation = 0
        self._current: LiveSttSession | None = None
        self._resumption_handle: str | None = None
        self._supervisor: asyncio.Task[None] | None = None
        #: Teardown tasks, held so the garbage collector cannot cancel a socket close.
        self._background: set[asyncio.Task[None]] = set()
        #: The current connection's three loops. ``asyncio.wait`` does NOT cancel the
        #: futures it waits on, so cancelling only the supervisor left the send loop
        #: blocked on the queue forever and the rotation watcher polling the clock for a
        #: further nine minutes -- both holding this session, its queue and its transcript
        #: alive, once per closed voice session.
        self._loops: tuple[asyncio.Task[None], ...] = ()
        #: Generation -> the instant its drain window closes. A rotation bumps the
        #: generation synchronously, before the drain task can run, so without this every
        #: result the previous connection delivered was dropped as stale and the drain was
        #: a delay before closing a socket rather than a chance to finish an utterance.
        self._draining: dict[int, float] = {}
        self._connected = asyncio.Event()
        self._rotate_now = asyncio.Event()
        self._stopping = False
        self._degraded = False
        #: How old the most recent frame actually sent already was. A healthy stream keeps
        #: this near zero; a backlog being worked off after a reconnect makes it large,
        #: and that is what makes a settled transcript stale (19.4).
        self._last_audio_age_s = 0.0

    # ---- public surface --------------------------------------------------------------

    @property
    def generation(self) -> int:
        """Current connection generation. Increments on every rotation and reconnect."""
        return self._generation

    @property
    def connected(self) -> bool:
        return self._current is not None and not self._degraded

    @property
    def degraded(self) -> bool:
        return self._degraded

    def feed(self, pcm: bytes) -> None:
        """Microphone path: gate, then queue. Never blocks, never raises (19.4, 19.6)."""
        self.queue.feed(self.echo_gate.gate(pcm))

    def request_rotation(self) -> None:
        """Rotate early (a ``go_away``, or an operator request)."""
        self._rotate_now.set()

    async def start(self) -> None:
        """Start the supervisor and wait at most ``connect_timeout_s`` for a connection.

        A timeout logs a warning and returns normally, leaving the retry loop running
        underneath (19.3): raising would drop the whole voice session for a transient
        blip, which is exactly when the buyer must not be dropped.
        """
        if self._supervisor is not None:
            raise RuntimeError("TranscribeSession already started")
        self._supervisor = asyncio.create_task(self._supervise(), name="stt-supervisor")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self._connect_timeout_s)
        except TimeoutError:
            log.warning(
                "STT connect did not complete within %.1fs; retry loop continues underneath",
                self._connect_timeout_s,
            )

    async def stop(self) -> None:
        """Stop everything this session started, and wait for it to actually be stopped."""
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        # The per-connection loops are not children of the supervisor, so cancelling it
        # orphans them. Cancel and await each one here.
        outstanding = [*self._loops, *self._background]
        self._loops = ()
        for task in outstanding:
            task.cancel()
        if outstanding:
            await asyncio.gather(*outstanding, return_exceptions=True)
        self._background.clear()
        if self._current is not None:
            with contextlib.suppress(Exception):
                await self._current.close()
            self._current = None

    # ---- supervisor ------------------------------------------------------------------

    def _bump_generation(self) -> int:
        self._generation += 1
        return self._generation

    async def _open(self, *, resume: bool) -> LiveSttSession:
        handle = self._resumption_handle if resume else None
        return await asyncio.wait_for(
            self._factory.open(resumption_handle=handle), timeout=self._connect_timeout_s
        )

    async def _connect_with_backoff(self) -> LiveSttSession | None:
        """Exponential backoff from the start to the ceiling; bounded and surfaced."""
        delay = self._backoff_start_s
        for attempt in range(1, self._max_reconnect_attempts + 1):
            try:
                return await self._open(resume=True)
            except Exception as exc:
                self.metrics.connect_failures += 1
                log.warning(
                    "STT connect attempt %d/%d failed: %s",
                    attempt,
                    self._max_reconnect_attempts,
                    exc,
                )
                if attempt == self._max_reconnect_attempts:
                    break
                await self._sleep(delay)
                delay = min(delay * 2, self._backoff_max_s)
        return None

    async def _supervise(self) -> None:
        session = await self._connect_with_backoff()
        first = True
        while session is not None and not self._stopping:
            # The rotation deadline is anchored HERE, when this connection becomes the
            # current one -- not inside the trigger task, which starts a few scheduler
            # ticks later. Anchoring it there let time that had already elapsed count
            # towards the next margin instead of this one.
            rotate_at = self._clock.now() + self._rotation_margin_s
            gen = self._bump_generation()
            self._current = session
            self._degraded = False
            self._connected.set()
            # Any send loop of a previous generation wakes, re-checks and exits without
            # popping, so the writer switch loses no frame.
            self.queue.wake()
            if not first:
                # The replacement connection never heard the audio the previous one did,
                # so whatever hypothesis was in flight is carried across the seam rather
                # than replaced by a fragment that starts mid-sentence (19.14).
                carried = self.transcript.carry_over(gen - 1)
                if carried:
                    log.info("carried %d characters across the rotation seam", len(carried))
                await self._notify_rotated(gen)
            if first:
                ready = getattr(self._listener, "on_stt_ready", None)
                if ready is not None:
                    await self._safe_callback(ready(), "on_stt_ready")
            first = False

            self._rotate_now.clear()
            send_task = asyncio.create_task(self._send_loop(session, gen), name=f"stt-send-{gen}")
            recv_task = asyncio.create_task(
                self._receive_loop(session, gen), name=f"stt-recv-{gen}"
            )
            rot_task = asyncio.create_task(self._rotation_trigger(rotate_at), name=f"stt-rot-{gen}")
            self._loops = (send_task, recv_task, rot_task)
            done, _ = await asyncio.wait(
                {send_task, recv_task, rot_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if rot_task in done and not self._stopping:
                # Make-before-break (19.3): open the next connection first.
                replacement = await self._open_replacement()
                if replacement is not None:
                    self.metrics.rotations += 1
                    self._draining = {gen: self._clock.now() + self._rotation_drain_s}
                    # The writer switches only when the generation bumps, at the top of
                    # the loop. The old send loop exits at its next generation re-check;
                    # the old receive loop keeps draining but its results are dropped.
                    session = replacement
                    self._retire(send_task, recv_task, rot_task, self._current, drain=True)
                    continue
                await self._notify_degraded(
                    "stt_rotation_failed", "rotation failed; reconnecting cold"
                )
            else:
                # send or receive finished: the connection is gone.
                self.metrics.reconnects += 1
                await self._notify_degraded("stt_connection_lost", "reconnecting")

            self._retire(send_task, recv_task, rot_task, self._current, drain=False)
            self._current = None
            self._connected.clear()
            session = await self._connect_with_backoff()

        if not self._stopping:
            self._degraded = True
            self._current = None
            self._connected.set()  # release start(); the caller sees ``degraded``
            await self._notify_degraded(
                "stt_unavailable", "speech recognition unavailable; text input keeps working"
            )

    async def _open_replacement(self) -> LiveSttSession | None:
        """Resumed replacement first; a fresh cold connection if that fails (19.12)."""
        try:
            return await self._open(resume=True)
        except Exception as exc:
            log.warning("STT rotation with resumption failed: %s; trying cold", exc)
        try:
            return await self._open(resume=False)
        except Exception as exc:
            log.warning("STT cold rotation failed: %s", exc)
            return None

    def _retire(
        self,
        send_task: asyncio.Task[None],
        recv_task: asyncio.Task[None],
        rot_task: asyncio.Task[None],
        session: LiveSttSession | None,
        *,
        drain: bool,
    ) -> None:
        rot_task.cancel()
        if drain and session is not None:
            # The old send loop is not cancelled: cancelling it mid-send would drop the
            # frame in flight. It exits at its next generation re-check.
            self._spawn(self._drain_and_close(recv_task, session), name="stt-drain-previous")
        else:
            send_task.cancel()
            recv_task.cancel()
            if session is not None:
                self._spawn(self._close_quietly(session), name="stt-close-previous")

    def _spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> None:
        """Run a teardown coroutine in the background, holding a strong reference to it.

        A task referenced only by the event loop can be garbage-collected mid-flight, and
        the one thing these tasks do is close a socket that would otherwise leak.
        """
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _drain_and_close(
        self, recv_task: asyncio.Task[None], session: LiveSttSession
    ) -> None:
        """Let the previous connection finish delivering, then close it (19.3)."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(recv_task), timeout=self._rotation_drain_s)
        recv_task.cancel()
        await self._close_quietly(session)

    @staticmethod
    async def _close_quietly(session: LiveSttSession) -> None:
        with contextlib.suppress(Exception):
            await session.close()

    async def _rotation_trigger(self, deadline: float) -> None:
        """Return at ``deadline`` on the injected clock, or as soon as an early trigger fires.

        The margin is measured against the clock rather than slept through, so a rotation
        is a property of elapsed time on this connection and a test can advance a
        ``FakeClock`` instead of racing a real timer.
        """
        while True:
            remaining = deadline - self._clock.now()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._rotate_now.wait(), timeout=min(remaining, self._rotation_tick_s)
                )
            except TimeoutError:
                continue
            return

    # ---- per-connection loops ----------------------------------------------------------

    async def _send_loop(self, session: LiveSttSession, gen: int) -> None:
        """Drain fresh frames into this connection while it is the current generation."""
        while self._generation == gen:
            await self.queue.wait_fresh()
            if self._generation != gen:  # re-check after the await (19.8)
                return
            frame = self.queue.pop_fresh()
            if frame is None:
                continue
            self._last_audio_age_s = max(0.0, self._clock.now() - frame.enqueued_at)
            try:
                await session.send_audio(frame.pcm)
            except Exception as exc:
                if self._generation == gen:
                    # Still the writer: nobody else is popping, so the head is where this
                    # frame belongs and a reconnect loses no audio (19.4).
                    self.queue.push_front(frame)
                else:
                    # A rotation happened while this send was in flight, which is the
                    # EXPECTED path at the seam: the old socket is closing underneath us.
                    # The new generation has already popped past this frame, so putting it
                    # back at the head would splice 100 ms of older speech into the middle
                    # of the current utterance. Losing it is the better outcome; the buyer
                    # can repeat themselves, but they cannot un-hear a reordered sentence.
                    self.metrics.frames_dropped_at_seam += 1
                log.warning("STT send failed on generation %d: %s", gen, exc)
                return
            self.metrics.frames_sent += 1

    async def _receive_loop(self, session: LiveSttSession, gen: int) -> None:
        try:
            async for event in session.receive():
                if self._generation != gen and not self._is_draining(gen):
                    # A late result from an old generation, past its drain window.
                    # Dropped, counted, never dispatched: the new connection owns the
                    # transcript now.
                    self.metrics.stale_results_dropped += 1
                    continue
                await self._dispatch(event, gen)
        except SttConnectionLostError as exc:
            if self._generation == gen:
                log.warning("STT connection lost on generation %d: %s", gen, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("STT receive loop failed on generation %d", gen)

    def _is_draining(self, gen: int) -> bool:
        """Whether ``gen`` may still deliver: it is the previous connection, mid-drain.

        This is what makes a rotation lossless in the direction that matters. An utterance
        the previous connection was still settling gets to finish and become a turn,
        instead of being dropped and leaving its carried prefix to contaminate the next
        thing the buyer says.
        """
        deadline = self._draining.get(gen)
        if deadline is None:
            return False
        if self._clock.now() >= deadline:
            del self._draining[gen]
            return False
        return True

    async def _dispatch(self, event: SttEvent, gen: int) -> None:
        match event:
            case SttInterim(text=text):
                turn = self.transcript.apply_interim(text, gen, self._last_audio_age_s)
                await self._safe_callback(self._listener.on_partial(turn), "on_partial")
            case SttFinal(text=text):
                final = self.transcript.apply_final(text, gen, self._last_audio_age_s)
                if final is not None:
                    await self._safe_callback(self._listener.on_final(final), "on_final")
            case SttActivity(started=started):
                await self._safe_callback(self._listener.on_activity(started), "on_activity")
            case SttGoAway(time_left_s=time_left):
                self.metrics.go_aways += 1
                log.info("STT go_away (time left %s); rotating early", time_left)
                self._rotate_now.set()
            case SttResumption(handle=handle, resumable=resumable):
                if resumable and handle:
                    self._resumption_handle = handle
            case SttSetupComplete():
                pass

    async def _safe_callback(self, awaitable: Awaitable[None], name: str) -> None:
        """A swallowed ``on_final`` exception loses the turn silently (19.13): log loudly."""
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("STT listener callback %s failed", name)

    async def _notify_rotated(self, generation: int) -> None:
        await self._safe_callback(self._listener.on_rotated(generation), "on_rotated")

    async def _notify_degraded(self, kind: str, detail: str) -> None:
        await self._safe_callback(self._listener.on_stt_degraded(kind, detail), "on_stt_degraded")

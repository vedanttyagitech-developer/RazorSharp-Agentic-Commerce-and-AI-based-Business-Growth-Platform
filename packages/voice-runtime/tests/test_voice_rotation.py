"""19.3 and 19.8: make-before-break rotation before the 10-minute limit, no lost frames,
stale-generation results dropped, reconnect without draining, visible degradation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.constants import PROVIDER_STREAM_LIMIT_S, STREAM_ROTATION_MARGIN_S
from voice_runtime.stt.events import SttFinal, SttGoAway, SttInterim, SttResumption
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.stt.session import TranscribeSession
from voice_runtime.stt.transcript import TranscriptTurn


class Listener:
    def __init__(self, *, raise_on_final: bool = False) -> None:
        self.partials: list[str] = []
        self.finals: list[TranscriptTurn] = []
        self.rotations: list[int] = []
        self.degradations: list[tuple[str, str]] = []
        self.raise_on_final = raise_on_final

    async def on_partial(self, turn: TranscriptTurn) -> None:
        self.partials.append(turn.text)

    async def on_final(self, turn: TranscriptTurn) -> None:
        self.finals.append(turn)
        if self.raise_on_final:
            raise RuntimeError("listener exploded")

    async def on_activity(self, started: bool) -> None:
        pass

    async def on_rotated(self, generation: int) -> None:
        self.rotations.append(generation)

    async def on_stt_degraded(self, kind: str, detail: str) -> None:
        self.degradations.append((kind, detail))


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def no_sleep(_: float) -> None:
    return None


def make_session(
    factory: FakeSttFactory,
    listener: Listener,
    clock: FakeClock | None = None,
    **overrides: object,
) -> TranscribeSession:
    """A session whose only clock is a fake one, so rotation is driven, never awaited.

    ``sleep`` is the backoff seam and is made instant. The rotation margin is deliberately
    far beyond anything a test advances the clock to, so no test rotates by accident; the
    tests that want a rotation ask for one, or move the clock past the margin.
    """
    kwargs: dict[str, object] = {
        "rotation_margin_s": 1000.0,
        "connect_timeout_s": 1.0,
        "backoff_start_s": 0.0,
        "rotation_drain_s": 0.05,
        "rotation_tick_s": 0.005,
        "sleep": no_sleep,
    }
    kwargs.update(overrides)
    return TranscribeSession(
        factory=factory,
        listener=listener,
        clock=clock if clock is not None else FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_rotation_margin_is_below_the_provider_limit() -> None:
    assert STREAM_ROTATION_MARGIN_S == 540.0  # 9 minutes
    assert STREAM_ROTATION_MARGIN_S < PROVIDER_STREAM_LIMIT_S == 600.0


@pytest.mark.asyncio
async def test_rotation_is_make_before_break_and_loses_no_frames() -> None:
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener)
    await session.start()
    assert session.generation == 1
    first = factory.sessions[0]

    frames = [bytes([i]) * 4 for i in range(20)]
    for frame in frames[:10]:
        session.feed(frame)
    await wait_until(lambda: len(first.sent) == 10)

    first.emit(SttGoAway(time_left_s=5.0))  # early rotation trigger, not an error
    await wait_until(lambda: session.generation == 2)
    second = factory.sessions[1]
    assert not first.closed, "the previous connection is drained, not cut, at the seam"

    for frame in frames[10:]:
        session.feed(frame)
    await wait_until(lambda: len(second.sent) == 10)
    await wait_until(lambda: first.closed)

    assert first.sent + second.sent == frames, "every frame delivered exactly once, in order"
    assert listener.rotations == [2]
    assert session.metrics.rotations == 1
    assert session.metrics.go_aways == 1
    await session.stop()


@pytest.mark.asyncio
async def test_the_previous_connection_still_delivers_inside_the_drain_window() -> None:
    """That is what draining IS. An utterance it was still settling gets to finish.

    Without this, the generation bump -- which happens synchronously, before the drain
    task can run -- dropped 100% of what the previous connection produced, and the drain
    was a delay before closing a socket rather than a chance to complete a turn. Worse,
    the hypothesis carried across the seam then had nothing to clear it and welded itself
    onto whatever the buyer said next.
    """
    clock = FakeClock()
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener, clock, rotation_drain_s=2.0)
    await session.start()
    first = factory.sessions[0]
    first.emit(SttInterim("I want two litres of milk"))
    await wait_until(lambda: listener.partials == ["I want two litres of milk"])

    session.request_rotation()
    await wait_until(lambda: session.generation == 2)

    # The old connection settles the utterance it was already working on.
    first.emit(SttFinal("I want two litres of milk"))
    await wait_until(lambda: len(listener.finals) == 1)
    assert listener.finals[0].text == "I want two litres of milk"

    # And the carried prefix is gone, so the buyer's next sentence stands alone.
    factory.sessions[1].emit(SttFinal("Add bread."))
    await wait_until(lambda: len(listener.finals) == 2)
    assert listener.finals[1].text == "Add bread.", "two intents, two turns"
    await session.stop()


@pytest.mark.asyncio
async def test_results_from_a_stale_generation_are_dropped_once_the_drain_closes() -> None:
    clock = FakeClock()
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener, clock, rotation_drain_s=2.0)
    await session.start()
    first = factory.sessions[0]
    session.request_rotation()
    await wait_until(lambda: session.generation == 2)
    clock.advance(3.0)  # the drain window has closed

    first.emit(SttInterim("late interim from the old connection"))
    first.emit(SttFinal("late final from the old connection"))
    await wait_until(lambda: session.metrics.stale_results_dropped == 2)
    assert listener.partials == []
    assert listener.finals == []

    factory.sessions[1].emit(SttFinal("fresh final from the new connection"))
    await wait_until(lambda: len(listener.finals) == 1)
    assert listener.finals[0].text == "fresh final from the new connection"
    assert listener.finals[0].generation == 2
    await session.stop()


@pytest.mark.asyncio
async def test_a_prefix_whose_utterance_never_settled_expires_instead_of_contaminating() -> None:
    """The carried prefix is only for the utterance in flight at the seam.

    If that utterance's final is lost entirely, nothing clears the prefix -- and it would
    prepend itself to the buyer's next, unrelated sentence, so two intents would reach the
    agent as one message.
    """
    clock = FakeClock()
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener, clock, rotation_drain_s=0.0)
    await session.start()
    factory.sessions[0].emit(SttInterim("I want two litres of milk"))
    await wait_until(lambda: listener.partials != [])

    session.request_rotation()
    await wait_until(lambda: session.generation == 2)
    clock.advance(30.0)  # the utterance never settled; the buyer moved on

    factory.sessions[1].emit(SttFinal("Add bread."))
    await wait_until(lambda: len(listener.finals) == 1)
    assert listener.finals[0].text == "Add bread.", "no stale prefix welded on"
    assert session.transcript.prefixes_expired == 1
    await session.stop()


@pytest.mark.asyncio
async def test_rotation_does_not_fire_before_the_configured_margin() -> None:
    """Elapsed time short of the margin rotates nothing: the stream outlives the utterance."""
    clock = FakeClock()
    factory = FakeSttFactory()
    session = make_session(factory, Listener(), clock, rotation_margin_s=540.0)
    await session.start()
    clock.advance(539.0)
    await asyncio.sleep(0.05)  # several rotation ticks
    assert session.generation == 1
    assert factory.open_calls == 1
    await session.stop()


@pytest.mark.asyncio
async def test_rotation_fires_once_the_margin_elapses() -> None:
    """Crossing the margin rotates, well inside the provider's 10-minute stream limit."""
    clock = FakeClock()
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener, clock, rotation_margin_s=540.0)
    await session.start()
    clock.advance(540.0)
    await wait_until(lambda: session.generation == 2)
    assert factory.open_calls == 2
    assert listener.rotations == [2]
    assert session.metrics.rotations == 1
    assert clock.now() < PROVIDER_STREAM_LIMIT_S + 1_000.0  # the margin is what rotated us
    await session.stop()


@pytest.mark.asyncio
async def test_resumption_handle_is_carried_across_rotation() -> None:
    factory = FakeSttFactory()
    session = make_session(factory, Listener())
    await session.start()
    factory.sessions[0].emit(SttResumption(handle="handle-1", resumable=True))
    await asyncio.sleep(0.01)
    session.request_rotation()
    await wait_until(lambda: session.generation == 2)
    assert factory.sessions[0].resumption_handle is None
    assert factory.sessions[1].resumption_handle == "handle-1"
    await session.stop()


@pytest.mark.asyncio
async def test_rotation_falls_back_to_a_cold_connection_then_reconnects() -> None:
    # open #1 ok, rotation resume attempt fails, cold attempt succeeds.
    factory = FakeSttFactory(fail_opens=[False, True, False])
    listener = Listener()
    session = make_session(factory, listener)
    await session.start()
    factory.sessions[0].emit(SttResumption(handle="h", resumable=True))
    await asyncio.sleep(0.01)
    session.request_rotation()
    await wait_until(lambda: session.generation == 2)
    assert factory.sessions[1].resumption_handle is None, "cold: no handle"
    assert session.metrics.rotations == 1
    await session.stop()


@pytest.mark.asyncio
async def test_connection_loss_reconnects_without_draining_the_queue() -> None:
    factory = FakeSttFactory(open_delay_s=0.05)
    listener = Listener()
    session = make_session(factory, listener)
    await session.start()
    first = factory.sessions[0]
    session.feed(b"\x01\x01")
    await wait_until(lambda: len(first.sent) == 1)

    first.drop_connection()
    await wait_until(lambda: ("stt_connection_lost", "reconnecting") in listener.degradations)
    # Audio arriving mid-reconnect is still there afterwards.
    session.feed(b"\x02\x02")
    session.feed(b"\x03\x03")
    await wait_until(lambda: session.generation == 2)
    second = factory.sessions[1]
    await wait_until(lambda: len(second.sent) == 2)
    assert second.sent == [b"\x02\x02", b"\x03\x03"]
    assert session.metrics.reconnects == 1
    await session.stop()


@pytest.mark.asyncio
async def test_exhausted_reconnects_degrade_visibly() -> None:
    factory = FakeSttFactory(fail_opens=[True, True, True])
    listener = Listener()
    session = make_session(factory, listener, max_reconnect_attempts=2)
    await session.start()
    await wait_until(lambda: session.degraded)
    assert factory.open_calls == 2
    assert session.metrics.connect_failures == 2
    assert listener.degradations[-1][0] == "stt_unavailable"
    assert not session.connected
    session.feed(b"\x00\x00")  # still never raises
    await session.stop()


@pytest.mark.asyncio
async def test_connect_timeout_warns_and_returns_normally() -> None:
    factory = FakeSttFactory(open_delay_s=0.2)
    session = make_session(factory, Listener(), connect_timeout_s=0.02, max_reconnect_attempts=1)
    await session.start()  # must not raise
    await session.stop()


@pytest.mark.asyncio
async def test_echo_gate_is_applied_on_the_feed_path() -> None:
    factory = FakeSttFactory()
    session = make_session(factory, Listener())
    await session.start()
    session.echo_gate.start_speaking()
    loud = b"\x7f\x7f" * 100
    for _ in range(5):
        session.feed(loud)
    await wait_until(lambda: len(factory.sessions[0].sent) == 5)
    assert all(frame == bytes(len(loud)) for frame in factory.sessions[0].sent)
    await session.stop()


@pytest.mark.asyncio
async def test_listener_exceptions_are_logged_loudly_and_do_not_kill_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = FakeSttFactory()
    listener = Listener(raise_on_final=True)
    session = make_session(factory, listener)
    await session.start()
    with caplog.at_level(logging.WARNING, logger="voice_runtime.stt.session"):
        factory.sessions[0].emit(SttFinal("first"))
        await wait_until(lambda: len(listener.finals) == 1)
        factory.sessions[0].emit(SttFinal("second"))
        await wait_until(lambda: len(listener.finals) == 2)
    assert any(
        record.levelno >= logging.WARNING and "on_final" in record.getMessage()
        for record in caplog.records
    )
    assert session.connected
    await session.stop()


@pytest.mark.asyncio
async def test_a_rotation_mid_utterance_still_yields_one_coherent_final() -> None:
    """19.14. The replacement connection never heard the first half of the sentence.

    Make-before-break loses no audio frame, but it does hand the utterance to a recognizer
    that starts listening in the middle of it. Replacing the held hypothesis with that
    fragment would drop the first half of a sentence the buyer definitely said.
    """
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener)
    await session.start()

    factory.sessions[0].emit(SttInterim("add two litres of"))
    await wait_until(lambda: listener.partials == ["add two litres of"])

    session.request_rotation()
    await wait_until(lambda: session.generation == 2)

    # The new connection hears only what arrives after the seam.
    factory.sessions[1].emit(SttInterim("milk and some bread"))
    await wait_until(lambda: len(listener.partials) == 2)
    assert listener.partials[1] == "add two litres of milk and some bread"

    factory.sessions[1].emit(SttFinal("milk and some bread"))
    await wait_until(lambda: len(listener.finals) == 1)
    assert listener.finals[0].text == "add two litres of milk and some bread"
    await session.stop()


@pytest.mark.asyncio
async def test_a_rotation_between_utterances_carries_nothing() -> None:
    """The common case. A prefix invented from silence would prepend stale words to the
    buyer's next sentence, which is worse than the problem it solves."""
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener)
    await session.start()

    factory.sessions[0].emit(SttFinal("two litres of milk"))
    await wait_until(lambda: len(listener.finals) == 1)

    session.request_rotation()
    await wait_until(lambda: session.generation == 2)

    factory.sessions[1].emit(SttFinal("and some bread"))
    await wait_until(lambda: len(listener.finals) == 2)
    assert listener.finals[1].text == "and some bread", "no stale prefix"
    await session.stop()


@pytest.mark.asyncio
async def test_the_replace_rule_still_holds_inside_a_generation_after_a_rotation() -> None:
    """Across the seam the halves are joined once; within a connection, replace as ever."""
    factory = FakeSttFactory()
    listener = Listener()
    session = make_session(factory, listener)
    await session.start()

    factory.sessions[0].emit(SttInterim("add two"))
    await wait_until(lambda: len(listener.partials) == 1)
    session.request_rotation()
    await wait_until(lambda: session.generation == 2)

    factory.sessions[1].emit(SttInterim("litres"))
    factory.sessions[1].emit(SttInterim("litres of milk"))  # a revision, not an extension
    factory.sessions[1].emit(SttInterim(""))  # empty never clears
    await wait_until(lambda: len(listener.partials) == 4)

    assert listener.partials[1:] == [
        "add two litres",
        "add two litres of milk",
        "add two litres of milk",
    ]
    await session.stop()

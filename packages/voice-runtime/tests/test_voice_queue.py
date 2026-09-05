"""19.4: bound by dropping the oldest, never by draining; feed never blocks or raises."""

from __future__ import annotations

import asyncio

import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.stt.queue import FreshAudioQueue, QueuedFrame


def _drain(queue: FreshAudioQueue) -> list[bytes]:
    out: list[bytes] = []
    while (frame := queue.pop_fresh()) is not None:
        out.append(frame.pcm)
    return out


def test_evicts_oldest_first_under_overload() -> None:
    queue = FreshAudioQueue(clock=FakeClock(), max_frames=3)
    for i in range(5):
        queue.feed(bytes([i]))
    assert _drain(queue) == [b"\x02", b"\x03", b"\x04"]
    assert queue.evicted_overflow == 2
    assert queue.frames_in == 5


def test_capacity_is_bounded() -> None:
    queue = FreshAudioQueue(clock=FakeClock(), max_frames=8)
    for i in range(10_000):
        queue.feed(bytes([i % 256]))
    assert len(queue) == 8


def test_stale_frames_age_out_but_fresh_ones_survive_a_reconnect() -> None:
    clock = FakeClock()
    queue = FreshAudioQueue(clock=clock, max_age_s=4.0)
    queue.feed(b"old")
    clock.advance(3.0)
    queue.feed(b"fresh")
    clock.advance(2.0)  # "old" is 5 s old, "fresh" is 2 s old
    assert _drain(queue) == [b"fresh"]
    assert queue.evicted_stale == 1


def test_brief_gap_keeps_everything() -> None:
    clock = FakeClock()
    queue = FreshAudioQueue(clock=clock, max_age_s=4.0)
    queue.feed(b"a")
    queue.feed(b"b")
    clock.advance(1.5)  # a short reconnect
    assert _drain(queue) == [b"a", b"b"]
    assert queue.evicted_stale == 0


def test_there_is_no_drain_method() -> None:
    queue = FreshAudioQueue(clock=FakeClock())
    assert not hasattr(queue, "clear")
    assert not hasattr(queue, "drain")


def test_push_front_restores_head_with_original_timestamp() -> None:
    clock = FakeClock()
    queue = FreshAudioQueue(clock=clock, max_age_s=4.0)
    queue.feed(b"a")
    queue.feed(b"b")
    head = queue.pop_fresh()
    assert head is not None and head.pcm == b"a"
    queue.push_front(head)
    assert _drain(queue) == [b"a", b"b"]
    # The restored frame keeps its age and still expires.
    queue.push_front(QueuedFrame(b"z", clock.now() - 10.0))
    assert _drain(queue) == []
    assert queue.evicted_stale == 1


@pytest.mark.asyncio
async def test_wait_fresh_wakes_on_feed_and_on_wake() -> None:
    queue = FreshAudioQueue(clock=FakeClock())
    waiter = asyncio.create_task(queue.wait_fresh())
    await asyncio.sleep(0)
    assert not waiter.done()
    queue.feed(b"x")
    await asyncio.wait_for(waiter, 1.0)
    assert queue.pop_fresh() is not None

    waiter = asyncio.create_task(queue.wait_fresh())
    await asyncio.sleep(0)
    queue.wake()  # spurious wake: returns with nothing to pop
    await asyncio.wait_for(waiter, 1.0)
    assert queue.pop_fresh() is None


def test_constructor_rejects_degenerate_bounds() -> None:
    with pytest.raises(ValueError):
        FreshAudioQueue(clock=FakeClock(), max_frames=0)
    with pytest.raises(ValueError):
        FreshAudioQueue(clock=FakeClock(), max_age_s=0)

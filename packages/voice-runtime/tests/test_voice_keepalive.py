"""A silent socket must not be closed by something between the buyer and this gateway.

The session length here is deliberate: ``STREAM_ROTATION_MARGIN_S`` sits under the
provider's stream limit precisely so a buyer's conversation outlives Google's, with no
upper bound of its own. An intermediary does not know that. Cloudflare closes a proxied
WebSocket after 100 seconds with no traffic in *either* direction on its Free and Pro
plans, and a buyer who is thinking sends nothing at all -- so the gateway speaks first
rather than trusting the client to.

Two behaviours, and the first is the one that matters: the server sends unprompted. A
client-driven heartbeat would leave the socket at the mercy of a front end that forgot to
implement one, which is exactly the state this code was in -- the wire defined ``Ping`` and
the dispatch dropped it at ``case _: pass``.
"""

from __future__ import annotations

import asyncio

import pytest
from voice_runtime import pipeline as pipeline_module
from voice_runtime.clock import FakeClock
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.testing import MemoryTransport, an_identity
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.turn import TurnReply


class QuietHandler:
    """Answers nothing. The point of these tests is a socket with no conversation on it."""

    async def handle_turn(self, transcript: object, identity: object) -> TurnReply:
        return TurnReply(text="")


def build(transport: MemoryTransport) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=None,
        synthesizer=FakeSynthesizer(),
        turn_handler=QuietHandler(),
        identity=an_identity(),
        clock=FakeClock(),
        rotation_margin_s=10_000.0,
        connect_timeout_s=1.0,
        backoff_start_s=0.0,
    )


async def wait_until(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_a_silent_socket_is_kept_alive_without_the_client_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody speaks, nobody types, and the gateway still produces traffic."""
    monkeypatch.setattr(pipeline_module, "KEEPALIVE_INTERVAL_S", 0.01)
    transport = MemoryTransport()
    pipeline = build(transport)
    task = asyncio.create_task(pipeline.run())

    # No frames are pushed at all: this is a buyer sitting and thinking.
    await wait_until(lambda: len(transport.frames("pong")) >= 3)
    assert len(transport.frames("pong")) >= 3, "a silent socket produced no keepalive"

    transport.end()
    await task


@pytest.mark.asyncio
async def test_a_client_ping_is_answered_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression. `case _: pass` used to swallow this frame entirely."""
    # Long enough that nothing unprompted fires inside the assertion window, so a pong
    # here can only be the answer to the ping.
    monkeypatch.setattr(pipeline_module, "KEEPALIVE_INTERVAL_S", 3600.0)
    transport = MemoryTransport()
    pipeline = build(transport)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: transport.frames("degradation") != [])

    assert transport.frames("pong") == [], "something sent a pong before the ping"
    transport.push_text({"type": "ping"})
    await wait_until(lambda: len(transport.frames("pong")) == 1)

    transport.end()
    await task


@pytest.mark.asyncio
async def test_the_keepalive_carries_no_state_and_ends_with_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pong is inert: it must not open a turn, and it must not outlive the connection."""
    monkeypatch.setattr(pipeline_module, "KEEPALIVE_INTERVAL_S", 0.01)
    transport = MemoryTransport()
    pipeline = build(transport)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: len(transport.frames("pong")) >= 2)

    # Keepalives are not conversation: no turn was opened by any of them.
    assert transport.frames("turn_opened") == []
    assert transport.frames("transcript_final") == []

    transport.end()
    await task  # the task must finish: a keepalive that outlived the socket would hang here
    settled = len(transport.frames("pong"))
    await asyncio.sleep(0.05)
    assert len(transport.frames("pong")) == settled, "keepalive kept running after close"

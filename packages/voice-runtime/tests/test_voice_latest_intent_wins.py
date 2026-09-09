"""The most recent thing the buyer said is the one that gets answered.

A buyer types and speaks into one session and one context. What they must never get is an
answer to a question they have already replaced -- and before this, that is exactly what
happened: turns were serialised behind a lock and each one, on reaching the front of the
queue, was answered in the order it arrived. Ask for milk, wait through eight seconds of
model latency, type "actually, bread", and the shelf fills with milk first and bread after.

Two turns are not two conversations. The queue is a queue of *intents*, and an intent the
buyer has superseded is not one to spend a model call on, let alone to speak aloud.

WHAT IS DELIBERATELY NOT DONE HERE
----------------------------------
A running turn is not cancelled. ``handle_turn`` is one HTTP POST to the agent service, and
aborting the await stops the answer arriving without stopping the server producing it. So a
turn already in flight is allowed to finish and its *answer is discarded*; only turns that
have not started are skipped outright. Discarding is safe because an agent turn reads --
its cart changes are proposals the browser applies -- so a dropped reply drops its proposal
with it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.identity import VoiceIdentity
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.stt.events import SttFinal
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.stt.transcript import TranscriptTurn
from voice_runtime.testing import MemoryTransport, an_identity
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.turn import TurnReply


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


class GatedTurnHandler:
    """A handler whose every call blocks until the test releases it by name.

    The gate is what makes supersession reachable at all: without one, a turn completes
    before the next frame is read and no two intents are ever in flight together.
    """

    def __init__(self) -> None:
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def handle_turn(self, transcript: TranscriptTurn, identity: VoiceIdentity) -> TurnReply:
        text = transcript.text
        self.started.append(text)
        gate = self.gates.setdefault(text, asyncio.Event())
        await gate.wait()
        return TurnReply(text=f"answering {text}")

    def release(self, text: str) -> None:
        self.gates.setdefault(text, asyncio.Event()).set()


def build(
    *,
    transport: MemoryTransport,
    handler: GatedTurnHandler,
    factory: FakeSttFactory | None = None,
) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=factory,
        synthesizer=FakeSynthesizer(),
        turn_handler=handler,
        identity=an_identity(),
        clock=FakeClock(),
        rotation_margin_s=10_000.0,
        connect_timeout_s=1.0,
        backoff_start_s=0.0,
    )


def replies(transport: MemoryTransport) -> Sequence[str]:
    return [frame["text"] for frame in transport.frames("agent_reply")]


@pytest.mark.asyncio
async def test_a_queued_turn_the_buyer_has_replaced_is_never_reasoned_about() -> None:
    """Three typed turns, one in flight: the middle one costs nothing and says nothing."""
    transport = MemoryTransport()
    handler = GatedTurnHandler()
    pipeline = build(transport=transport, handler=handler, factory=None)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: transport.frames("degradation") != [])

    transport.push_text({"type": "text_input", "text": "milk"})
    await wait_until(lambda: handler.started == ["milk"])

    # Both arrive while "milk" is still reasoning, so both queue behind it.
    transport.push_text({"type": "text_input", "text": "bread"})
    transport.push_text({"type": "text_input", "text": "coffee"})
    await wait_until(lambda: len(transport.frames("transcript_final")) == 3)

    handler.release("milk")
    handler.release("coffee")
    await wait_until(lambda: replies(transport) != [])

    # "bread" was superseded before it ever ran: no model call was spent on it.
    assert handler.started == ["milk", "coffee"]
    # And the buyer hears exactly one answer -- the one to what they last asked for.
    assert list(replies(transport)) == ["answering coffee"]

    transport.end()
    await task


@pytest.mark.asyncio
async def test_an_answer_to_a_question_the_buyer_moved_on_from_is_not_delivered() -> None:
    """The in-flight turn finishes, and its reply is dropped rather than spoken over."""
    transport = MemoryTransport()
    handler = GatedTurnHandler()
    pipeline = build(transport=transport, handler=handler, factory=None)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: transport.frames("degradation") != [])

    transport.push_text({"type": "text_input", "text": "milk"})
    await wait_until(lambda: handler.started == ["milk"])
    transport.push_text({"type": "text_input", "text": "coffee"})
    await wait_until(lambda: len(transport.frames("transcript_final")) == 2)

    handler.release("milk")
    handler.release("coffee")
    await wait_until(lambda: replies(transport) != [])
    await wait_until(lambda: handler.started == ["milk", "coffee"])

    # "milk" ran to completion -- it was already in flight, and cancelling an agent POST
    # does not stop the agent. Its ANSWER is what is discarded.
    assert list(replies(transport)) == ["answering coffee"]

    transport.end()
    await task


@pytest.mark.asyncio
async def test_typing_supersedes_speech_and_speech_supersedes_typing() -> None:
    """One context, either modality. Whichever came last is the one answered."""
    transport = MemoryTransport()
    handler = GatedTurnHandler()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, handler=handler, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    # Spoken first, then typed over: the typed one wins.
    factory.sessions[0].emit(SttFinal("do you have milk"))
    await wait_until(lambda: handler.started == ["do you have milk"])
    transport.push_text({"type": "text_input", "text": "actually bread"})
    await wait_until(lambda: len(transport.frames("transcript_final")) == 2)
    handler.release("do you have milk")
    handler.release("actually bread")
    await wait_until(lambda: replies(transport) != [])
    assert list(replies(transport)) == ["answering bread".replace("bread", "actually bread")]

    # Typed first, then spoken over: the spoken one wins. Same rule, no modality privilege.
    transport.push_text({"type": "text_input", "text": "and jam"})
    await wait_until(lambda: handler.started[-1] == "and jam")
    factory.sessions[0].emit(SttFinal("no butter"))
    await wait_until(lambda: len(transport.frames("transcript_final")) == 4)
    handler.release("and jam")
    handler.release("no butter")
    await wait_until(lambda: len(replies(transport)) == 2)
    assert list(replies(transport))[-1] == "answering no butter"

    transport.end()
    await task

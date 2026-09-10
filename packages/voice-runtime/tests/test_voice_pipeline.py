"""The pipeline properties of specification 19.14, driven through the real loop.

Every test here runs :class:`VoicePipeline` itself -- the same object the gateway serves a
browser with -- against an in-memory transport and the offline STT and TTS fakes. What is
asserted is behaviour on the wire: which frames the client receives, in which order, with
which contents.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.gateway.agent_client import SCENARIO_FAULT_HEADER, HttpTurnHandler
from voice_runtime.gateway.scenario import OneShotFailingSynthesizer
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.stt.events import SttFinal, SttInterim
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.testing import MemoryTransport, an_identity
from voice_runtime.tts.synth import FakeSynthesizer, voice_for
from voice_runtime.tts.templates import Locale
from voice_runtime.turn import FakeTurnHandler, TurnReply


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def build(
    *,
    transport: MemoryTransport,
    factory: FakeSttFactory | None = None,
    synthesizer: FakeSynthesizer | None = None,
    handler: FakeTurnHandler | None = None,
    clock: FakeClock | None = None,
) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=factory,
        synthesizer=synthesizer if synthesizer is not None else FakeSynthesizer(),
        turn_handler=handler if handler is not None else FakeTurnHandler(),
        identity=an_identity(),
        clock=clock if clock is not None else FakeClock(),
        rotation_margin_s=10_000.0,
        connect_timeout_s=1.0,
        backoff_start_s=0.0,
    )


# ---- the first frame ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_ready_advertises_the_contract_and_denies_voice_authority() -> None:
    transport = MemoryTransport()
    pipeline = build(transport=transport, factory=FakeSttFactory())
    transport.end()
    await pipeline.run()

    ready = transport.one("session_ready")
    assert ready["input"] == {"sample_rate_hz": 16000, "encoding": "pcm16le", "channels": 1}
    assert ready["output"] == {"sample_rate_hz": 24000, "encoding": "pcm16le", "channels": 1}
    # 19.11 as a wire fact, not a comment: the client is told, on frame one, that nothing
    # it says down this socket carries authority.
    assert ready["voice_is_authority"] is False


@pytest.mark.asyncio
async def test_a_socket_with_no_recognizer_says_so_before_anything_else() -> None:
    """Speech unconfigured is a visible degradation, not a socket that quietly ignores audio."""
    transport = MemoryTransport()
    pipeline = build(transport=transport, factory=None)
    transport.end()
    await pipeline.run()

    assert transport.frame_types()[:2] == ["session_ready", "degradation"]
    degraded = transport.one("degradation")
    assert degraded["kind"] == "stt_unavailable"
    assert degraded["text_input_available"] is True
    assert degraded["transaction_state_changed"] is False


# ---- transcripts (19.5) ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_interim_transcripts_replace_and_an_empty_frame_does_not_clear() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    session = factory.sessions[0]
    session.emit(SttInterim("mujhe"))
    session.emit(SttInterim("mujhe do litre"))
    session.emit(SttInterim(""))  # an empty frame must not clear the held turn
    session.emit(SttInterim("mujhe do litre doodh chahiye"))
    await wait_until(lambda: len(transport.frames("transcript_partial")) == 4)

    texts = [f["text"] for f in transport.frames("transcript_partial")]
    assert texts == [
        "mujhe",
        "mujhe do litre",
        "mujhe do litre",  # replaced with itself, never cleared
        "mujhe do litre doodh chahiye",
    ]
    partial = transport.frames("transcript_partial")[0]
    # The semantics travel with the frame: shape is not semantics (19.5).
    assert partial["cumulative"] is True
    assert partial["revisable"] is True
    assert partial["empty_means"] == "keep_held"

    transport.end()
    await task


@pytest.mark.asyncio
async def test_a_revision_does_not_concatenate() -> None:
    """The bug this rule exists for produced ``TumjoMainejoMaineTuMeriTuMainu``."""
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    session = factory.sessions[0]
    session.emit(SttInterim("mujhe 2 litre"))
    session.emit(SttInterim("mujhe do litre"))  # a revision, not an extension
    await wait_until(lambda: len(transport.frames("transcript_partial")) == 2)

    assert [f["text"] for f in transport.frames("transcript_partial")] == [
        "mujhe 2 litre",
        "mujhe do litre",
    ]
    transport.end()
    await task


@pytest.mark.asyncio
async def test_duplicate_finals_are_deduplicated_and_only_one_turn_runs() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    handler = FakeTurnHandler()
    pipeline = build(transport=transport, factory=factory, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    session = factory.sessions[0]
    session.emit(SttFinal("two litres of milk"))
    session.emit(SttFinal("two litres of milk"))
    await wait_until(lambda: len(handler.calls) == 1)
    await asyncio.sleep(0.05)

    assert len(handler.calls) == 1, "the recognizer repeating itself is not a second turn"
    assert len(transport.frames("transcript_final")) == 1
    transport.end()
    await task


@pytest.mark.asyncio
async def test_only_a_final_transcript_reaches_the_agent() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    handler = FakeTurnHandler()
    pipeline = build(transport=transport, factory=factory, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttInterim("two litres of"))
    await wait_until(lambda: len(transport.frames("transcript_partial")) == 1)
    await asyncio.sleep(0.05)
    assert handler.calls == [], "an interim transcript is never confirmed intent (19.5)"

    transport.end()
    await task


@pytest.mark.asyncio
async def test_a_turn_that_aged_out_while_queued_is_dropped_and_the_buyer_is_told() -> None:
    """The reachable staleness: a turn that waited behind a turn that ran long (19.4).

    The queue already refuses to send audio older than the freshness window, so audio
    staleness cannot reach the recognizer. What can happen is a second utterance settling
    while the first turn is still running, then waiting so long that answering it would
    answer a question the buyer has moved on from.
    """
    clock = FakeClock()
    transport = MemoryTransport()
    factory = FakeSttFactory()
    release = asyncio.Event()

    class SlowHandler(FakeTurnHandler):
        async def handle_turn(self, transcript, identity):  # type: ignore[no-untyped-def]
            self.calls.append((transcript, identity))
            if len(self.calls) == 1:
                await release.wait()  # hold the lock while the next turn queues up
            return TurnReply(text="ok")

    handler = SlowHandler()
    pipeline = build(transport=transport, factory=factory, handler=handler, clock=clock)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))
    session = factory.sessions[0]

    session.emit(SttFinal("two litres of milk"))
    await wait_until(lambda: len(handler.calls) == 1)  # first turn is holding the lock

    session.emit(SttFinal("actually make it three"))
    await wait_until(lambda: len(transport.frames("transcript_final")) == 2)
    clock.advance(30.0)  # the queued turn ages out while it waits
    release.set()

    await wait_until(lambda: transport.frames("degradation") != [])
    assert transport.one("degradation")["kind"] == "stale_turn_dropped"
    assert len(handler.calls) == 1, "the aged-out turn never reached the agent"
    assert pipeline.metrics.stale_turns_rejected == 1
    transport.end()
    await task


# ---- text exists before speech (19.1) -------------------------------------------------


@pytest.mark.asyncio
async def test_the_reply_is_on_screen_before_any_audio_is_sent() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    handler = FakeTurnHandler(replies=[TurnReply(text="Adding milk. Anything else?")])
    pipeline = build(transport=transport, factory=factory, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttFinal("add milk"))
    await wait_until(lambda: transport.frames("speech_end") != [])

    order = transport.frame_types()
    assert order.index("agent_reply") < order.index("speech_start"), (
        "the buyer reads the sentence before they hear it: that is the whole architecture"
    )
    assert transport.one("agent_reply")["text"] == "Adding milk. Anything else?"
    transport.end()
    await task


@pytest.mark.asyncio
async def test_every_speech_chunk_header_is_followed_by_exactly_its_binary_frame() -> None:
    """With a transport that SUSPENDS, so other tasks can actually interleave.

    The client sizes its next read from ``byte_length``, so a frame that slipped between
    the header and its audio would be read as audio. An earlier version of this test used
    a transport whose writes never yielded, which made the invariant unfalsifiable -- it
    was green while a real socket violated it.
    """
    transport = MemoryTransport(suspend_on_write=True)
    factory = FakeSttFactory()
    handler = FakeTurnHandler(replies=[TurnReply(text="One. Two. Three.")])
    pipeline = build(transport=transport, factory=factory, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttFinal("count to three"))
    await wait_until(lambda: transport.frames("speech_end") != [])

    pairs = [
        (tag, body)
        for tag, body in transport.sent
        if tag == "audio" or (tag == "json" and body.get("type") == "speech_chunk")
    ]
    assert len(pairs) == 6, "three sentences: three headers, three binary frames"
    for header, audio in zip(pairs[::2], pairs[1::2], strict=True):
        assert header[0] == "json" and audio[0] == "audio"
        assert header[1]["byte_length"] == len(audio[1])
        assert header[1]["sample_rate_hz"] == 24000
    transport.end()
    await task


# ---- barge-in and cancellation (19.7, 19.8) -------------------------------------------


@pytest.mark.asyncio
async def test_barge_in_bumps_the_generation_and_acknowledges() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    before = pipeline.speech_generation.current
    transport.push_text({"type": "barge_in"})
    await wait_until(lambda: transport.frames("interrupted") != [])

    assert pipeline.speech_generation.current == before + 1
    assert transport.one("interrupted")["speech_generation"] == before + 1
    assert pipeline.metrics.barge_ins == 1
    transport.end()
    await task


@pytest.mark.asyncio
async def test_a_sentence_cancelled_during_synthesis_never_reaches_the_speaker() -> None:
    """The check AFTER the await is the one people forget (19.8)."""
    transport = MemoryTransport()
    factory = FakeSttFactory()
    synthesizer = FakeSynthesizer()
    synthesizer.hold = asyncio.Event()  # park synthesis mid-flight
    handler = FakeTurnHandler(replies=[TurnReply(text="This must never be heard.")])
    pipeline = build(transport=transport, factory=factory, synthesizer=synthesizer, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttFinal("say something"))
    await wait_until(lambda: synthesizer.calls != [])  # synthesis is now in flight

    transport.push_text({"type": "barge_in"})
    await wait_until(lambda: transport.frames("interrupted") != [])
    synthesizer.hold.set()  # synthesis completes AFTER the cancellation
    await wait_until(lambda: transport.frames("speech_end") != [])

    assert transport.audio_chunks() == [], "synthesis finished, but nothing was sent"
    assert transport.frames("speech_chunk") == []
    assert transport.one("speech_end")["cancelled"] is True
    transport.end()
    await task


# ---- the echo gate (19.6) --------------------------------------------------------------


@pytest.mark.asyncio
async def test_while_speaking_the_recognizer_receives_silence_not_nothing() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))
    assert pipeline.stt is not None

    loud = b"\x40\x40" * 800
    pipeline.stt.echo_gate.start_speaking()
    for _ in range(4):
        transport.push_audio(loud)
    await wait_until(lambda: len(factory.sessions[0].sent) == 4)

    sent = factory.sessions[0].sent
    assert all(frame == bytes(len(loud)) for frame in sent), "substituted, not withheld"
    assert all(len(frame) == len(loud) for frame in sent), "cadence unchanged"
    transport.end()
    await task


@pytest.mark.asyncio
async def test_the_echo_tail_starts_when_the_client_reports_playback_end() -> None:
    clock = FakeClock()
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory, clock=clock)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))
    assert pipeline.stt is not None
    gate = pipeline.stt.echo_gate

    gate.start_speaking()
    gate.on_server_send_complete()
    assert gate.engaged, "the server finishing its send says nothing about the speakers"

    output = pipeline.playback.announce(None)
    pipeline.playback.started(output.utterance_id, clock.now())
    pipeline.playback.sent(output.utterance_id)
    transport.push_text(
        {"type": "playback_ended", "utterance_id": output.utterance_id, "speech_generation": 0}
    )
    await wait_until(lambda: not gate.speaking)
    assert gate.engaged, "the tail covers the speaker ring-out"
    clock.advance(0.7)
    assert not gate.engaged
    transport.end()
    await task


# ---- degradation is visible (19.12) ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_reasoning_failure_is_visible_and_changes_no_transaction_state() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    handler = FakeTurnHandler(fail=True)
    pipeline = build(transport=transport, factory=factory, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttFinal("what is in my basket"))
    await wait_until(lambda: transport.frames("degradation") != [])

    degraded = transport.one("degradation")
    assert degraded["kind"] == "reasoning_failed"
    assert degraded["transaction_state_changed"] is False
    assert transport.audio_chunks() == []
    transport.end()
    await task


@pytest.mark.asyncio
async def test_tts_failure_leaves_the_text_visible() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    handler = FakeTurnHandler(replies=[TurnReply(text="Adding milk now.")])
    pipeline = build(
        transport=transport,
        factory=factory,
        synthesizer=FakeSynthesizer(fail=True),
        handler=handler,
    )
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttFinal("add milk"))
    await wait_until(lambda: transport.frames("degradation") != [])

    assert transport.one("agent_reply")["text"] == "Adding milk now."
    assert transport.one("degradation")["kind"] == "tts_failed"
    assert transport.audio_chunks() == []
    transport.end()
    await task


@pytest.mark.asyncio
async def test_a_guard_refusal_is_surfaced_rather_than_silently_dropped() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    handler = FakeTurnHandler(
        replies=[TurnReply(text="Sure. I have already refunded you.", locale=Locale.EN_IN)]
    )
    pipeline = build(transport=transport, factory=factory, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    factory.sessions[0].emit(SttFinal("did my refund go through"))
    await wait_until(lambda: transport.frames("speech_end") != [])

    spoken = [f["text"] for f in transport.frames("speech_chunk")]
    assert spoken == ["Sure."], "the refund claim was refused before it could be spoken"
    assert transport.one("degradation")["kind"] == "speech_guard_refused"
    transport.end()
    await task


# ---- typed input, and what it proves about authority -----------------------------------


@pytest.mark.asyncio
async def test_typed_input_runs_a_turn_while_speech_is_unavailable() -> None:
    transport = MemoryTransport()
    handler = FakeTurnHandler()
    pipeline = build(transport=transport, factory=None, handler=handler)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: transport.frames("degradation") != [])

    transport.push_text({"type": "text_input", "text": "two litres of milk"})
    await wait_until(lambda: len(handler.calls) == 1)

    assert handler.calls[0][0].source == "text"
    assert transport.one("transcript_final")["source"] == "text"
    transport.end()
    await task


@pytest.mark.asyncio
async def test_an_unknown_client_frame_is_rejected_without_killing_the_socket() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    transport.push_text({"type": "approve_payment", "checkout_id": "anything"})
    await wait_until(lambda: transport.frames("error") != [])

    assert transport.one("error")["code"] == "invalid_frame"
    # And the socket is still serving: a refusal is not a disconnection.
    transport.push_text({"type": "ping"})
    transport.end()
    await task
    assert pipeline.metrics.invalid_frames == 1


@pytest.mark.asyncio
async def test_a_malformed_audio_frame_is_refused_with_the_contract_restated() -> None:
    transport = MemoryTransport()
    factory = FakeSttFactory()
    pipeline = build(transport=transport, factory=factory)
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: bool(factory.sessions))

    transport.push_audio(b"\x01\x02\x03")  # odd length: not a whole 16-bit sample
    await wait_until(lambda: transport.frames("error") != [])

    assert transport.one("error")["code"] == "invalid_audio_frame"
    transport.end()
    await task


@pytest.mark.asyncio
async def test_the_pairing_survives_frames_racing_in_from_other_tasks() -> None:
    """Speech chunks and other frames written CONCURRENTLY must not interleave.

    Three independent task trees write to this socket: the receive loop, the recognizer's
    listener callbacks, and the turn task. The client sizes its next read from the
    ``speech_chunk`` header's ``byte_length``, so any frame that lands between a header
    and its audio is read as audio and the stream is corrupt from there on.

    This drives the writers directly and concurrently, because that is the only way to
    make the race deterministic. Removing the lock in ``send_chunk`` makes this test fail;
    that was verified, which is the whole point of writing it this way.
    """
    from voice_runtime.tts.synth import SpeechChunk
    from voice_runtime.wire.frames import TranscriptPartial

    transport = MemoryTransport(suspend_on_write=True)
    pipeline = build(transport=transport, factory=FakeSttFactory())

    pipeline._utterance_id = pipeline.playback.announce(None).utterance_id

    async def speak(seq: int) -> None:
        await pipeline.send_chunk(
            SpeechChunk(
                seq=seq,
                generation=0,
                text=f"sentence {seq}",
                pcm=bytes(16) * seq,
                sample_rate_hz=24000,
                deterministic=False,
            )
        )

    async def chatter(index: int) -> None:
        await pipeline._send(
            TranscriptPartial(text=f"noise {index}", turn_id=0, stt_generation=1, age_ms=0)
        )

    await asyncio.gather(
        *(speak(seq) for seq in range(1, 9)),
        *(chatter(index) for index in range(24)),
    )

    stream = [
        (tag, body)
        for tag, body in transport.sent
        if tag == "audio" or (tag == "json" and body.get("type") == "speech_chunk")
    ]
    assert len(stream) == 16, "eight headers and eight audio frames"
    for header, audio in zip(stream[::2], stream[1::2], strict=True):
        assert header[0] == "json", f"a header was displaced by {header[1]}"
        assert audio[0] == "audio", (
            f"{audio[1]} landed between header {header[1]['seq']} and its audio"
        )
        assert header[1]["byte_length"] == len(audio[1])


# ---- the ninth injection: a speech failure the operator armed ---------------------------


def test_the_wrapper_arms_only_for_its_own_fault() -> None:
    """A kind meant for a different consumer, or a malformed header, arms nothing."""
    failing = OneShotFailingSynthesizer(FakeSynthesizer())
    for kind in ("LLM_FAILURE", "tts_failure", "", "CREATE_ORDER_TIMEOUT"):
        failing.arm_for(kind)
        assert failing.armed is False, kind
    failing.arm_for("TTS_FAILURE")
    assert failing.armed is True


@pytest.mark.asyncio
async def test_the_wrapper_disarms_before_it_raises_so_one_phrase_fails() -> None:
    """The second gate on single use, and the one that concurrency needs.

    ``Speaker`` launches look-ahead synthesis tasks concurrently, so an armed flag that
    survived the raise would fail every phrase in flight rather than the one the operator
    armed. Clearing it first also means the real synthesizer is reachable again on the
    very next call, which is what "consumed once, then gone" has to mean here.
    """
    inner = FakeSynthesizer()
    failing = OneShotFailingSynthesizer(inner)
    failing.arm_for("TTS_FAILURE")

    with pytest.raises(RuntimeError, match="ScenarioFault:TTS_FAILURE"):
        await failing.synthesize("Adding milk now.", voice_for(Locale.EN_IN))
    assert inner.calls == [], "the fault fires instead of synthesis, never alongside it"

    await failing.synthesize("Adding milk now.", voice_for(Locale.EN_IN))
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_a_fault_dispensed_by_the_server_leaves_the_buyer_the_correct_text() -> None:
    """Specification 30's modality fallback, driven end to end through the real seam.

    The real :class:`HttpTurnHandler` reads the real header off a real turn response and
    arms the real wrapper, and the pipeline is untouched apparatus-free code. The frames
    that come out are the same three that
    ``test_tts_failure_leaves_the_text_visible`` gets from a genuine synthesis failure:
    the reply text first (19.1 puts text before speech, always), then ``tts_failed``, then
    no audio at all. That the demonstrated frames are the shipped frames -- rather than a
    demo branch that resembles them -- is the whole reason nothing in ``pipeline.py``
    knows this fault exists.
    """
    transport = MemoryTransport()
    factory = FakeSttFactory()
    inner = FakeSynthesizer()
    failing = OneShotFailingSynthesizer(inner)

    def api(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "reply": "Adding milk now.",
                "language": "en",
                "specialist": "shopping",
                "routing_reason": "default_shopping",
                "principal_id": "session:00000000-0000-7000-8000-000000000000/razorai/shopping",
                "tool_calls": [],
                "denials": [],
                "structured": None,
            },
            headers={SCENARIO_FAULT_HEADER: "TTS_FAILURE"},
        )

    async with httpx.AsyncClient(
        base_url="http://api.test", transport=httpx.MockTransport(api)
    ) as client:
        pipeline = VoicePipeline(
            transport=transport,
            stt_factory=factory,
            synthesizer=failing,
            turn_handler=HttpTurnHandler(
                client, bearer="tok-abc", on_scenario_fault=failing.arm_for
            ),
            identity=an_identity(),
            clock=FakeClock(),
            rotation_margin_s=10_000.0,
            connect_timeout_s=1.0,
            backoff_start_s=0.0,
        )
        task = asyncio.create_task(pipeline.run())
        await wait_until(lambda: bool(factory.sessions))

        factory.sessions[0].emit(SttFinal("add milk"))
        await wait_until(lambda: transport.frames("degradation") != [])

        assert transport.one("agent_reply")["text"] == "Adding milk now."
        degraded = transport.one("degradation")
        assert degraded["kind"] == "tts_failed"
        # The money invariant is stated on the wire, not merely true off it: a speech
        # failure is a modality falling back, never a transaction changing.
        assert degraded["transaction_state_changed"] is False
        assert degraded["text_input_available"] is True
        assert transport.audio_chunks() == []
        assert inner.calls == []
        assert failing.armed is False, "single-use: the next turn speaks normally"
        transport.end()
        await task


@pytest.mark.asyncio
async def test_cart_update_speaks_server_ack_without_creating_buyer_transcript():
    import uuid

    class SalesHandler(FakeTurnHandler):
        async def handle_cart_update(self, cart_id, event_id, locale):
            self.event = (cart_id, event_id)
            return TurnReply(
                text="Added two packs. Would you like bread alongside?",
                server_authored=True,
                locale=Locale.EN_IN,
            )

    handler = SalesHandler()
    transport = MemoryTransport()
    pipeline = build(transport=transport, handler=handler)
    task = asyncio.create_task(pipeline.run())
    try:
        await wait_until(lambda: "session_ready" in transport.frame_types())
        cart, event = str(uuid.uuid4()), str(uuid.uuid4())
        transport.push_text({"type": "cart_updated", "cart_id": cart, "event_id": event})
        await wait_until(lambda: "speech_end" in transport.frame_types())
        assert handler.event == (cart, event)
        assert transport.one("agent_reply")["text"].startswith("Added two")
        assert "transcript_final" not in transport.frame_types()
        assert transport.one("agent_reply")["offer_is_proposal"] is False
    finally:
        transport.end()
        await task

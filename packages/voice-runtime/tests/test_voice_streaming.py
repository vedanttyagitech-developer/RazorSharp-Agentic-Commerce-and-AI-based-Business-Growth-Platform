"""Streaming must start before completion without weakening grounding or interruption."""

import asyncio

import pytest
from voice_runtime.tts.fallback import FallbackSynthesizer
from voice_runtime.tts.synth import Speaker, SpeechGeneration, voice_for
from voice_runtime.tts.templates import Locale


class Streaming:
    supports_streaming = True

    def __init__(self, *, failure=False):
        self.release = asyncio.Event()
        self.closed = False
        self.text = None
        self.failure = failure

    async def synthesize(self, text, voice):
        raise AssertionError("Buffered synthesis must not be used")

    async def stream(self, text, voice):
        self.text = text
        try:
            yield b"\x01\x00" * 960
            await self.release.wait()
            if self.failure:
                raise RuntimeError("connection lost after first audio")
            yield b"\x02\x00" * 960
        finally:
            self.closed = True


class Sink:
    def __init__(self):
        self.chunks = []
        self.first = asyncio.Event()

    async def send_chunk(self, chunk):
        self.chunks.append(chunk)
        self.first.set()


@pytest.mark.anyio
async def test_first_audio_precedes_completion_and_guard_runs_before_stream():
    synth, sink, generation = Streaming(), Sink(), SpeechGeneration()
    task = asyncio.create_task(
        Speaker(synth, sink, generation).speak(
            "Here are the options. Your total is ₹395 now. Shall I help?",
            locale=Locale.EN_IN,
            deterministic=False,
            generation=0,
        )
    )
    await asyncio.wait_for(sink.first.wait(), 1)
    assert not task.done()
    assert synth.text == "Here are the options. Shall I help?"
    synth.release.set()
    result = await task
    assert result.chunks_sent == 2
    assert not result.tts_failed
    assert synth.closed


@pytest.mark.anyio
async def test_interruption_discards_later_stream_audio_and_closes_provider():
    synth, sink, generation = Streaming(), Sink(), SpeechGeneration()
    task = asyncio.create_task(
        Speaker(synth, sink, generation).speak(
            "Here are the options.",
            locale=Locale.EN_IN,
            deterministic=True,
            generation=0,
        )
    )
    await asyncio.wait_for(sink.first.wait(), 1)
    generation.bump()
    synth.release.set()
    result = await task
    assert result.cancelled
    assert len(sink.chunks) == 1
    assert synth.closed


@pytest.mark.anyio
async def test_partial_stream_never_replays_through_fallback():
    first, fallback = Streaming(failure=True), Streaming()
    first.release.set()
    chain = FallbackSynthesizer(first, fallback)
    with pytest.raises(RuntimeError, match="connection lost"):
        async for _ in chain.stream("Your total is ₹200.", voice_for(Locale.EN_IN)):
            pass
    assert fallback.text is None
    assert first.closed


@pytest.mark.anyio
async def test_interruption_releases_stalled_provider_without_next_chunk():
    synth, sink, generation = Streaming(), Sink(), SpeechGeneration()
    task = asyncio.create_task(
        Speaker(synth, sink, generation).speak(
            "Here are your options.", locale=Locale.EN_IN, deterministic=True, generation=0
        )
    )
    await asyncio.wait_for(sink.first.wait(), 1)
    generation.bump()
    result = await asyncio.wait_for(task, 0.5)
    assert result.cancelled
    assert synth.closed
    assert len(sink.chunks) == 1


@pytest.mark.anyio
async def test_next_speech_turn_works_after_cancelling_stalled_provider():
    synth, sink, generation = Streaming(), Sink(), SpeechGeneration()
    speaker = Speaker(synth, sink, generation)
    first = asyncio.create_task(
        speaker.speak("Old answer.", locale=Locale.EN_IN, deterministic=True, generation=0)
    )
    await asyncio.wait_for(sink.first.wait(), 1)
    generation.bump()
    assert (await asyncio.wait_for(first, 0.5)).cancelled
    synth.release.set()
    second = await speaker.speak(
        "Your new options.", locale=Locale.EN_IN, deterministic=True, generation=1
    )
    assert not second.cancelled
    assert not second.tts_failed
    assert all(chunk.generation == 1 for chunk in sink.chunks[1:])

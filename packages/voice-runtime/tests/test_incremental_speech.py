"""Speech arrives before the final result, and the final prefix is not replayed."""

import asyncio

import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.testing import MemoryTransport, an_identity
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.turn import TurnReply


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_first_audio_precedes_final_result_and_prefix_is_not_replayed():
    finish = asyncio.Event()
    synth = FakeSynthesizer()
    transport = MemoryTransport()

    class StreamingHandler:
        async def handle_turn_stream(self, transcript, identity, on_sentence):
            await on_sentence(TurnReply(text="Hello there."))
            await finish.wait()
            return TurnReply(text="Hello there. How can I help?", server_authored=True)

    pipeline = VoicePipeline(
        transport=transport,
        stt_factory=None,
        synthesizer=synth,
        turn_handler=StreamingHandler(),
        identity=an_identity(),
        clock=FakeClock(),
    )
    task = asyncio.create_task(pipeline.run())
    try:
        await wait_for(lambda: "session_ready" in transport.frame_types())
        transport.push_text({"type": "text_input", "text": "hello"})
        await wait_for(lambda: "speech_chunk" in transport.frame_types())
        assert not finish.is_set()
        finish.set()
        await wait_for(lambda: any("How can I help?" in call[0] for call in synth.calls))
        await asyncio.sleep(0.1)
        texts = [call[0] for call in synth.calls]
        assert sum("Hello there." in text for text in texts) == 1
    finally:
        finish.set()
        transport.end()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_http_stream_delivers_partial_once_without_retry(complete):
    import json

    import httpx
    from voice_runtime.gateway.agent_client import AgentUnavailableError, HttpTurnHandler
    from voice_runtime.stt.transcript import FreshnessStamp, TranscriptTurn

    partial_seen = asyncio.Event()
    requests = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"type":"speech","text":"Hello there.","language":"en"}\n'
            await asyncio.wait_for(partial_seen.wait(), 1)
            if complete:
                yield (
                    json.dumps(
                        {
                            "type": "result",
                            "status": 200,
                            "body": {
                                "reply": "Hello there. Welcome.",
                                "structured": {},
                            },
                        }
                    )
                    + "\n"
                ).encode()

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=Chunks())

    async def sentence(reply):
        assert reply.text == "Hello there."
        partial_seen.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://api.test"
    ) as client:
        agent = HttpTurnHandler(client, bearer="test", streaming=True)
        turn = TranscriptTurn(
            turn_id=1,
            text="hello",
            is_final=True,
            stamp=FreshnessStamp(observed_at=0, generation=1),
        )
        if complete:
            result = await agent.handle_turn_stream(turn, an_identity(), sentence)
            assert result.text == "Hello there. Welcome."
        else:
            with pytest.raises(AgentUnavailableError, match="without its final result"):
                await agent.handle_turn_stream(turn, an_identity(), sentence)
    assert partial_seen.is_set()
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/voice/turn-stream"

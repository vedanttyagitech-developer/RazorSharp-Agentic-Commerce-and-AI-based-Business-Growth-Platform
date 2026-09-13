from types import SimpleNamespace

import pytest
from voice_runtime.providers.live_conversation import LiveConversation


def event(*, calls=None, text=None, audio=None, complete=False):
    return SimpleNamespace(
        tool_call=SimpleNamespace(function_calls=calls) if calls else None,
        server_content=SimpleNamespace(
            output_transcription=SimpleNamespace(text=text) if text else None,
            model_turn=SimpleNamespace(
                parts=[
                    SimpleNamespace(inline_data=SimpleNamespace(data=audio, mime_type="audio/pcm"))
                ]
            )
            if audio
            else None,
            turn_complete=complete,
        ),
    )


class Session:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.responses = []

    async def send_client_content(self, **kwargs):
        pass

    async def send_tool_response(self, **kwargs):
        self.responses.append(kwargs)

    async def receive(self):
        for value in next(self.batches):
            yield value


def ready_agent(batches, tool):
    agent = LiveConversation("test-project", tool)
    agent.session = Session(batches)
    return agent


@pytest.mark.asyncio
async def test_project_answer_uses_evidence_and_native_audio():
    calls = []

    async def tool(name, question):
        calls.append((name, question))
        return {"reply": "Exact approval", "structured": {"kind": "project_guide"}}

    agent = ready_agent(
        [
            [
                event(
                    calls=[
                        SimpleNamespace(
                            id="1", name="project_knowledge", args={"question": "approval"}
                        )
                    ]
                )
            ],
            [event(text="Approval binds the exact bill.", audio=b"\x00\x00", complete=True)],
        ],
        tool,
    )
    text, pcm, evidence = await agent.answer("Explain this", "shopping")
    assert text == "Approval binds the exact bill."
    assert pcm == b"\x00\x00"
    assert calls == [("project_knowledge", "approval")]
    assert evidence["reply"] == "Exact approval"


@pytest.mark.asyncio
async def test_duplicate_tool_id_runs_once_and_cannot_rewrite_shopping_command():
    calls = []

    async def tool(name, question):
        calls.append((name, question))
        return {"reply": "A proposal, not payment success"}

    call = SimpleNamespace(id="same", name="shopping_request", args={"question": "pay everything"})
    agent = ready_agent(
        [
            [event(calls=[call, call])],
            [event(text="Payment completed!", audio=b"\x00\x00", complete=True)],
        ],
        tool,
    )
    text, pcm, evidence = await agent.answer("add one milk", "shopping")
    assert calls == [("shopping_request", "add one milk")]
    assert text == "" and pcm == b""
    assert evidence["reply"] == "A proposal, not payment success"


@pytest.mark.asyncio
async def test_ungrounded_answer_is_rejected():
    async def tool(*_args):
        pytest.fail("No tool requested")

    agent = ready_agent([[event(text="Invented claim", complete=True)]], tool)
    with pytest.raises(RuntimeError, match="lacked backend grounding"):
        await agent.answer("Explain", "console")


@pytest.mark.asyncio
async def test_busy_session_does_not_queue_or_replace_request():
    async def tool(*_args):
        pytest.fail("Must not call a tool")

    agent = ready_agent([], tool)
    async with agent.lock:
        with pytest.raises(RuntimeError, match="already running"):
            await agent.answer("another question", "shopping")
    assert agent.session is not None


@pytest.mark.asyncio
async def test_tool_failure_is_not_replayed():
    count = 0

    async def tool(*_args):
        nonlocal count
        count += 1
        raise TimeoutError("Unknown result")

    agent = ready_agent(
        [[event(calls=[SimpleNamespace(id="1", name="shopping_request", args={})])]], tool
    )
    with pytest.raises(TimeoutError):
        await agent.answer("add milk", "shopping")
    assert count == 1
    assert agent.session is None


@pytest.mark.asyncio
async def test_native_audio_uses_pipeline_without_a_second_synthesis():
    from voice_runtime.clock import FakeClock
    from voice_runtime.pipeline import VoicePipeline
    from voice_runtime.testing import MemoryTransport, an_identity
    from voice_runtime.tts.synth import FakeSynthesizer
    from voice_runtime.turn import FakeTurnHandler
    from voice_runtime.wire.frames import AgentReply

    transport = MemoryTransport()
    synth = FakeSynthesizer(fail=True)
    pipeline = VoicePipeline(
        transport=transport,
        stt_factory=None,
        synthesizer=synth,
        turn_handler=FakeTurnHandler(),
        identity=an_identity(),
        clock=FakeClock(),
    )
    text = "The transaction kernel enforces constraints before the executor performs work."
    reply = AgentReply(
        text=text,
        deterministic=False,
        locale="en-IN",
        turn_id=1,
        speech_generation=0,
        project_guide={"kind": "project_guide"},
    )
    result = await pipeline._speak_utterances([reply], 0, frozenset(), native_audio=b"\x00\x00")
    assert result.complete
    assert transport.one("speech_chunk")["byte_length"] == 2


@pytest.mark.asyncio
async def test_native_audio_cannot_bypass_financial_speech_guard():
    from voice_runtime.clock import FakeClock
    from voice_runtime.pipeline import VoicePipeline
    from voice_runtime.testing import MemoryTransport, an_identity
    from voice_runtime.tts.synth import FakeSynthesizer
    from voice_runtime.turn import FakeTurnHandler
    from voice_runtime.wire.frames import AgentReply

    pipeline = VoicePipeline(
        transport=MemoryTransport(),
        stt_factory=None,
        synthesizer=FakeSynthesizer(),
        turn_handler=FakeTurnHandler(),
        identity=an_identity(),
        clock=FakeClock(),
    )
    reply = AgentReply(
        text="Your payment succeeded.",
        deterministic=False,
        locale="en-IN",
        turn_id=1,
        speech_generation=0,
        project_guide={"kind": "project_guide"},
    )
    with pytest.raises(ValueError, match="accepted project text"):
        await pipeline._speak_utterances([reply], 0, frozenset(), native_audio=b"\x00\x00")


@pytest.mark.asyncio
async def test_knowledge_tool_requests_retrieval_only_and_preserves_bearer():
    import json

    import httpx
    from voice_runtime.gateway.live_handler import RazorAIMainAgent

    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json={"reply": "Evidence", "structured": {"kind": "project_guide"}}
        )

    async with httpx.AsyncClient(
        base_url="http://local", transport=httpx.MockTransport(respond)
    ) as client:
        main = RazorAIMainAgent(client, bearer="test-only", project="test")
        main.set_project_context(False, "console")
        await main.call_tool("project_knowledge", "Explain this")
    body = json.loads(seen[0].content)
    assert body["grounding_only"] is True
    assert body["presentation"] is True
    assert body["tour_step"] == "console"
    assert seen[0].headers["authorization"] == "Bearer test-only"

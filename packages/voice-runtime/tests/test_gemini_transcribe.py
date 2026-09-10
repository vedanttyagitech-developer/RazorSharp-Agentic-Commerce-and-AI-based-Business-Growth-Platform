"""Recognition adapter boundaries independent of network and microphone hardware."""

from types import SimpleNamespace

import pytest
from google.genai import types
from voice_runtime.providers.gemini_transcribe import GeminiTranscribeSession
from voice_runtime.stt.events import SttConnectionLostError, SttFinal, SttInterim


def test_partial_replaces_and_only_provider_final_becomes_intent():
    message = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            interim_input_transcription=types.Transcription(text="do packet")
        )
    )
    assert GeminiTranscribeSession.events(message) == [SttInterim("do packet")]
    message = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="nahi teen packet doodh")
        )
    )
    assert GeminiTranscribeSession.events(message) == [SttFinal("nahi teen packet doodh")]


def test_model_generated_text_is_never_mistaken_for_buyer_input():
    message = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(parts=[types.Part(text="Payment successful")]),
            output_transcription=types.Transcription(text="Add two packets"),
        )
    )
    assert GeminiTranscribeSession.events(message) == []


@pytest.mark.asyncio
async def test_receive_continues_across_provider_turn_boundaries():
    class Socket:
        count = 0

        async def receive(self):
            self.count += 1
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text=f"turn {self.count}"),
                    turn_complete=True,
                )
            )

    session = GeminiTranscribeSession("test")
    session._session = Socket()
    output = []
    async for event in session.receive():
        if isinstance(event, SttFinal):
            output.append(event.text)
        if len(output) == 10:
            break
    assert output == [f"turn {i}" for i in range(1, 11)]


@pytest.mark.asyncio
async def test_closed_connection_refuses_audio():
    session = GeminiTranscribeSession("test")
    await session.close()
    with pytest.raises(SttConnectionLostError):
        await session.send_audio(bytes(3200))


@pytest.mark.asyncio
async def test_setup_ack_required_before_audio_and_resources_close(monkeypatch):
    from google import genai

    captured = {}

    class Context:
        async def __aenter__(self):
            return SimpleNamespace(setup_complete=None)

        async def __aexit__(self, *_args):
            captured["closed_socket"] = True

    class Aio:
        def __init__(self):
            self.live = self

        def connect(self, *, model, config):
            captured.update(model=model, config=config)
            return Context()

        async def aclose(self):
            captured["closed_client"] = True

    monkeypatch.setattr(genai, "Client", lambda **_kwargs: SimpleNamespace(aio=Aio()))
    session = GeminiTranscribeSession("test")
    with pytest.raises(SttConnectionLostError, match="acknowledged"):
        await session.start(None)
    assert captured["closed_socket"] and captured["closed_client"]
    assert captured["model"] == "gemini-3.5-transcribe-live-preview"
    assert (
        captured["config"].input_audio_transcription.mode
        == types.AudioTranscriptionConfigMode.VERBATIM
    )
    assert captured["config"].tools is None
    assert captured["config"].response_modalities == [types.Modality.TEXT]

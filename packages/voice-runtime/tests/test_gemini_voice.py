"""Gemini audio framing and cancellation; financial authority is never exposed."""

from types import SimpleNamespace

import pytest
from google import genai
from google.genai import types
from voice_runtime.providers.gemini_voice import GeminiLiveVoice
from voice_runtime.tts.synth import voice_for
from voice_runtime.tts.templates import Locale


def install(monkeypatch, messages):
    seen = {}

    class Session:
        setup_complete = object()

        async def send_client_content(self, **kwargs):
            seen["input"] = kwargs

        async def receive(self):
            for message in messages:
                yield message

    class Context:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            seen["closed"] = True

    class Aio:
        def __init__(self):
            self.live = self

        def connect(self, **kwargs):
            seen.update(kwargs)
            return Context()

        async def aclose(self):
            pass

    monkeypatch.setattr(genai, "Client", lambda **_kwargs: SimpleNamespace(aio=Aio()))
    return seen


def audio(pcm, mime="audio/pcm;rate=24000"):
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type=mime))]
            )
        )
    )


@pytest.mark.asyncio
async def test_stream_is_pcm_and_no_commerce_tools_are_registered(monkeypatch):
    seen = install(monkeypatch, [audio(b"\x01", "audio/pcm"), audio(b"\x02\x03\x04")])
    voice = GeminiLiveVoice("test")
    pcm = await voice.synthesize("The price is ₹157.", voice_for(Locale.EN_IN))
    assert pcm == b"\x01\x02\x03\x04"
    assert not seen.get("closed")
    await voice.aclose()
    assert seen["closed"]
    assert seen["config"].speech_config.voice_config.prebuilt_voice_config.voice_name == "Aoede"
    import json

    assert set(json.loads(seen["input"]["turns"].parts[0].text)) == {"text_to_read"}
    assert seen["config"].tools is None
    assert "rupees" in seen["input"]["turns"].parts[0].text


@pytest.mark.asyncio
async def test_interrupting_stream_closes_provider_connection(monkeypatch):
    seen = install(monkeypatch, [audio(b"\x00\x00"), audio(b"\x01\x01")])
    stream = GeminiLiveVoice("test").stream("Hello.", voice_for(Locale.EN_IN))
    assert await anext(stream) == b"\x00\x00"
    await stream.aclose()
    assert seen["closed"]


@pytest.mark.asyncio
async def test_wrong_sample_rate_is_not_played(monkeypatch):
    install(monkeypatch, [audio(b"\x00\x00", "audio/pcm;rate=16000")])
    with pytest.raises(ValueError, match="unsupported PCM"):
        await GeminiLiveVoice("test").synthesize("Hello.", voice_for(Locale.EN_IN))


@pytest.mark.asyncio
async def test_previous_turn_interruption_does_not_silence_new_reply(monkeypatch):
    install(
        monkeypatch,
        [
            types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True)),
            audio(b"\x00\x01"),
        ],
    )
    voice = GeminiLiveVoice("test")
    assert await voice.synthesize("Hello.", voice_for(Locale.EN_IN)) == b"\x00\x01"
    await voice.aclose()

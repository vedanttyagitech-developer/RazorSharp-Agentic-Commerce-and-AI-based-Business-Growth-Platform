"""The gateway over a real WebSocket, with real speech on it.

Everything else exercises :class:`~voice_runtime.pipeline.VoicePipeline` through an
in-memory transport. That covers the pipeline but not the one piece between it and a
browser: :class:`~voice_runtime.gateway.transport.WebSocketTransport`, and the ordering
guarantee it has to preserve -- a ``speech_chunk`` header immediately followed by the
binary frame it announced, with nothing between them, because the client sizes its next
read from that header.

So this runs the actual ASGI app on a real port, connects with a real client, redeems a
real ticket and speaks real audio down it. The only fakes are the ones a test must have:
the commerce API is an ``httpx.MockTransport`` where the socket path is under test, and
the real API where the end-to-end path is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket as socketlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
import websockets
from voice_runtime.audio import frames_of, silence
from voice_runtime.constants import MIC_FRAME_BYTES, MIC_FRAME_MS
from voice_runtime.gateway import VoiceGateway, create_app
from voice_runtime.gateway.settings import GatewaySettings
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.testing import CAPABILITIES
from voice_runtime.tts.synth import FakeSynthesizer

pytestmark = pytest.mark.slow

ORIGIN = "http://localhost:3000"


def free_port() -> int:
    with socketlib.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def api_double(reply: str = "Adding milk. Anything else?") -> httpx.AsyncClient:
    """The commerce API, stubbed. What is under test here is the socket, not RazorAI."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agent/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        return httpx.Response(200, json={"reply": reply, "language": "en", "structured": None})

    return httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))


@contextlib.asynccontextmanager
async def running_gateway(**overrides: Any) -> AsyncIterator[tuple[str, VoiceGateway]]:
    """The real ASGI app on a real port, torn down afterwards."""
    port = free_port()
    gateway = VoiceGateway(
        GatewaySettings(project="p", use_vertex=True, allowed_origins=(ORIGIN,)),
        http_client=overrides.pop("http_client", None) or api_double(),
        stt_factory=overrides.pop("stt_factory", FakeSttFactory()),
        synthesizer=overrides.pop("synthesizer", FakeSynthesizer()),
    )
    config = uvicorn.Config(create_app(gateway), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - the server failed to bind
            raise AssertionError("gateway did not start")
        yield f"127.0.0.1:{port}", gateway
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await gateway.aclose()


async def mint(base: str) -> str:
    async with httpx.AsyncClient(base_url=f"http://{base}") as client:
        response = await client.post(
            "/v1/voice/tickets", headers={"Authorization": "Bearer buyer-token"}
        )
    assert response.status_code == 200, response.text
    return str(response.json()["ticket"])


async def collect(socket: Any, *, until: str, timeout: float = 20.0) -> list[tuple[str, Any]]:
    """Read frames until ``until`` arrives, preserving JSON/binary interleaving."""
    seen: list[tuple[str, Any]] = []

    async def pump() -> None:
        while True:
            message = await socket.recv()
            if isinstance(message, bytes):
                seen.append(("audio", message))
                continue
            body = json.loads(message)
            seen.append(("json", body))
            if body.get("type") == until:
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return seen


@pytest.mark.asyncio
async def test_a_real_socket_serves_the_contract_and_carries_audio_in_order() -> None:
    async with running_gateway() as (base, gateway):
        ticket = await mint(base)
        async with websockets.connect(
            f"ws://{base}/v1/voice/stream?ticket={ticket}",
            additional_headers={"Origin": ORIGIN},
        ) as socket:
            ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert ready["type"] == "session_ready"
            assert ready["input"]["sample_rate_hz"] == 16000
            assert ready["output"]["sample_rate_hz"] == 24000
            assert ready["voice_is_authority"] is False

            await socket.send(json.dumps({"type": "text_input", "text": "add milk"}))
            seen = await collect(socket, until="speech_end")

    kinds = [body["type"] for tag, body in seen if tag == "json"]
    assert kinds.index("agent_reply") < kinds.index("speech_start"), (
        "over a real socket too: the buyer reads it before they hear it"
    )
    # The header/binary pairing survives the transport. This is the property the
    # in-memory transport cannot prove, and the client sizes its next read from it.
    pairs = [
        (tag, body)
        for tag, body in seen
        if tag == "audio" or (tag == "json" and body.get("type") == "speech_chunk")
    ]
    assert pairs, "the reply was spoken"
    for header, audio in zip(pairs[::2], pairs[1::2], strict=True):
        assert header[0] == "json" and audio[0] == "audio"
        assert header[1]["byte_length"] == len(audio[1])
    assert gateway.sockets_opened == 1


@pytest.mark.asyncio
async def test_real_microphone_frames_reach_the_recognizer_over_the_socket() -> None:
    """Binary frames in, unchanged, at the 16 kHz contract."""
    factory = FakeSttFactory()
    async with running_gateway(stt_factory=factory) as (base, _):
        ticket = await mint(base)
        async with websockets.connect(
            f"ws://{base}/v1/voice/stream?ticket={ticket}",
            additional_headers={"Origin": ORIGIN},
        ) as socket:
            await asyncio.wait_for(socket.recv(), timeout=10)  # session_ready
            spoken = bytes(range(256)) * 40  # 10240 bytes: not silence, and even-length
            frames = frames_of(spoken, MIC_FRAME_BYTES)
            for frame in frames:
                await socket.send(frame)
                await asyncio.sleep(MIC_FRAME_MS / 1000)
            for _ in range(60):
                if factory.sessions and len(factory.sessions[0].sent) >= len(frames):
                    break
                await asyncio.sleep(0.05)

    assert factory.sessions, "the recognizer session was opened"
    sent = factory.sessions[0].sent
    assert len(sent) >= len(frames)
    assert b"".join(sent[: len(frames)]) == b"".join(frames), "bytes arrive unaltered"
    assert all(len(frame) == MIC_FRAME_BYTES for frame in sent[: len(frames)])


@pytest.mark.asyncio
async def test_a_refused_origin_never_reaches_the_pipeline_over_a_real_socket() -> None:
    async with running_gateway() as (base, gateway):
        ticket = await mint(base)
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(
                f"ws://{base}/v1/voice/stream?ticket={ticket}",
                additional_headers={"Origin": "http://evil.test"},
            ):
                pass
    assert gateway.sockets_opened == 0
    assert gateway.sockets_refused == 1


@pytest.mark.asyncio
async def test_the_echo_gate_substitutes_silence_over_a_real_socket() -> None:
    """While the assistant speaks, the recognizer receives frames -- all of them silent."""
    factory = FakeSttFactory()
    async with running_gateway(stt_factory=factory) as (base, gateway):
        ticket = await mint(base)
        async with websockets.connect(
            f"ws://{base}/v1/voice/stream?ticket={ticket}",
            additional_headers={"Origin": ORIGIN},
        ) as socket:
            await asyncio.wait_for(socket.recv(), timeout=10)
            for _ in range(60):
                if factory.sessions:
                    break
                await asyncio.sleep(0.05)
            assert factory.sessions

            # Engage the gate the way a reply does, then send loud microphone audio.
            await socket.send(json.dumps({"type": "text_input", "text": "say something"}))
            await collect(socket, until="speech_start")
            loud = b"\x40\x40" * (MIC_FRAME_BYTES // 2)
            before = len(factory.sessions[0].sent)
            for _ in range(5):
                await socket.send(loud)
                await asyncio.sleep(MIC_FRAME_MS / 1000)
            for _ in range(60):
                if len(factory.sessions[0].sent) >= before + 5:
                    break
                await asyncio.sleep(0.05)

    gated = factory.sessions[0].sent[before : before + 5]
    assert len(gated) == 5, "every frame was still sent: substituted, never withheld"
    assert all(frame == silence(MIC_FRAME_BYTES) for frame in gated)
    assert gateway.sockets_opened == 1

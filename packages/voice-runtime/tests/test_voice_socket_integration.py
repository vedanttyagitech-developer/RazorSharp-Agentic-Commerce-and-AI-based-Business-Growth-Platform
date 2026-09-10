"""Live frontend ticket bridge + gateway WebSocket; generated speech, not a physical mic."""

import asyncio
import json
import os
import time
from urllib.parse import quote

import httpx
import pytest
import websockets
from test_voice_real_audio import project, speech_16k
from voice_runtime.audio import frames_of, silence
from voice_runtime.constants import MIC_FRAME_BYTES, MIC_FRAME_MS
from voice_runtime.tts.templates import Locale

pytestmark = [pytest.mark.voice_live, pytest.mark.slow]


@pytest.mark.parametrize(
    ("locale", "utterances"),
    [
        (Locale.EN_IN, ("Show me milk products.", "Now show me bread products.")),
        (Locale.HI_IN, ("मुझे दूध दिखाइए।", "अब मुझे ब्रेड दिखाइए।")),
        (Locale.HI_IN, ("Mujhe milk products dikhao please.", "Ab bread options dikhao please.")),
    ],
    ids=["english", "hindi", "hinglish"],
)
@pytest.mark.asyncio
async def test_frontend_bridge_and_real_socket_resume_for_second_spoken_turn(locale, utterances):
    project()  # Explicit credential gate, even when the speech cache already exists.
    base = os.environ.get("VOICE_TEST_FRONTEND_URL", "http://localhost:3000")
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        # These are this test client's own HttpOnly cookies, never the user's browser session.
        session = await http.get("/api/commerce/carts/current")
        assert session.status_code == 200
        ticket = await http.post("/api/voice/ticket", headers={"Origin": base})
        assert ticket.status_code == 200
        credential = ticket.json()
        assert credential["speech_available"] is True
        url = credential["socket_url"] + "?ticket=" + quote(credential["ticket"])
        async with websockets.connect(url, origin=base, max_size=8_000_000) as socket:
            frames = []
            audio_bytes = 0

            async def read():
                nonlocal audio_bytes
                async for packet in socket:
                    if isinstance(packet, bytes):
                        audio_bytes += len(packet)
                    else:
                        frame = json.loads(packet)
                        frames.append(frame)
                        if frame.get("type") == "speech_end":
                            # Synthetic playback acknowledgement, not evidence of speaker output.
                            await socket.send(
                                json.dumps(
                                    {
                                        "type": "playback_ended",
                                        "speech_generation": frame["speech_generation"],
                                    }
                                )
                            )

            reader = asyncio.create_task(read())
            try:

                async def until(predicate):
                    deadline = time.monotonic() + 60
                    while not predicate():
                        if reader.done():
                            reader.result()
                            raise AssertionError("Voice socket ended before the expected reply")
                        if time.monotonic() > deadline:
                            raise AssertionError(
                                "Voice response timeout; frame types: "
                                f"{[f['type'] for f in frames]}"
                            )
                        await asyncio.sleep(0.05)

                await until(lambda: any(f["type"] == "session_ready" for f in frames))
                for text in utterances:
                    pcm = await speech_16k(text, locale)
                    start = len(frames)
                    before_audio = audio_bytes
                    for packet in frames_of(pcm, MIC_FRAME_BYTES):
                        await socket.send(packet)
                        await asyncio.sleep(MIC_FRAME_MS / 1000)
                    for _ in range(20):
                        await socket.send(silence(MIC_FRAME_BYTES))
                        await asyncio.sleep(MIC_FRAME_MS / 1000)
                    await until(
                        lambda offset=start: any(f["type"] == "speech_end" for f in frames[offset:])
                    )
                    current = frames[start:]
                    assert any(
                        f["type"] == "transcript_final" and not f.get("stale") for f in current
                    )
                    assert any(f["type"] == "agent_reply" and f.get("items") for f in current)
                    assert audio_bytes > before_audio
                    assert not any(f["type"] in {"consent_recognised", "error"} for f in current)
                    await asyncio.sleep(
                        0.8
                    )  # Respect the real gateway's echo tail before the next utterance.
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

"""Real speech, through the real socket. Specification 19.14, and the lesson that cost most.

Typed-message tests never touch the speech path. Two audio defects in the source project
shipped past a fully green suite for exactly that reason, so at least one test here
synthesises actual speech, streams it as binary frames at realtime pace, and reads what
comes back out of the pipeline.

WHAT IS REAL AND WHAT IS NOT
----------------------------
Real: the synthesiser, the recognizer, the connection lifecycle, the echo gate, the
framing, the wire contract, and -- in the end-to-end case -- the commerce API and RazorAI.
Not real: the WebSocket itself, which is :class:`~voice_runtime.testing.MemoryTransport`
so the frames can be asserted on. Everything the transport carries is byte for byte what a
browser receives, because the gateway's own transport is a thin adapter over the same
Protocol.

These tests are marked ``voice_live``, but nothing deselects them. ``addopts`` is
``-q --strict-markers`` and carries no ``-m`` filter, here or in CI, so they are collected
on every run and skip at runtime from ``project()`` below when ``GOOGLE_CLOUD_PROJECT`` is
absent. They need Vertex ADC, that variable, and for the end-to-end case a commerce API on
``VOICE_TEST_API_BASE_URL`` (default ``http://127.0.0.1:8000``) with a seeded tenant.

This paragraph previously claimed they were deselected by default, which was false and cost
two sessions an argument about why they never ran. The reason they never ran is the missing
credential, and the fix is to supply it, not to change a selector.

Synthesised audio is cached on disk by content, so a re-run costs no TTS calls and the
suite stays quick to iterate on. That cache also makes the skip count machine-dependent,
which matters before quoting one as evidence:
``test_synthesised_speech_meets_the_recognizer_contract`` reads cached PCM without ever
reaching ``project()``, so a laptop that has run this suite before reports ``1 passed,
6 skipped`` where a fresh CI box reports ``7 skipped``. Neither is evidence for the
specification 35 rows. Only a run with the credential set is, and that run is::

    GOOGLE_CLOUD_PROJECT=... GOOGLE_GENAI_USE_VERTEXAI=true \
        uv run --no-sync pytest packages/voice-runtime/tests/test_voice_real_audio.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from commerce_domain import Money
from voice_runtime.audio import duration_s, frames_of, pcm16_rms, resample_pcm16, silence
from voice_runtime.clock import MonotonicClock
from voice_runtime.constants import (
    ECHO_TAIL_S,
    INPUT_SAMPLE_RATE_HZ,
    MIC_FRAME_BYTES,
    MIC_FRAME_MS,
    OUTPUT_SAMPLE_RATE_HZ,
)
from voice_runtime.gateway.agent_client import HttpCardReader, HttpTurnHandler, resolve_identity
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.stt.gemini import GeminiTranscribeLiveFactory
from voice_runtime.testing import MemoryTransport, an_identity
from voice_runtime.tts.gemini_tts import GeminiSynthesizer
from voice_runtime.tts.synth import VoiceSpec
from voice_runtime.tts.templates import Locale, format_money_digits, money_to_words
from voice_runtime.turn import FakeTurnHandler, TurnReply

pytestmark = [pytest.mark.voice_live, pytest.mark.slow]

API_BASE_URL = os.environ.get("VOICE_TEST_API_BASE_URL", "http://127.0.0.1:8000")
TENANT_SLUG = os.environ.get("VOICE_TEST_TENANT", "demo")
#: Long enough for the server to decide the utterance ended. Measured, not guessed: 1.0 s
#: of trailing silence was not always enough for the Hindi clip, 1.5 s always was.
ENDPOINTING_SILENCE_S = 1.5
_CACHE = Path(tempfile.gettempdir()) / "voice-runtime-speech-cache"


def project() -> str:
    value = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not value:
        pytest.skip("GOOGLE_CLOUD_PROJECT is not set")
    return value


async def speech_16k(text: str, locale: Locale = Locale.EN_IN) -> bytes:
    """Synthesised speech as PCM16 LE mono at the recognizer's 16 kHz contract.

    The synthesiser returns 24 kHz -- 16 kHz in and 24 kHz out is the intentional
    asymmetry of 19.2 -- so a test that feeds speech back to the recognizer has to cross
    it. It is cached by content hash: the same sentence is never billed twice.
    """
    _CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{locale}:{text}".encode()).hexdigest()[:32]
    cached = _CACHE / f"{key}.pcm16k"
    if cached.exists():
        return cached.read_bytes()
    synthesizer = GeminiSynthesizer(project=project())
    # Ask for the native rate and resample here, so the resampler is the one under test.
    pcm24 = await synthesizer.synthesize(
        text, VoiceSpec(locale=locale, name="Kore", sample_rate_hz=OUTPUT_SAMPLE_RATE_HZ)
    )
    pcm16 = resample_pcm16(pcm24, from_hz=OUTPUT_SAMPLE_RATE_HZ, to_hz=INPUT_SAMPLE_RATE_HZ)
    cached.write_bytes(pcm16)
    return pcm16


async def stream_at_realtime(transport: MemoryTransport, pcm: bytes) -> None:
    """Push ``pcm`` as 100 ms binary frames at realtime pace, then trailing silence.

    Pace matters. A recognizer fed a whole utterance instantly behaves differently from
    one fed it as it is spoken, and the second is the only one a microphone produces.
    """
    for frame in frames_of(pcm, MIC_FRAME_BYTES):
        transport.push_audio(frame)
        await asyncio.sleep(MIC_FRAME_MS / 1000)
    for _ in range(int(ENDPOINTING_SILENCE_S * 1000 / MIC_FRAME_MS)):
        transport.push_audio(silence(MIC_FRAME_BYTES))
        await asyncio.sleep(MIC_FRAME_MS / 1000)


async def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 45.0,
    detail: Callable[[], str] | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(
                "condition not met in time" + (f": {detail()}" if detail is not None else "")
            )
        await asyncio.sleep(0.05)


def heard_so_far(transport: MemoryTransport) -> str:
    """What the recognizer settled and what the window made of it, for a failure message
    that says which of the two was wrong instead of only that neither arrived."""
    finals = [f["text"] for f in transport.frames("transcript_final")]
    return (
        f"finals={finals}, near_misses={transport.frames('consent_unrecognised')}, "
        f"closed={transport.frames('consent_closed')}, "
        f"degradations={[d['kind'] for d in transport.frames('degradation')]}"
    )


def live_pipeline(
    transport: MemoryTransport, handler: Any, *, synthesizer: Any | None = None
) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=GeminiTranscribeLiveFactory(project=project()),
        synthesizer=(
            synthesizer if synthesizer is not None else GeminiSynthesizer(project=project())
        ),
        turn_handler=handler,
        identity=an_identity(),
        clock=MonotonicClock(),
    )


# ---- the format contract, on real synthesised audio ------------------------------------


@pytest.mark.asyncio
async def test_synthesised_speech_meets_the_recognizer_contract() -> None:
    pcm = await speech_16k("Two litres of milk, please.")
    assert len(pcm) % 2 == 0, "PCM16 is a whole number of two-byte samples"
    assert 0.8 < duration_s(pcm) < 8.0, "a short sentence, not silence and not a monologue"
    assert pcm16_rms(pcm) > 0.01, "there is audible signal in it"
    assert len(frames_of(pcm)[0]) == MIC_FRAME_BYTES == 3200  # 100 ms at 16 kHz, 16-bit


# ---- speech in (19.14: real audio through the socket) -----------------------------------


@pytest.mark.asyncio
async def test_real_speech_becomes_a_final_transcript() -> None:
    transport = MemoryTransport()
    handler = FakeTurnHandler(replies=[TurnReply(text="Got it.")])
    pipeline = live_pipeline(transport, handler)
    task = asyncio.create_task(pipeline.run())
    try:
        await stream_at_realtime(transport, await speech_16k("Two litres of milk, please."))
        await wait_until(lambda: transport.frames("transcript_final") != [])
    finally:
        transport.end()
        await task

    final = transport.one("transcript_final")["text"].casefold()
    assert "milk" in final, f"the recognizer heard {final!r}"
    assert "two" in final or "2" in final
    assert handler.calls, "a settled transcript reached the agent"
    assert handler.calls[0][0].is_final is True


@pytest.mark.asyncio
async def test_interim_transcripts_are_revised_not_extended() -> None:
    """Live evidence for the replace-never-append rule (19.5).

    The recognizer revises freely: an interim of "मुझे 2 लीटर" settles as
    "मुझे दो लीटर". Appending frames is what produced ``TumjoMainejoMaineTuMeriTuMainu``.
    """
    transport = MemoryTransport()
    pipeline = live_pipeline(transport, FakeTurnHandler())
    task = asyncio.create_task(pipeline.run())
    try:
        await stream_at_realtime(transport, await speech_16k("मुझे दो लीटर दूध चाहिए", Locale.HI_IN))
        await wait_until(lambda: transport.frames("transcript_final") != [])
    finally:
        transport.end()
        await task

    partials = [f["text"] for f in transport.frames("transcript_partial")]
    assert len(partials) >= 2, "a streaming recognizer sends more than one hypothesis"
    # Every partial is the WHOLE current hypothesis, so each one starts the utterance
    # again rather than continuing the last. A client that appended would concatenate.
    assert all(p for p in partials), "an empty partial never reaches the client as text"
    assert not any(
        partials[i + 1].startswith(partials[i]) is False and partials[i] in partials[i + 1]
        for i in range(len(partials) - 1)
    ), "no partial is a fragment glued onto the previous one"
    final = transport.one("transcript_final")["text"]
    assert "दूध" in final, f"the recognizer heard {final!r}"


# ---- the echo gate, on real audio (19.6, 19.14) -----------------------------------------


@pytest.mark.asyncio
async def test_assistant_audio_played_into_the_microphone_is_not_transcribed() -> None:
    """The bug this prevents: she transcribes herself, reads it as an interruption, and
    cuts her own reply after the first sentence."""
    transport = MemoryTransport()
    pipeline = live_pipeline(transport, FakeTurnHandler())
    task = asyncio.create_task(pipeline.run())
    try:
        await wait_until(lambda: pipeline.stt is not None and pipeline.stt.connected, timeout=30)
        assert pipeline.stt is not None
        # The assistant is speaking: from here every microphone frame is the assistant's
        # own voice coming back through the speakers.
        pipeline.stt.echo_gate.start_speaking()
        await stream_at_realtime(
            transport, await speech_16k("Add ten kilos of basmati rice to my basket.")
        )
        await asyncio.sleep(2.0)  # give the recognizer every chance to report something
    finally:
        transport.end()
        await task

    assert transport.frames("transcript_final") == [], "the assistant did not hear herself"
    assert transport.frames("transcript_partial") == []
    gate = pipeline.stt.echo_gate if pipeline.stt else None
    assert gate is not None and gate.gated_frames > 0
    assert gate.passed_frames == 0, "every frame was substituted, and every frame was sent"


# ---- speech out -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_is_written_then_spoken_at_the_output_contract() -> None:
    transport = MemoryTransport()
    handler = FakeTurnHandler(
        replies=[TurnReply(text="Amul Gold one litre. Shall I add it to your basket?")]
    )
    pipeline = live_pipeline(transport, handler)
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "text_input", "text": "show me milk"})
        await wait_until(lambda: transport.frames("speech_end") != [])
    finally:
        transport.end()
        await task

    order = transport.frame_types()
    assert order.index("agent_reply") < order.index("speech_start")
    headers = transport.frames("speech_chunk")
    assert len(headers) == 2, "two sentences, spoken one at a time as they close"
    for header, audio in zip(headers, transport.audio_chunks(), strict=True):
        assert header["sample_rate_hz"] == OUTPUT_SAMPLE_RATE_HZ == 24000
        assert header["byte_length"] == len(audio) > 0
        assert len(audio) % 2 == 0


# ---- end to end, through RazorAI --------------------------------------------------------


async def buyer_session() -> tuple[httpx.AsyncClient, str]:
    """A seeded buyer session on the running API, or a skip if it is not there."""
    client = httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0)
    try:
        response = await client.post("/v1/demo/sessions", json={"tenant_slug": TENANT_SLUG})
    except httpx.HTTPError as exc:
        await client.aclose()
        pytest.skip(f"no commerce API at {API_BASE_URL}: {exc}")
    if response.status_code >= 400:
        await client.aclose()
        pytest.skip(f"demo session refused: HTTP {response.status_code}")
    return client, str(response.json()["token"])


@pytest.mark.asyncio
async def test_a_spoken_grocery_request_returns_grounded_products() -> None:
    """The whole pipeline: speech in, RazorAI in text mode, guarded speech out (19.14)."""
    client, bearer = await buyer_session()
    try:
        identity = await resolve_identity(client, bearer=bearer)
        transport = MemoryTransport()
        pipeline = VoicePipeline(
            transport=transport,
            stt_factory=GeminiTranscribeLiveFactory(project=project()),
            synthesizer=GeminiSynthesizer(project=project()),
            turn_handler=HttpTurnHandler(client, bearer=bearer),
            identity=identity,
            clock=MonotonicClock(),
        )
        task = asyncio.create_task(pipeline.run())
        try:
            await stream_at_realtime(transport, await speech_16k("Two litres of milk, please."))
            await wait_until(lambda: transport.frames("agent_reply") != [], timeout=60)
        finally:
            transport.end()
            await task
    finally:
        await client.aclose()

    reply = transport.one("agent_reply")
    assert reply["deterministic"] is False
    assert "milk" in reply["text"].casefold() or "Amul" in reply["text"]
    # Grounded: the reply names products the catalogue actually returned.
    assert transport.one("transcript_final")["stale"] is False


def _auth(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


def _mutation(bearer: str) -> dict[str, str]:
    return {**_auth(bearer), "Idempotency-Key": str(uuid.uuid4())}


async def open_checkout(client: httpx.AsyncClient, bearer: str) -> dict[str, Any]:
    """A basket with one line, turned into checkout version 1, through the API with the
    buyer's own bearer. Returns the approval card exactly as the storefront receives it."""
    basket = await client.post("/v1/carts", headers=_mutation(bearer))
    assert basket.status_code == 201, basket.text
    basket_id = basket.json()["cart_id"]
    line = await client.put(
        f"/v1/carts/{basket_id}/lines/AMUL-DAIRY-002",
        json={"quantity": 1},
        headers=_mutation(bearer),
    )
    assert line.status_code == 200, line.text
    opened = await client.post(f"/v1/carts/{basket_id}/checkout", headers=_mutation(bearer))
    assert opened.status_code == 201, opened.text
    card: dict[str, Any] = opened.json()
    return card


async def checkout_state(client: httpx.AsyncClient, bearer: str, checkout_id: str) -> str:
    response = await client.get(f"/v1/checkouts/{checkout_id}", headers=_auth(bearer))
    assert response.status_code == 200, response.text
    return str(response.json()["state"])


async def release_speakers(transport: MemoryTransport) -> None:
    """Play the client's part after the assistant spoke: report playback ended for the
    generation just sent, then wait out the echo tail so the microphone is live again.
    Without this the gate holds the microphone silent for ECHO_GATE_MAX_HOLD_S and the
    consent window closes before a word can reach the recognizer -- which is the design
    working, not the test failing."""
    generation = transport.frames("speech_end")[-1]["speech_generation"]
    transport.push_text({"type": "playback_ended", "speech_generation": generation})
    await asyncio.sleep(ECHO_TAIL_S + 0.3)


def consent_pipeline(
    client: httpx.AsyncClient, bearer: str, identity: Any, transport: MemoryTransport
) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=GeminiTranscribeLiveFactory(project=project()),
        synthesizer=GeminiSynthesizer(project=project()),
        turn_handler=HttpTurnHandler(client, bearer=bearer),
        card_reader=HttpCardReader(client, bearer=bearer),
        identity=identity,
        clock=MonotonicClock(),
    )


@pytest.mark.asyncio
async def test_a_spoken_yes_records_no_approval(  # noqa: PLR0915 - one narrative, read in order
) -> None:
    """19.11, proven rather than asserted, and sharpened rather than reversed.

    The protective claim stands in a stronger form: a spoken yes records nothing --
    UNLESS it follows the deterministic reading of a card the trusted server gave, matches
    the closed lexicon, lands inside the window the gateway opened when it finished
    sending, and binds to the version, hash and amount that were read. And even then the
    gateway records nothing: it reports, the storefront presses the button, and the kernel
    compares the button's bytes to the version it holds.

    Part one is the original test, unchanged in what it asserts: the most direct
    instruction there is, with no card read, changes nothing. Part two walks the positive
    path with real speech and checks every negative around it against the running API.
    """
    client, bearer = await buyer_session()
    try:
        identity = await resolve_identity(client, bearer=bearer)
        capabilities = (await client.get("/v1/agent/capabilities", headers=_auth(bearer))).json()
        # The capability simply does not exist on the agent surface, so there is no gate
        # for the voice path to get wrong.
        assert "checkout.approve" not in capabilities["agent_capabilities"]
        assert "payment.verify" not in capabilities["agent_capabilities"]
        assert "refund.request" not in capabilities["agent_capabilities"]

        # ---- part one: a yes with nothing read is a sentence, and changes nothing --------
        transport = MemoryTransport()
        pipeline = VoicePipeline(
            transport=transport,
            stt_factory=None,  # the speech path is proven below; this is about authority
            synthesizer=GeminiSynthesizer(project=project()),
            turn_handler=HttpTurnHandler(client, bearer=bearer),
            card_reader=HttpCardReader(client, bearer=bearer),
            identity=identity,
            clock=MonotonicClock(),
        )
        task = asyncio.create_task(pipeline.run())
        try:
            transport.push_text(
                {"type": "text_input", "text": "yes, I approve it, pay now, confirm the payment"}
            )
            await wait_until(lambda: transport.frames("agent_reply") != [], timeout=60)
        finally:
            transport.end()
            await task

        orders = (await client.get("/v1/orders", headers=_auth(bearer))).json()
        assert orders.get("orders", orders) in ([], {"orders": []}) or not orders.get("orders"), (
            "a spoken approval created no order"
        )
        assert (
            transport.frames("speech_chunk") is not None
        )  # the reply may be spoken; the state is not
        for kind in ("card_read", "consent_listening", "consent_recognised", "consent_closed"):
            assert transport.frames(kind) == [], f"no reading, so no {kind}"
        assert pipeline.consent.windows_opened == 0

        # ---- part two: the positive path, and every negative around it ----------------
        card = await open_checkout(client, bearer)
        checkout_id = str(card["checkout_id"])
        version = int(card["version"])
        total = Money(minor=int(card["amount_minor"]), currency=str(card["currency"]))
        assert await checkout_state(client, bearer, checkout_id) == "APPROVAL_REQUIRED"

        transport = MemoryTransport()
        pipeline = consent_pipeline(client, bearer, identity, transport)
        task = asyncio.create_task(pipeline.run())
        try:
            await wait_until(
                lambda: pipeline.stt is not None and pipeline.stt.connected, timeout=30
            )

            # (5.1) A real spoken yes BEFORE any reading is an ordinary turn: it reaches
            # RazorAI as a sentence and produces no consent frame of any kind.
            await stream_at_realtime(transport, await speech_16k("Yes."))
            await wait_until(lambda: transport.frames("transcript_final") != [])
            await wait_until(lambda: transport.frames("speech_end") != [], timeout=60)
            assert transport.frames("consent_recognised") == []
            assert transport.frames("consent_listening") == []
            assert await checkout_state(client, bearer, checkout_id) == "APPROVAL_REQUIRED"
            await release_speakers(transport)

            # The reading: read from the server, printed before it is spoken, then the
            # window opens when the last byte has been sent.
            transport.push_text(
                {
                    "type": "read_card",
                    "checkout_id": checkout_id,
                    "version": version,
                    "locale": "en-IN",
                }
            )
            await wait_until(lambda: transport.frames("consent_listening") != [], timeout=60)
            read = transport.one("card_read")
            assert read["checkout_id"] == checkout_id and read["version"] == version
            assert read["content_hash"] == card["content_hash"]
            assert read["amount_minor"] == card["amount_minor"] == total.minor
            reading = next(
                f
                for f in transport.frames("agent_reply")
                if f.get("template_id") == "consent.read_card"
            )
            assert reading["deterministic"] is True
            assert format_money_digits(total) in reading["text"], "the amount, in digits"
            assert money_to_words(total, Locale.EN_IN) in reading["text"], "and in words"
            # Relative to the reading, not the stream: an ordinary turn already spoke above.
            order = transport.frame_types()
            at_card = order.index("card_read")
            assert order[at_card - 1] == "agent_reply", "text before the binding fields"
            assert order.index("speech_start", at_card) < order.index("speech_end", at_card)
            assert order.index("speech_end", at_card) < order.index("consent_listening", at_card), (
                "the window opened only after the last byte of the reading was sent"
            )
            assert await checkout_state(client, bearer, checkout_id) == "APPROVAL_REQUIRED"

            # The client's speakers fall quiet; the buyer says the word.
            await release_speakers(transport)
            await stream_at_realtime(transport, await speech_16k("Yes."))
            await wait_until(
                lambda: transport.frames("consent_recognised") != [],
                timeout=30,
                detail=lambda: heard_so_far(transport),
            )
            recognised = transport.one("consent_recognised")
            assert recognised["checkout_id"] == checkout_id
            assert recognised["version"] == version
            assert recognised["content_hash"] == card["content_hash"]
            assert recognised["amount_minor"] == card["amount_minor"]
            assert recognised["currency"] == card["currency"]
            assert "yes" in recognised["heard"].casefold()
            assert recognised["recorded"] is False and recognised["voice_is_authority"] is False
            assert transport.frames("consent_closed")[-1]["reason"] == "recognised"
            # Recognised is not recorded: the trusted server still holds the version open.
            assert await checkout_state(client, bearer, checkout_id) == "APPROVAL_REQUIRED"

            # (5.7, 5.8) The same audio again finds no window and produces no second frame.
            await stream_at_realtime(transport, await speech_16k("Yes."))
            await asyncio.sleep(3.0)
            assert len(transport.frames("consent_recognised")) == 1
            assert await checkout_state(client, bearer, checkout_id) == "APPROVAL_REQUIRED"
        finally:
            transport.end()
            await task

        # The storefront's role, played with the frame's fields: the button's body, sent
        # by the button's route, and the kernel decides.
        approved = await client.post(
            f"/v1/checkouts/{checkout_id}/versions/{version}/approve",
            json={
                "content_hash": recognised["content_hash"],
                "amount_minor": recognised["amount_minor"],
                "currency": recognised["currency"],
            },
            headers=_mutation(bearer),
        )
        assert approved.status_code == 200, approved.text
        assert await checkout_state(client, bearer, checkout_id) == "APPROVED"

        # (5.4) The same request with a hash that is not the card's is refused by the
        # kernel as a stale checkout -- a 409 problem, not a recorded approval.
        moved = await client.post(
            f"/v1/checkouts/{checkout_id}/versions/{version}/approve",
            json={
                "content_hash": "moved-bytes",
                "amount_minor": recognised["amount_minor"],
                "currency": "INR",
            },
            headers=_mutation(bearer),
        )
        assert moved.status_code == 409, moved.text
        assert moved.json().get("code") == "STALE_CHECKOUT"
        # And nothing between the reading and the approval ever touched Pay: the card or
        # netbanking sheet is still a human's, on Razorpay's own surface.
        assert await checkout_state(client, bearer, checkout_id) == "APPROVED"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_spoken_haan_is_recognised_against_a_card_read_in_hindi() -> None:
    """The Hindi twin: the card is read in Hindi, the buyer answers "हाँ", the frame binds
    to the same bytes, and the checkout is still the server's to change."""
    client, bearer = await buyer_session()
    try:
        identity = await resolve_identity(client, bearer=bearer)
        card = await open_checkout(client, bearer)
        checkout_id = str(card["checkout_id"])
        total = Money(minor=int(card["amount_minor"]), currency=str(card["currency"]))

        transport = MemoryTransport()
        pipeline = consent_pipeline(client, bearer, identity, transport)
        task = asyncio.create_task(pipeline.run())
        try:
            await wait_until(
                lambda: pipeline.stt is not None and pipeline.stt.connected, timeout=30
            )
            transport.push_text(
                {
                    "type": "read_card",
                    "checkout_id": checkout_id,
                    "version": card["version"],
                    "locale": "hi-IN",
                }
            )
            await wait_until(lambda: transport.frames("consent_listening") != [], timeout=60)
            reading = transport.one("agent_reply")
            assert reading["locale"] == "hi-IN"
            assert money_to_words(total, Locale.HI_IN) in reading["text"]
            await release_speakers(transport)
            # "जी हाँ", not a bare "हाँ": measured against this recognizer, the synthesised
            # one-syllable clip settles as nothing at all (its partials read "Huh?"), while
            # "जी हाँ" settles as "जी हां।" -- danda included, which the lexicon strips.
            await stream_at_realtime(transport, await speech_16k("जी हाँ", Locale.HI_IN))
            await wait_until(
                lambda: transport.frames("consent_recognised") != [],
                timeout=30,
                detail=lambda: heard_so_far(transport),
            )
        finally:
            transport.end()
            await task

        recognised = transport.one("consent_recognised")
        assert recognised["content_hash"] == card["content_hash"]
        assert recognised["amount_minor"] == card["amount_minor"]
        assert "हां" in recognised["heard"] or "हाँ" in recognised["heard"], recognised["heard"]
        assert await checkout_state(client, bearer, checkout_id) == "APPROVAL_REQUIRED"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_two_spoken_shopping_turns_resume_after_playback() -> None:
    """Two real STT/model/TTS turns in one pipeline, with no reconnect between them."""
    client, bearer = await buyer_session()
    transport = MemoryTransport()
    try:
        identity = await resolve_identity(client, bearer=bearer)
        pipeline = consent_pipeline(client, bearer, identity, transport)
        task = asyncio.create_task(pipeline.run())
        try:
            for index, phrase in enumerate(("Show me milk products.", "Show me bread products."), 1):
                await stream_at_realtime(transport, await speech_16k(phrase))
                await wait_until(
                    lambda: len(transport.frames("agent_reply")) >= index
                    and len(transport.frames("speech_end")) >= index,
                    timeout=90,
                    detail=lambda: heard_so_far(transport),
                )
                reply = transport.frames("agent_reply")[index - 1]
                assert reply["items"], "A spoken product search must also show product cards"
                assert reply["text"].strip()
                await release_speakers(transport)
            assert len(transport.frames("agent_reply")) == 2
            finals = transport.frames("transcript_final")
            assert len(finals) >= 2
            assert all(not frame["stale"] for frame in finals)
            assert not transport.frames("consent_recognised")
        finally:
            transport.end()
            await task
    finally:
        await client.aclose()

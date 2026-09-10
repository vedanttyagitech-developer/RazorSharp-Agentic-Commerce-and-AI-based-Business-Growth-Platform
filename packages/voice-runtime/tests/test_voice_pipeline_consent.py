"""Spoken consent through the real pipeline loop (19.11), against the offline fakes.

Every test here drives :class:`VoicePipeline` -- the object the gateway serves a browser
with -- over an in-memory transport, and asserts frames. The claim under test is the one
the design rests on: a spoken yes produces a *report* bound to the card that was read,
inside a window the server opened when it finished sending, and nothing else. The gateway
sends no approval; the last test records every outbound request across a full
read-yes-recognised flow and shows there is none.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from voice_runtime.clock import FakeClock
from voice_runtime.consent import CardReader
from voice_runtime.gateway.agent_client import HttpCardReader, HttpTurnHandler
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.stt.events import SttFinal, SttInterim
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.testing import (
    SAMPLE_CARD,
    FakeCardReader,
    MemoryTransport,
    a_card,
    an_identity,
)
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.turn import FakeTurnHandler, TurnReply

CHECKOUT_ID = SAMPLE_CARD["checkout_id"]


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
    factory: FakeSttFactory | None,
    card_reader: CardReader | None,
    synthesizer: FakeSynthesizer | None = None,
    handler: FakeTurnHandler | None = None,
    clock: FakeClock | None = None,
    consent_window_s: float = 10.0,
) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=factory,
        synthesizer=synthesizer if synthesizer is not None else FakeSynthesizer(),
        turn_handler=handler if handler is not None else FakeTurnHandler(),
        identity=an_identity(),
        clock=clock if clock is not None else FakeClock(),
        card_reader=card_reader,
        consent_window_s=consent_window_s,
        rotation_margin_s=10_000.0,
        connect_timeout_s=1.0,
        backoff_start_s=0.0,
    )


def read_card(transport: MemoryTransport, *, version: int = 1, locale: str = "en-IN") -> None:
    transport.push_text(
        {"type": "read_card", "checkout_id": CHECKOUT_ID, "version": version, "locale": locale}
    )


class Scene:
    """A pipeline with one card the server will give, listening for a word."""

    def __init__(self, **overrides: Any) -> None:
        self.transport = MemoryTransport()
        self.factory = FakeSttFactory()
        self.handler = overrides.pop("handler", FakeTurnHandler())
        self.reader = overrides.pop("reader", FakeCardReader(a_card()))
        self.clock = overrides.pop("clock", FakeClock())
        self.pipeline = build(
            transport=self.transport,
            factory=self.factory,
            card_reader=self.reader,
            handler=self.handler,
            clock=self.clock,
            **overrides,
        )
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Scene:
        self.task = asyncio.create_task(self.pipeline.run())
        await wait_until(lambda: bool(self.factory.sessions))
        return self

    async def __aexit__(self, *_: object) -> None:
        self.transport.end()
        assert self.task is not None
        await self.task

    async def listen(self, *, version: int = 1, locale: str = "en-IN") -> dict[str, Any]:
        read_card(self.transport, version=version, locale=locale)
        before = len(self.transport.frames("consent_listening"))
        await wait_until(lambda: len(self.transport.frames("consent_listening")) > before)
        return self.transport.frames("consent_listening")[-1]

    def final(self, text: str) -> None:
        self.factory.sessions[0].emit(SttFinal(text))

    async def settle(self) -> None:
        await asyncio.sleep(0.05)


# ---- text before speech, then the window --------------------------------------------------


@pytest.mark.asyncio
async def test_the_reading_and_its_binding_fields_are_on_screen_before_any_audio() -> None:
    async with Scene() as scene:
        await scene.listen()
        order = scene.transport.frame_types()
        assert order.index("agent_reply") < order.index("card_read") < order.index("speech_start")

        reply = scene.transport.one("agent_reply")
        assert reply["deterministic"] is True
        assert reply["template_id"] == "consent.read_card"
        assert reply["text"] == (
            "Version 1, ₹608.63, six hundred eight rupees and sixty-three paise. "
            "Say yes to approve this exact version, or no to decline."
        )
        assert reply["fields"]["content_hash"] == SAMPLE_CARD["content_hash"]
        assert reply["fields"]["amount_minor"] == "60863"

        card = scene.transport.one("card_read")
        assert card["checkout_id"] == CHECKOUT_ID
        assert card["version"] == 1
        assert card["content_hash"] == SAMPLE_CARD["content_hash"]
        assert card["amount_minor"] == 60863
        assert card["currency"] == "INR"
        assert scene.reader.calls == [(CHECKOUT_ID, 1)], "the gateway asked for what was named"


@pytest.mark.asyncio
async def test_the_window_opens_only_after_the_reading_has_been_fully_sent() -> None:
    async with Scene() as scene:
        listening = await scene.listen()
        order = scene.transport.frame_types()
        assert order.index("speech_end") < order.index("consent_listening"), (
            "the window is anchored on send-completion, not on a timer guessed at send time"
        )
        assert scene.transport.one("speech_end")["chunks"] > 0
        assert listening["closes_in_s"] == 10.0
        assert scene.pipeline.consent.open_window is not None


# ---- a yes, a no, and the things that are neither ------------------------------------------


@pytest.mark.asyncio
async def test_a_yes_inside_the_window_is_recognised_against_the_card_that_was_read() -> None:
    async with Scene() as scene:
        listening = await scene.listen()
        scene.clock.advance(3.0)
        scene.final("Yes.")
        await wait_until(lambda: scene.transport.frames("consent_recognised") != [])

        recognised = scene.transport.one("consent_recognised")
        assert recognised["consent_id"] == listening["consent_id"]
        assert recognised["checkout_id"] == CHECKOUT_ID
        assert recognised["version"] == 1
        assert recognised["content_hash"] == SAMPLE_CARD["content_hash"]
        assert recognised["amount_minor"] == 60863
        assert recognised["currency"] == "INR"
        assert recognised["heard"] == "Yes."
        assert recognised["offset_ms"] == 3000
        # Stated on the wire, not merely true off it.
        assert recognised["recorded"] is False
        assert recognised["voice_is_authority"] is False
        closed = scene.transport.one("consent_closed")
        assert closed == {
            "type": "consent_closed",
            "consent_id": listening["consent_id"],
            "reason": "recognised",
        }
        await scene.settle()
        assert scene.handler.calls == [], "a recognised word is not a turn; no agent ran"


@pytest.mark.asyncio
async def test_a_second_yes_finds_no_window_and_is_an_ordinary_turn() -> None:
    async with Scene() as scene:
        await scene.listen()
        scene.final("yes")
        await wait_until(lambda: scene.transport.frames("consent_recognised") != [])
        scene.final("yes please")
        await wait_until(lambda: len(scene.handler.calls) == 1)

        assert len(scene.transport.frames("consent_recognised")) == 1, "single use"
        assert scene.handler.calls[0][0].text == "yes please"
        assert scene.pipeline.consent.recognised == 1


@pytest.mark.asyncio
async def test_a_spoken_no_declines_without_touching_the_version() -> None:
    async with Scene() as scene:
        listening = await scene.listen()
        scene.final("nahi")
        await wait_until(lambda: scene.transport.frames("consent_declined") != [])

        assert scene.transport.one("consent_declined")["heard"] == "nahi"
        assert scene.transport.one("consent_closed")["reason"] == "declined"
        assert scene.transport.one("consent_closed")["consent_id"] == listening["consent_id"]
        assert scene.transport.frames("consent_recognised") == []
        await scene.settle()
        assert scene.handler.calls == []


@pytest.mark.asyncio
async def test_an_interim_yes_is_never_consent() -> None:
    async with Scene() as scene:
        await scene.listen()
        scene.factory.sessions[0].emit(SttInterim("yes"))
        await wait_until(lambda: scene.transport.frames("transcript_partial") != [])
        await scene.settle()
        assert scene.transport.frames("consent_recognised") == []
        assert scene.pipeline.consent.open_window is not None, "still listening for the final"
        # The revision settles as a no: only the FINAL settles the window.
        scene.final("no")
        await wait_until(lambda: scene.transport.frames("consent_declined") != [])


@pytest.mark.asyncio
async def test_a_near_miss_is_shown_and_the_window_stays_open() -> None:
    async with Scene() as scene:
        listening = await scene.listen()
        scene.final("yes please do it now")
        await wait_until(lambda: scene.transport.frames("consent_unrecognised") != [])
        miss = scene.transport.one("consent_unrecognised")
        assert miss == {
            "type": "consent_unrecognised",
            "consent_id": listening["consent_id"],
            "text": "yes please do it now",
            "reason": "not_in_lexicon",
        }
        assert scene.transport.frames("consent_closed") == []
        scene.final("yes")
        await wait_until(lambda: scene.transport.frames("consent_recognised") != [])


@pytest.mark.asyncio
async def test_a_hindi_reading_is_spoken_in_hindi_and_answered_in_hindi() -> None:
    async with Scene() as scene:
        await scene.listen(locale="hi-IN")
        reply = scene.transport.one("agent_reply")
        assert reply["locale"] == "hi-IN"
        assert reply["text"].startswith("संस्करण 1, ₹608.63, छह सौ आठ रुपये तिरसठ पैसे।")
        scene.final("हाँ।")
        await wait_until(lambda: scene.transport.frames("consent_recognised") != [])
        assert scene.transport.one("consent_recognised")["heard"] == "हाँ।"


# ---- every way a yes must not count --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_yes_with_no_reading_is_an_ordinary_turn_and_no_consent_frame() -> None:
    async with Scene() as scene:
        scene.final("yes, approve it, pay now")
        await wait_until(lambda: len(scene.handler.calls) == 1)
        assert scene.handler.calls[0][0].text == "yes, approve it, pay now"
        for kind in (
            "consent_listening",
            "consent_recognised",
            "consent_declined",
            "consent_unrecognised",
            "consent_closed",
            "card_read",
        ):
            assert scene.transport.frames(kind) == [], kind
        assert scene.reader.calls == [], "nothing was read; nothing was asked for"


@pytest.mark.asyncio
async def test_a_typed_yes_during_the_window_never_arms_consent() -> None:
    async with Scene() as scene:
        await scene.listen()
        scene.transport.push_text({"type": "text_input", "text": "yes"})
        await wait_until(lambda: len(scene.handler.calls) == 1)
        assert scene.handler.calls[0][0].source == "text"
        await wait_until(lambda: scene.transport.frames("speech_end") != [])
        assert scene.transport.frames("consent_recognised") == []
        assert scene.pipeline.consent.open_window is not None, "the button exists for typing"


@pytest.mark.asyncio
async def test_a_model_reply_full_of_yes_records_nothing() -> None:
    """No model is on the consent path: the agent's own text never reaches the lexicon."""
    handler = FakeTurnHandler(replies=[TurnReply(text="Yes! Yes, yes, absolutely yes.")])
    async with Scene(handler=handler) as scene:
        await scene.listen()
        scene.transport.push_text({"type": "text_input", "text": "should I?"})
        await wait_until(lambda: len(scene.transport.frames("agent_reply")) == 2)
        await wait_until(lambda: len(scene.transport.frames("speech_end")) == 2)

        assert scene.transport.frames("agent_reply")[1]["text"] == "Yes! Yes, yes, absolutely yes."
        assert scene.transport.frames("speech_chunk")[-1]["deterministic"] is False, "spoken"
        assert scene.transport.frames("consent_recognised") == []
        assert scene.pipeline.consent.recognised == 0
        assert scene.pipeline.consent.open_window is not None


@pytest.mark.asyncio
async def test_a_yes_after_the_window_lapsed_expires_it_visibly_and_counts_nothing() -> None:
    async with Scene() as scene:
        listening = await scene.listen()
        scene.clock.advance(10.5)
        scene.final("yes")
        await wait_until(lambda: scene.transport.frames("consent_closed") != [])
        assert scene.transport.one("consent_closed") == {
            "type": "consent_closed",
            "consent_id": listening["consent_id"],
            "reason": "expired",
        }
        await scene.settle()
        assert scene.transport.frames("consent_recognised") == []
        assert scene.handler.calls == [], "a late yes was an answer to the reading, not a question"


@pytest.mark.asyncio
async def test_a_playback_report_never_extends_the_window() -> None:
    async with Scene() as scene:
        await scene.listen()
        scene.clock.advance(9.0)
        generation = scene.transport.one("consent_listening")["speech_generation"]
        scene.transport.push_text(
            {
                "type": "playback_ended",
                "utterance_id": scene.transport.one("speech_start")["utterance_id"],
                "speech_generation": generation,
            }
        )
        await scene.settle()
        scene.clock.advance(2.0)
        scene.final("yes")
        await wait_until(lambda: scene.transport.frames("consent_closed") != [])
        assert scene.transport.one("consent_closed")["reason"] == "expired"
        assert scene.transport.frames("consent_recognised") == []


@pytest.mark.asyncio
async def test_an_unanswered_window_is_closed_by_the_timer_so_silence_is_visible() -> None:
    async with Scene(consent_window_s=0.05) as scene:
        listening = await scene.listen()
        await wait_until(lambda: scene.transport.frames("consent_closed") != [])
        assert scene.transport.one("consent_closed") == {
            "type": "consent_closed",
            "consent_id": listening["consent_id"],
            "reason": "expired",
        }
        assert scene.pipeline.consent.expired == 1


@pytest.mark.asyncio
async def test_a_barge_in_before_the_reading_finished_opens_no_window() -> None:
    synthesizer = FakeSynthesizer()
    synthesizer.hold = asyncio.Event()
    async with Scene(synthesizer=synthesizer) as scene:
        read_card(scene.transport)
        await wait_until(lambda: synthesizer.calls != [])
        scene.transport.push_text({"type": "barge_in"})
        await wait_until(lambda: scene.transport.frames("interrupted") != [])
        synthesizer.hold.set()
        await wait_until(lambda: scene.transport.frames("speech_end") != [])
        await scene.settle()

        assert scene.transport.one("speech_end")["cancelled"] is True
        assert scene.transport.frames("consent_listening") == [], "they did not hear the amount"
        scene.final("yes")
        await wait_until(lambda: len(scene.handler.calls) == 1)
        assert scene.transport.frames("consent_recognised") == []


@pytest.mark.asyncio
async def test_a_barge_in_after_the_window_opened_closes_it() -> None:
    async with Scene() as scene:
        listening = await scene.listen()
        scene.transport.push_text({"type": "barge_in"})
        await wait_until(lambda: scene.transport.frames("consent_closed") != [])
        assert scene.transport.one("consent_closed") == {
            "type": "consent_closed",
            "consent_id": listening["consent_id"],
            "reason": "barge_in",
        }
        scene.final("yes")
        await wait_until(lambda: len(scene.handler.calls) == 1)
        assert scene.transport.frames("consent_recognised") == []


@pytest.mark.asyncio
async def test_a_second_reading_supersedes_the_first_window() -> None:
    reader = FakeCardReader(a_card(), a_card(version=2, content_hash="v2hash", amount_minor=70000))
    async with Scene(reader=reader) as scene:
        first = await scene.listen(version=1)
        second = await scene.listen(version=2)
        assert first["consent_id"] != second["consent_id"]
        assert scene.transport.one("consent_closed") == {
            "type": "consent_closed",
            "consent_id": first["consent_id"],
            "reason": "superseded",
        }
        scene.final("yes")
        await wait_until(lambda: scene.transport.frames("consent_recognised") != [])
        recognised = scene.transport.one("consent_recognised")
        assert recognised["consent_id"] == second["consent_id"]
        assert recognised["version"] == 2
        assert recognised["content_hash"] == "v2hash"
        assert recognised["amount_minor"] == 70000


@pytest.mark.asyncio
async def test_an_utterance_in_flight_when_the_reading_ended_cannot_finalise_into_consent() -> None:
    async with Scene() as scene:
        scene.factory.sessions[0].emit(SttInterim("ye"))
        await wait_until(lambda: scene.transport.frames("transcript_partial") != [])
        listening = await scene.listen()
        scene.final("yes")  # the turn that began before the amount was read
        await wait_until(lambda: scene.transport.frames("consent_unrecognised") != [])
        assert scene.transport.one("consent_unrecognised") == {
            "type": "consent_unrecognised",
            "consent_id": listening["consent_id"],
            "text": "yes",
            "reason": "began_before_reading_ended",
        }
        assert scene.transport.frames("consent_recognised") == []
        scene.final("yes please")  # the next, whole utterance
        await wait_until(lambda: scene.transport.frames("consent_recognised") != [])
        assert scene.transport.one("consent_recognised")["heard"] == "yes please"


@pytest.mark.asyncio
async def test_a_card_the_server_will_not_give_is_a_visible_degradation() -> None:
    async with Scene(reader=FakeCardReader()) as scene:
        read_card(scene.transport, version=1)
        await wait_until(lambda: scene.transport.frames("degradation") != [])
        degraded = scene.transport.one("degradation")
        assert degraded["kind"] == "card_unavailable"
        assert degraded["transaction_state_changed"] is False
        assert "could not be read" in degraded["message"]
        assert scene.transport.frames("agent_reply") == [], "nothing was read aloud"
        assert scene.transport.frames("consent_listening") == []


@pytest.mark.asyncio
async def test_a_reading_is_refused_when_nothing_could_hear_the_answer() -> None:
    transport = MemoryTransport()
    pipeline = build(transport=transport, factory=None, card_reader=FakeCardReader(a_card()))
    task = asyncio.create_task(pipeline.run())
    await wait_until(lambda: transport.frames("degradation") != [])  # stt_unavailable
    read_card(transport)
    await wait_until(lambda: len(transport.frames("degradation")) == 2)
    assert transport.frames("degradation")[1]["kind"] == "card_unavailable"
    assert transport.frames("agent_reply") == []
    transport.end()
    await task


@pytest.mark.asyncio
async def test_a_synthesis_failure_during_the_reading_opens_no_window() -> None:
    async with Scene(synthesizer=FakeSynthesizer(fail=True)) as scene:
        read_card(scene.transport)
        await wait_until(lambda: scene.transport.frames("degradation") != [])
        assert scene.transport.one("degradation")["kind"] == "tts_failed"
        assert scene.transport.one("agent_reply")["template_id"] == "consent.read_card"
        await scene.settle()
        assert scene.transport.frames("consent_listening") == []


@pytest.mark.asyncio
async def test_the_read_card_frame_cannot_carry_a_hash_or_an_amount() -> None:
    """A client that could name the bytes would be a client-supplied consent surface."""
    async with Scene() as scene:
        for extra in ({"content_hash": "x"}, {"amount_minor": 1}, {"currency": "INR"}):
            scene.transport.push_text(
                {"type": "read_card", "checkout_id": CHECKOUT_ID, "version": 1, **extra}
            )
        await wait_until(lambda: len(scene.transport.frames("error")) == 3)
        assert {f["code"] for f in scene.transport.frames("error")} == {"invalid_frame"}
        assert scene.reader.calls == []


# ---- the gateway sends nothing but a GET ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_read_yes_recognised_flow_makes_no_request_that_could_approve() -> None:
    """Recorded at the HTTP seam, across the real adapters, with the buyer's bearer."""
    requests: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"/v1/checkouts/{CHECKOUT_ID}":
            return httpx.Response(
                200,
                json={
                    "checkout_id": CHECKOUT_ID,
                    "state": "APPROVAL_REQUIRED",
                    "current_version": 1,
                    "approval_card": SAMPLE_CARD,
                },
            )
        if request.url.path == "/v1/agent/turn":
            return httpx.Response(
                200, json={"reply": "Sure.", "language": "en", "structured": None}
            )
        return httpx.Response(404, json={"detail": "no such route"})

    transport = MemoryTransport()
    factory = FakeSttFactory()
    async with httpx.AsyncClient(
        base_url="http://api.test", transport=httpx.MockTransport(api)
    ) as client:
        pipeline = VoicePipeline(
            transport=transport,
            stt_factory=factory,
            synthesizer=FakeSynthesizer(),
            turn_handler=HttpTurnHandler(client, bearer="tok-abc"),
            card_reader=HttpCardReader(client, bearer="tok-abc"),
            identity=an_identity(),
            clock=FakeClock(),
            rotation_margin_s=10_000.0,
            connect_timeout_s=1.0,
            backoff_start_s=0.0,
        )
        task = asyncio.create_task(pipeline.run())
        await wait_until(lambda: bool(factory.sessions))

        factory.sessions[0].emit(SttFinal("what is in my basket"))  # an ordinary turn first
        await wait_until(lambda: transport.frames("agent_reply") != [])
        read_card(transport)
        await wait_until(lambda: transport.frames("consent_listening") != [])
        factory.sessions[0].emit(SttFinal("yes"))
        await wait_until(lambda: transport.frames("consent_recognised") != [])
        await asyncio.sleep(0.05)
        transport.end()
        await task

    recognised = transport.one("consent_recognised")
    assert recognised["content_hash"] == SAMPLE_CARD["content_hash"]
    assert recognised["amount_minor"] == SAMPLE_CARD["amount_minor"]

    seen = [(request.method, request.url.path) for request in requests]
    assert ("GET", f"/v1/checkouts/{CHECKOUT_ID}") in seen
    assert all("approve" not in path for _, path in seen), seen
    assert [path for method, path in seen if method == "POST"] == ["/v1/agent/turn"], (
        "the only POST the gateway ever makes is a turn; the yes produced no request at all"
    )
    for request in requests:
        assert request.headers.get("authorization") == "Bearer tok-abc"
    # And the turn that DID go out carried a message and nothing else.
    turn = next(request for request in requests if request.url.path == "/v1/agent/turn")
    assert set(json.loads(turn.content)) == {"message"}

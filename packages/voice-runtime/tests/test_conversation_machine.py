"""Adversarial conversational scenarios without a model or network."""

import asyncio

import pytest
from voice_runtime.intents import IntentLedger, IntentOutcome
from voice_runtime.listening import ListeningTurn, OnsetBuffer
from voice_runtime.playback import PlaybackLedger
from voice_runtime.tts.templates import Locale
from voice_runtime.turn import TurnReply


def test_cancel_and_completion_are_exactly_once():
    intents = IntentLedger()
    first = intents.open(1, "voice")
    assert intents.start(first.intent_id)
    assert intents.close(first.intent_id, IntentOutcome.CANCELLED)
    assert intents.close(first.intent_id, IntentOutcome.ANSWERED) is None
    assert not intents.deliverable(first.intent_id)
    assert not intents.start(first.intent_id)


def test_new_intent_suppresses_delivery_without_cancelling_backend_work():
    intents = IntentLedger()
    first = intents.open(1, "voice")
    intents.start(first.intent_id)
    second = intents.open(-1, "text")
    assert first.started and first.outcome is None
    assert not intents.deliverable(first.intent_id)
    assert intents.deliverable(second.intent_id)
    intents.interrupt_speech()
    assert second.speech_suppressed and second.outcome is None


def test_history_is_bounded_but_in_flight_work_is_not_lost():
    intents = IntentLedger(history_limit=3)
    running = intents.open(0, "voice")
    intents.start(running.intent_id)
    for i in range(20):
        entry = intents.open(i, "text")
        intents.close(entry.intent_id, IntentOutcome.ANSWERED)
    assert intents.get(running.intent_id) is running
    assert len(intents._entries) == 4


def test_audio_sent_does_not_mean_audio_heard_and_stale_acks_are_harmless():
    playback = PlaybackLedger()
    first = playback.announce(1)
    playback.sent(first.utterance_id)
    assert not playback.audible
    assert not playback.end(first.utterance_id)
    assert playback.started(first.utterance_id, 0)
    assert playback.audible == {first.utterance_id}
    playback.interrupt()
    second = playback.announce(2)
    playback.started(second.utterance_id, 1)
    assert not playback.end(first.utterance_id)
    assert playback.audible == {second.utterance_id}
    assert not playback.end(second.utterance_id)
    playback.sent(second.utterance_id)
    assert playback.end(second.utterance_id)
    assert not playback.audible


@pytest.mark.parametrize(
    "partial,final",
    [
        ("Add milk", "Add milk, no, make it two packets"),
        ("दूध डालो", "दूध डालो, नहीं, दो पैकेट डालो"),
        ("Doodh add karo", "Doodh add karo, nahi, do packet karo"),
        ("Pay with", "Pay with Razorpay"),
    ],
)
def test_pause_and_revised_partial_are_one_request(partial, final):
    listening = ListeningTurn(quiet_ms=550)
    listening.activity(True, 0)
    assert listening.transcript(partial, final=False, sequence=1)
    listening.activity(False, 200)
    assert listening.settle(1500) is None  # no provider endpoint: never act on an interim
    listening.activity(True, 1600)
    assert listening.transcript(final, final=True, sequence=1)
    assert listening.settle(3000) is None  # user still talking
    listening.activity(False, 3100)
    assert listening.settle(3500) is None
    result = listening.settle(3650)
    assert result.text == final
    assert listening.settle(4000) is None
    listening.activity(True, 4200)
    assert not listening.transcript("stale previous final", final=True, sequence=1)
    assert listening.sequence == 2


def test_onset_is_bounded_preserves_first_word_bytes_and_drains_once():
    onset = OnsetBuffer(max_bytes=8)
    onset.append(b"0000")
    onset.append(b"1111")
    onset.append(b"2233")
    assert onset.drain() == b"11112233"
    assert onset.drain() == b""
    with pytest.raises(ValueError):
        onset.append(b"x")


@pytest.mark.asyncio
async def test_every_failed_backend_intent_closes_and_next_turn_works():
    from test_voice_pipeline import build, wait_until
    from voice_runtime.testing import MemoryTransport

    class Handler:
        async def handle_turn(self, turn, identity):
            if turn.text == "first":
                raise RuntimeError("provider unavailable")
            return TurnReply(text="Which pack would you like?", locale=Locale.EN_IN)

    transport = MemoryTransport()
    pipeline = build(transport=transport, handler=Handler())
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "text_input", "text": "first"})
        await wait_until(lambda: len(transport.frames("turn_closed")) == 1)
        transport.push_text({"type": "text_input", "text": "second"})
        await wait_until(lambda: len(transport.frames("turn_closed")) == 2)
        assert [row["outcome"] for row in transport.frames("turn_closed")] == ["failed", "answered"]
        assert len({row["intent_id"] for row in transport.frames("turn_closed")}) == 2
        assert len(transport.frames("agent_reply")) == 1
    finally:
        transport.end()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["speak", "cancel"])
async def test_barge_in_distinguishes_interrupting_audio_from_cancelling_request(reason):
    from test_voice_pipeline import build, wait_until
    from voice_runtime.testing import MemoryTransport

    entered, release = asyncio.Event(), asyncio.Event()

    class Handler:
        async def handle_turn(self, turn, identity):
            entered.set()
            await release.wait()
            return TurnReply(
                text="Milk is available.",
                offer={"sku": "MILK", "quantity": 2},
                offer_is_proposal=True,
            )

    transport = MemoryTransport()
    pipeline = build(transport=transport, handler=Handler())
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "text_input", "text": "add two milk"})
        await entered.wait()
        transport.push_text({"type": "barge_in", "reason": reason})
        await wait_until(lambda: bool(transport.frames("interrupted")))
        release.set()
        await wait_until(lambda: bool(transport.frames("turn_closed")))
        await wait_until(lambda: not pipeline._turn_tasks)
        assert len(transport.frames("turn_closed")) == 1
        if reason == "cancel":
            assert not transport.frames("agent_reply")
            assert transport.frames("turn_closed")[0]["outcome"] == "cancelled"
        else:
            assert transport.frames("agent_reply")[0]["unspoken"]
            assert not transport.frames("agent_reply")[0]["offer_is_proposal"]
            assert not transport.frames("speech_start")
    finally:
        release.set()
        transport.end()
        await task


@pytest.mark.asyncio
async def test_loading_checkout_fences_shopping_before_a_bill_id_exists():
    from test_voice_pipeline import build, wait_until
    from voice_runtime.testing import MemoryTransport
    from voice_runtime.turn import FakeTurnHandler

    handler = FakeTurnHandler()
    transport = MemoryTransport()
    pipeline = build(transport=transport, handler=handler)
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "screen_context", "scope": "checkout"})
        transport.push_text({"type": "text_input", "text": "pay with UAP"})
        await wait_until(lambda: bool(transport.frames("turn_closed")))
        assert not handler.calls
        assert transport.frames("turn_closed")[0]["outcome"] == "routed_to_checkout"
        transport.push_text({"type": "screen_context", "scope": "shopping"})
        transport.push_text({"type": "text_input", "text": "show milk"})
        await wait_until(lambda: bool(handler.calls))
    finally:
        transport.end()
        await task


def test_failed_or_stale_checkout_read_never_reopens_shopping():
    from voice_runtime.focus import ConversationFocus

    focus = ConversationFocus()
    first = focus.enter_checkout()
    second = focus.enter_checkout()
    assert not focus.resolve(first, verified=True)
    assert focus.resolve(second, verified=False)
    assert not focus.shopping_allowed
    focus.leave_checkout()
    assert not focus.resolve(second, verified=True)
    assert focus.shopping_allowed


@pytest.mark.asyncio
async def test_hung_synthesis_does_not_hold_next_intent_or_its_completion():
    from test_voice_pipeline import build, wait_until
    from voice_runtime.testing import MemoryTransport
    from voice_runtime.tts.synth import FakeSynthesizer

    synth = FakeSynthesizer()
    synth.hold = asyncio.Event()
    transport = MemoryTransport()
    pipeline = build(transport=transport, synthesizer=synth)
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "text_input", "text": "Show milk"})
        await wait_until(lambda: bool(synth.calls))
        assert len(transport.frames("turn_closed")) == 1
        assert not transport.frames("speech_end")
        transport.push_text({"type": "text_input", "text": "Actually bread"})
        await wait_until(lambda: len(transport.frames("turn_closed")) == 2)
        assert len(transport.frames("agent_reply")) == 2
        assert [row["intent_id"] for row in transport.frames("turn_opened")] == [1, 2]
    finally:
        transport.end()
        await task


@pytest.mark.asyncio
async def test_old_playback_ack_cannot_release_new_utterance_in_same_generation():
    from test_voice_pipeline import build, wait_until
    from voice_runtime.stt.fakes import FakeSttFactory
    from voice_runtime.testing import MemoryTransport

    transport = MemoryTransport()
    pipeline = build(transport=transport, factory=FakeSttFactory())
    task = asyncio.create_task(pipeline.run())
    try:
        await wait_until(lambda: pipeline.stt is not None)
        a = pipeline.playback.announce(1)
        b = pipeline.playback.announce(2)
        for row in (a, b):
            pipeline.playback.sent(row.utterance_id)
            transport.push_text(
                {
                    "type": "playback_started",
                    "utterance_id": row.utterance_id,
                    "speech_generation": 0,
                }
            )
        await wait_until(lambda: len(pipeline.playback.audible) == 2)
        transport.push_text(
            {"type": "playback_ended", "utterance_id": a.utterance_id, "speech_generation": 0}
        )
        await wait_until(lambda: pipeline.playback.audible == {b.utterance_id})
        assert pipeline.stt.echo_gate.speaking
        transport.push_text(
            {"type": "playback_ended", "utterance_id": a.utterance_id, "speech_generation": 0}
        )
        await wait_until(lambda: pipeline.metrics.stale_playback_reports == 1)
        assert pipeline.stt.echo_gate.speaking
    finally:
        transport.end()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["Bill dikhao.", "बिल दिखाओ।", "Show me my bill", "proceed to checkout"]
)
async def test_review_navigation_does_not_race_a_shopping_reply(text):
    from test_voice_pipeline import build, wait_until
    from voice_runtime.testing import MemoryTransport
    from voice_runtime.turn import FakeTurnHandler

    handler = FakeTurnHandler()
    transport = MemoryTransport()
    pipeline = build(transport=transport, handler=handler)
    task = asyncio.create_task(pipeline.run())
    try:
        transport.push_text({"type": "text_input", "text": text})
        await wait_until(lambda: bool(transport.frames("turn_closed")))
        assert not handler.calls
        assert not transport.frames("agent_reply")
        assert transport.frames("turn_closed")[0]["outcome"] == "routed_to_checkout"
    finally:
        transport.end()
        await task


@pytest.mark.parametrize(
    "text",
    [
        "bill mat dikhao",
        "What is checkout?",
        "show milk",
        "Razorpay refund status",
        "do not place order",
    ],
)
def test_review_navigation_does_not_steal_questions_or_refusals(text):
    from voice_runtime.focus import requests_checkout_review

    assert not requests_checkout_review(text)


@pytest.mark.parametrize(
    "text",
    [
        "Checkout pe chal",
        "checkout par chalo",
        "Ab checkout pe chaliye",
        "चेकआउट पे चलो",
        "review",
        "review karo",
    ],
)
def test_conversational_review_matches_buyer_navigation(text):
    from voice_runtime.focus import requests_checkout_review

    assert requests_checkout_review(text)

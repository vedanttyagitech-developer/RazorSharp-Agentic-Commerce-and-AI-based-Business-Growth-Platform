"""19.14's money and language cases: what is spoken must be what the card says.

Every test here is about the sentence a buyer actually hears when money is involved. The
rule underneath all of them is one line of specification 19.10: the model does not author
these sentences. They are rendered from versioned templates filled with server-confirmed
fields, and the amount in the audio is the same integer as the amount on the approval card.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

import pytest
from commerce_domain import Money
from transaction_kernel.contracts import CheckoutRef, Delta, KernelDecision
from transaction_kernel.recovery import RecoveryCode
from voice_runtime.clock import FakeClock
from voice_runtime.pipeline import VoicePipeline
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.testing import MemoryTransport, an_identity
from voice_runtime.tts.guard import amounts_in
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.tts.templates import Locale, format_money_digits, render_decision
from voice_runtime.turn import FakeTurnHandler, TurnReply

CHECKOUT = CheckoutRef(checkout_id=uuid.uuid4(), version=3, content_hash="c0ffee")


def a_decision(
    code: RecoveryCode = RecoveryCode.OK,
    explanation: str = "reason",
    deltas: tuple[Delta, ...] = (),
) -> KernelDecision:
    allowed = code is RecoveryCode.OK
    return KernelDecision(
        decision_id=uuid.uuid4(),
        allowed=allowed,
        code=code,
        explanation=explanation,
        deltas=deltas,
        checkout=CHECKOUT,
        # The kernel refuses an allowed decision that names no Execution Grant: every
        # provider mutation consumes exactly one. A test that skipped it would be
        # rendering speech for a decision the kernel would never have produced.
        grant_id=uuid.uuid4() if allowed else None,
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def build(transport: MemoryTransport, handler: FakeTurnHandler) -> VoicePipeline:
    return VoicePipeline(
        transport=transport,
        stt_factory=FakeSttFactory(),
        synthesizer=FakeSynthesizer(),
        turn_handler=handler,
        identity=an_identity(),
        clock=FakeClock(),
        rotation_margin_s=10_000.0,
        connect_timeout_s=1.0,
        backoff_start_s=0.0,
    )


async def speak_a_decision(reply: TurnReply) -> MemoryTransport:
    transport = MemoryTransport()
    pipeline = build(transport, FakeTurnHandler(replies=[reply]))
    task = asyncio.create_task(pipeline.run())
    transport.push_text({"type": "text_input", "text": "what is my total"})
    try:
        await wait_until(lambda: transport.frames("speech_end") != [])
    finally:
        transport.end()
        await task
    return transport


# ---- the spoken amount is the card's amount --------------------------------------------


@pytest.mark.asyncio
async def test_the_spoken_amount_is_the_exact_integer_the_card_carries() -> None:
    """19.14: spoken amount and currency match the trusted approval card exactly."""
    total = Money(minor=39500, currency="INR")
    transport = await speak_a_decision(
        TurnReply(decision=a_decision(), amount=total, locale=Locale.EN_IN)
    )

    reply = transport.one("agent_reply")
    assert reply["deterministic"] is True, "a money sentence is never model prose"
    # The audit fields carry the integer, not a rendering of it.
    assert reply["fields"]["amount_minor"] == "39500"
    assert reply["fields"]["currency"] == "INR"
    assert reply["fields"]["content_hash"] == "c0ffee"
    assert reply["template_id"].startswith("decision.")
    assert reply["template_version"] == 1

    spoken = " ".join(f["text"] for f in transport.frames("speech_chunk"))
    assert amounts_in(spoken) == {39500}, (
        f"the audio says {amounts_in(spoken)}, the card says 39500"
    )
    assert format_money_digits(total) in spoken


@pytest.mark.asyncio
async def test_a_deterministic_money_sentence_is_never_refused_by_the_guard() -> None:
    """The guard checks model prose. A template is server-authored and passes whole."""
    transport = await speak_a_decision(
        TurnReply(
            decision=a_decision(RecoveryCode.REAPPROVAL_REQUIRED, "a_newer_version_exists"),
            amount=Money(minor=39500, currency="INR"),
            previous_amount=Money(minor=34000, currency="INR"),
        )
    )
    assert transport.frames("degradation") == [], "nothing was refused"
    assert transport.frames("speech_chunk"), "the refusal was spoken"


@pytest.mark.asyncio
async def test_a_refusal_speaks_every_delta_rather_than_summarising_it() -> None:
    """A buyer who does not hear the delta has not consented to the new price."""
    deltas = (
        Delta(field_path="total_minor", approved=34000, current=39500, reason="price_changed"),
    )
    transport = await speak_a_decision(
        TurnReply(
            decision=a_decision(RecoveryCode.REAPPROVAL_REQUIRED, "total_changed", deltas),
            amount=Money(minor=39500, currency="INR"),
            previous_amount=Money(minor=34000, currency="INR"),
        )
    )
    reply = transport.one("agent_reply")
    assert reply["fields"]["delta_count"] == "1"
    assert reply["fields"]["delta.0.field_path"] == "total_minor"
    spoken = " ".join(f["text"] for f in transport.frames("speech_chunk"))
    assert {34000, 39500} <= amounts_in(spoken), "both the old and the new figure are said"


# ---- language ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [Locale.EN_IN, Locale.HI_IN])
async def test_switching_language_does_not_alter_the_amount_or_the_currency(
    locale: Locale,
) -> None:
    """19.14: language switching does not alter structured amount or currency."""
    total = Money(minor=129950, currency="INR")
    transport = await speak_a_decision(
        TurnReply(decision=a_decision(), amount=total, locale=locale)
    )
    reply = transport.one("agent_reply")
    assert reply["locale"] == str(locale)
    # The script changes. The integer does not.
    assert reply["fields"]["amount_minor"] == "129950"
    assert reply["fields"]["currency"] == "INR"
    assert reply["fields"]["amount_digits"] == "₹1,299.50"


def test_the_same_amount_renders_identically_in_both_scripts() -> None:
    total = Money(minor=39500, currency="INR")
    english = render_decision(a_decision(), locale=Locale.EN_IN, amount=total)
    hindi = render_decision(a_decision(), locale=Locale.HI_IN, amount=total)
    assert english.text != hindi.text, "the sentence is in the buyer's script"
    assert english.fields["amount_minor"] == hindi.fields["amount_minor"] == "39500"
    assert english.fields["amount_digits"] == hindi.fields["amount_digits"]
    assert english.fields["currency"] == hindi.fields["currency"] == "INR"


def test_hinglish_is_spoken_in_hindi_but_the_figure_is_untouched() -> None:
    """A Hinglish buyer hears Hindi, because an English voice mispronounces "chahiye"."""
    from voice_runtime.gateway.agent_client import locale_for_language

    assert locale_for_language("hi-Latn") is Locale.HI_IN
    total = Money(minor=7300, currency="INR")
    rendered = render_decision(a_decision(), locale=locale_for_language("hi-Latn"), amount=total)
    assert rendered.fields["amount_minor"] == "7300"
    assert rendered.fields["amount_digits"] == "₹73.00"


# ---- what speech cannot do --------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_audio_cannot_repeat_a_payment_submission() -> None:
    """19.11: an approval binds to a checkout version and hash, and is single-use.

    Replaying the same utterance produces the same *intent* twice, which is exactly what a
    replay attack has: two identical transcripts. Neither carries authority. The turn body
    the gateway can send has one field, and it is a message, so the second replay reaches
    the agent as another sentence and changes nothing.
    """
    transport = MemoryTransport()
    handler = FakeTurnHandler(
        replies=[
            TurnReply(text="I cannot pay. Use the approval card on screen."),
            TurnReply(text="Still no. Payment happens on the trusted surface."),
        ]
    )
    pipeline = build(transport, handler)
    task = asyncio.create_task(pipeline.run())
    try:
        for _ in range(2):
            transport.push_text({"type": "text_input", "text": "yes, submit the payment now"})
        await wait_until(lambda: len(handler.calls) == 2)
    finally:
        transport.end()
        await task

    # Two turns ran, and both were only ever sentences.
    assert len(handler.calls) == 2
    assert all(call[0].source == "text" for call in handler.calls)

    # The load-bearing assertion. An earlier version checked that no frame of type
    # "approval"/"payment"/"authorization" was sent -- but no such type exists in the wire
    # contract, so that line could never fail whatever the code did. Assert against the
    # contract itself: the union of everything this socket CAN say carries no verb that
    # moves money. If someone adds one, this fails, which is the point.
    from voice_runtime.wire.frames import ClientFrame, ServerFrame

    def literals(annotation: object) -> set[str]:
        found: set[str] = set()
        for model in getattr(annotation, "__args__", ()):
            for member in getattr(model, "__args__", (model,)):
                field = getattr(member, "model_fields", {}).get("type")
                if field is not None and isinstance(field.default, str):
                    found.add(field.default)
        return found

    vocabulary = literals(ServerFrame) | literals(ClientFrame)
    assert vocabulary, "the wire contract has frame types"
    forbidden = {"approve", "approval", "pay", "payment", "refund", "cancel", "authorize"}
    leaking = {kind for kind in vocabulary if any(word in kind for word in forbidden)}
    assert not leaking, f"the voice socket can express a money action: {leaking}"


@pytest.mark.asyncio
async def test_an_stt_failure_leaves_typing_working_and_says_no_state_changed() -> None:
    """19.12: STT unavailable -> visible notice, text keeps working, no transaction moves."""
    transport = MemoryTransport()
    handler = FakeTurnHandler()
    pipeline = VoicePipeline(
        transport=transport,
        stt_factory=None,
        synthesizer=FakeSynthesizer(),
        turn_handler=handler,
        identity=an_identity(),
        clock=FakeClock(),
    )
    task = asyncio.create_task(pipeline.run())
    try:
        await wait_until(lambda: transport.frames("degradation") != [])
        transport.push_text({"type": "text_input", "text": "what is in my basket"})
        await wait_until(lambda: len(handler.calls) == 1)
    finally:
        transport.end()
        await task

    degraded = transport.one("degradation")
    assert degraded["kind"] == "stt_unavailable"
    assert degraded["text_input_available"] is True
    assert degraded["transaction_state_changed"] is False
    assert len(handler.calls) == 1, "typing still runs a turn"


# ---- the same facts, whether they arrive as an object or as a card ---------------------


def a_card(decision: KernelDecision) -> dict[str, object]:
    """The platform's own card for a decision. Built by agent-runtime, not by this test."""
    from agent_runtime.rendering.cards import decision_card

    card = decision_card(decision)["card"]
    assert isinstance(card, dict)
    return card


def test_a_decision_card_renders_from_the_same_templates_as_the_object() -> None:
    """Two entry points, one set of sentences. If they drift, this fails.

    The card is what actually arrives over HTTP; the ``KernelDecision`` is what the kernel
    hands to an in-process caller. Rendering them through separate template tables would
    let a buyer hear one sentence on one path and a different one on the other.
    """
    from voice_runtime.tts.templates import render_decision_card

    deltas = (Delta(field_path="total", approved=34000, current=39500, reason="total_changed"),)
    decision = a_decision(RecoveryCode.REAPPROVAL_REQUIRED, "total_changed", deltas)
    for locale in (Locale.EN_IN, Locale.HI_IN):
        from_object = render_decision(decision, locale=locale)
        from_card = render_decision_card(a_card(decision), locale=locale)
        assert from_card.template_id == from_object.template_id
        assert from_card.template_version == from_object.template_version
        assert from_card.locale is from_object.locale
        for key in ("code", "reason_key", "allowed", "delta_count", "currency"):
            assert from_card.fields[key] == from_object.fields[key], key
        # Every delta is named on both paths -- a buyer who does not hear the delta has
        # not consented to the new price.
        assert from_card.fields["delta.0.approved"] == "34000"
        assert from_card.fields["delta.0.current"] == "39500"


@pytest.mark.parametrize("code", list(RecoveryCode))
def test_every_recovery_code_renders_from_a_card_in_both_locales(code: RecoveryCode) -> None:
    """A decision this module cannot say is one the buyer would be left to read alone."""
    from voice_runtime.tts.templates import render_decision_card

    card = a_card(a_decision(code, "reason"))
    for locale in (Locale.EN_IN, Locale.HI_IN):
        rendered = render_decision_card(card, locale=locale)
        assert rendered.text.strip(), f"{code} says nothing in {locale}"
        assert rendered.deterministic is True


@pytest.mark.asyncio
async def test_a_card_is_spoken_deterministically_and_the_guard_lets_it_through() -> None:
    """The whole point of a template: it says the money fact the guard would refuse."""
    total = Money(minor=39500, currency="INR")
    decision = a_decision(RecoveryCode.REAPPROVAL_REQUIRED, "a_newer_version_exists")
    card = dict(a_card(decision))
    card["total"] = {"minor": total.minor, "currency": "INR", "display": "395.00"}
    card["current_version"] = 3

    transport = MemoryTransport()
    pipeline = build(transport, FakeTurnHandler(replies=[TurnReply(decision_card=card)]))
    task = asyncio.create_task(pipeline.run())
    transport.push_text({"type": "text_input", "text": "can I pay now"})
    try:
        await wait_until(lambda: transport.frames("speech_end") != [])
    finally:
        transport.end()
        await task

    reply = transport.one("agent_reply")
    assert reply["deterministic"] is True, "a money sentence is never model prose"
    assert reply["template_id"].startswith("decision.")
    assert reply["fields"]["amount_minor"] == "39500"
    assert transport.frames("speech_chunk"), "the template was spoken, not refused"
    assert transport.frames("degradation") == []

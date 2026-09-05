"""19.9: one tokenizer for the guard and the chunker; amounts and dandas survive."""

from __future__ import annotations

import pytest
from voice_runtime.tts.chunker import SentenceChunker, chunk_for_speech
from voice_runtime.tts.guard import SpeechGuard
from voice_runtime.tts.templates import Locale
from voice_runtime.tts.tokenizer import split_sentences


def test_amount_with_indian_grouping_and_paise_is_not_split() -> None:
    assert split_sentences("The total is ₹1,299.50 today. Thanks.") == [
        "The total is ₹1,299.50 today.",
        "Thanks.",
    ]


def test_decimal_version_and_domain_are_not_split() -> None:
    assert split_sentences("Version 3.3 is live at razorpay.com now. OK.") == [
        "Version 3.3 is live at razorpay.com now.",
        "OK.",
    ]


def test_danda_is_terminal_punctuation() -> None:
    assert split_sentences("कुल ₹395 है। धन्यवाद।") == ["कुल ₹395 है।", "धन्यवाद।"]


def test_hindi_sentence_ending_in_danda_is_one_unit() -> None:
    assert split_sentences("आपका कुल ₹1,299.50 है।") == ["आपका कुल ₹1,299.50 है।"]


def test_closing_quotes_and_newlines() -> None:
    assert split_sentences('He said "done." Then left.\nBye') == [
        'He said "done."',
        "Then left.",
        "Bye",
    ]


def test_guard_and_chunker_share_one_tokenizer_by_identity() -> None:
    assert chunk_for_speech is split_sentences
    assert SpeechGuard.tokenizer is split_sentences
    assert SentenceChunker.tokenizer is split_sentences


def test_guard_units_are_exactly_the_chunker_units() -> None:
    guard = SpeechGuard()
    samples = [
        "Sure. Adding two bananas now! Anything else?",
        "The total is ₹1,299.50 today. Thanks.",
        "कुल ₹395 है। धन्यवाद।",
        "No terminal punctuation at all",
    ]
    for text in samples:
        chunks = tuple(chunk_for_speech(text))
        assert tuple(guard.check(text, deterministic=True).allowed) == chunks
        verdict = guard.check(text, deterministic=False)
        seen = set(verdict.allowed) | {r.sentence for r in verdict.refused}
        assert seen == set(chunks)


def test_guard_refuses_money_and_outcome_sentences_from_the_model() -> None:
    """With no tool result behind it, every amount in model prose is ungrounded."""
    verdict = SpeechGuard().check(
        "Sure, adding bananas. Your total is ₹395 now. The refund is done. Anything else?",
        deterministic=False,
    )
    assert verdict.allowed == ("Sure, adding bananas.", "Anything else?")
    assert [r.reason for r in verdict.refused] == [
        "ungrounded_amount",
        "transaction_outcome_outside_template",
    ]


def test_guard_speaks_a_price_the_server_actually_returned() -> None:
    """A quoted list price is the shopping conversation, not a transactional claim."""
    verdict = SpeechGuard().check(
        "The 1 litre Amul Gold is ₹73. Shall I add it?",
        deterministic=False,
        grounded_amounts_minor=frozenset({7300, 2500}),
    )
    assert verdict.allowed == ("The 1 litre Amul Gold is ₹73.", "Shall I add it?")
    assert not verdict.refused_any


def test_guard_refuses_a_price_no_tool_returned() -> None:
    """One paisa of drift is a different amount, and a different amount is a refusal."""
    verdict = SpeechGuard().check(
        "The 1 litre Amul Gold is ₹72.99.",
        deterministic=False,
        grounded_amounts_minor=frozenset({7300}),
    )
    assert verdict.allowed == ()
    assert [r.reason for r in verdict.refused] == ["ungrounded_amount"]


def test_guard_refuses_a_transaction_outcome_even_when_its_amount_is_grounded() -> None:
    """There is no grounded version of "your refund is complete" a model may author."""
    verdict = SpeechGuard().check(
        "Your refund of ₹72 is complete.",
        deterministic=False,
        grounded_amounts_minor=frozenset({7200}),
    )
    assert verdict.allowed == ()
    assert [r.reason for r in verdict.refused] == ["transaction_outcome_outside_template"]


def test_guard_refuses_money_talk_that_names_no_figure() -> None:
    verdict = SpeechGuard().check(
        "I have the total in rupees for you.",
        deterministic=False,
        grounded_amounts_minor=frozenset({7300}),
    )
    assert [r.reason for r in verdict.refused] == ["money_fact_without_amount"]


def test_amounts_are_read_to_the_paisa_and_never_through_a_float() -> None:
    """Every example here is comma-grouped or three digits -- and that was the problem.

    The earlier version of this test used only those, which are exactly the inputs that
    survived a mis-anchored regex. UNGROUPED figures of four digits or more were being
    truncated to their first three: ``₹1234`` was read as ``₹123`` and ``₹123456789`` as
    ``₹123``. The guard checked one number while the buyer heard another, in both
    directions -- a grounded ``₹1299`` was refused, an ungrounded ``₹123456789`` spoken.
    The test was green throughout. The ungrouped cases below are the ones that matter.
    """
    from voice_runtime.tts.guard import amounts_in

    # Grouped: these always worked.
    assert amounts_in("₹1,299.50") == frozenset({129950})
    assert amounts_in("₹1,29,999") == frozenset({12999900})
    assert amounts_in("Rs. 73 and ₹25.05") == frozenset({7300, 2505})
    # 0.1 + 0.2 has no float shadow here: every value is an exact integer of minor units.
    assert amounts_in("₹0.10 and ₹0.20") == frozenset({10, 20})

    # UNGROUPED, four digits and beyond. These are the regression.
    assert amounts_in("₹1234") == frozenset({123400})
    assert amounts_in("₹1299") == frozenset({129900})
    assert amounts_in("₹12345.50") == frozenset({1234550})
    assert amounts_in("₹123456789") == frozenset({12345678900})
    assert amounts_in("Rs.12399999.99") == frozenset({1239999999})
    # Devanagari digits: \d matches them, but Decimal("७३") raises, so an unfolded
    # figure parsed to nothing -- and a sentence with no figure has nothing to check.
    assert amounts_in("₹७३०००") == frozenset({7300000})
    # A sign outside the currency mark is still a sign.
    assert amounts_in("-₹73") == frozenset({-7300})


def test_guard_lets_deterministic_template_speech_through_whole() -> None:
    text = "Payment is pending. I will not retry until Razorpay confirms the outcome."
    verdict = SpeechGuard().check(text, deterministic=True)
    assert verdict.allowed == tuple(split_sentences(text))
    assert not verdict.refused_any


def test_guard_catches_hindi_money_and_outcomes() -> None:
    verdict = SpeechGuard().check("आपका भुगतान हो गया। केले जोड़ दिए।", deterministic=False)
    assert verdict.allowed == ("केले जोड़ दिए।",)
    assert len(verdict.refused) == 1


def test_streaming_chunker_waits_for_a_confirmed_boundary() -> None:
    chunker = SentenceChunker()
    assert chunker.push("The total ") == []
    assert chunker.push("is ₹1,299.") == []  # no trailing whitespace: could still be 1,299.50
    assert chunker.push("50 today. Next") == ["The total is ₹1,299.50 today."]
    assert chunker.pending == "Next"
    assert chunker.flush() == ["Next"]
    assert chunker.pending == ""


def test_guard_speaks_the_price_format_razorai_actually_renders() -> None:
    """Captured from the running system: the currency code TRAILS the figure.

    A guard that knew only ``₹79.00`` refused this sentence as money-talk-without-a-figure,
    which silenced the entire product listing. Found by speaking to the gateway, not by
    reading the renderer.
    """
    from voice_runtime.tts.guard import amounts_in

    reply = (
        "I found 5 products: Amul Taaza Toned Milk 500 ml (79.00 INR), "
        "Amul Gold Full Cream Milk 1 L (73.00 INR), "
        "Amul Kool Kesar Flavoured Milk 180 ml (25.00 INR)."
    )
    assert amounts_in(reply) == frozenset({7900, 7300, 2500})
    verdict = SpeechGuard().check(
        reply, deterministic=False, grounded_amounts_minor=frozenset({7900, 7300, 2500})
    )
    assert not verdict.refused_any, [r.reason for r in verdict.refused]


def test_a_trailing_currency_code_must_still_be_grounded() -> None:
    verdict = SpeechGuard().check(
        "Amul Gold is 99.00 INR.",
        deterministic=False,
        grounded_amounts_minor=frozenset({7300}),
    )
    assert [r.reason for r in verdict.refused] == ["ungrounded_amount"]


# ---- splitting for synthesis latency (19.9's rule, applied in the safe direction) -------


def test_a_short_sentence_is_never_cut() -> None:
    from voice_runtime.tts.tokenizer import split_for_synthesis

    assert split_for_synthesis("Adding milk.", 160) == ["Adding milk."]


def test_an_amount_is_never_cut_by_the_phrase_split() -> None:
    """Including the SPACED grouping a model writes, which the earlier test never tried.

    "no space after the comma" was true of every example the original test used, and those
    are the only forms the splitter structurally cannot cut -- so it pinned nothing. A
    model that writes ``Rs. 1, 29, 999`` had its number cut into three pieces and the
    buyer heard one rupee.
    """
    from voice_runtime.tts.tokenizer import split_for_synthesis

    for amount in ("₹1,299.50", "₹1,29,999", "₹12,34,567.00", "1,299 rupees"):
        sentence = f"The total for everything in your basket right now comes to {amount} today."
        assert amount in " ".join(split_for_synthesis(sentence, 20))

    spaced = "Rs. 1, 29, 999 for the fridge, sir, and it is in stock right now."
    pieces = split_for_synthesis(spaced, 20)
    joined = " ".join(pieces)
    assert "1, 29, 999" in joined, f"the number was cut: {pieces}"
    # And every piece that contains part of the number contains all of it.
    carrying = [piece for piece in pieces if "29," in piece or "999" in piece]
    assert len(carrying) == 1, f"the number spans {len(carrying)} pieces: {pieces}"


def test_a_long_product_listing_is_cut_at_its_commas() -> None:
    """The real case: 21 seconds of synthesis before one sample could play."""
    from voice_runtime.tts.tokenizer import split_for_synthesis

    sentence = (
        "I found 5 products: Amul Taaza Toned Milk 500 ml (81.23 INR), "
        "Amul Gold Full Cream Milk 1 L (73.00 INR), "
        "Amul Kool Kesar Flavoured Milk 180 ml (25.00 INR), "
        "Mother Dairy Full Cream Milk 1 L (68.00 INR)."
    )
    pieces = split_for_synthesis(sentence, 80)
    assert len(pieces) > 1, "a 230-character sentence must start playing sooner than that"
    assert len(pieces[0]) <= 100, "the first phrase is short, so audio starts sooner"
    # Nothing is dropped: every character survives, only the pauses change.
    assert "".join(pieces).replace(" ", "") == sentence.replace(" ", "")


def test_a_sentence_with_no_safe_cut_is_spoken_long_rather_than_wrong() -> None:
    from voice_runtime.tts.tokenizer import split_for_synthesis

    sentence = "आपका कुल " + "बहुत " * 60 + "है।"
    assert split_for_synthesis(sentence, 40) == [sentence]


def test_the_guard_still_sees_whole_sentences() -> None:
    """The speaker's unit may be smaller than the guard's; never the other way round.

    A claim spanning a phrase boundary must be caught, which it is, because the guard
    never sees the phrases at all.
    """
    from voice_runtime.tts.tokenizer import split_for_synthesis

    sentence = "Your payment is captured, and the total was ₹395."
    assert SpeechGuard().reason_to_refuse(sentence, frozenset({39500})) is not None
    # Split for synthesis, one phrase alone would have looked innocent.
    pieces = split_for_synthesis(sentence, 25)
    assert len(pieces) > 1
    assert SpeechGuard().reason_to_refuse(pieces[1], frozenset({39500})) is None


# ---- the speaker's look-ahead ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lookahead_of_zero_still_speaks() -> None:
    """A misconfigured look-ahead must not silently turn the assistant mute."""
    import asyncio

    from voice_runtime.tts.synth import FakeSynthesizer, Speaker, SpeechChunk, SpeechGeneration

    sent: list[SpeechChunk] = []

    class Sink:
        async def send_chunk(self, chunk: SpeechChunk) -> None:
            sent.append(chunk)

    speaker = Speaker(
        synthesizer=FakeSynthesizer(),
        sink=Sink(),
        generation=SpeechGeneration(),
        lookahead=0,
    )
    result = await speaker.speak("One. Two.", locale=Locale.EN_IN, deterministic=True, generation=0)
    assert result.chunks_sent == 2
    assert [chunk.text for chunk in sent] == ["One.", "Two."]
    assert not asyncio.all_tasks() - {asyncio.current_task()}, "no task was left running"


@pytest.mark.asyncio
async def test_a_failure_in_the_lookahead_is_reported_not_dropped() -> None:
    """An un-retrieved task exception surfaces at GC time, from somewhere unrelated."""
    from voice_runtime.tts.synth import Speaker, SpeechChunk, SpeechGeneration, VoiceSpec

    class ExplodingSynthesizer:
        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, text: str, voice: VoiceSpec) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"\\x01\\x00" * 10
            raise RuntimeError("second phrase failed")

    class Sink:
        async def send_chunk(self, chunk: SpeechChunk) -> None:
            return None

    speaker = Speaker(
        synthesizer=ExplodingSynthesizer(), sink=Sink(), generation=SpeechGeneration()
    )
    result = await speaker.speak(
        "One. Two. Three.", locale=Locale.EN_IN, deterministic=True, generation=0
    )
    assert result.chunks_sent == 1
    assert result.tts_failed is True
    assert "second phrase failed" in (result.failure_detail or "")


def test_the_real_product_listing_is_spoken_whole() -> None:
    """Captured from the running gateway. A quantity word is not an amount word.

    RazorAI quotes the buyer's own request back inside its reply, so the reply contains
    "Two liters of milk" -- and a rule that refused any sentence holding both a grounded
    figure and a number word gagged the entire product listing. The number word only
    matters when it is used AS an amount, i.e. adjacent to a currency term.
    """
    reply = (
        "I found 5 products for “Two liters of milk, please.”: "
        "Amul Taaza Toned Milk 500 ml (28.00 INR), "
        "Amul Gold Full Cream Milk 1 L (73.00 INR), "
        "Amul Kool Kesar Flavoured Milk 180 ml (25.00 INR). "
        "Prices and stock are live from the store; tell me which one and how many to add."
    )
    verdict = SpeechGuard().check(
        reply, deterministic=False, grounded_amounts_minor=frozenset({2800, 7300, 2500})
    )
    assert not verdict.refused_any, [r.reason for r in verdict.refused]
    assert len(verdict.allowed) == 2


def test_a_number_word_next_to_a_currency_word_is_still_an_amount() -> None:
    guard = SpeechGuard()
    grounded = frozenset({7300})
    for sentence in (
        "That is seventy-three rupees.",
        "The total is three hundred and ninety five rupees.",
        "It costs ₹ seventy three.",
    ):
        assert guard.reason_to_refuse(sentence, grounded) == "amount_in_words", sentence


# ---- the guard must hold both languages to the SAME rule -------------------------------
#
# Hindi was held to a stricter one, and the failure was invisible from an English demo: a
# refused sentence is silence plus a degradation banner, so the buyer sees correct text and
# simply never hears it. Two causes, both fixed in `guard.py`, both asserted here.

#: Ordinary shopping talk that names no money movement. Each Hindi line is a translation of
#: the English beside it, so any difference in verdict is a difference in the RULE.
_ORDINARY: tuple[tuple[str, str], ...] = (
    ("दो पैकेट दूध जोड़ करें।", "Please add two packets of milk."),
    ("मुझे तीन केले चाहिए, जल्दी करें।", "I want three bananas, please hurry."),
    ("आपके ऑर्डर में दो पैकेट दूध हैं।", "Your order has two packets of milk."),
    ("कौन सा ऑर्डर देखना है?", "Which order would you like to see?"),
    ("यह चार लीटर वाला पैक है।", "This is the four litre pack."),
)

#: Claims that money moved or an order changed state. Refused in EVERY register (19.10).
_OUTCOMES: tuple[str, ...] = (
    "आपका ऑर्डर पूरा हो गया है।",
    "आपका ऑर्डर रद्द हो गया।",
    "आपका आदेश भेज दिया गया है।",
    "मैंने आपका रिफंड कर दिया है।",
    "पैसे लौटा दिए गए हैं।",
    "पैसा वापस हो गया है।",
    "Paisa wapas ho gaya hai.",
    "Aapka order pura ho gaya hai.",
    "Your order is complete.",
    "Your money has been returned to your account.",
    "Your payment was successful.",
)


def _refused(sentence: str) -> bool:
    verdict = SpeechGuard().check(
        sentence, deterministic=False, grounded_amounts_minor=frozenset()
    )
    return bool(verdict.refused)


@pytest.mark.parametrize(("hindi", "english"), _ORDINARY)
def test_ordinary_talk_is_spoken_in_both_languages(hindi: str, english: str) -> None:
    """Neither sentence claims money moved, so neither may be silently swallowed.

    `कर\\b` used to match inside करें, करो and जोड़कर -- Devanagari vowel signs are combining
    marks and so are not `\\w`, which leaves a word boundary standing in the middle of the
    commonest imperative in the language. Paired with a number word that made ordinary
    basket talk an "amount in words" and refused it.
    """
    assert not _refused(english), "the English control must be speakable"
    assert not _refused(hindi), "the Hindi translation must be held to the same rule"


@pytest.mark.parametrize("sentence", _OUTCOMES)
def test_a_claim_that_money_moved_is_refused_in_every_register(sentence: str) -> None:
    """The parity fix above must not have bought itself a leak.

    "Paisa wapas ho gaya hai." is in this list because an adversarial review once walked
    through an allowlist-shaped guard with exactly it. A sentence that says money moved is
    refused whether it is written in English, Devanagari or Latin-script Hindi.
    """
    assert _refused(sentence)


def test_tax_is_still_a_money_word_when_it_stands_alone() -> None:
    """Narrowing `कर` must not have deleted it: as a noun it still needs a grounded figure."""
    assert _refused("सेवा कर 50 रुपये है।")
    grounded = SpeechGuard().check(
        "सेवा कर 50 रुपये है।", deterministic=False, grounded_amounts_minor=frozenset({5000})
    )
    assert not grounded.refused, "a grounded figure is speakable"

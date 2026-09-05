"""19.9: one tokenizer for the guard and the chunker; amounts and dandas survive."""

from __future__ import annotations

from voice_runtime.tts.chunker import SentenceChunker, chunk_for_speech
from voice_runtime.tts.guard import SpeechGuard
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
    from voice_runtime.tts.guard import amounts_in

    assert amounts_in("₹1,299.50") == frozenset({129950})
    assert amounts_in("₹1,29,999") == frozenset({12999900})
    assert amounts_in("Rs. 73 and ₹25.05") == frozenset({7300, 2505})
    # 0.1 + 0.2 has no float shadow here: every value is an exact integer of minor units.
    assert amounts_in("₹0.10 and ₹0.20") == frozenset({10, 20})


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

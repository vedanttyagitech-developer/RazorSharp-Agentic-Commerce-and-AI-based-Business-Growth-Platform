"""The consent lexicon and window, as pure functions (19.11).

Nothing here touches a socket, a recognizer or a model. The lexicon is a set and the
window is arithmetic on a clock, and both are asserted exactly, in all three registers the
buyer speaks. The module under test is also asserted to import no model SDK, the same way
``tests/test_voice_templates.py`` asserts it of the templates: the consent path has no
model on it, and that is checked rather than remembered.
"""

from __future__ import annotations

import inspect
import subprocess
import sys

import pytest
from voice_runtime import consent
from voice_runtime.clock import FakeClock
from voice_runtime.consent import (
    AFFIRMATIVE,
    COMPANION,
    NEGATIVE,
    ConsentTracker,
    classify,
    tokens,
)
from voice_runtime.stt.transcript import FreshnessStamp, TranscriptTurn
from voice_runtime.testing import a_card

# ---- the lexicon ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # English
        "yes", "Yes.", "yeah", "yep", "approve", "Approved", "confirm", "yes please",
        # Hindi, with and without the danda the recognizer appends
        "हाँ", "हाँ।", "हां", "जी हाँ", "जी हां।", "मंज़ूर", "मंजूर", "स्वीकार", "हाँ जी",
        # Hinglish
        "haan", "han", "haa", "ji haan", "manzoor", "manjoor", "haan bilkul", "approve kar do",
        "haan ji", "confirm karo",
    ],
)  # fmt: skip
def test_every_affirmative_in_three_languages_is_a_yes(text: str) -> None:
    assert classify(text) == "yes", text


@pytest.mark.parametrize(
    "text",
    [
        # English
        "no", "No.", "nope", "dont", "don't", "reject", "cancel", "stop", "wait",
        # Hindi
        "नहीं", "नहीं।", "नही", "ना", "मत", "रुको", "रद्द",
        # Hinglish
        "nahi", "nahin", "nai", "na", "mat", "ruko", "raddi",
    ],
)  # fmt: skip
def test_every_negative_in_three_languages_is_a_no(text: str) -> None:
    assert classify(text) == "no", text


def test_a_negative_anywhere_wins_over_a_yes() -> None:
    """ "haan na" is a Hindi tag question. Refusing it costs nothing; accepting it could."""
    assert classify("haan na") == "no"
    assert classify("yes no") == "no"
    assert classify("yes wait") == "no"
    assert classify("हाँ नहीं") == "no"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "please",  # a companion alone is nothing
        "bilkul",
        "ji",  # also "pardon?"
        "ok",
        "okay",
        "theek hai",
        "ठीक है",
        "sahi",
        "correct",
        "yesterday",  # not a token in the set, whatever it starts with
        "yes please do it now",  # "it" and "now" are outside the sets
        "yes yes yes yes yes",  # five words is a sentence, not a word
        "say yes to approve",  # the reading's own ask, if it ever leaked back
        "Yes, approved, done!",  # a model's sentence
        "I approve of this",
        "two litres of milk",
    ],
)
def test_a_near_miss_is_nothing(text: str) -> None:
    assert classify(text) == "none", text


def test_nfkc_makes_the_precomposed_and_decomposed_nukta_the_same_word() -> None:
    """A Hindi IME emits U+095B; the table may be written with U+091C U+093C. Same word."""
    precomposed = "\u092e\u0902\u095b\u0942\u0930"  # मंज़ूर with ज़ as one code point
    decomposed = "\u092e\u0902\u091c\u093c\u0942\u0930"  # the same, with ज + nukta
    assert precomposed != decomposed, "the test is only meaningful if the strings differ"
    assert classify(precomposed) == "yes"
    assert classify(decomposed) == "yes"
    assert tokens(precomposed) == tokens(decomposed)


def test_fullwidth_latin_folds_to_ascii() -> None:
    assert classify("ｙｅｓ") == "yes"
    assert classify("ＮＯ") == "no"


def test_tokenisation_matches_the_servers_own_rule_and_drops_the_danda() -> None:
    """Same NFKC-then-casefold and word pattern as ``agent_service._tokens``, with one
    addition the recognizer forces: the danda ``।`` sits INSIDE the Devanagari block that
    pattern admits, so without stripping it a spoken "हाँ।" would be a near-miss."""
    assert tokens("Haan, Bilkul!") == ["haan", "bilkul"]
    assert tokens("जी हाँ।") == ["जी", "हाँ"]
    assert tokens("नहीं॥") == ["नहीं"]
    assert tokens("YES") == ["yes"]
    assert tokens("don't") == ["don", "t"], "an apostrophe splits, as it does server-side"


def test_the_tables_do_not_overlap() -> None:
    assert not (AFFIRMATIVE & NEGATIVE)
    assert not (COMPANION & NEGATIVE)
    assert not (AFFIRMATIVE & COMPANION)


def test_the_consent_module_never_touches_a_model_sdk() -> None:
    source = inspect.getsource(consent)
    assert "genai" not in source and "google" not in source and "httpx" not in source
    probe = (
        "import sys, voice_runtime.consent as c; "
        "assert not [m for m in sys.modules if m.startswith(('google', 'httpx'))], "
        "'a network or model SDK was imported by the consent module'"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)  # noqa: S603


# ---- the window ---------------------------------------------------------------------------


def a_turn(text: str, *, at: float, turn_id: int = 3, audio_age_s: float = 0.0) -> TranscriptTurn:
    return TranscriptTurn(
        turn_id=turn_id,
        text=text,
        is_final=True,
        stamp=FreshnessStamp(observed_at=at, generation=1, audio_age_s=audio_age_s),
    )


def open_tracker(clock: FakeClock, *, excluded: int | None = None) -> ConsentTracker:
    tracker = ConsentTracker(clock=clock, window_s=10.0)
    tracker.open(consent_id="c1", card=a_card(), speech_generation=0, excluded_turn_id=excluded)
    return tracker


def test_no_window_means_no_opinion() -> None:
    tracker = ConsentTracker(clock=FakeClock())
    observed = tracker.observe(a_turn("yes", at=1000.0))
    assert observed.kind == "no_window"
    assert observed.window is None


def test_a_yes_inside_the_window_is_recognised_and_consumes_it() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    clock.advance(4.0)
    observed = tracker.observe(a_turn("haan ji", at=1004.0))
    assert observed.kind == "recognised"
    assert observed.window is not None
    assert observed.window.card == a_card()
    assert observed.window.closed == "recognised"
    # Single use: the next yes finds no window.
    assert tracker.observe(a_turn("yes", at=1004.5)).kind == "no_window"


def test_a_no_inside_the_window_declines_and_consumes_it() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    observed = tracker.observe(a_turn("नहीं", at=1002.0))
    assert observed.kind == "declined"
    assert tracker.open_window is None


def test_a_near_miss_leaves_the_window_open() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    assert tracker.observe(a_turn("yes please do it now", at=1001.0)).kind == "unrecognised"
    assert tracker.open_window is not None, "the buyer may still answer"
    assert tracker.observe(a_turn("yes", at=1002.0)).kind == "recognised"


def test_the_window_bounds_are_inclusive_at_both_ends() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    assert tracker.observe(a_turn("yes", at=1000.0)).kind == "recognised"

    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    clock.advance(10.0)
    assert tracker.observe(a_turn("yes", at=1010.0)).kind == "recognised"


def test_a_yes_after_the_window_expires_it_visibly_and_counts_nothing() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    clock.advance(10.001)
    observed = tracker.observe(a_turn("yes", at=1010.001))
    assert observed.kind == "expired"
    assert observed.verdict == "yes", "the word was a yes; the timing was not"
    assert observed.window is not None and observed.window.closed == "expired"
    assert tracker.recognised == 0
    assert tracker.expired == 1


def test_a_yes_observed_before_the_window_opened_does_not_count() -> None:
    """A stamp older than the opening is a final that settled before the amount was read."""
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    assert tracker.observe(a_turn("yes", at=999.0)).kind == "expired"


def test_a_yes_whose_audio_is_older_than_the_window_does_not_count() -> None:
    """The recognizer working off a backlog: the word arrived now, the speech was old."""
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    clock.advance(1.0)
    assert tracker.observe(a_turn("yes", at=1001.0, audio_age_s=11.0)).kind == "expired"


def test_the_utterance_in_flight_when_the_reading_ended_cannot_become_consent() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock, excluded=7)
    observed = tracker.observe(a_turn("yes", at=1001.0, turn_id=7))
    assert observed.kind == "began_before_reading"
    assert tracker.open_window is not None, "the next, whole utterance may still answer"
    assert tracker.observe(a_turn("yes", at=1002.0, turn_id=8)).kind == "recognised"


def test_a_new_window_supersedes_the_old_one_rather_than_stacking() -> None:
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    first = tracker.open_window
    assert first is not None
    tracker.open(
        consent_id="c2", card=a_card(version=2), speech_generation=1, excluded_turn_id=None
    )
    assert first.closed == "superseded"
    assert tracker.open_window is not None and tracker.open_window.consent_id == "c2"
    observed = tracker.observe(a_turn("yes", at=1001.0))
    assert observed.kind == "recognised"
    assert observed.window is not None and observed.window.card.version == 2


def test_close_if_only_closes_the_window_it_was_started_for() -> None:
    """The expiry timer of a superseded window must not close its replacement."""
    clock = FakeClock(1000.0)
    tracker = open_tracker(clock)
    tracker.open(consent_id="c2", card=a_card(), speech_generation=1, excluded_turn_id=None)
    assert tracker.close_if("c1", "expired") is None
    assert tracker.open_window is not None and tracker.open_window.consent_id == "c2"
    assert tracker.close_if("c2", "expired") is not None
    assert tracker.open_window is None

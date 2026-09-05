"""19.5: replace, never append; empty never clears; duplicates dropped; freshness enforced."""

from __future__ import annotations

from voice_runtime.clock import FakeClock
from voice_runtime.stt.transcript import FreshnessStamp, TranscriptState, apply_stream_text


def test_merge_rule_is_replace_or_keep() -> None:
    assert apply_stream_text("held", "new") == "new"
    assert apply_stream_text("held", "") == "held"
    assert apply_stream_text("held", None) == "held"
    assert apply_stream_text("", "") == ""


def test_interim_revisions_replace_rather_than_concatenate() -> None:
    state = TranscriptState(clock=FakeClock())
    state.apply_interim("Tum jo", 1)
    state.apply_interim("Tum jo maine", 1)
    turn = state.apply_interim("Tu meri", 1)
    assert turn.text == "Tu meri"
    assert "TumjoMaine" not in turn.text.replace(" ", "")
    assert not turn.is_final


def test_empty_interim_does_not_clear_held_text() -> None:
    state = TranscriptState(clock=FakeClock())
    state.apply_interim("add bananas", 1)
    assert state.apply_interim("", 1).text == "add bananas"
    assert state.apply_interim(None, 1).text == "add bananas"


def test_final_replaces_then_closes_the_turn() -> None:
    state = TranscriptState(clock=FakeClock())
    state.apply_interim("add two banana", 3)
    final = state.apply_final("add two bananas", 3)
    assert final is not None
    assert final.is_final and final.text == "add two bananas"
    assert final.turn_id == 0 and final.generation == 3
    assert state.turn_id == 1
    assert state.held == ""


def test_empty_final_settles_the_held_hypothesis() -> None:
    state = TranscriptState(clock=FakeClock())
    state.apply_interim("checkout please", 1)
    final = state.apply_final("", 1)
    assert final is not None and final.text == "checkout please"


def test_final_with_nothing_held_is_not_a_turn() -> None:
    state = TranscriptState(clock=FakeClock())
    assert state.apply_final("", 1) is None
    assert state.turn_id == 0


def test_identical_consecutive_finals_are_deduplicated() -> None:
    state = TranscriptState(clock=FakeClock())
    assert state.apply_final("yes", 1) is not None
    assert state.apply_final("yes", 1) is None
    assert state.duplicates_dropped == 1
    # A genuine second "yes" in a later turn (a revision happened in between) is kept.
    state.apply_interim("yes", 1)
    second = state.apply_final("yes", 1)
    assert second is not None and second.turn_id == 1


def test_freshness_rejects_text_older_than_the_window() -> None:
    clock = FakeClock()
    state = TranscriptState(clock=clock, freshness_window_s=4.0)
    final = state.apply_final("add milk", 1)
    assert final is not None
    clock.advance(3.9)
    assert state.is_fresh(final)
    clock.advance(0.2)
    assert not state.is_fresh(final)
    assert state.stale_finals == 1


def test_stamp_age_never_negative() -> None:
    stamp = FreshnessStamp(observed_at=100.0, generation=1)
    assert stamp.age(99.0) == 0.0
    assert stamp.is_fresh(103.0, 4.0)
    assert not stamp.is_fresh(104.5, 4.0)

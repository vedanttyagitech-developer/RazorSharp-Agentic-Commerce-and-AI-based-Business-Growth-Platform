"""19.6: substitute silence, never withhold; the tail starts at client playback end."""

from __future__ import annotations

from voice_runtime.clock import FakeClock
from voice_runtime.stt.echo_gate import EchoGate

FRAME = bytes(range(256)) * 12  # 3072 bytes of non-silent audio


def test_substitutes_digital_silence_of_identical_length_while_speaking() -> None:
    gate = EchoGate(clock=FakeClock())
    gate.start_speaking()
    out = gate.gate(FRAME)
    assert len(out) == len(FRAME)
    assert out == bytes(len(FRAME))
    assert gate.gated_frames == 1


def test_never_withholds_a_frame_cadence_unchanged() -> None:
    gate = EchoGate(clock=FakeClock())
    gate.start_speaking()
    outputs = [gate.gate(FRAME) for _ in range(50)]
    assert len(outputs) == 50
    assert all(len(o) == len(FRAME) for o in outputs)


def test_passes_frames_through_when_idle() -> None:
    gate = EchoGate(clock=FakeClock())
    assert gate.gate(FRAME) == FRAME
    assert gate.passed_frames == 1


def test_tail_starts_when_the_client_reports_playback_end_not_when_server_finishes() -> None:
    clock = FakeClock()
    gate = EchoGate(clock=clock, tail_s=0.6)
    gate.start_speaking()
    gate.on_server_send_complete()
    clock.advance(5.0)
    assert gate.engaged, "a deeply buffered client is still playing; stay engaged"
    gate.on_client_playback_ended()
    assert gate.engaged, "tail covers speaker ring-out"
    clock.advance(0.59)
    assert gate.engaged
    clock.advance(0.02)
    assert not gate.engaged
    assert gate.gate(FRAME) == FRAME


def test_bounded_hold_releases_when_the_client_never_reports_and_is_flagged() -> None:
    clock = FakeClock()
    gate = EchoGate(clock=clock, tail_s=0.6, max_hold_s=30.0)
    gate.start_speaking()
    gate.on_server_send_complete()
    clock.advance(31.0)
    assert gate.engaged, "release starts the tail first"
    clock.advance(1.0)
    assert not gate.engaged
    assert gate.consume_hold_expired() is True
    assert gate.consume_hold_expired() is False


def test_barge_in_releases_with_tail() -> None:
    clock = FakeClock()
    gate = EchoGate(clock=clock, tail_s=0.6)
    gate.start_speaking()
    gate.on_barge_in()
    assert not gate.speaking
    assert gate.engaged
    clock.advance(0.7)
    assert not gate.engaged


def test_engagement_time_metric_is_derived_from_gated_audio() -> None:
    gate = EchoGate(clock=FakeClock())
    gate.start_speaking()
    for _ in range(10):
        gate.gate(bytes(3200))  # 100 ms each at 16 kHz PCM16
    assert abs(gate.engaged_seconds - 1.0) < 1e-9


def test_the_echo_tail_stays_below_the_shortest_leak_the_recognizer_can_hear() -> None:
    """Measured, not assumed. See ADR 0006 section 4.5 for the table.

    Transcribe Live returns nothing from a leak of 500 ms or less and starts transcribing
    at 700 ms. The tail only has to cover the client's playback-ended report plus speaker
    ring-out, and it sits under the threshold at which a leak could be heard at all. If
    someone raises the tail past that threshold they have not made it safer, they have made
    the microphone deaf for longer than necessary -- and if someone lowers the threshold
    constant to match a new measurement, this is where the two are compared.
    """
    from voice_runtime.constants import ECHO_TAIL_S, RECOGNIZABLE_LEAK_S

    assert ECHO_TAIL_S < RECOGNIZABLE_LEAK_S, (
        f"a {ECHO_TAIL_S}s tail no longer sits under the {RECOGNIZABLE_LEAK_S}s "
        "leak the recognizer can hear; re-measure before changing either"
    )
    assert ECHO_TAIL_S > 0.0

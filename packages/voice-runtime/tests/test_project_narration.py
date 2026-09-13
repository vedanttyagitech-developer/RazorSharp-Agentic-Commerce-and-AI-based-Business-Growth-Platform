import pytest
from voice_runtime.tts.guard import SpeechGuard


def test_project_explanation_is_not_a_personal_transaction_outcome():
    text = "The transaction kernel enforces constraints before the executor performs work."
    assert SpeechGuard().check(text, deterministic=False).refused_any
    assert not SpeechGuard().check(text, deterministic=False, project_narration=True).refused_any


@pytest.mark.parametrize(
    "text",
    [
        "Your payment succeeded.",
        "The kernel confirmed your payment.",
        "The kernel authorized the charge.",
        "The transaction kernel transferred funds.",
        "The checkout total is ₹500.",
        "The system credited the merchant account.",
        "आपका भुगतान पूरा हुआ।",
    ],
)
def test_project_mode_never_authorizes_transaction_claims(text):
    assert SpeechGuard().check(text, deterministic=False, project_narration=True).refused_any


def test_settled_transcript_does_not_mean_settled_funds():
    guard = SpeechGuard()
    assert not guard.check(
        "When voice input ends, settled transcripts reach the backend agent.",
        deterministic=False,
        project_narration=True,
    ).refused_any
    assert guard.check(
        "The kernel settled funds.",
        deterministic=False,
        project_narration=True,
    ).refused_any

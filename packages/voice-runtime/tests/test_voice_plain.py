"""Markdown is for the screen; the voice says the words."""

from __future__ import annotations

import pytest
from voice_runtime.tts.plain import plain_for_speech


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        (
            "Here are the options:\n\n- **Amul Gold Full Cream Milk (1 L)** – ₹73.00"
            "\n- **Amul Taaza** – ₹28.00",
            "Here are the options:\nAmul Gold Full Cream Milk (1 L) – ₹73.00\nAmul Taaza – ₹28.00",
        ),
        (
            "1. **Aashirvaad Atta 5 kg** at ₹255\n2. Sharbati at ₹310",
            "Aashirvaad Atta 5 kg at ₹255\nSharbati at ₹310",
        ),
        (
            "Your total is **₹175.50**. Say *yes* to approve.",
            "Your total is ₹175.50. Say yes to approve.",
        ),
        ("See `AMUL-DAIRY-002` or [the shelf](/c/dairy).", "See AMUL-DAIRY-002 or the shelf."),
        ("### Milk\n---\nTwo litres a day", "Milk\nTwo litres a day"),
        ("", ""),
    ],
)
def test_markup_goes_and_the_words_stay(written: str, spoken: str) -> None:
    assert plain_for_speech(written) == spoken


def test_amounts_and_currency_are_untouched() -> None:
    written = "**2 × ₹73.00 = ₹146.00**, delivery ₹25.00, tax ₹4.50, total **₹175.50**."
    spoken = plain_for_speech(written)
    for figure in ("₹73.00", "₹146.00", "₹25.00", "₹4.50", "₹175.50", "2 ×"):
        assert figure in spoken
    assert "*" not in spoken


def test_hindi_passes_through() -> None:
    assert plain_for_speech("**आपका कुल** ₹175.50 है।") == "आपका कुल ₹175.50 है।"

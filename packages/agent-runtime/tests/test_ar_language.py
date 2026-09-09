"""Language detection is deterministic, model-free, and errs towards English.

The harness, not the model, decides the reply script and the search locale from the
buyer's own words. These tests pin the three verdicts, the direction of the tie-break
(an English buyer must never be answered in Hinglish), and the absence of any model
import in the module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from agent_runtime.language import Language, detect_language
from merchant_sim import Locale

_MODULE = Path(__file__).resolve().parents[1] / "src" / "agent_runtime" / "language.py"


@pytest.mark.parametrize(
    "text",
    [
        "I need two litres of milk",
        "add milk to my cart",
        "how much is delivery?",
        "Do the eggs come in a tray of six?",
        "so me too, the same order please",
        "",
        "   ",
        "12345 !!!",
    ],
)
def test_english_and_neutral_text_is_english(text: str) -> None:
    assert detect_language(text) is Language.EN


@pytest.mark.parametrize(
    "text",
    [
        "मुझे दो लीटर दूध चाहिए",
        "दूध",
        "milk aur दही",  # any Devanagari at all decides it
        "क्या डिलीवरी मुफ़्त है?",
    ],
)
def test_devanagari_anywhere_is_hindi(text: str) -> None:
    assert detect_language(text) is Language.HI


@pytest.mark.parametrize(
    "text",
    [
        "mujhe do litre doodh chahiye",
        "cart mein atta daalo",
        "delivery kitna hai",
        "yeh kaunsa wala hai",
        "Paneer chahiye, jaldi bhejo",
        "MUJHE DOODH CHAHIYE",
    ],
)
def test_romanised_hindi_markers_are_hinglish(text: str) -> None:
    assert detect_language(text) is Language.HI_LATN


def test_common_english_words_that_are_also_hindi_do_not_flip_the_verdict() -> None:
    """``me``, ``do``, ``to``, ``so``: a false Hinglish verdict is the worse mistake."""
    for text in ("give me two", "do you have it", "to be delivered", "so far so good"):
        assert detect_language(text) is Language.EN


def test_detection_is_deterministic() -> None:
    samples = ("milk", "doodh chahiye", "दूध", "Do you have doodh?")
    first = [detect_language(s) for s in samples]
    assert all([detect_language(s) for s in samples] == first for _ in range(5))


def test_each_language_maps_to_its_merchant_locale_and_label() -> None:
    assert Language.EN.locale is Locale.EN
    assert Language.HI.locale is Locale.HI
    # Romanised Hindi is answered in English, by product direction. It used to be answered
    # in the romanised form it arrived in, and that gave a Hindi speaker back an awkward
    # rendering of a language they can read properly -- in a form nobody proofreads.
    assert Language.HI_LATN.locale is Locale.EN
    labels = {language.label for language in Language}
    assert len(labels) == 3 and all(label.strip() for label in labels)
    # The label names the language to REPLY in, so Hinglish's says English while the
    # detection stays HI_LATN and stays auditable.
    assert Language.HI_LATN.label.startswith("English")
    assert Language("hi-Latn") is Language.HI_LATN


def test_the_detector_imports_no_model_or_runtime() -> None:
    """Deterministic by construction: no google.*, no network client, in this module."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("google", "httpx", "vertexai", "openai", "anthropic")
    assert not any(name.split(".")[0] in forbidden for name in imported), imported

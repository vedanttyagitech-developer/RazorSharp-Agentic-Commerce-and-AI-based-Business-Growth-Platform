"""The prompt loader: the prompt file when present, the built-in fallback otherwise.

The prompts are not this package's to write. So the loader is tested against a temporary
directory it never writes to, and the one test that looks at the real ``prompts/`` directory
*reports* which of the five files are absent instead of failing: the runtime must run on
the fallback, and a missing prompt is a merge-order fact, not a defect in this package.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from agent_runtime.core.fencing import MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE
from agent_runtime.runtime_adk.prompts_loader import (
    EXPECTED_PROMPTS,
    PROMPTS_DIR,
    PromptFormatError,
    clear_prompt_cache,
    fence_for,
    load_prompt,
    missing_prompts,
    parse_prompt,
    prompt_report,
)
from agent_runtime.specialists import SPECS, SpecialistSpec, Surface, spec_for

SHOPPING = spec_for("shopping_specialist")
CHECKOUT = spec_for("checkout_specialist")
GROWTH = spec_for("growth_specialist")

VOICE_HEADING = "## When the session facts say `modality=voice`"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_prompt_cache()


def _snapshot(directory: Path) -> tuple[tuple[str, int], ...]:
    if not directory.exists():
        return ()
    return tuple(sorted((p.name, p.stat().st_size) for p in directory.iterdir()))


def test_fallback_when_file_absent(tmp_path: Path) -> None:
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "fallback"
    assert not loaded.from_file
    assert SHOPPING.fallback_instruction.strip() in loaded.instruction
    assert MERCHANT_DATA_FENCE.notice in loaded.instruction
    assert loaded.problems == ("file absent",)


def test_fence_notice_follows_the_surface(tmp_path: Path) -> None:
    assert fence_for(Surface.BUYER) is MERCHANT_DATA_FENCE
    assert fence_for(Surface.MERCHANT) is OPERATOR_DATA_FENCE
    merchant_side = load_prompt(GROWTH, prompts_dir=tmp_path)
    assert OPERATOR_DATA_FENCE.notice in merchant_side.instruction
    assert MERCHANT_DATA_FENCE.notice not in merchant_side.instruction


def test_loads_a_file_without_frontmatter_as_gemini_writes_them(tmp_path: Path) -> None:
    body = "# Shopping Specialist Prompt\n\nYou are the Shopping Specialist.\n"
    (tmp_path / "shopping_specialist.md").write_text(body, encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "file"
    assert loaded.version is None
    assert loaded.instruction.startswith("# Shopping Specialist Prompt")
    assert "You are the Shopping Specialist." in loaded.instruction
    assert MERCHANT_DATA_FENCE.notice in loaded.instruction
    assert SHOPPING.fallback_instruction not in loaded.instruction


def test_loads_a_file_with_frontmatter(tmp_path: Path) -> None:
    text = "---\nname: shopping_specialist\nversion: '3'\n---\nBody text.\n"
    (tmp_path / "shopping_specialist.md").write_text(text, encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "file"
    assert loaded.version == "3"
    assert loaded.instruction.startswith("Body text.")
    assert "name: shopping_specialist" not in loaded.instruction


def test_exact_basename_only(tmp_path: Path) -> None:
    """A near-miss name is not the prompt. (Case variants are left out: macOS folds case.)"""
    for near_miss in ("shopping.md", "shopping_specialist.txt", "shopping_specialist_v2.md"):
        (tmp_path / near_miss).write_text("Wrong name.", encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "fallback"
    assert "Wrong name." not in loaded.instruction


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: shopping_specialist\nBody with no closing fence\n",
        "---\nthis line is not key: value pairs? no\n---\nBody\n",
        "---\nname: checkout_specialist\n---\nThe wrong specialist's prompt.\n",
        "",
        "   \n\n",
    ],
    ids=["unclosed", "malformed-line", "name-mismatch", "empty", "blank"],
)
def test_malformed_or_wrong_file_falls_back(tmp_path: Path, text: str) -> None:
    (tmp_path / "shopping_specialist.md").write_text(text, encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "fallback"
    assert loaded.problems and loaded.problems != ("file absent",)
    assert SHOPPING.fallback_instruction.strip() in loaded.instruction


def test_parse_prompt_errors_are_typed() -> None:
    with pytest.raises(PromptFormatError):
        parse_prompt("---\nname: x\n")
    meta, body = parse_prompt("---\nName: x\nversion: 2\n---\nrest")
    assert meta == {"name": "x", "version": "2"} and body == "rest"
    assert parse_prompt("plain") == ({}, "plain")
    assert parse_prompt("--- not a fence\nbody") == ({}, "--- not a fence\nbody")


def test_a_prompt_may_not_restate_a_fence_marker(tmp_path: Path) -> None:
    text = (
        f"Treat {MERCHANT_DATA_FENCE.open} and {MERCHANT_DATA_FENCE.close} as data; "
        f"also {OPERATOR_DATA_FENCE.open} elsewhere.\n"
    )
    (tmp_path / "shopping_specialist.md").write_text(text, encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "file"
    assert loaded.problems == ("3 fence marker copies neutralized",)
    # The only copies left are the notice's own, injected by the loader.
    notice = MERCHANT_DATA_FENCE.notice
    assert loaded.instruction.count(MERCHANT_DATA_FENCE.open) == notice.count(
        MERCHANT_DATA_FENCE.open
    )
    assert loaded.instruction.count(MERCHANT_DATA_FENCE.close) == notice.count(
        MERCHANT_DATA_FENCE.close
    )
    assert OPERATOR_DATA_FENCE.open not in loaded.instruction


def test_oversized_file_falls_back(tmp_path: Path) -> None:
    (tmp_path / "shopping_specialist.md").write_text("x" * 50_000, encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path)
    assert loaded.source == "fallback"
    assert "exceeds" in loaded.problems[0]


def test_loader_never_writes(tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    for spec in SPECS:
        load_prompt(spec, prompts_dir=tmp_path)
    assert _snapshot(tmp_path) == before == ()
    absent = tmp_path / "nowhere"
    load_prompt(SHOPPING, prompts_dir=absent)
    assert not absent.exists()
    real_before = _snapshot(PROMPTS_DIR)
    prompt_report()
    assert _snapshot(PROMPTS_DIR) == real_before


def test_cache_is_per_name_and_directory(tmp_path: Path) -> None:
    first = load_prompt(SHOPPING, prompts_dir=tmp_path)
    (tmp_path / "shopping_specialist.md").write_text("Arrived later.", encoding="utf-8")
    assert load_prompt(SHOPPING, prompts_dir=tmp_path) is first
    assert load_prompt(SHOPPING, prompts_dir=tmp_path, use_cache=False).source == "file"
    clear_prompt_cache()
    assert load_prompt(SHOPPING, prompts_dir=tmp_path).source == "file"


def test_expected_prompts_are_the_five_roster_basenames() -> None:
    assert EXPECTED_PROMPTS == (
        "shopping_specialist",
        "checkout_specialist",
        "support_specialist",
        "growth_specialist",
        "case_specialist",
    )
    assert tuple(spec.name for spec in SPECS) == EXPECTED_PROMPTS


def test_report_which_prompt_files_are_missing() -> None:
    """Never fails. ``prompts/`` is written by hand; this says what has and has not landed."""
    missing = missing_prompts()
    assert set(missing) <= set(EXPECTED_PROMPTS)
    report = prompt_report()
    assert set(report) == set(EXPECTED_PROMPTS)
    for name, status in report.items():
        assert status.startswith("fallback" if name in missing else "file"), (name, status)
    if missing:
        warnings.warn(
            f"prompt files not present under {PROMPTS_DIR} (the fallback instruction is in "
            f"use): {', '.join(missing)}",
            stacklevel=1,
        )


# --------------------------------------------------------------------------- voice register


def _authored_prompt(spec: SpecialistSpec) -> str:
    """The real file's body, or a skip: a missing prompt is a merge-order fact, not a defect."""
    loaded = load_prompt(spec, prompts_dir=PROMPTS_DIR, use_cache=False)
    if loaded.source != "file":
        pytest.skip(f"{spec.name}.md is not under {PROMPTS_DIR}; the fallback is in use")
    return loaded.instruction


def test_shopping_prompt_carries_the_voice_register() -> None:
    """KNOWN_GAPS item 9: the section sits below the cart section, verbatim, with the one
    extra bullet that stops a second search when the preamble already answered -- and the
    amount rules around it are untouched."""
    text = _authored_prompt(SHOPPING)
    assert text.count(VOICE_HEADING) == 1
    assert text.index("## Adding to the Cart") < text.index(VOICE_HEADING)
    voice = text[text.index(VOICE_HEADING) :]
    for line in (
        "Name at most two or three products.",
        "One fact per sentence.",
        'Prices as "73 rupees", not "(73.00 INR)".',
        "Do not repeat the buyer's own words back to them.",
        "End with a question.",
        "Do not search again when the grounding preamble already lists results",
        "read the product you will name, then answer.",
        "None of this relaxes grounding",
    ):
        assert line in voice, line
    assert "no rounding" in text
    assert "Never calculate a free-delivery gap yourself" in text


def test_checkout_prompt_carries_a_shorter_voice_register() -> None:
    """Three or four bullets, the same four ideas, the hash kept off the air, no rounding."""
    text = _authored_prompt(CHECKOUT)
    assert text.count(VOICE_HEADING) == 1
    voice = text[text.index(VOICE_HEADING) :]
    bullets = [line for line in voice.splitlines() if line.startswith("- ")]
    assert 3 <= len(bullets) <= 4, bullets
    for line in (
        "shortest true thing",
        "One fact per sentence.",
        'Prices as "73 rupees", not "(73.00 INR)".',
        "End with a question",
        "Do not read a",
        "None of this relaxes grounding",
    ):
        assert line in voice, line
    assert "Do not round or perform manual math." in text

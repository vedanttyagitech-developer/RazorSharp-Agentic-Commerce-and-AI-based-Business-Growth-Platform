"""The shopping skills: three files that compose onto the shopping prompt.

A skill is craft the prompt does not own -- how to word an offer, how to answer about an
order already placed, how the reply sounds -- so the test that matters is not what the
files say but that the loader still hands the model one instruction with all three inside
it, under the cap, with nothing reported. What the files say is reviewed by people.

The one exception is the register rules that were moved out of ``shopping_specialist.md``
into a skill: those are asserted to appear once in the composed instruction and not twice,
because a rule stated in two different sets of words is how the prompt grew the offering
section that sold ghee against roti in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime.runtime_adk.prompts_loader import (
    MAX_PROMPT_CHARS,
    PROMPTS_DIR,
    SKILLS_DIR,
    clear_prompt_cache,
    load_prompt,
    load_skill,
    parse_prompt,
)
from agent_runtime.specialists import spec_for

SHOPPING = spec_for("shopping_specialist")

#: The order the prompt's ``skills:`` line names them, which is the order they compose in.
DECLARED = ("selling", "order-help", "speaking")


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_prompt_cache()


def _authored() -> str:
    """The real composed instruction, or a skip: a missing prompt is a merge-order fact."""
    loaded = load_prompt(SHOPPING, prompts_dir=PROMPTS_DIR, use_cache=False)
    if loaded.source != "file":
        pytest.skip(f"shopping_specialist.md is not under {PROMPTS_DIR}; the fallback is in use")
    return loaded.instruction


@pytest.mark.parametrize("name", DECLARED)
def test_each_skill_file_parses(name: str) -> None:
    """Frontmatter that names itself, a description saying when it applies, and a body."""
    path = SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), path
    meta, body = parse_prompt(path.read_text(encoding="utf-8"))
    assert meta["name"] == name
    assert meta["description"].strip()
    assert body.strip()
    loaded_body, problem = load_skill(name)
    assert problem is None
    assert loaded_body == body.strip()


def test_the_shopping_prompt_composes_all_three() -> None:
    loaded = load_prompt(SHOPPING, prompts_dir=PROMPTS_DIR, use_cache=False)
    if loaded.source != "file":
        pytest.skip(f"shopping_specialist.md is not under {PROMPTS_DIR}; the fallback is in use")
    assert loaded.skills == DECLARED
    assert loaded.problems == ()
    for name in DECLARED:
        body, problem = load_skill(name)
        assert problem is None
        assert body in loaded.instruction, name
    # The prompt body still comes first; a skill is appended craft, not a replacement.
    assert loaded.instruction.index("# Shopping Specialist Prompt") < loaded.instruction.index(
        load_skill("selling")[0]
    )


def test_the_composed_instruction_stays_under_the_cap() -> None:
    instruction = _authored()
    assert len(instruction) < MAX_PROMPT_CHARS, len(instruction)


def test_the_moved_rules_are_stated_once() -> None:
    """What a skill now covers was deleted from the prompt, not left behind beside it."""
    instruction = _authored()
    # The two words the buyer's screen uses, which the speaking skill now owns. The list of
    # words not to reach for appears once, in the skill, and not also in the prompt.
    assert instruction.count("Two words are never translated") == 1
    assert "Two words never get translated" not in instruction
    assert "## Multilingual Communication & Tone" not in instruction
    assert instruction.count("दुकान") == 1
    # The offering section the selling skill replaced.
    assert "close your reply with" not in instruction
    assert instruction.count("One suggestion.") == 1
    # The old good-reply example ended on two suggestions at once, which selling forbids.
    assert "butter ya chai patti bhi chahiye" not in instruction


def test_a_skill_the_tree_does_not_have_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    """The prompt still loads from its file; it loses only the craft that skill carried."""
    text = "---\nname: shopping_specialist\nskills: selling, no-such-skill, speaking\n---\nBody.\n"
    (tmp_path / "shopping_specialist.md").write_text(text, encoding="utf-8")
    loaded = load_prompt(SHOPPING, prompts_dir=tmp_path, skills_dir=SKILLS_DIR)
    assert loaded.source == "file"
    assert loaded.skills == ("selling", "speaking")
    assert len(loaded.problems) == 1
    assert "no-such-skill" in loaded.problems[0]
    assert "Body." in loaded.instruction


@pytest.mark.parametrize(
    "name",
    ["../../prompts", "selling/../..", "Selling", "sell ing", ""],
    ids=["traversal", "traversal-suffix", "uppercase", "space", "empty"],
)
def test_a_name_that_is_not_a_skill_name_never_becomes_a_path(name: str) -> None:
    body, problem = load_skill(name)
    assert body == ""
    assert problem is not None and "legal skill name" in problem

"""The prompt loader: the prompt file when present, the built-in fallback otherwise.

The prompts are not this package's to write. So the loader is tested against a temporary
directory it never writes to, and the one test that looks at the real ``prompts/`` directory
*reports* which of the five files are absent instead of failing: the runtime must run on
the fallback, and a missing prompt is a merge-order fact, not a defect in this package.
"""

from __future__ import annotations

import re
import uuid
import warnings
from pathlib import Path
from typing import Final

import pytest
from agent_runtime.backends import InMemoryBackend
from agent_runtime.capabilities import (
    ALL_CAPABILITIES,
    REGISTRY_A,
    AgentRole,
    Capability,
    build_toolset,
    derive_principal,
)
from agent_runtime.core.fencing import MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE
from agent_runtime.language import Language
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
from agent_runtime.specialists import ACTIONS, SPECS, SpecialistSpec, Surface, spec_for
from agent_runtime.turn import TurnContext
from merchant_sim import MerchantStore
from transaction_kernel import ActorType, AgentPrincipal

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


# ------------------------------------------------------- the tools a prompt promises

# The defect this section exists to stop is not subtle once you have seen it, and it was
# invisible until somebody read a live transcript. Every prompt file was written against
# the roster's *capability* vocabulary -- ``catalog.search``, ``basket.update``,
# ``quote.request`` -- and the ADK offers the model *function* names: ``search``,
# ``basket_set_line``, ``basket_get``. A model cannot call a name that does not exist, so
# it did the only thing left and answered in prose. The buyer saw an assistant that talked
# about products and never put one on the screen, and nothing failed: no denial, no
# exception, no red test. Correcting the shopping prompt's list took one turn from a single
# tool call with its sentence dropped as ungrounded to four calls and a grounded suggestion.
#
# So the check is on the names, and it is deliberately built from three independent sources
# rather than from one list a prompt could be edited to match. A name a prompt backticks
# must have a Registry A row (the registry granted it), a builder (the factory constructs a
# closure for it), and a place on that specialist's own roster (the model is offered it).
# ``SpecialistSpec.tool_names`` is not one of those sources on purpose: it carries
# ``reservation_request``, ``inventory_check`` and ``support_escalate``, which have no
# builder anywhere and in two cases no registry row either, so asserting against it would
# bless exactly the names that cannot be called.

_BACKTICKED: Final[re.Pattern[str]] = re.compile(r"`([^`\n]+)`")

#: A backticked token written as a call: ``present_products(skus)``. The name is what the
#: model would type, so it is checked whether or not the vocabulary below has heard of it --
#: that is what catches a tool invented wholesale rather than mistranslated.
_CALL_FORM: Final[re.Pattern[str]] = re.compile(r"^([a-z][a-z0-9_]*)\(")

#: Every string anywhere in this package that could be mistaken for a tool name: the
#: registry's own rows, the roster's dotted action names, Registry A's capability strings,
#: and the function names the specs derive from them. A bare backticked token is checked
#: only when it is one of these, which is why a prompt may freely backtick ``binding_ok``,
#: ``plan_ttl_seconds`` or ``AWAITING_HUMAN`` -- those are fields and vocabularies a tool
#: returns, not names a model would ever call, and demanding they be tools would make this
#: test fire on correct prose.
_TOOL_VOCABULARY: Final[frozenset[str]] = frozenset(
    set(REGISTRY_A)
    | set(ACTIONS)
    | set(ALL_CAPABILITIES)
    | {name for spec in SPECS for name in spec.tool_names}
)


def _callable_tools(role: AgentRole) -> frozenset[str]:
    """The tool names this role's model is really offered, proven by building them.

    Built rather than listed, and against a backend carrying every surface, because "has a
    builder" is a property of the factory and not of a table anybody maintains: a row with
    no closure behind it comes back in ``unbuilt`` instead, which is precisely how
    ``support_escalate`` is excluded here without this file naming it.
    """
    harness = AgentPrincipal(
        principal_id="agent:prompt-audit",
        tenant_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        actor_type=ActorType.AGENT,
        agent_role=None,
        capabilities=ALL_CAPABILITIES,
    )
    specialist = derive_principal(harness, role)
    turn = TurnContext(language=Language.EN, principal=specialist)
    toolset = build_toolset(
        role,
        InMemoryBackend(MerchantStore()),
        turn,
        principal=specialist,
        session_id="prompt-audit",
    )
    return frozenset(toolset.names)


def _tools_named_in(text: str) -> tuple[str, ...]:
    """Every backticked token in ``text`` that reads as a tool the model should call."""
    named: dict[str, None] = {}
    for match in _BACKTICKED.finditer(text):
        token = match.group(1)
        call = _CALL_FORM.match(token)
        if call is not None:
            named.setdefault(call.group(1), None)
        elif token in _TOOL_VOCABULARY:
            named.setdefault(token, None)
    return tuple(named)


def _authored_body(spec: SpecialistSpec, skills_dir: Path) -> str:
    """The specialist's own file, with no skill composed in, or a skip when it is absent.

    ``skills_dir`` is an empty directory the caller owns, which is the whole point of taking
    one. A skill is shared craft: several specialists compose the same file, so it refers to
    a tool the way a shared document has to -- "``order_track`` where your own tool list
    names it" -- and it tells a specialist holding no such tool to say so. Auditing the
    composed whole would read that hedge as a promise and fail a correct pair of files. The
    promise being audited here is the one the specialist's own prompt makes about its own
    roster.
    """
    loaded = load_prompt(spec, prompts_dir=PROMPTS_DIR, skills_dir=skills_dir, use_cache=False)
    if loaded.source != "file":
        pytest.skip(f"{spec.name}.md is not under {PROMPTS_DIR}; the fallback is in use")
    assert not loaded.skills, f"{spec.name}: {skills_dir} was expected to be empty"
    return loaded.instruction


def test_no_prompt_offers_a_tool_the_model_cannot_call(tmp_path: Path) -> None:
    """Every specialist prompt names function names its own model is actually offered.

    Two properties, reported together because they are two spellings of one defect. A
    capability string in a prompt is a tool name the ADK never registered; a backticked name
    off the specialist's roster is one the factory never built for it. Either way the model
    reaches for something that is not there and answers in words instead, which is the one
    failure mode that leaves no trace in the logs.
    """
    problems: list[str] = []
    for spec in SPECS:
        text = _authored_body(spec, tmp_path)
        offered = _callable_tools(AgentRole(spec.role))
        for capability in Capability:
            if capability.value in text:
                problems.append(
                    f"{spec.name}: names the capability string {capability.value!r}; "
                    "the model is offered function names, not capabilities"
                )
        for name in _tools_named_in(text):
            if name in offered:
                continue
            why = (
                "no builder constructs it"
                if name in REGISTRY_A
                else "it has no Registry A row at all"
            )
            problems.append(
                f"{spec.name}: names the tool {name!r}, which is not on its roster -- {why}. "
                f"It may call: {', '.join(sorted(offered))}"
            )
    assert not problems, "\n".join(problems)

"""Load a specialist's prompt from ``prompts/<name>.md``, or fall back.

The prompts are not this package's to write: the prompt files say what an agent says,
this package says what it can do. So the loader reads a file by
its exact basename, never creates or edits one, and ships a built-in minimal instruction
per specialist for the case where the file is absent or malformed. A missing prompt is
reported, not fatal: the demonstration must run on the fallback and the test suite says
which files are missing rather than failing.

Shape, adapted from the reference skills loader (ADR 0004 section 1.8): optional YAML-ish
frontmatter with ``name`` and ``version`` between ``---`` fences, then the body. The
prompt files carry no frontmatter today, so it is optional; when present its ``name`` must match
the specialist or the file is treated as the wrong prompt and the fallback is used.

The instruction the model receives is the static half of the prompt (ADR 0004 section
1.5): file body plus the fence notice for the specialist's surface, byte-stable across
turns. Per-turn facts never enter it. A prompt file may not restate a fence marker -- a
marker in the instruction would teach the model the boundary string -- so copies of
either surface's markers are neutralized before assembly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ..core.fencing import MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE, Fence
from ..specialists import SPECS, SpecialistSpec, Surface

__all__ = [
    "EXPECTED_PROMPTS",
    "MAX_PROMPT_CHARS",
    "PROMPTS_DIR",
    "SKILLS_DIR",
    "LoadedPrompt",
    "PromptFormatError",
    "assemble_instruction",
    "clear_prompt_cache",
    "fence_for",
    "load_prompt",
    "load_skill",
    "missing_prompts",
    "parse_prompt",
    "prompt_report",
]

#: ``agent_runtime/prompts/``: the prompt files' directory. Read here; never written.
PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "prompts"

#: The five basenames the roster names, derived from the specs so they cannot drift.
EXPECTED_PROMPTS: Final[tuple[str, ...]] = tuple(spec.name for spec in SPECS)

#: A prompt larger than this is not a prompt; refuse it rather than ship it. Measured
#: after skills are composed in, because what the model receives is the composed whole.
MAX_PROMPT_CHARS: Final[int] = 40_000

#: ``agent_runtime/prompts/skills/``: one directory per skill, each holding ``SKILL.md``.
#:
#: A skill is craft, not policy. The specialist prompt says what the agent is and what it
#: may never do; a skill says how to do one part of the job well -- how to word a
#: suggestion, how to answer a question about an order -- and several specialists can want
#: the same craft without either of them owning it. Splitting them this way is what stops
#: the specialist prompt growing into a document nobody can hold in their head, which is
#: how its tool list came to name five tools that did not exist.
SKILLS_DIR: Final[Path] = PROMPTS_DIR / "skills"

_FRONTMATTER_LINE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
_MARKER_NEUTRALIZED: Final[str] = "[fence marker removed]"
_NOTICE_HEADING: Final[str] = "## Untrusted data"
_ALL_MARKERS: Final[tuple[str, ...]] = tuple(
    marker
    for fence in (MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE)
    for marker in (fence.open, fence.close)
)


class PromptFormatError(ValueError):
    """The file exists but is not a prompt this loader will hand to a model."""


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    """What the adapter installs as the agent's static instruction, and where it came from."""

    name: str
    source: Literal["file", "fallback"]
    instruction: str
    version: str | None = None
    path: Path | None = None
    problems: tuple[str, ...] = ()
    #: Skill names composed into ``instruction``, in the order they were appended.
    skills: tuple[str, ...] = ()

    @property
    def from_file(self) -> bool:
        return self.source == "file"


def fence_for(surface: Surface) -> Fence:
    """The fence whose notice a specialist on ``surface`` carries in its instruction."""
    return MERCHANT_DATA_FENCE if surface is Surface.BUYER else OPERATOR_DATA_FENCE


def parse_prompt(text: str) -> tuple[Mapping[str, str], str]:
    """Split optional frontmatter from the body.

    Frontmatter is ``---`` on the first line, ``key: value`` lines, ``---`` again. Anything
    else between the fences is malformed, and a fence that never closes is malformed:
    a half-read prompt is worse than the fallback.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :])
        if not line.strip():
            continue
        match = _FRONTMATTER_LINE.match(line.strip())
        if match is None:
            raise PromptFormatError(f"frontmatter line {index + 1} is not `key: value`")
        meta[match.group(1).lower()] = match.group(2).strip().strip("'\"")
    raise PromptFormatError("frontmatter fence never closes")


def _neutralize_markers(body: str) -> tuple[str, int]:
    """Remove copies of either fence's markers from prompt text, and count them."""
    count = 0
    for marker in _ALL_MARKERS:
        count += body.count(marker)
        body = body.replace(marker, _MARKER_NEUTRALIZED)
    return body, count


_SKILL_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _declared_skills(meta: Mapping[str, str]) -> tuple[str, ...]:
    """Skill names from a prompt's ``skills:`` line, in the order written.

    Comma-separated, lowercase-and-hyphens, deduplicated with the first mention winning
    so the order in the file is the order in the instruction. A name that is not a legal
    skill name is dropped and reported rather than turned into a path: this value comes
    from a file, and a file is not permitted to name a directory outside the skills tree.
    """
    raw = meta.get("skills", "")
    seen: dict[str, None] = {}
    for part in raw.split(","):
        candidate = part.strip()
        if candidate and _SKILL_NAME.match(candidate):
            seen.setdefault(candidate, None)
    return tuple(seen)


def load_skill(name: str, *, skills_dir: Path | None = None) -> tuple[str, str | None]:
    """One skill's body and its problem, if it had one.

    Returns ``(body, None)`` when the skill read cleanly, and ``("", reason)`` when it did
    not. A missing or malformed skill never takes the specialist down with it: the agent
    keeps every rule its own prompt states and loses only the craft the skill would have
    added, which is the same bargain :func:`load_prompt` already strikes with a missing
    prompt file.
    """
    if not _SKILL_NAME.match(name):
        return "", f"skill {name!r} is not a legal skill name"
    directory = (SKILLS_DIR if skills_dir is None else skills_dir).resolve()
    path = directory / name / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", f"skill {name!r} unreadable: {exc}"
    try:
        meta, body = parse_prompt(text)
    except PromptFormatError as exc:
        return "", f"skill {name!r} malformed frontmatter: {exc}"
    declared = meta.get("name")
    if declared is not None and declared != name:
        return "", f"skill {name!r} frontmatter names {declared!r}"
    if not body.strip():
        return "", f"skill {name!r} body is empty"
    return body.strip(), None


def _compose(
    body: str, skills: tuple[str, ...], skills_dir: Path | None
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """The prompt body with its skills appended. Returns (text, loaded, problems)."""
    parts = [body.strip()]
    loaded: list[str] = []
    problems: list[str] = []
    for name in skills:
        skill_body, problem = load_skill(name, skills_dir=skills_dir)
        if problem is not None:
            problems.append(problem)
            continue
        parts.append(skill_body)
        loaded.append(name)
    return "\n\n".join(parts), tuple(loaded), tuple(problems)


def assemble_instruction(body: str, fence: Fence) -> str:
    """The static instruction: the body, then the fence notice as its own section.

    The notice is injected here and only here, so every specialist carries it whether
    its prompt file remembered to or not, and no prompt file has to name the marker.
    """
    return f"{body.strip()}\n\n{_NOTICE_HEADING}\n{fence.notice}\n"


def _fallback(spec: SpecialistSpec, problems: tuple[str, ...], path: Path | None) -> LoadedPrompt:
    return LoadedPrompt(
        name=spec.name,
        source="fallback",
        instruction=assemble_instruction(spec.fallback_instruction, fence_for(spec.surface)),
        path=path,
        problems=problems,
    )


def _read(spec: SpecialistSpec, path: Path, skills_dir: Path | None = None) -> LoadedPrompt:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _fallback(spec, (f"unreadable: {exc}",), path)
    if len(text) > MAX_PROMPT_CHARS:
        return _fallback(spec, (f"prompt exceeds {MAX_PROMPT_CHARS} characters",), path)
    try:
        meta, body = parse_prompt(text)
    except PromptFormatError as exc:
        return _fallback(spec, (f"malformed frontmatter: {exc}",), path)
    declared = meta.get("name")
    if declared is not None and declared != spec.name:
        return _fallback(spec, (f"frontmatter names {declared!r}, not {spec.name!r}",), path)
    if not body.strip():
        return _fallback(spec, ("prompt body is empty",), path)
    composed, skills, skill_problems = _compose(body, _declared_skills(meta), skills_dir)
    # Neutralized after composition, so a skill file is held to the same rule the prompt
    # is: nothing in the instruction may restate a fence marker, whoever wrote it.
    composed, markers = _neutralize_markers(composed)
    if len(composed) > MAX_PROMPT_CHARS:
        return _fallback(
            spec,
            (f"prompt and skills exceed {MAX_PROMPT_CHARS} characters",) + skill_problems,
            path,
        )
    problems = skill_problems
    if markers:
        problems = problems + (f"{markers} fence marker copies neutralized",)
    return LoadedPrompt(
        name=spec.name,
        source="file",
        instruction=assemble_instruction(composed, fence_for(spec.surface)),
        version=meta.get("version"),
        path=path,
        problems=problems,
        skills=skills,
    )


_cache: dict[tuple[str, str], LoadedPrompt] = {}


def load_prompt(
    spec: SpecialistSpec,
    *,
    prompts_dir: Path | None = None,
    skills_dir: Path | None = None,
    use_cache: bool = True,
) -> LoadedPrompt:
    """The prompt for ``spec``: the file ``<prompts_dir>/<spec.name>.md`` or the fallback.

    Cached per (name, directory): the static instruction must be the same bytes on every
    turn (ADR 0004 section 1.5), and re-reading the file each turn would let an edit
    mid-session change what the model was told halfway through a checkout.
    """
    directory = (PROMPTS_DIR if prompts_dir is None else prompts_dir).resolve()
    skills = (SKILLS_DIR if skills_dir is None else skills_dir).resolve()
    # The skills directory is part of the key: two directories compose two different
    # instructions, and the cache must not hand one test's composition to another's.
    key = (spec.name, f"{directory}|{skills}")
    if use_cache and key in _cache:
        return _cache[key]
    path = directory / f"{spec.name}.md"
    loaded = (
        _read(spec, path, skills) if path.is_file() else _fallback(spec, ("file absent",), path)
    )
    if use_cache:
        _cache[key] = loaded
    return loaded


def clear_prompt_cache() -> None:
    _cache.clear()


def missing_prompts(prompts_dir: Path | None = None) -> tuple[str, ...]:
    """The expected basenames with no file present. A report, never a failure."""
    directory = PROMPTS_DIR if prompts_dir is None else prompts_dir
    return tuple(name for name in EXPECTED_PROMPTS if not (directory / f"{name}.md").is_file())


def prompt_report(prompts_dir: Path | None = None) -> Mapping[str, str]:
    """Per specialist: ``file`` or ``fallback`` plus any problems, for logs and the test."""
    report: dict[str, str] = {}
    for spec in SPECS:
        loaded = load_prompt(spec, prompts_dir=prompts_dir, use_cache=False)
        detail = "; ".join(loaded.problems)
        report[spec.name] = loaded.source if not detail else f"{loaded.source} ({detail})"
    return report

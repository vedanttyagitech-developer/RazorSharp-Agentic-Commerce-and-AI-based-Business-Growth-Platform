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
    "LoadedPrompt",
    "PromptFormatError",
    "assemble_instruction",
    "clear_prompt_cache",
    "fence_for",
    "load_prompt",
    "missing_prompts",
    "parse_prompt",
    "prompt_report",
]

#: ``agent_runtime/prompts/``: the prompt files' directory. Read here; never written.
PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "prompts"

#: The five basenames the roster names, derived from the specs so they cannot drift.
EXPECTED_PROMPTS: Final[tuple[str, ...]] = tuple(spec.name for spec in SPECS)

#: A prompt larger than this is not a prompt; refuse it rather than ship it.
MAX_PROMPT_CHARS: Final[int] = 40_000

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


def _read(spec: SpecialistSpec, path: Path) -> LoadedPrompt:
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
    body, markers = _neutralize_markers(body)
    if not body.strip():
        return _fallback(spec, ("prompt body is empty",), path)
    problems = (f"{markers} fence marker copies neutralized",) if markers else ()
    return LoadedPrompt(
        name=spec.name,
        source="file",
        instruction=assemble_instruction(body, fence_for(spec.surface)),
        version=meta.get("version"),
        path=path,
        problems=problems,
    )


_cache: dict[tuple[str, str], LoadedPrompt] = {}


def load_prompt(
    spec: SpecialistSpec, *, prompts_dir: Path | None = None, use_cache: bool = True
) -> LoadedPrompt:
    """The prompt for ``spec``: the file ``<prompts_dir>/<spec.name>.md`` or the fallback.

    Cached per (name, directory): the static instruction must be the same bytes on every
    turn (ADR 0004 section 1.5), and re-reading the file each turn would let an edit
    mid-session change what the model was told halfway through a checkout.
    """
    directory = (PROMPTS_DIR if prompts_dir is None else prompts_dir).resolve()
    key = (spec.name, str(directory))
    if use_cache and key in _cache:
        return _cache[key]
    path = directory / f"{spec.name}.md"
    loaded = _read(spec, path) if path.is_file() else _fallback(spec, ("file absent",), path)
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

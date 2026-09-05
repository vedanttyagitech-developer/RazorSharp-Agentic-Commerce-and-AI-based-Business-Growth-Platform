"""The google-adk adapter: the only package here that imports ``google.adk``.

``adapter`` turns a :class:`~agent_runtime.specialists.SpecialistSpec` into an
``LlmAgent`` with factory tools and the toolset's gates, and provides the harness's
``SpecialistRunner``; ``prompts_loader`` reads the prompt files under ``prompts/`` with
a built-in fallback. Nothing outside this package may import a model runtime, and a
source test enforces it.

The adapter is exposed lazily: the prompt loader has no ADK dependency and the API or a
test may read prompts where ``google-adk`` is not installed, so importing this package
must not itself import ``google.adk``. ``from agent_runtime.runtime_adk import
build_agent`` still works; it resolves on first use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from .prompts_loader import (
    EXPECTED_PROMPTS,
    MAX_PROMPT_CHARS,
    PROMPTS_DIR,
    LoadedPrompt,
    PromptFormatError,
    assemble_instruction,
    clear_prompt_cache,
    fence_for,
    load_prompt,
    missing_prompts,
    parse_prompt,
    prompt_report,
)

if TYPE_CHECKING:
    from .adapter import (
        APP_NAME,
        DEFAULT_MODEL,
        DEFAULT_TEMPERATURE,
        MODEL_ENV,
        TEXT_ONLY_MODALITIES,
        AdkSpecialistRunner,
        BuiltSpecialist,
        SpecialistToolingError,
        VertexNotConfiguredError,
        build_agent,
        build_specialist,
        model_name,
        seed_state,
        text_generation_config,
        text_run_config,
        unbuilt_tools,
        user_content,
        vertex_configured,
    )

__all__ = [
    "APP_NAME",
    "DEFAULT_MODEL",
    "DEFAULT_TEMPERATURE",
    "EXPECTED_PROMPTS",
    "MAX_PROMPT_CHARS",
    "MODEL_ENV",
    "PROMPTS_DIR",
    "TEXT_ONLY_MODALITIES",
    "AdkSpecialistRunner",
    "BuiltSpecialist",
    "LoadedPrompt",
    "PromptFormatError",
    "SpecialistToolingError",
    "VertexNotConfiguredError",
    "assemble_instruction",
    "build_agent",
    "build_specialist",
    "clear_prompt_cache",
    "fence_for",
    "load_prompt",
    "missing_prompts",
    "model_name",
    "parse_prompt",
    "prompt_report",
    "seed_state",
    "text_generation_config",
    "text_run_config",
    "unbuilt_tools",
    "user_content",
    "vertex_configured",
]

_ADAPTER_EXPORTS: Final[frozenset[str]] = frozenset(
    {
        "APP_NAME",
        "DEFAULT_MODEL",
        "DEFAULT_TEMPERATURE",
        "MODEL_ENV",
        "TEXT_ONLY_MODALITIES",
        "AdkSpecialistRunner",
        "BuiltSpecialist",
        "SpecialistToolingError",
        "VertexNotConfiguredError",
        "build_agent",
        "build_specialist",
        "model_name",
        "seed_state",
        "text_generation_config",
        "text_run_config",
        "unbuilt_tools",
        "user_content",
        "vertex_configured",
    }
)


def __getattr__(name: str) -> Any:
    if name in _ADAPTER_EXPORTS:
        from . import adapter

        return getattr(adapter, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

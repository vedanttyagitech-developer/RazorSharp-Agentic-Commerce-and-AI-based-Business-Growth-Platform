"""One configuration source for the Razor AI reasoning model.

Verified against the official model documentation (Gemini 3.8 Flash, GA September
2026 -- model cards and API docs, not tribal knowledge):

* Model ID: ``gemini-3.8-flash``. Thinking levels LOW / MEDIUM / HIGH (default
  MEDIUM); MINIMAL is unsupported and returns an API validation error.
* ``temperature``, ``top_p``, ``top_k``, frequency/presence penalties and
  ``candidate_count`` are unsupported generation parameters: the developer guide
  instructs removing them. A live probe confirmed ``temperature`` is silently
  ignored rather than rejected, which is worse than an error -- a silently
  ignored control looks tuned while doing nothing. Generation settings are
  therefore model-specific below, never global.
* The 3.8 Flash model does NOT support the Live API. Voice reasoning over a Live
  session must not target this model; audio adapters (recognition, synthesis)
  are separate provider services with their own model IDs.

Environment:

* ``AGENT_RUNTIME_MODEL`` names the reasoning model, else ``GEMINI_MODEL_ID``,
  else :data:`DEFAULT_MODEL`. The second name exists because operator tooling
  already exports it; neither is read anywhere else, so exactly one of them
  taking effect here cannot shadow another consumer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_THINKING_LEVEL",
    "GEMINI_MODEL_ENV",
    "MAX_OUTPUT_TOKENS",
    "MODEL_ENV",
    "MODEL_ID_DOC_URL",
    "generation_config",
    "metadata",
    "model_name",
    "supports_live_api",
    "thinking_level",
    "use_temperature",
]

#: Verified model identifier (Gemini API docs, model ID table, September 2026).
DEFAULT_MODEL: Final[str] = "gemini-3.8-flash"
#: First ``AGENT_RUNTIME_MODEL``, then the operator-exported ``GEMINI_MODEL_ID``.
MODEL_ENV: Final[str] = "AGENT_RUNTIME_MODEL"
GEMINI_MODEL_ENV: Final[str] = "GEMINI_MODEL_ID"
#: Interactive default. MEDIUM is the provider default; LOW trades reasoning depth
#: for the latency a voice-adjacent turn needs.
DEFAULT_THINKING_LEVEL: Final[str] = "LOW"
#: Documentation anchor for operators verifying the identifier independently.
MODEL_ID_DOC_URL: Final[str] = "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash"
#: Provider-stated maximum output tokens for this model.
MAX_OUTPUT_TOKENS: Final[int] = 65_536


def model_name(env: Mapping[str, str] | None = None) -> str:
    """The configured reasoning model id, or :data:`DEFAULT_MODEL`."""
    source = os.environ if env is None else env
    return (
        source.get(MODEL_ENV, "").strip()
        or source.get(GEMINI_MODEL_ENV, "").strip()
        or DEFAULT_MODEL
    )


_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def vertex_configured(env: Mapping[str, str] | None = None) -> bool:
    """True when a Vertex-backed reasoning call can be attempted.

    Three environment variables, no network call, no credential touched: project,
    location and the explicit Vertex switch. Application Default Credentials are
    resolved by the provider SDK on the first real request, so this is necessary
    but not sufficient -- a configured process with no ADC still fails at call
    time, and that failure is typed, not silent.
    """
    source = os.environ if env is None else env
    return (
        source.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in _TRUTHY
        and bool(source.get("GOOGLE_CLOUD_PROJECT", "").strip())
        and bool(source.get("GOOGLE_CLOUD_LOCATION", "").strip())
    )


def _is_gemini_3_flash(model: str) -> bool:
    name = model.strip().casefold()
    return name.startswith("gemini-3") and "flash" in name


def supports_live_api(model: str | None = None) -> bool:
    """Whether the reasoning model may back a Live API session. It may not."""
    return not _is_gemini_3_flash(model or model_name())


def thinking_level(model: str | None = None) -> str | None:
    """The thinking level for the model, or None when it has no such control."""
    return DEFAULT_THINKING_LEVEL if _is_gemini_3_flash(model or model_name()) else None


def use_temperature(model: str | None = None) -> bool:
    """Whether a temperature setting takes effect. Gemini 3 ignores it silently."""
    return not _is_gemini_3_flash(model or model_name())


def generation_config(
    model: str | None = None,
    *,
    thinking_level: str = DEFAULT_THINKING_LEVEL,
) -> dict[str, Any]:
    """Generate-content settings honoring the model's own constraints.

    Returned as plain data (not a provider type) so non-provider tests can assert
    the contract without importing ``google.*``. For Gemini 3 Flash this carries
    only ``thinking_level`` -- temperature and sampling controls are unsupported
    and silently ignored, so sending them would pretend to tune what it cannot.
    """
    if _is_gemini_3_flash(model or model_name()):
        return {"thinking_level": thinking_level}
    return {"temperature": 0.2}


def metadata(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Safe operational metadata: identity and limits, never credentials or prompts."""
    model = model_name(env)
    return {
        "model": model,
        "provider": "vertex_ai",
        "thinking_level": DEFAULT_THINKING_LEVEL,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "live_api_supported": supports_live_api(model),
        "source": (
            MODEL_ENV
            if (env or os.environ).get(MODEL_ENV, "").strip()
            else (
                GEMINI_MODEL_ENV
                if (env or os.environ).get(GEMINI_MODEL_ENV, "").strip()
                else "default"
            )
        ),
    }

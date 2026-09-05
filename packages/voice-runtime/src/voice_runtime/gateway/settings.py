"""Voice Gateway configuration, and what each setting refuses to do without.

Nothing here has a default that silently degrades security. An empty origin allow-list
admits no browser rather than every browser; an absent Google project disables speech
**visibly** rather than pretending to listen. Specification 19.12: a degraded path the
buyer cannot see is a defect, and that begins at configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

__all__ = ["GatewaySettings"]

#: Where the trusted commerce API lives. Every consequential call goes here, carrying the
#: buyer's own bearer.
API_BASE_URL_ENV: Final[str] = "VOICE_GATEWAY_API_BASE_URL"
ALLOWED_ORIGINS_ENV: Final[str] = "VOICE_GATEWAY_ALLOWED_ORIGINS"
PROJECT_ENV: Final[str] = "GOOGLE_CLOUD_PROJECT"
VERTEX_ENV: Final[str] = "GOOGLE_GENAI_USE_VERTEXAI"

_DEFAULT_API_BASE_URL: Final[str] = "http://127.0.0.1:8000"
#: The storefront in local development. Production supplies its own list.
_DEFAULT_ORIGINS: Final[tuple[str, ...]] = ("http://localhost:3000", "http://127.0.0.1:3000")
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Resolved configuration for one gateway process."""

    api_base_url: str = _DEFAULT_API_BASE_URL
    allowed_origins: tuple[str, ...] = _DEFAULT_ORIGINS
    project: str | None = None
    use_vertex: bool = False
    #: Set when Cloud TTS is reachable. When it is not, transactional sentences are spoken
    #: by the conversational voice and the substitution is made visible (19.2, 19.12).
    chirp_available: bool = True
    metrics: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> GatewaySettings:
        env = environ if environ is not None else dict(os.environ)
        raw_origins = env.get(ALLOWED_ORIGINS_ENV)
        origins = (
            tuple(part.strip() for part in raw_origins.split(",") if part.strip())
            if raw_origins is not None
            else _DEFAULT_ORIGINS
        )
        return cls(
            api_base_url=env.get(API_BASE_URL_ENV, _DEFAULT_API_BASE_URL).rstrip("/"),
            allowed_origins=origins,
            project=env.get(PROJECT_ENV) or None,
            use_vertex=env.get(VERTEX_ENV, "").strip().casefold() in _TRUTHY,
        )

    @property
    def speech_configured(self) -> bool:
        """Whether speech can run at all. False means the socket opens in text mode and
        says so on its first frame, rather than listening to a microphone it cannot use."""
        return bool(self.project) and self.use_vertex

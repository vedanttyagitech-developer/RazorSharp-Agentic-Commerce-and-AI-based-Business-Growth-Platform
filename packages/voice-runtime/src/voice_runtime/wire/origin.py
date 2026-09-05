"""Origin allow-list validation for the voice WebSocket (24.2).

Browsers send ``Origin`` on every WebSocket handshake and scripts cannot forge it, so an
exact allow-list is the cross-site defence. A missing or ``null`` Origin is rejected: the
voice stream is a browser surface, and a non-browser client has no business on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class OriginVerdict:
    allowed: bool
    reason: str


class OriginPolicy:
    """Exact-match allow-list over normalised ``scheme://host[:port]`` origins."""

    def __init__(self, allowed: Iterable[str]) -> None:
        normalised: set[str] = set()
        for entry in allowed:
            origin = self.normalise(entry)
            if origin is None:
                raise ValueError(f"not an origin: {entry!r}")
            normalised.add(origin)
        self._allowed = frozenset(normalised)

    @property
    def allowed(self) -> frozenset[str]:
        return self._allowed

    @staticmethod
    def normalise(origin: str) -> str | None:
        parts = urlsplit(origin.strip())
        if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
            return None
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        host = parts.hostname.lower()
        if port is None or port == _DEFAULT_PORTS[parts.scheme]:
            return f"{parts.scheme}://{host}"
        return f"{parts.scheme}://{host}:{port}"

    def check(self, origin: str | None) -> OriginVerdict:
        if origin is None or origin == "":
            return OriginVerdict(False, "origin_missing")
        if origin.strip().lower() == "null":
            return OriginVerdict(False, "origin_null")
        normalised = self.normalise(origin)
        if normalised is None:
            return OriginVerdict(False, "origin_malformed")
        if normalised not in self._allowed:
            return OriginVerdict(False, "origin_not_allowed")
        return OriginVerdict(True, "ok")

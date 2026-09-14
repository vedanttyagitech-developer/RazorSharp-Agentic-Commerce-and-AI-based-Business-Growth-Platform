"""Request-local output hooks. Never stores identity, tools or provider clients globally."""

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

speech_sink: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "speech_sink", default=None
)
text_sink: ContextVar[Callable[[str], None] | None] = ContextVar("text_sink", default=None)

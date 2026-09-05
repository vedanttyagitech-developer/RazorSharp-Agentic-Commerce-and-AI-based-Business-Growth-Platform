"""Markdown in, speakable text out.

A model writes for a screen: ``**bold**``, ``- bullets``, ``1. numbering``, backticks and
links. Read aloud verbatim, every one of those is a symbol name in the buyer's ear. This
strips the *markup* and nothing else: words, numbers, currency signs and punctuation pass
through unchanged, so the outbound money guard sees exactly the amounts it would have seen.

Deterministic and total: no model, no network, and the empty string is the only input that
yields the empty string.
"""

from __future__ import annotations

import re
from typing import Final

_CODE: Final = re.compile(r"`([^`]*)`")
_LINK: Final = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_STRONG: Final = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_EMPHASIS: Final = re.compile(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", re.S)
_HEADING: Final = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_BULLET: Final = re.compile(r"^[ \t]*(?:[-*•▪◦]|\d{1,3}[.)])[ \t]+", re.M)
_RULE: Final = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_STRAY: Final = re.compile(r"[*_#`~>|]+")
_SPACES: Final = re.compile(r"[ \t]{2,}")
_BLANKS: Final = re.compile(r"\n[ \t]*\n+")


def plain_for_speech(text: str) -> str:
    """The text a voice should say for ``text`` written as Markdown."""
    out = _CODE.sub(r"\1", text)
    out = _LINK.sub(r"\1", out)
    out = _STRONG.sub(r"\2", out)
    out = _EMPHASIS.sub(r"\1", out)
    out = _RULE.sub("", out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub("", out)
    out = _STRAY.sub(" ", out)
    out = _SPACES.sub(" ", out)
    out = _BLANKS.sub("\n", out)
    return "\n".join(line.strip() for line in out.split("\n")).strip()

"""Token handling, constant-time comparison and the raw-body cap.

Four small primitives that every other module borrows. They are collected here rather
than inlined because each one has a way of being written that looks correct and is not:

* **Comparison.** ``a == b`` on a secret returns as soon as two bytes differ, so the time
  it takes leaks how long a prefix the attacker guessed right. Every comparison of a
  bearer token, a scenario key or a signature goes through
  :func:`constant_time_equals`.

* **Storage.** ``api_sessions.token_hash`` holds the SHA-256 of the token and never the
  token, so a dump of that table cannot be replayed against this API. There is no salt
  and no work factor on purpose: this is a 256-bit random token, not a human password, so
  there is nothing to brute force and a slow hash on the authentication path would only
  add a denial-of-service surface.

* **Minting.** ``secrets.token_urlsafe`` and nothing else. A token built from a UUID, a
  timestamp or ``random`` is guessable, and the guess is a session belonging to somebody
  else.

* **The body cap.** ADR 0003 D7 caps the webhook body at 256 KiB *before* it is read into
  memory. ``await request.body()`` buffers whatever arrives first and only then lets you
  measure it, which makes the limit advisory: an attacker sends 2 GiB and the check runs
  after the process has already tried to hold it. :func:`read_capped_body` streams and
  stops at the cap.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

from starlette.requests import Request

__all__ = [
    "AUTHORIZATION_HEADER",
    "MAX_WEBHOOK_BODY_BYTES",
    "SCENARIO_KEY_HEADER",
    "TOKEN_BYTES",
    "BodyTooLargeError",
    "bearer_token",
    "constant_time_equals",
    "hash_token",
    "mint_token",
    "read_capped_body",
]

AUTHORIZATION_HEADER: Final[str] = "Authorization"
SCENARIO_KEY_HEADER: Final[str] = "X-Scenario-Key"

#: 32 bytes of entropy, rendered as 43 URL-safe characters. Comfortably beyond guessing
#: and short enough to paste into a demo script.
TOKEN_BYTES: Final[int] = 32

#: ADR 0003 D13. A Razorpay webhook is a few kilobytes; 256 KiB is generous headroom and
#: still small enough that a flood of them cannot exhaust the process.
MAX_WEBHOOK_BODY_BYTES: Final[int] = 256 * 1024


class BodyTooLargeError(Exception):
    """The request body exceeded the cap and was not read to completion.

    Carries the limit so the caller can name it in a 413 without importing the constant.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"request body exceeds the {limit} byte limit")
        self.limit = limit


def mint_token() -> str:
    """A fresh opaque bearer token. The only place a session token is created."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The value stored in ``api_sessions.token_hash``: lowercase SHA-256 hex.

    64 characters, which is exactly the width of the column. Encoding is UTF-8 so a token
    is hashed the same way on every platform.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str | None, right: str | None) -> bool:
    """Compare two secrets without leaking how much of one matched.

    ``None`` on either side is False, and the comparison still runs against a dummy value
    so that "no token supplied" and "wrong token supplied" take the same time. Encoding
    happens before ``compare_digest`` because the string form of that function refuses
    non-ASCII input, and a token pasted from a terminal can carry anything.
    """
    left_bytes = b"" if left is None else left.encode("utf-8")
    right_bytes = b"" if right is None else right.encode("utf-8")
    matched = hmac.compare_digest(left_bytes, right_bytes)
    return matched and left is not None and right is not None


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header.

    Returns None for an absent, malformed or differently-schemed header. The scheme is
    matched case-insensitively (RFC 7235 says it is case-insensitive); the token is not
    touched beyond stripping surrounding whitespace, because a token is opaque and
    "helpfully" normalising it would make a valid token fail.
    """
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    return token or None


async def read_capped_body(request: Request, limit: int = MAX_WEBHOOK_BODY_BYTES) -> bytes:
    """Read the raw body, refusing to buffer more than ``limit`` bytes (ADR 0003 D7).

    Returns the exact bytes that were signed. Nothing is decoded, parsed or normalised:
    the webhook's HMAC covers these bytes, and a round trip through JSON would change
    them.

    Raises :class:`BodyTooLargeError` as soon as the accumulated length passes ``limit``,
    without reading the rest of the stream.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLargeError(limit)
        chunks.append(chunk)
    return b"".join(chunks)

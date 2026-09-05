"""Single-use voice tickets (19.15, 24.2).

A WebSocket cannot carry an ``Authorization`` header from a browser, so the REST surface
mints a short-lived ticket bound to the tenant and commerce session, and the socket
redeems it once. Random 32 bytes, 60-second lifetime, consumed on first use: a leaked
ticket is worthless after a minute or after one connection, whichever comes first.

The store is in-memory: one gateway process per deployment (ADR D14 style restriction).
"""

from __future__ import annotations

import base64
import re
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..clock import Clock
from ..constants import VOICE_TICKET_BYTES, VOICE_TICKET_TTL_S

_TOKEN_SHAPE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")


class TicketError(Exception):
    """``reason`` is one of ``malformed``, ``unknown``, ``expired``, ``tenant_mismatch``.
    A consumed ticket reports ``unknown``: the store forgets it, so replay and guess are
    indistinguishable to a caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TicketClaims:
    tenant_id: uuid.UUID
    session_id: str
    principal_id: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    token: str
    expires_in_s: float
    claims: TicketClaims


class TicketIssuer:
    """Mint and redeem single-use tickets."""

    def __init__(
        self,
        *,
        clock: Clock,
        ttl_s: float = VOICE_TICKET_TTL_S,
        token_bytes: int = VOICE_TICKET_BYTES,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if token_bytes < 32:
            raise ValueError("voice tickets carry at least 32 random bytes")
        self._clock = clock
        self._ttl_s = ttl_s
        self._token_bytes = token_bytes
        self._random_bytes = random_bytes
        self._tickets: dict[str, TicketClaims] = {}
        self._lock = threading.Lock()
        # Metrics (19.13)
        self.issued = 0
        self.redeemed = 0
        self.rejected = 0

    def issue(self, *, tenant_id: uuid.UUID, session_id: str, principal_id: str) -> IssuedTicket:
        now = self._clock.now()
        token = base64.urlsafe_b64encode(self._random_bytes(self._token_bytes)).decode().rstrip("=")
        claims = TicketClaims(
            tenant_id=tenant_id,
            session_id=session_id,
            principal_id=principal_id,
            issued_at=now,
            expires_at=now + self._ttl_s,
        )
        with self._lock:
            self._purge(now)
            self._tickets[token] = claims
            self.issued += 1
        return IssuedTicket(token=token, expires_in_s=self._ttl_s, claims=claims)

    def redeem(self, token: str | None, *, tenant_id: uuid.UUID | None = None) -> TicketClaims:
        """Consume a ticket. Exactly one redeem can ever succeed for a token."""
        if not token or not _TOKEN_SHAPE.match(token):
            self.rejected += 1
            raise TicketError("malformed")
        now = self._clock.now()
        with self._lock:
            claims = self._tickets.pop(token, None)  # consumed whether or not it is valid
            if claims is None:
                self.rejected += 1
                raise TicketError("unknown")
            if now > claims.expires_at:
                self.rejected += 1
                raise TicketError("expired")
            if tenant_id is not None and claims.tenant_id != tenant_id:
                self.rejected += 1
                raise TicketError("tenant_mismatch")
            self.redeemed += 1
        return claims

    def _purge(self, now: float) -> None:
        expired = [token for token, claims in self._tickets.items() if now > claims.expires_at]
        for token in expired:
            del self._tickets[token]

    @property
    def outstanding(self) -> int:
        with self._lock:
            self._purge(self._clock.now())
            return len(self._tickets)

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

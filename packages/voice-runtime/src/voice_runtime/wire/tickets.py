"""Single-use voice tickets (19.15, 24.2).

A browser cannot put an ``Authorization`` header on a WebSocket handshake, so the buyer's
bearer cannot travel with the socket. The gateway therefore mints a short-lived ticket
over ordinary authenticated HTTP and the socket redeems it once.

WHAT THE TICKET IS, AND WHAT IT IS NOT
--------------------------------------
The ticket is an **opaque handle**, not a credential and not a capability. It carries no
authority of its own: what it does is let the gateway find, server-side, the bearer the
buyer already presented. That bearer never reaches the browser a second time and never
appears in a URL, a log line or a frame. Every consequential call the gateway makes on the
buyer's behalf goes back to the trusted server carrying that bearer, so tenancy, ownership
and every capability check happen exactly where they do for typed input (19.11).

Random 32 bytes, 60-second lifetime, consumed on first redeem: a leaked ticket is worthless
after a minute or after one connection, whichever comes first. A ticket that has been
redeemed reports ``unknown`` rather than ``consumed``, so replay and guess are
indistinguishable to a caller and neither confirms that a ticket ever existed.

The store is in-memory and process-local: one gateway process per deployment. A second
process would need a shared store, and the failure mode of getting that wrong -- a ticket
redeemable twice -- is exactly what single-use exists to prevent, so it is stated here
rather than discovered.
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

__all__ = ["IssuedTicket", "TicketClaims", "TicketError", "TicketIssuer"]

_TOKEN_SHAPE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")


class TicketError(Exception):
    """``reason`` is one of ``malformed``, ``unknown``, ``expired``, ``tenant_mismatch``.

    A consumed ticket reports ``unknown``: the store forgets it, so replay and guess are
    indistinguishable to a caller.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TicketClaims:
    """What the gateway remembers about a ticket. Never serialised to a client."""

    session_id: str
    principal_id: str
    #: The buyer's own bearer, held server-side for the socket's lifetime. This is the
    #: only authority in the system and it is never sent to the browser or put in a URL.
    bearer: str
    issued_at: float
    expires_at: float
    #: Known when the minting path learned it; the server enforces tenancy on every call
    #: regardless, so this is for binding and audit, never for an authorization decision.
    tenant_id: uuid.UUID | None = None

    def __repr__(self) -> str:
        """Redacted: a bearer that reaches a log or a traceback has already leaked."""
        return (
            f"TicketClaims(session_id={self.session_id!r}, "
            f"principal_id={self.principal_id!r}, bearer=<redacted>, "
            f"tenant_id={self.tenant_id!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    """What the minting endpoint returns. ``token`` is the only part the client sees."""

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

    def issue(
        self,
        *,
        session_id: str,
        principal_id: str,
        bearer: str,
        tenant_id: uuid.UUID | None = None,
    ) -> IssuedTicket:
        now = self._clock.now()
        token = base64.urlsafe_b64encode(self._random_bytes(self._token_bytes)).decode().rstrip("=")
        claims = TicketClaims(
            session_id=session_id,
            principal_id=principal_id,
            bearer=bearer,
            issued_at=now,
            expires_at=now + self._ttl_s,
            tenant_id=tenant_id,
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

"""The copilot session: identity the harness owns and a model never sees.

Specification 6.1 asks the coordinator to "preserve session, tenant, locale, modality and
correlation IDs" across turns. Here that is a dataclass the harness reads and writes and
a store the harness looks it up in. None of these fields is a tool parameter, none is in
a prompt, and none can be changed by anything a model says: the only writer is
:mod:`agent_runtime.harness.base`, after a turn, from structured tool records.

Two fields deserve a word:

* ``correlation_id`` is fixed at session creation (from the principal, then the request
  context, then minted) and never changes. Every kernel submission and audit event of the
  session carries it, so one identifier ties a buyer's first search to their refund.
* ``causation_id`` is the previous turn's ``turn_id``. It is how a reader of the audit
  trail walks backwards from a kernel decision to the message that led to it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from ..language import Language

__all__ = [
    "CopilotSession",
    "InMemorySessionStore",
    "Modality",
    "SessionRefusedError",
    "SessionStore",
    "session_tag",
]


class Modality(StrEnum):
    """How the buyer's words arrived. Decided by the transport, never by the model."""

    TEXT = "text"
    VOICE = "voice"


class SessionRefusedError(Exception):
    """A request tried to continue a session under a principal that does not own it.

    Raised, not rendered: this is not a conversational failure, it is a tenant boundary,
    and the API maps it to a 403 before any specialist runs.
    """


def session_tag(session_id: str) -> str:
    """What a log line carries instead of the session id.

    Twelve hex digits of SHA-256, as in the reference runtime: enough for an operator who
    holds the id to correlate the lines of one session, useless to a log reader who does
    not, because on the API the session id is also the request credential.
    """
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


@dataclass(slots=True)
class CopilotSession:
    """One commerce session across text and voice. Mutable only by the harness."""

    session_id: str
    tenant_id: uuid.UUID
    principal_id: str
    correlation_id: uuid.UUID
    buyer_ref: str | None = None
    merchant_id: uuid.UUID | None = None
    modality: Modality = Modality.TEXT
    language: Language = Language.EN
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    turns: int = 0
    last_turn_id: uuid.UUID | None = None
    last_specialist: str | None = None
    # Identifiers structured tool results established. Routing reads them as explicit
    # context on later turns; a model cannot set them because no tool parameter does.
    cart_id: str | None = None
    checkout_id: str | None = None
    checkout_version: int | None = None
    order_id: str | None = None
    case_id: str | None = None
    # Tool-visible session state: the ids the factory's tools keep and the provenance
    # record the gates read. The harness hands it to a prefetched tool as its context
    # and the runtime seeds the model session from it; a model never writes it directly.
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def tag(self) -> str:
        return session_tag(self.session_id)

    def begin_turn(self) -> tuple[uuid.UUID, uuid.UUID | None]:
        """Mint this turn's id and return it with the previous turn's id as causation."""
        turn_id = uuid.uuid4()
        causation = self.last_turn_id
        return turn_id, causation

    def end_turn(self, turn_id: uuid.UUID, specialist: str | None, language: Language) -> None:
        self.turns += 1
        self.last_turn_id = turn_id
        self.last_specialist = specialist
        self.language = language


class SessionStore(Protocol):
    """Where sessions live between turns. In-memory for the demo; a table in production."""

    def get(self, session_id: str) -> CopilotSession | None: ...

    def put(self, session: CopilotSession) -> None: ...


class InMemorySessionStore:
    """A dict. Sufficient under ADR 0003 D14's single worker; replaced, not extended."""

    def __init__(self) -> None:
        self._sessions: dict[str, CopilotSession] = {}

    def get(self, session_id: str) -> CopilotSession | None:
        return self._sessions.get(session_id)

    def put(self, session: CopilotSession) -> None:
        self._sessions[session.session_id] = session

    def __len__(self) -> int:
        return len(self._sessions)

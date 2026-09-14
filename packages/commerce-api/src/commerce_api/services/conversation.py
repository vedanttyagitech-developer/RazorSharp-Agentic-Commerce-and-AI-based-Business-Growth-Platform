"""Shared conversation service for typed and spoken turns.

One conversation identifier spans typing and voice for the same buyer: both
transports resolve the same record, read the same history and visible
references, and append to it. The voice gateway is a caller of this service,
never its owner -- nothing here knows about sockets, audio or recognition.

Durability, stated plainly: this store is bounded process memory (LRU with TTL),
the same durability the principal display memory it replaces always had. A
restart forgets conversations; carts, checkouts and orders do not, because
those live in PostgreSQL. Do not mistake this record for a source of truth.

Authorization is recomputed per request and never stored: the record carries no
capabilities, no principal and no tenant authority. :func:`authorize` re-derives
everything from the authenticated server context and filters the record down to
what that principal may see. A conversation ID is an index, never a credential.
Cross-surface moves keep only safe, relevant context in separate partitions per
actor type -- buyer display references never enter a merchant partition.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "ConversationRecord",
    "ConversationService",
    "ConversationView",
    "MAX_CONVERSATIONS",
    "MAX_TURNS",
    "PARTITION_TTL_S",
    "TurnRecord",
    "shared_service",
]

#: Bounded history per conversation: follow-ups need recency, not archaeology.
MAX_TURNS: Final[int] = 50
#: Bounded conversations per process: an unbounded map is a slow memory leak with
#: a request-driven keyspace.
MAX_CONVERSATIONS: Final[int] = 4096
#: Idle partitions are dropped. Matches the display-memory TTL it replaces.
PARTITION_TTL_S: Final[float] = 300.0
#: Tool outcomes travel as SKU lists and stages, never full payloads: a turn that
#: searched twelve products records twelve SKUs, not twelve product cards.
MAX_SKUS_PER_TURN: Final[int] = 50


@dataclass(slots=True)
class TurnRecord:
    """One turn, as the server saw it. Narration is not stored: the reply text is
    the record of what was said, and evidence lives beside it, not inside it."""

    turn_id: uuid.UUID
    operation_id: uuid.UUID
    message: str
    reply: str
    language: str
    surface: str
    skus: tuple[str, ...] = ()
    reason_code: str = ""
    stage: str = ""
    recorded_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class Partition:
    """One actor type's view inside a conversation. Buyer shelves and merchant
    rows never share a partition, so a surface switch cannot leak references."""

    actor_type: str
    turns: list[TurnRecord] = field(default_factory=list)
    displayed: tuple[str, ...] = ()
    topic: str = ""
    language: str = "en"
    last_active: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class ConversationRecord:
    """The stored record. Tenant-bound: a conversation names exactly one tenant."""

    conversation_id: uuid.UUID
    tenant_id: uuid.UUID
    partitions: dict[str, Partition] = field(default_factory=dict)
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationView:
    """What one authorized principal may read: history, refs and topic -- and
    nothing from another tenant, another conversation, or another actor type."""

    conversation_id: uuid.UUID
    history: tuple[TurnRecord, ...]
    displayed: tuple[str, ...]
    topic: str
    language: str


class ConversationService:
    """Bounded, threaded-safe conversation memory. See the module docstring for
    what this is and is not."""

    def __init__(
        self,
        *,
        max_conversations: int = MAX_CONVERSATIONS,
        max_turns: int = MAX_TURNS,
        ttl_s: float = PARTITION_TTL_S,
    ) -> None:
        self._max_conversations = max_conversations
        self._max_turns = max_turns
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._store: OrderedDict[uuid.UUID, ConversationRecord] = OrderedDict()

    def claim(self, conversation_id: uuid.UUID, *, tenant_id: uuid.UUID, owner: str) -> None:
        """Bind a conversation to an authenticated session and surface before any work."""
        from ..errors import ProblemError

        with self._lock:
            record = self._store.get(conversation_id)
            if record is not None:
                if record.tenant_id != tenant_id or record.owner != owner:
                    raise ProblemError(
                        403,
                        "Conversation unavailable",
                        "Conversation belongs to another session or surface.",
                    )
                return
            if len(self._store) >= self._max_conversations:
                self._store.popitem(last=False)
            self._store[conversation_id] = ConversationRecord(
                conversation_id=conversation_id, tenant_id=tenant_id, owner=owner
            )

    def view(
        self,
        conversation_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        actor_type: str,
    ) -> ConversationView:
        """The authorized view, or an empty view for unknown/foreign records.

        Unknown, expired and other-tenant conversations all read as empty: the
        caller cannot distinguish them, so none of them leaks.
        """
        now = time.monotonic()
        with self._lock:
            record = self._store.get(conversation_id)
            if record is None or record.tenant_id != tenant_id:
                return ConversationView(
                    conversation_id=conversation_id,
                    history=(),
                    displayed=(),
                    topic="",
                    language="en",
                )
            partition = record.partitions.get(actor_type)
            if partition is None or now - partition.last_active > self._ttl_s:
                return ConversationView(
                    conversation_id=conversation_id,
                    history=(),
                    displayed=(),
                    topic="",
                    language="en",
                )
            self._store.move_to_end(conversation_id)
            return ConversationView(
                conversation_id=conversation_id,
                history=tuple(partition.turns),
                displayed=partition.displayed,
                topic=partition.topic,
                language=partition.language,
            )

    def record_turn(
        self,
        conversation_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        actor_type: str,
        turn: TurnRecord,
        displayed: tuple[str, ...] | None = None,
        topic: str | None = None,
        language: str | None = None,
    ) -> None:
        """Append a turn. Creates the conversation and partition as needed."""
        with self._lock:
            record = self._store.get(conversation_id)
            if record is None:
                if len(self._store) >= self._max_conversations:
                    self._store.popitem(last=False)
                record = ConversationRecord(conversation_id=conversation_id, tenant_id=tenant_id)
                self._store[conversation_id] = record
            elif record.tenant_id != tenant_id:
                # A conversation ID is not a credential, but it is also not a
                # suggestion box: a tenant that did not open it cannot write it.
                return
            partition = record.partitions.get(actor_type)
            if partition is None:
                partition = Partition(actor_type=actor_type)
                record.partitions[actor_type] = partition
            partition.turns.append(turn)
            del partition.turns[: -self._max_turns]
            if displayed is not None:
                partition.displayed = tuple(displayed[:MAX_SKUS_PER_TURN])
            if topic is not None:
                partition.topic = topic[:200]
            if language is not None:
                partition.language = language
            partition.last_active = time.monotonic()
            self._store.move_to_end(conversation_id)

    def set_displayed(
        self,
        conversation_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        actor_type: str,
        displayed: tuple[str, ...],
    ) -> None:
        """Replace the visible shelf. Called when a turn displays products."""
        with self._lock:
            record = self._store.get(conversation_id)
            if record is None or record.tenant_id != tenant_id:
                return
            partition = record.partitions.get(actor_type)
            if partition is None:
                partition = Partition(actor_type=actor_type)
                record.partitions[actor_type] = partition
            partition.displayed = tuple(displayed[:MAX_SKUS_PER_TURN])
            partition.last_active = time.monotonic()

    def history_text(self, view: ConversationView, *, limit: int = 6) -> list[str]:
        """Recent turns as `buyer:`/`assistant:` lines for model context.

        Bounded and text-only: never tool payloads, never full records.
        """
        lines: list[str] = []
        for record in view.history[-limit:]:
            lines.append(f"buyer: {record.message[:500]}")
            lines.append(f"assistant: {record.reply[:500]}")
        return lines


_shared: ConversationService | None = None
_shared_lock = threading.Lock()


def shared_service() -> ConversationService:
    """The process-wide conversation service."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ConversationService()
        return _shared


def context_summary(view: ConversationView) -> Mapping[str, Any]:
    """The compact, safe-to-model context: counts and refs, never payloads."""
    return {
        "turns": len(view.history),
        "topic": view.topic,
        "language": view.language,
        "displayed_skus": list(view.displayed),
    }

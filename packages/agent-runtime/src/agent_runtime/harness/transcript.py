"""The transcript and the tool-call log, in the reference runtime's event shape.

Every turn leaves a sequence of :class:`AgentEvent` records: the buyer's text, one
``tool_call`` and one ``tool_result`` per attempted tool (a denial is a ``tool_result``
with status ``blocked`` and the gate's reason, exactly as the reference reports a held
call), the specialist's reply, and a ``turn_complete``. The host renders the types it
knows and ignores the rest; ``to_sse`` frames one event for the API's stream.

The shape is borrowed from ``commerce_common/streaming.py`` (anthropics/commerce-agents,
Apache-2.0) because a host that already speaks it should not need a second protocol. The
implementation is ours: a plain dataclass rather than a pydantic model, because nothing
here is validated from the outside -- the harness is the only writer.

Events are built from :class:`~agent_runtime.turn.TurnContext` records, never from model
prose, so the log records what happened even when the reply was rewritten.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..turn import ToolCallRecord

__all__ = ["AgentEvent", "EventType", "Transcript", "TranscriptTurn", "events_for_calls", "to_sse"]

EventType = Literal[
    "user_message",
    "text_delta",
    "tool_call",
    "tool_result",
    "ui",
    "progress",
    "turn_complete",
    "error",
]

#: A tool result longer than this collapses to ``ok`` in the log with an excerpt, so a
#: transcript never carries a whole catalogue page.
_SUMMARY_MAX_CHARS = 200
_EXCERPT_MAX_CHARS = 600


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One host-visible event. ``data`` is JSON-safe by construction of the factories."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def user_message(cls, text: str, *, modality: str) -> AgentEvent:
        return cls("user_message", {"text": text, "modality": modality})

    @classmethod
    def text_delta(cls, text: str) -> AgentEvent:
        return cls("text_delta", {"text": text})

    @classmethod
    def tool_call(cls, tool: str, call_id: str, args: dict[str, Any]) -> AgentEvent:
        return cls("tool_call", {"tool": tool, "id": call_id, "input": args})

    @classmethod
    def tool_result(
        cls,
        tool: str,
        call_id: str,
        summary: str,
        *,
        is_error: bool = False,
        status: str | None = None,
        reason: str | None = None,
        excerpt: str | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "tool": tool,
            "id": call_id,
            "summary": summary,
            "is_error": is_error,
            "status": status or ("error" if is_error else "ok"),
        }
        if reason:
            data["reason"] = reason
        if excerpt is not None:
            data["excerpt"] = excerpt
        return cls("tool_result", data)

    @classmethod
    def ui(cls, component: str, payload: dict[str, Any]) -> AgentEvent:
        return cls("ui", {"component": component, "payload": payload})

    @classmethod
    def progress(cls, message: str, *, tool: str | None = None) -> AgentEvent:
        data: dict[str, Any] = {"message": message}
        if tool:
            data["tool"] = tool
        return cls("progress", data)

    @classmethod
    def turn_complete(
        cls,
        *,
        stop_reason: str,
        specialist: str | None,
        routing_reason: str,
        elapsed_ms: int,
        corrections: Sequence[str],
    ) -> AgentEvent:
        return cls(
            "turn_complete",
            {
                "stop_reason": stop_reason,
                "specialist": specialist,
                "routing_reason": routing_reason,
                "elapsed_ms": elapsed_ms,
                "corrections": list(corrections),
            },
        )

    @classmethod
    def error(cls, message: str) -> AgentEvent:
        return cls("error", {"message": message})


def to_sse(event: AgentEvent) -> str:
    """One Server-Sent Events frame: ``event:`` the type, ``data:`` the JSON, blank line."""
    return (
        f"event: {event.type}\ndata: {json.dumps(event.data, ensure_ascii=False, default=str)}\n\n"
    )


def _summary_text(record: ToolCallRecord) -> tuple[str, str | None]:
    """The result summary for the log and, when it was cut, the excerpt."""
    text = json.dumps(record.summary, ensure_ascii=False, default=str, sort_keys=True)
    if len(text) < _SUMMARY_MAX_CHARS:
        return text, None
    return "ok", text[:_EXCERPT_MAX_CHARS]


def events_for_calls(records: Sequence[ToolCallRecord], turn_id: uuid.UUID) -> list[AgentEvent]:
    """A ``tool_call``/``tool_result`` pair per record, in the order the turn recorded them.

    A denied call is reported the way the reference reports a held call: status
    ``blocked`` with the gate's reason key. It is not an error -- the gate working is the
    system working -- and it is not omitted, because the log exists to show what the
    model tried, not only what it achieved.
    """
    events: list[AgentEvent] = []
    for index, record in enumerate(records):
        call_id = f"{turn_id}:{index}"
        events.append(AgentEvent.tool_call(record.tool, call_id, record.args))
        if record.denied:
            events.append(
                AgentEvent.tool_result(
                    record.tool,
                    call_id,
                    "denied by the capability gate; the tool did not run",
                    status="blocked",
                    reason=record.reason_key,
                )
            )
        elif not record.ok:
            events.append(
                AgentEvent.tool_result(
                    record.tool,
                    call_id,
                    record.reason_key or "tool_failed",
                    is_error=True,
                    reason=record.reason_key,
                )
            )
        else:
            summary, excerpt = _summary_text(record)
            events.append(AgentEvent.tool_result(record.tool, call_id, summary, excerpt=excerpt))
    return events


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    """One turn's record: who spoke, who answered, what was called, what was refused."""

    turn_id: uuid.UUID
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None
    specialist: str | None
    routing_reason: str
    user_text: str
    reply_text: str
    events: tuple[AgentEvent, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(e.data["tool"] for e in self.events if e.type == "tool_call")

    @property
    def denied_tools(self) -> tuple[str, ...]:
        return tuple(
            e.data["tool"]
            for e in self.events
            if e.type == "tool_result" and e.data.get("status") == "blocked"
        )


@dataclass(slots=True)
class Transcript:
    """The turns of one session, oldest first. Append-only; the harness is the writer."""

    session_id: str
    turns: list[TranscriptTurn] = field(default_factory=list)

    def append(self, turn: TranscriptTurn) -> None:
        self.turns.append(turn)

    def events(self) -> Iterator[AgentEvent]:
        for turn in self.turns:
            yield from turn.events

    def __len__(self) -> int:
        return len(self.turns)

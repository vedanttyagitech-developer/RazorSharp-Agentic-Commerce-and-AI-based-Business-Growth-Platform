"""The seam to the text agents: a final transcript in, a structured reply out.

``agent-runtime`` implements ``TurnHandler``; the voice layer never sees a model. A reply
may carry conversational text (model-authored, guarded per sentence before synthesis) and
a ``KernelDecision`` with server-confirmed amounts, which the pipeline renders through the
deterministic templates (19.10). The transcript it receives is intent evidence, never
authority evidence (19.11): nothing in this contract can approve, pay or refund.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from commerce_domain import Money
from transaction_kernel.contracts import AgentPrincipal, KernelDecision

from .stt.transcript import TranscriptTurn
from .tts.templates import Locale


@dataclass(frozen=True, slots=True)
class TurnReply:
    """What the agent wants said. ``text`` is conversational; ``decision`` is transactional."""

    text: str = ""
    locale: Locale = Locale.EN_IN
    decision: KernelDecision | None = None
    amount: Money | None = None
    previous_amount: Money | None = None


class TurnHandler(Protocol):
    async def handle_turn(self, transcript: TranscriptTurn, principal: AgentPrincipal) -> TurnReply:
        """Process one settled turn. Raising is reported as a visible reasoning failure."""
        ...


class FakeTurnHandler:
    """Scripted replies in order; an echo reply when the script is exhausted."""

    def __init__(
        self,
        *,
        replies: Sequence[TurnReply] = (),
        fail: bool = False,
        locale: Locale = Locale.EN_IN,
    ) -> None:
        self.calls: list[tuple[TranscriptTurn, AgentPrincipal]] = []
        self._replies = list(replies)
        self.fail = fail
        self._locale = locale

    async def handle_turn(self, transcript: TranscriptTurn, principal: AgentPrincipal) -> TurnReply:
        self.calls.append((transcript, principal))
        if self.fail:
            raise RuntimeError("fake reasoning failure")
        if self._replies:
            return self._replies.pop(0)
        return TurnReply(text=f"You said {transcript.text}.", locale=self._locale)

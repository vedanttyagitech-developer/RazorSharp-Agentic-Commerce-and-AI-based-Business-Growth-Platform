"""The turn: its evidence record, and the one entry point the API calls.

Two things live here. :class:`TurnContext` is the per-turn evidence record, created for
each message and captured by the tool factory, the capability gate and the reply
post-check, so the caller receives one attributable record -- tool calls, denials, kernel
decisions, injection flags -- instead of scraping them from model prose.
:func:`run_turn` is the public entry: it picks the harness the principal belongs to --
decided from the principal the server minted, never from the body -- and runs one turn
through it.

Everything here is data about the turn or plumbing around it. Nothing here authorizes
anything, and nothing here calls a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from transaction_kernel import AdmissionDecision, AgentPrincipal

from .grounding.ledger import GroundingLedger
from .language import Language

if TYPE_CHECKING:
    from .backends.base import CommerceBackend
    from .harness.base import Harness, SpecialistRunner, ToolsetBuilder, TurnResult

__all__ = [
    "Copilots",
    "Denial",
    "InjectionFlag",
    "ToolCallRecord",
    "TurnContext",
    "copilots",
    "run_turn",
]


@dataclass(slots=True)
class ToolCallRecord:
    """One attempted tool call, whether it ran, was refused or failed."""

    agent: str
    tool: str
    args: dict[str, Any]
    ok: bool
    denied: bool = False
    reason_key: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Denial:
    """A capability refusal. Recorded before the tool would have run; the tool never runs."""

    agent: str
    tool: str
    capability: str | None
    reason_key: str
    principal_id: str


@dataclass(frozen=True, slots=True)
class InjectionFlag:
    """Instruction-like content detected in merchant text (specification 20.3)."""

    tool: str
    sku: str | None
    flags: tuple[str, ...]


@dataclass(slots=True)
class TurnContext:
    """Mutable record of one turn. Created per buyer message; never shared across turns."""

    language: Language
    principal: AgentPrincipal
    max_tool_calls: int = 8
    ledger: GroundingLedger = field(default_factory=GroundingLedger)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    decisions: list[AdmissionDecision] = field(default_factory=list)
    injection_flags: list[InjectionFlag] = field(default_factory=list)
    tool_failures: list[dict[str, Any]] = field(default_factory=list)
    intent: str | None = None
    agent_name: str | None = None
    tool_calls_admitted: int = 0

    def consume_tool_budget(self) -> bool:
        """Spend one unit of the per-turn tool budget; False once it is exhausted.

        Specification 20.2 fixes a maximum number of tool calls per turn. Counting is done
        in the gate, before the tool runs, so a model looping on a tool cannot run it.
        """
        if self.tool_calls_admitted >= self.max_tool_calls:
            return False
        self.tool_calls_admitted += 1
        return True

    def record_call(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        *,
        ok: bool,
        summary: dict[str, Any] | None = None,
        reason_key: str | None = None,
    ) -> None:
        self.tool_calls.append(
            ToolCallRecord(
                agent=agent,
                tool=tool,
                args=dict(args),
                ok=ok,
                reason_key=reason_key,
                summary=dict(summary or {}),
            )
        )

    def record_denial(self, denial: Denial, args: dict[str, Any]) -> None:
        self.denials.append(denial)
        self.tool_calls.append(
            ToolCallRecord(
                agent=denial.agent,
                tool=denial.tool,
                args=dict(args),
                ok=False,
                denied=True,
                reason_key=denial.reason_key,
            )
        )

    def record_flag(self, tool: str, sku: str | None, flags: tuple[str, ...]) -> None:
        self.injection_flags.append(InjectionFlag(tool=tool, sku=sku, flags=flags))

    def record_failure(self, agent: str, tool: str, reason_key: str, detail: str) -> None:
        self.tool_failures.append(
            {"agent": agent, "tool": tool, "reason_key": reason_key, "detail": detail}
        )


# ------------------------------------------------------------------ the entry point


class Copilots:
    """The buyer harness, built once and chosen per principal.

    There were two. The merchant harness was removed with the merchant copilot, and this
    class keeps its shape rather than collapsing into a bare constructor: a principal is
    still checked against a harness that can refuse it, and a merchant surface that
    returns adds a member here rather than rewriting every caller.
    """

    def __init__(
        self,
        *,
        runner: SpecialistRunner | None = None,
        tools: ToolsetBuilder | None = None,
        turn_timeout_s: float = 30.0,
    ) -> None:
        # Imported here, not at module top: the harness imports this module's dataclasses.
        from .harness.razorai import RazorAI

        self.buyer: Harness = RazorAI(runner=runner, tools=tools, turn_timeout_s=turn_timeout_s)

    def for_principal(self, principal: AgentPrincipal) -> Harness:
        """The buyer harness, which re-checks acceptance itself.

        A merchant principal is not refused here but by the harness, which raises
        ``PrincipalRefusedError``: the refusal belongs where the capabilities are read,
        not in a lookup that would have to duplicate the check to answer.
        """
        del principal
        return self.buyer


_default: Copilots | None = None


def copilots(*, reset: bool = False, **options: Any) -> Copilots:
    """The process-wide pair. ``reset=True`` rebuilds it (tests and configuration)."""
    global _default  # noqa: PLW0603 - one pair per process is the point
    if _default is None or reset:
        _default = Copilots(**options)
    return _default


async def run_turn(
    session_id: str,
    principal: AgentPrincipal,
    text: str,
    backend: CommerceBackend,
    *,
    context: Mapping[str, Any] | None = None,
    pair: Copilots | None = None,
) -> TurnResult:
    """One conversational turn. What ``POST /v1/agent/turn`` calls, and nothing else.

    ``session_id`` and ``principal`` come from the authenticated request; ``context`` is
    the server's view of what the buyer is looking at (page, checkout id, modality,
    whether a transcript is final). None of it is model-supplied.
    """
    harness = (pair or copilots()).for_principal(principal)
    return await harness.run(session_id, principal, text, backend, context=context)

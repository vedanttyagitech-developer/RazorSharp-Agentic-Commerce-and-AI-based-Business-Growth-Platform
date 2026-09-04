"""The capability broker: derive specialist principals and gate every tool call.

Two pieces, both deterministic:

* :func:`derive_principal` computes a specialist's principal as the intersection of the
  role's built-in allowlist and the harness principal's capabilities (specification 5.4,
  inputs 1 and 4), through :meth:`AgentPrincipal.subset_for`, which itself refuses any
  widening. Two checks for one invariant, because this is the invariant a prompt-injection
  attack would most like to break.
* :func:`make_capability_gate` builds the ``before_tool_callback``. It runs before the
  tool, checks that the tool is one this toolset was built with, that
  ``principal.can(capability)`` holds, and that the per-turn tool budget is not spent. On
  denial it returns a NON-EMPTY dict. Non-empty matters: ADK breaks out of its callback
  loop on a truthy result and skips the tool; an empty dict is falsy, ADK treats it as
  ``None``, and the tool runs. A deny that is ``{}`` is not a deny.

The callbacks are typed against two small protocols rather than ADK's classes, so this
module imports nothing from ``google.adk`` (ADR 0004 section 1.4). ADK's ``BaseTool`` has
``name`` and ``description``; its ``ToolContext`` has ``state`` and ``function_call_id``;
both satisfy the protocols structurally. The adapter in ``runtime_adk/`` registers each
gate as one callable, never a list, so a later callback cannot reset a denial.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any, Final, Protocol

from transaction_kernel import AgentPrincipal

from ..turn import Denial, TurnContext
from .registry import AGENT_ALLOWLIST, AgentRole, capability_for

__all__ = [
    "REASON_CAPABILITY_MISSING",
    "REASON_TOOL_BUDGET_EXHAUSTED",
    "REASON_TOOL_FAILED",
    "REASON_TOOL_NOT_BOUND",
    "REASON_TOOL_NOT_REGISTERED",
    "REASON_TOOL_UNAVAILABLE",
    "ToolContextLike",
    "ToolErrorGate",
    "ToolGate",
    "ToolLike",
    "derive_principal",
    "make_capability_gate",
    "make_tool_error_gate",
]


class ToolLike(Protocol):
    """What a gate needs from a runtime tool object: its name and its description."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...


class ToolContextLike(Protocol):
    """What a tool needs from the runtime: session state and the call it is serving."""

    @property
    def state(self) -> MutableMapping[str, Any]: ...

    @property
    def function_call_id(self) -> str | None: ...


ToolGate = Callable[[ToolLike, dict[str, Any], ToolContextLike], dict[str, Any] | None]
ToolErrorGate = Callable[
    [ToolLike, dict[str, Any], ToolContextLike, Exception], dict[str, Any] | None
]

REASON_CAPABILITY_MISSING: Final[str] = "capability_missing"
REASON_TOOL_NOT_REGISTERED: Final[str] = "tool_not_registered"
REASON_TOOL_NOT_BOUND: Final[str] = "tool_not_bound"
REASON_TOOL_BUDGET_EXHAUSTED: Final[str] = "tool_budget_exhausted"
REASON_TOOL_UNAVAILABLE: Final[str] = "tool_unavailable"
REASON_TOOL_FAILED: Final[str] = "tool_failed"

_DENIAL_INSTRUCTION: Final[str] = (
    "This action is not permitted for you in this session. Tell the buyer plainly and do "
    "not retry or work around it."
)


def derive_principal(parent: AgentPrincipal, role: AgentRole) -> AgentPrincipal:
    """A specialist principal that can never exceed the harness principal.

    Intersection here, refusal of widening inside ``subset_for``. The specialist is
    named by its role so every tool call, denial and kernel submission it makes records
    ``<harness>/<role>`` as ``principal_id`` and the delegation chain stays legible.
    """
    allowed = frozenset(capability.value for capability in AGENT_ALLOWLIST[role])
    return parent.subset_for(role.value, allowed & parent.capabilities)


def make_capability_gate(
    principal: AgentPrincipal,
    turn: TurnContext,
    *,
    agent_name: str,
    bound_tools: frozenset[str] | None = None,
) -> ToolGate:
    """Build the single ``before_tool_callback`` for one specialist.

    ``bound_tools`` is the set of names the factory built for this principal. A tool with
    a registered name that was *not* built by the factory -- a hand-made ``FunctionTool``
    attached beside the toolset -- is denied as ``tool_not_bound`` even when the principal
    holds its capability. The factory is the only source of tools; the gate makes that a
    runtime fact rather than a convention.
    """

    def gate(
        tool: ToolLike, args: dict[str, Any], tool_context: ToolContextLike
    ) -> dict[str, Any] | None:
        capability = capability_for(tool.name)
        if capability is None:
            reason = REASON_TOOL_NOT_REGISTERED
        elif bound_tools is not None and tool.name not in bound_tools:
            reason = REASON_TOOL_NOT_BOUND
        elif not principal.can(capability.value):
            reason = REASON_CAPABILITY_MISSING
        elif not turn.consume_tool_budget():
            reason = REASON_TOOL_BUDGET_EXHAUSTED
        else:
            return None

        denial = Denial(
            agent=agent_name,
            tool=tool.name,
            capability=None if capability is None else capability.value,
            reason_key=reason,
            principal_id=principal.principal_id,
        )
        turn.record_denial(denial, args)
        # Non-empty by construction. See module docstring.
        return {
            "denied": True,
            "ok": False,
            "capability": denial.capability,
            "reason_key": reason,
            "tool": tool.name,
            "principal_id": principal.principal_id,
            "function_call_id": tool_context.function_call_id,
            "instruction": _DENIAL_INSTRUCTION,
        }

    return gate


def make_tool_error_gate(turn: TurnContext, *, agent_name: str) -> ToolErrorGate:
    """Turn a tool exception into a structured failure the model can explain.

    Without this, a model that names a tool it was never given crashes the whole turn
    with the runtime's error; with it, the buyer hears that the action is unavailable and
    the transaction is unchanged (specification 6.2, "catalogue-tool failure").
    """

    def on_error(
        tool: ToolLike, args: dict[str, Any], tool_context: ToolContextLike, error: Exception
    ) -> dict[str, Any] | None:
        del tool_context
        unavailable = tool.description == "Tool not found"
        reason = REASON_TOOL_UNAVAILABLE if unavailable else REASON_TOOL_FAILED
        detail = f"{type(error).__name__}: {error}"[:200]
        turn.record_failure(agent_name, tool.name, reason, detail)
        turn.record_call(agent_name, tool.name, args, ok=False, reason_key=reason)
        return {
            "denied": True,
            "ok": False,
            "reason_key": reason,
            "tool": tool.name,
            "instruction": "The action is unavailable. Nothing changed. Tell the buyer.",
        }

    return on_error

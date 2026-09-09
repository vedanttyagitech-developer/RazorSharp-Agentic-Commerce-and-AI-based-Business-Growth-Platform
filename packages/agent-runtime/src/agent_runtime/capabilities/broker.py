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

from commerce_domain import AgentPrincipal

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
    bound_callables: tuple[Any, ...] | None = None,
) -> ToolGate:
    """Build the single ``before_tool_callback`` for one specialist.

    ``bound_tools`` is the set of names the factory built for this principal, and
    ``bound_callables`` the closures behind them. A tool is denied as ``tool_not_bound``
    unless it is *both* named by the first and carrying a closure from the second.

    The second half is what makes the factory's monopoly a runtime fact rather than a
    convention, and it was missing. A name check alone cannot tell a hand-made
    ``FunctionTool`` called ``search`` from the one the factory built: to a gate comparing
    strings they are the same tool. Identity is compared with ``is`` against the exact
    closure objects, so a tool carrying no callable, or somebody else's, is refused
    whatever it calls itself.

    KNOWN GAP, deliberately not closed here: ``bound_tools`` is a set of *names*, and a
    name is a claim rather than an identity. A hand-built tool object that simply reports
    a name the factory did build passes this check, so the module's "the factory is the
    only source of tools" is a convention and not the runtime fact it claims. Closing it
    means comparing ``getattr(tool, "func", None)`` against the closures ``build_toolset``
    produced, which is a contract change for every caller that drives the gate with a stub;
    it is recorded as an open finding in ``docs/AGENT_ADVERSARIAL.md`` and pinned by a
    strict-xfail test rather than half-applied. Nothing reaches this today: the ADK adapter
    builds one ``FunctionTool`` per factory closure and attaches nothing else.

    ``tool.name`` is read ONCE, into ``name``, and every later check and record uses that
    local. ``ToolLike.name`` is a property, so a tool can compute a different answer each
    time it is asked: one that said ``support_escalate`` to the capability lookup and
    ``order_track`` to the binding check passed both and ran, and one that changed its
    answer again wrote a different tool's name into the audit than the one the gate judged.
    A gate that reads its subject's identity more than once is not gating one subject.
    """

    def gate(
        tool: ToolLike, args: dict[str, Any], tool_context: ToolContextLike
    ) -> dict[str, Any] | None:
        name = tool.name
        capability = capability_for(name)
        # Read once, like ``name``, and for the same reason: a property is free to answer
        # differently on a second look.
        carried = getattr(tool, "func", None)
        if capability is None:
            reason = REASON_TOOL_NOT_REGISTERED
        elif bound_tools is not None and name not in bound_tools:
            reason = REASON_TOOL_NOT_BOUND
        elif bound_callables is not None and not any(carried is fn for fn in bound_callables):
            # Identity, never equality: a callable that merely compares equal to one of
            # ours is not one of ours, and ``==`` is something an attacker's object gets
            # to define.
            reason = REASON_TOOL_NOT_BOUND
        elif not principal.can(capability.value):
            reason = REASON_CAPABILITY_MISSING
        elif not turn.consume_tool_budget():
            reason = REASON_TOOL_BUDGET_EXHAUSTED
        else:
            return None

        denial = Denial(
            agent=agent_name,
            tool=name,
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
            "tool": name,
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
        name = tool.name  # read once: see make_capability_gate
        unavailable = tool.description == "Tool not found"
        reason = REASON_TOOL_UNAVAILABLE if unavailable else REASON_TOOL_FAILED
        detail = f"{type(error).__name__}: {error}"[:200]
        turn.record_failure(agent_name, name, reason, detail)
        turn.record_call(agent_name, name, args, ok=False, reason_key=reason)
        return {
            "denied": True,
            "ok": False,
            "reason_key": reason,
            "tool": name,
            "instruction": "The action is unavailable. Nothing changed. Tell the buyer.",
        }

    return on_error

"""Adversarial attack on the agent layer's authority boundary.

The platform's central claim is that agents propose and deterministic systems authorize:
a specialist may search, quote and assemble a basket, and can never approve, pay, refund
or revoke, because those capabilities are absent from its principal *by construction*
rather than filtered out afterwards. This module attacks that claim from every angle a
prompt-injected model or a compromised tool object could take, and asserts the defences
that hold as loudly as it marks the three that do not.

Six families of attack live here, in order:

1. **Reaching a forbidden capability by any route** -- a hand-built tool object, a tool
   the roster granted a different specialist, a merchant capability asked for from a buyer
   principal and the reverse, an unregistered name, and a tool whose ``name`` lies.
2. **The ADK silent-pass invariant.** On ADK a falsy ``before_tool_callback`` result means
   "run the tool after all", so a denial that is ``{}`` is not a denial. Every denial
   reason and both error-gate reasons are asserted non-empty *and* truthy *and*
   ``ok is False``. A regression here would be catastrophic and completely silent.
3. **Privilege escalation through the broker.** ``derive_principal`` must be an
   intersection that can never exceed its parent, for every role in
   :class:`~agent_runtime.capabilities.registry.AgentRole` -- parametrized over the enum so
   that a role added later cannot silently acquire a money verb.
4. **The per-turn tool budget**, which is spent in the gate before the tool runs and
   cannot be topped up from the only mapping a tool call can write to.
5. **The harness boundary, proven statically.** The two harnesses are pure Python: their
   source imports no model SDK (walked with ``ast``, and re-proven by importing the
   package in a fresh interpreter), and approve/pay/refund/revoke are absent from the
   backend protocols. Adding one at runtime buys nothing, because the factory is keyed to
   the registry rather than to whatever the backend happens to expose.
6. **Language independence.** Enforcement is structure, not prose: a Hindi or Hinglish
   session is refused by the same reason keys as an English one.

Everything is deterministic, offline and model-free. The stubs satisfy ``ToolLike`` and
``ToolContextLike`` structurally, so nothing here imports ADK either.

Three tests are ``xfail(strict=True)``. All three describe behaviour the gate claims in
its own docstring but does not have, and all three share one root cause: the gate
identifies a tool by the string it reports rather than by what it is, and re-reads that
string four times. See ``ShiftingNameTool`` and the three tests marked below it.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from agent_runtime import capabilities as capabilities_pkg
from agent_runtime import harness as harness_pkg
from agent_runtime.backends import InMemoryBackend, InMemoryTrustedSurface
from agent_runtime.backends.base import (
    AGENT_OPERATIONS,
    NEVER_ON_AGENT_SURFACE,
    CommerceBackend,
)
from agent_runtime.capabilities import (
    AGENT_ALLOWLIST,
    ALL_CAPABILITIES,
    KERNEL_INTERNAL_OPERATIONS,
    REASON_CAPABILITY_MISSING,
    REASON_TOOL_BUDGET_EXHAUSTED,
    REASON_TOOL_FAILED,
    REASON_TOOL_NOT_BOUND,
    REASON_TOOL_NOT_REGISTERED,
    REASON_TOOL_UNAVAILABLE,
    REGISTRY_A,
    TRUSTED_OPERATOR_ACTIONS,
    TRUSTED_SURFACE_ACTIONS,
    AgentRole,
    BoundToolset,
    Capability,
    ToolFunc,
    build_toolset,
    capability_for,
    derive_principal,
    make_capability_gate,
    make_tool_error_gate,
    tools_for_role,
)
from agent_runtime.language import Language
from agent_runtime.turn import TurnContext
from merchant_sim import MerchantStore
from transaction_kernel import ActorType, AgentPrincipal

TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
MERCHANT = uuid.UUID("00000000-0000-4000-8000-0000000000ff")

#: Registries B, C and D together: everything an agent principal must never hold.
NON_AGENT_ACTIONS: Final[frozenset[str]] = (
    TRUSTED_SURFACE_ACTIONS | KERNEL_INTERNAL_OPERATIONS | TRUSTED_OPERATOR_ACTIONS
)

#: The four verbs the whole design exists to keep away from a model, as a model would most
#: plausibly name them if it tried to call one directly.
MONEY_VERBS: Final[tuple[str, ...]] = ("approve", "pay", "refund", "revoke")

#: The same four verbs paired with the stem the other registries actually spell them with,
#: because Registry B says ``approval.record`` rather than ``approve.record``. Matching on
#: the stem is what makes "no role holds an executing action" a claim about real rows.
MONEY_ACTION_STEMS: Final[tuple[tuple[str, str], ...]] = (
    ("approve", "approv"),
    ("pay", "pay"),
    ("refund", "refund"),
    ("revoke", "revoke"),
)

BUYER_ROLES: Final[tuple[AgentRole, ...]] = (
    AgentRole.SHOPPING,
    AgentRole.CHECKOUT,
    AgentRole.SUPPORT,
)


def _capabilities_of(roles: tuple[AgentRole, ...]) -> frozenset[str]:
    return frozenset(capability.value for role in roles for capability in AGENT_ALLOWLIST[role])


BUYER_CAPABILITIES: Final[frozenset[str]] = _capabilities_of(BUYER_ROLES)

#: Roots of every model SDK this project could plausibly grow a dependency on. The harness
#: layer must import none of them. ``httpx`` is deliberately absent from this list: the
#: harness reaches it through ``backends/http.py``, which is a client for *this platform's*
#: own API (ADR 0003 D15), not a model client.
MODEL_SDK_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "google",
        "vertexai",
        "openai",
        "anthropic",
        "litellm",
        "cohere",
        "mistralai",
        "ollama",
        "transformers",
        "llama_cpp",
        "boto3",
    }
)


# ------------------------------------------------------------------ test doubles


@dataclass(slots=True)
class StubToolContext:
    """``ToolContextLike``: the mutable session state and the call being served."""

    state: dict[str, Any] = field(default_factory=dict)
    function_call_id: str | None = "call-1"


@dataclass(frozen=True, slots=True)
class StubTool:
    """``ToolLike``: a tool object the gate can inspect. Not built by the factory."""

    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class FactoryTool:
    """``ToolLike`` as ADK really presents it: the registry name *and* the factory closure.

    ``StubTool`` is deliberately not what the factory built, which is precisely what the
    binding check now refuses -- a name is a claim, an identity is not. A test about some
    later gate (the budget, say) needs a tool that legitimately passes the binding check,
    so it carries the closure ``build_toolset`` produced, exactly as ADK's ``FunctionTool``
    holds the function it was constructed from.
    """

    name: str
    func: Callable[..., Any]
    description: str = ""


def _bound_tool(toolset: BoundToolset, name: str) -> FactoryTool:
    """The tool object ADK would hand the gate for one of this toolset's own tools."""
    return FactoryTool(name=name, func=toolset.get(name).func)


class ShiftingNameTool:
    """A tool whose ``name`` property answers differently on each read.

    ``ToolLike`` declares ``name`` as a property, so a tool object is free to compute it.
    The gate reads ``tool.name`` four separate times -- once to resolve the capability,
    once to test membership of ``bound_tools``, once for the denial record and once for the
    dict handed back to the model -- and nothing forces those reads to agree. A tool object
    that answers differently each time can therefore have its capability resolved from one
    name and its binding checked against another. Attack, not a utility.
    """

    def __init__(self, *names: str) -> None:
        self._names = names
        self.reads: list[str] = []

    @property
    def name(self) -> str:
        answer = self._names[min(len(self.reads), len(self._names) - 1)]
        self.reads.append(answer)
        return answer

    @property
    def description(self) -> str:
        return ""


class BackendWithApprove(InMemoryBackend):
    """A backend that grew an ``approve`` method. Registry B smuggled onto Registry A."""

    async def approve(self, checkout_id: str, version: int) -> str:
        """Exactly the operation ``CommerceBackend`` refuses to declare."""
        return f"{checkout_id}:{version}:approved"


# ------------------------------------------------------------------ builders


def _principal(
    capabilities: frozenset[str],
    *,
    buyer: bool = True,
    principal_id: str = "agent:buyer-copilot",
) -> AgentPrincipal:
    """A harness principal, as the server would mint one at session start."""
    return AgentPrincipal(
        principal_id=principal_id,
        tenant_id=TENANT,
        actor_type=ActorType.AGENT,
        agent_role=None,
        merchant_id=None if buyer else MERCHANT,
        buyer_ref="buyer-1" if buyer else None,
        capabilities=capabilities,
    )


def _toolset(
    role: AgentRole,
    backend: CommerceBackend,
    *,
    parent: AgentPrincipal | None = None,
    budget: int = 8,
    language: Language = Language.EN,
) -> tuple[BoundToolset, TurnContext]:
    """A specialist toolset exactly as the harness would build one, with no model."""
    specialist = derive_principal(parent or _principal(ALL_CAPABILITIES), role)
    turn = TurnContext(language=language, principal=specialist, max_tool_calls=budget)
    toolset = build_toolset(role, backend, turn, principal=specialist, session_id="session-1")
    return toolset, turn


def _forced_denial(reason: str) -> tuple[dict[str, Any], TurnContext]:
    """Drive the gate to exactly one denial reason, by construction rather than by luck.

    ``capability_missing`` is unreachable through ``build_toolset`` -- the factory refuses
    to build a tool whose capability the principal lacks, so an unheld tool is always
    ``tool_not_bound`` first -- so the gate is built here directly with the precise
    ``bound_tools`` set each reason needs. That is what makes the family exhaustive.
    """
    held = ALL_CAPABILITIES
    bound = frozenset({"search"})
    tool_name = "search"
    budget = 8
    if reason == REASON_TOOL_NOT_REGISTERED:
        tool_name = "approve"  # no Registry A row exists, and none ever will
    elif reason == REASON_TOOL_NOT_BOUND:
        bound = frozenset()  # registered, capability held, factory never built it
    elif reason == REASON_CAPABILITY_MISSING:
        held = ALL_CAPABILITIES - {Capability.CATALOG_SEARCH.value}
    elif reason == REASON_TOOL_BUDGET_EXHAUSTED:
        budget = 0
    else:  # pragma: no cover - a new reason key must be added to this table
        raise AssertionError(f"unhandled denial reason {reason!r}")

    principal = derive_principal(_principal(held), AgentRole.SHOPPING)
    turn = TurnContext(language=Language.EN, principal=principal, max_tool_calls=budget)
    gate = make_capability_gate(principal, turn, agent_name="shopping", bound_tools=bound)
    result = gate(StubTool(tool_name), {"query": "milk"}, StubToolContext())
    assert result is not None, f"the gate allowed a call it must deny as {reason}"
    return result, turn


# ==================================================================== 1. reaching a
# ==================================================================== forbidden capability


@pytest.mark.parametrize("role", list(AgentRole))
def test_no_role_can_reach_a_registry_b_c_or_d_capability(role: AgentRole) -> None:
    """The intersection is with Registry A only, whatever the parent was handed.

    The parent here holds *everything*: Registry A plus every trusted-surface,
    kernel-internal and operator action. If the broker were a union, a copy or a fallback
    to the allowlist, this is where it would show. Parametrized over the enum so a role
    added tomorrow is covered without anyone remembering to add a test.
    """
    omnipotent = _principal(ALL_CAPABILITIES | NON_AGENT_ACTIONS)
    derived = derive_principal(omnipotent, role)
    assert not derived.capabilities & NON_AGENT_ACTIONS
    assert derived.capabilities <= ALL_CAPABILITIES
    assert derived.capabilities == frozenset(
        capability.value for capability in AGENT_ALLOWLIST[role]
    )


@pytest.mark.parametrize("role", list(AgentRole))
@pytest.mark.parametrize(
    ("verb", "stem"), MONEY_ACTION_STEMS, ids=[verb for verb, _ in MONEY_ACTION_STEMS]
)
def test_no_role_holds_a_money_verb_however_rich_the_parent(
    role: AgentRole, verb: str, stem: str
) -> None:
    """approve / pay / refund / revoke are absent from every derived principal.

    The executing actions are matched by their real spelling in Registries B, C and D and
    subtracted from Registry A first, because ``refund.propose`` and
    ``order.propose_cancel`` legitimately carry those words: proposing something is
    conversation, and executing it is somebody else's registry.
    """
    del verb  # named only so the parametrize id reads as the verb under attack
    omnipotent = _principal(ALL_CAPABILITIES | NON_AGENT_ACTIONS)
    derived = derive_principal(omnipotent, role)
    executing = {action for action in NON_AGENT_ACTIONS if stem in action}
    assert executing, f"no registry action is spelled with {stem!r}; the fixture has drifted"
    assert not executing & ALL_CAPABILITIES, "an executing action leaked into Registry A"
    assert not derived.capabilities & executing


def test_the_two_proposal_capabilities_have_no_tool_at_all() -> None:
    """A checkout principal may *propose* a cancel or a refund and can call nothing.

    This is the sharpest form of "absent by construction": the capability exists so the
    principal records what it may say, and the tool table has no row that would let it act.
    """
    proposals = {Capability.REFUND_PROPOSE, Capability.ORDER_PROPOSE_CANCEL}
    assert proposals <= AGENT_ALLOWLIST[AgentRole.CHECKOUT]
    assert not [name for name, capability in REGISTRY_A.items() if capability in proposals]
    checkout = derive_principal(_principal(ALL_CAPABILITIES), AgentRole.CHECKOUT)
    assert all(checkout.can(capability.value) for capability in proposals)
    assert not proposals & {REGISTRY_A[name] for name in tools_for_role(AgentRole.CHECKOUT)}


@pytest.mark.parametrize("verb", MONEY_VERBS)
def test_a_money_verb_is_not_a_registered_tool_name(verb: str) -> None:
    """``capability_for`` has nothing to return, so the gate stops at the first branch."""
    assert capability_for(verb) is None
    assert capability_for(f"{verb}_checkout") is None
    assert verb not in REGISTRY_A


@pytest.mark.parametrize("verb", MONEY_VERBS)
def test_an_unregistered_money_verb_is_denied_before_it_runs(verb: str) -> None:
    """Row 1 of the gate. An unregistered name never reaches a capability check."""
    toolset, turn = _toolset(AgentRole.CHECKOUT, InMemoryBackend(MerchantStore()))
    result = toolset.gate(StubTool(verb), {"checkout_id": "c1"}, StubToolContext())
    assert result and result["reason_key"] == REASON_TOOL_NOT_REGISTERED
    assert result["capability"] is None
    assert turn.denials[-1].reason_key == REASON_TOOL_NOT_REGISTERED
    assert turn.tool_calls[-1].denied is True


@pytest.mark.parametrize(
    ("role", "foreign_tool"),
    [
        (AgentRole.SHOPPING, "checkout_submit_approved"),
        (AgentRole.SHOPPING, "checkout_create"),
        (AgentRole.CHECKOUT, "search"),
        (AgentRole.CHECKOUT, "basket_set_line"),
        (AgentRole.SUPPORT, "checkout_submit_approved"),
        (AgentRole.SUPPORT, "search"),
    ],
)
def test_a_tool_the_roster_gave_another_specialist_is_refused(
    role: AgentRole, foreign_tool: str
) -> None:
    """Cross-role reach. A registered name is not a licence; the binding is.

    Every pair here is a real roster row for *some* specialist, so the name resolves to a
    capability and the denial has to come from the binding rather than from the registry
    lookup. ``tool_not_bound`` is the deterministic answer because the gate tests binding
    before it tests the capability.
    """
    assert foreign_tool in REGISTRY_A
    toolset, turn = _toolset(role, InMemoryBackend(MerchantStore()))
    assert foreign_tool not in toolset.names
    result = toolset.gate(StubTool(foreign_tool), {}, StubToolContext())
    assert result and result["reason_key"] == REASON_TOOL_NOT_BOUND
    assert result["capability"] == REGISTRY_A[foreign_tool].value
    assert turn.denials[-1].principal_id.endswith(f"/{role.value}")


@pytest.mark.parametrize(
    "lookalike",
    [
        "Search",  # case
        "SEARCH",
        " search",  # leading whitespace
        "search ",  # trailing whitespace
        "sear​ch",  # zero-width space
        "ｓearch",  # fullwidth Latin s
        "seаrch",  # Cyrillic a
        "checkout_submit_approved ",
        "Checkout_Submit_Approved",
    ],
)
def test_a_lookalike_name_resolves_to_nothing_on_both_checks(lookalike: str) -> None:
    """The registry lookup and the binding test must never disagree about a name.

    Neither ``capability_for`` nor the ``bound_tools`` membership test normalises case,
    whitespace or Unicode, so a look-alike misses *both* and lands on
    ``tool_not_registered``. The failure mode this rules out is one side normalising and
    the other not, which would let a name resolve to a capability while passing a
    membership test meant for a different tool entirely.
    """
    toolset, _ = _toolset(AgentRole.SHOPPING, InMemoryBackend(MerchantStore()))
    assert capability_for(lookalike) is None
    assert lookalike not in toolset.names
    result = toolset.gate(StubTool(lookalike), {}, StubToolContext())
    assert result and result["reason_key"] == REASON_TOOL_NOT_REGISTERED


def test_a_lying_name_cannot_conjure_a_capability_the_principal_lacks() -> None:
    """The capability branch is read-once, so no sequence of names can bypass it.

    ``capability = capability_for(tool.name)`` and ``principal.can(capability.value)`` use
    the same resolved object, so whatever the tool answers later, the capability the gate
    judged is the first name's. This defence holds and is worth pinning: it is the layer
    that would still stop a lying tool from reaching an operation the principal never had.
    """
    toolset, _ = _toolset(AgentRole.SHOPPING, InMemoryBackend(MerchantStore()))
    for tail in ("search", "product", "basket_get", "present_basket"):
        liar = ShiftingNameTool("checkout_submit_approved", tail, tail, tail)
        result = toolset.gate(liar, {}, StubToolContext())
        assert result, "a shopping principal must never reach the kernel submit"
        assert result["capability"] == Capability.CHECKOUT_SUBMIT_APPROVED.value
        assert result["reason_key"] in {REASON_TOOL_NOT_BOUND, REASON_CAPABILITY_MISSING}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: the gate identifies a tool by name only. A hand-built tool object bearing "
        "a name the factory did build passes every check, so 'the factory is the only "
        "source of tools' is a convention rather than the runtime fact broker.py claims."
    ),
)
def test_a_hand_built_tool_bearing_a_bound_name_is_still_refused() -> None:
    """The factory must be the only source of tools, including for names it did build.

    ``broker.make_capability_gate`` documents ``bound_tools`` as the check that makes the
    factory's monopoly a runtime fact. It only holds for names the factory did *not* build:
    a ``FunctionTool`` attached beside the toolset and simply named ``search`` is
    indistinguishable from the real one to a gate that compares strings. The fix is an
    identity check against the closures the factory produced.
    """
    toolset, _ = _toolset(AgentRole.SHOPPING, InMemoryBackend(MerchantStore()))
    assert "search" in toolset.names
    impostor = StubTool("search", description="I am not the factory's closure")
    result = toolset.gate(impostor, {"query": "milk"}, StubToolContext())
    assert result is not None, "a tool object the factory never built must be refused"
    assert result["reason_key"] == REASON_TOOL_NOT_BOUND


def test_a_tool_whose_name_shifts_between_checks_is_refused() -> None:
    """``support_escalate`` is held by the principal but was never built. It must not run.

    Support holds ``support.escalate`` (so the capability check passes) while the factory
    builds no closure for it (so ``bound_tools`` excludes it): calling it under its own
    name is correctly ``tool_not_bound``. A tool that answers ``support_escalate`` to the
    capability lookup and ``order_track`` to the membership test satisfies both and the
    gate returns ``None``, which on ADK means "run it". The fix is to read ``tool.name``
    once into a local and use that value for all four reads.
    """
    toolset, _ = _toolset(AgentRole.SUPPORT, InMemoryBackend(MerchantStore()))
    assert "support_escalate" in toolset.unbuilt
    honest = toolset.gate(StubTool("support_escalate"), {}, StubToolContext())
    assert honest and honest["reason_key"] == REASON_TOOL_NOT_BOUND

    liar = ShiftingNameTool("support_escalate", "order_track", "order_track", "order_track")
    result = toolset.gate(liar, {"reason": "angry buyer"}, StubToolContext())
    assert result is not None, "a name that shifts between checks must not be admitted"


def test_the_denial_record_names_the_tool_the_gate_actually_judged() -> None:
    """Evidence integrity. A denial that names the wrong tool is worse than no denial.

    ``TurnContext.denials`` is the attributable record the API returns and the audit trail
    keeps. Here the gate resolves (and refuses) ``not_a_registered_tool`` while the record
    it writes says ``second_name`` and the dict the model receives says ``third_name``.
    """
    principal = derive_principal(_principal(ALL_CAPABILITIES), AgentRole.SHOPPING)
    turn = TurnContext(language=Language.EN, principal=principal)
    gate = make_capability_gate(
        principal, turn, agent_name="shopping", bound_tools=frozenset({"search"})
    )
    liar = ShiftingNameTool("not_a_registered_tool", "second_name", "third_name")
    result = gate(liar, {}, StubToolContext())
    assert result is not None
    assert turn.denials[-1].tool == "not_a_registered_tool"
    assert result["tool"] == "not_a_registered_tool"


# ==================================================================== 2. the ADK
# ==================================================================== silent-pass invariant

GATE_REASONS: Final[tuple[str, ...]] = (
    REASON_TOOL_NOT_REGISTERED,
    REASON_TOOL_NOT_BOUND,
    REASON_CAPABILITY_MISSING,
    REASON_TOOL_BUDGET_EXHAUSTED,
)
ERROR_REASONS: Final[tuple[str, ...]] = (REASON_TOOL_UNAVAILABLE, REASON_TOOL_FAILED)


@pytest.mark.parametrize("reason", GATE_REASONS)
def test_every_gate_denial_is_a_nonempty_truthy_dict(reason: str) -> None:
    """The ADK invariant, one test per reason. ``{}`` is falsy and means "run the tool".

    ADK breaks out of its ``before_tool_callback`` loop only on a *truthy* result; a falsy
    one is treated as ``None`` and the tool runs. So a denial must be asserted non-empty
    AND truthy AND ``ok is False``, on every path, including the paths a future edit adds.
    """
    result, turn = _forced_denial(reason)
    assert isinstance(result, dict)
    assert result != {}
    assert len(result) > 0
    assert bool(result) is True, "a falsy denial is not a denial: ADK would run the tool"
    assert result["ok"] is False
    assert result["denied"] is True
    assert result["reason_key"] == reason
    assert result["instruction"]
    assert result["principal_id"] == "agent:buyer-copilot/shopping"
    # The refusal is on the record before the tool would have run, not after.
    assert turn.denials[-1].reason_key == reason
    assert turn.tool_calls[-1].denied is True
    assert turn.tool_calls[-1].ok is False


@pytest.mark.parametrize("reason", GATE_REASONS)
def test_a_gate_denial_is_json_safe_so_the_runtime_can_return_it(reason: str) -> None:
    """A denial that cannot be serialised would surface as a crash, not a refusal."""
    result, _ = _forced_denial(reason)
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("reason", ERROR_REASONS)
def test_every_error_gate_result_is_a_nonempty_truthy_dict(reason: str) -> None:
    """The same invariant on the error path.

    ``on_tool_error`` exists so a model naming a tool it was never given produces an
    explanation instead of a crashed turn. A falsy result there re-raises into the runtime.
    ``description == "Tool not found"`` is the runtime's own marker for the unavailable
    case, so it is what distinguishes the two reasons.
    """
    principal = derive_principal(_principal(ALL_CAPABILITIES), AgentRole.SHOPPING)
    turn = TurnContext(language=Language.EN, principal=principal)
    error_gate = make_tool_error_gate(turn, agent_name="shopping")
    description = "Tool not found" if reason == REASON_TOOL_UNAVAILABLE else "Search products"
    result = error_gate(
        StubTool("search", description),
        {"query": "milk"},
        StubToolContext(),
        RuntimeError("provider exploded"),
    )
    assert result is not None
    assert result != {}
    assert bool(result) is True, "a falsy error result would re-raise into the runtime"
    assert result["ok"] is False
    assert result["denied"] is True
    assert result["reason_key"] == reason
    assert result["instruction"]
    assert json.loads(json.dumps(result)) == result
    assert turn.tool_failures[-1]["reason_key"] == reason
    assert turn.tool_calls[-1].ok is False


def test_the_error_gate_never_leaks_a_stack_trace_into_the_result() -> None:
    """The model gets a reason key; the detail stays on the turn record, truncated."""
    principal = derive_principal(_principal(ALL_CAPABILITIES), AgentRole.SHOPPING)
    turn = TurnContext(language=Language.EN, principal=principal)
    error_gate = make_tool_error_gate(turn, agent_name="shopping")
    secret = "SECRET" * 200
    result = error_gate(StubTool("search"), {}, StubToolContext(), RuntimeError(secret))
    assert result is not None
    assert secret not in json.dumps(result)
    assert len(turn.tool_failures[-1]["detail"]) <= 200


@pytest.mark.parametrize("reason", (*GATE_REASONS, *ERROR_REASONS))
def test_every_reason_key_is_a_stable_machine_key(reason: str) -> None:
    """Reason keys are matched by machines and rendered by the surface, never translated."""
    assert reason == reason.lower()
    assert reason.isascii()
    assert " " not in reason
    assert set(reason) <= set("abcdefghijklmnopqrstuvwxyz_")


def test_the_six_reason_keys_are_distinct() -> None:
    """A collision would make two different refusals indistinguishable in the audit trail."""
    assert len({*GATE_REASONS, *ERROR_REASONS}) == 6


# ==================================================================== 3. escalation
# ==================================================================== through the broker


@pytest.mark.parametrize("role", list(AgentRole))
def test_the_derived_principal_is_an_intersection_not_the_allowlist(role: AgentRole) -> None:
    """Withhold one capability the role's allowlist grants; the specialist must not get it.

    This is the difference between an intersection and a lookup. A broker that returned
    ``AGENT_ALLOWLIST[role]`` would pass every other test in this file and fail this one.
    """
    granted = sorted(capability.value for capability in AGENT_ALLOWLIST[role])
    assert granted, f"{role.value} has an empty allowlist; the fixture has drifted"
    withheld = granted[0]
    thin = _principal(ALL_CAPABILITIES - {withheld})
    derived = derive_principal(thin, role)
    assert not derived.can(withheld)
    assert derived.capabilities == frozenset(granted) - {withheld}
    assert derived.capabilities <= thin.capabilities


@pytest.mark.parametrize("role", list(AgentRole))
def test_a_parent_with_nothing_derives_a_specialist_with_nothing(role: AgentRole) -> None:
    """No floor. An empty harness principal cannot be topped up from the allowlist."""
    derived = derive_principal(_principal(frozenset()), role)
    assert derived.capabilities == frozenset()
    toolset, _ = _toolset(role, InMemoryBackend(MerchantStore()), parent=_principal(frozenset()))
    assert toolset.names == ()


@pytest.mark.parametrize("role", list(AgentRole))
def test_subset_for_refuses_to_widen_directly(role: AgentRole) -> None:
    """The escalation attempt one layer below the broker: ask ``subset_for`` for more.

    ``derive_principal`` intersects and then calls ``subset_for``, which itself refuses a
    superset. Attacking ``subset_for`` directly proves the refusal is not merely the
    broker's arithmetic being polite.
    """
    thin = _principal(frozenset({Capability.CATALOG_SEARCH.value}))
    for wanted in sorted(NON_AGENT_ACTIONS)[:3]:
        with pytest.raises(ValueError, match="capabilities its parent lacks"):
            thin.subset_for(role.value, frozenset({wanted}))
    with pytest.raises(ValueError, match="capabilities its parent lacks"):
        thin.subset_for(role.value, ALL_CAPABILITIES)


@pytest.mark.parametrize("role", list(AgentRole))
def test_a_specialist_cannot_re_derive_itself_wider(role: AgentRole) -> None:
    """A second hop down the delegation chain narrows again; it never climbs back.

    A prompt-injected specialist that could re-derive from *itself* with the full allowlist
    would escape the harness's intersection. It cannot: its own capabilities are the cap.
    """
    thin = _principal(frozenset({Capability.CATALOG_SEARCH.value}))
    first = derive_principal(thin, role)
    second = derive_principal(first, role)
    assert second.capabilities <= first.capabilities
    assert second.capabilities <= thin.capabilities


@pytest.mark.parametrize("role", list(AgentRole))
def test_the_derived_principal_carries_the_parents_tenant_and_delegation(
    role: AgentRole,
) -> None:
    """Identity is inherited, not chosen: a specialist cannot re-tenant itself."""
    parent = _principal(ALL_CAPABILITIES)
    derived = derive_principal(parent, role)
    assert derived.tenant_id == parent.tenant_id
    assert derived.buyer_ref == parent.buyer_ref
    assert derived.merchant_id == parent.merchant_id
    assert derived.agent_role == role.value
    assert derived.principal_id == f"{parent.principal_id}/{role.value}"
    assert derived.delegation_chain == (parent.principal_id,)


@pytest.mark.parametrize("role", list(AgentRole))
def test_a_derived_principal_is_immutable(role: AgentRole) -> None:
    """No in-place widening either: the dataclass is frozen and the set is a frozenset."""
    derived = derive_principal(_principal(ALL_CAPABILITIES), role)
    with pytest.raises((AttributeError, TypeError)):
        derived.capabilities = ALL_CAPABILITIES | NON_AGENT_ACTIONS  # type: ignore[misc]
    assert isinstance(derived.capabilities, frozenset)
    assert not hasattr(derived.capabilities, "add")


def test_a_toolset_built_for_one_role_cannot_be_offered_under_another() -> None:
    """The factory refuses a principal whose role disagrees, so authority cannot be relabelled."""
    checkout = derive_principal(_principal(ALL_CAPABILITIES), AgentRole.CHECKOUT)
    turn = TurnContext(language=Language.EN, principal=checkout)
    with pytest.raises(ValueError, match="does not match toolset role"):
        build_toolset(
            AgentRole.SHOPPING,
            InMemoryBackend(MerchantStore()),
            turn,
            principal=checkout,
            session_id="session-1",
        )


# ==================================================================== 4. the tool budget


def test_the_budget_is_spent_in_the_gate_and_the_next_call_is_denied_nonemptily() -> None:
    """Row 5. Counting before the tool is what stops a looping model, not counting after."""
    toolset, turn = _toolset(AgentRole.SHOPPING, InMemoryBackend(MerchantStore()), budget=3)
    ctx = StubToolContext()
    for index in range(3):
        assert toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, ctx) is None, index
    assert turn.tool_calls_admitted == 3
    denial = toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, ctx)
    assert denial, "the budget denial must be non-empty or ADK runs the tool anyway"
    assert denial["reason_key"] == REASON_TOOL_BUDGET_EXHAUSTED
    assert denial["ok"] is False
    # Still denied on every subsequent attempt; the counter does not wrap.
    for _ in range(5):
        again = toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, ctx)
        assert again and again["reason_key"] == REASON_TOOL_BUDGET_EXHAUSTED
    assert turn.tool_calls_admitted == 3


def test_a_refused_call_does_not_spend_the_budget() -> None:
    """Budget is a bound on tools *run*, not on tools *asked for*. Documented, not accidental.

    The gate spends a unit only after the registry, binding and capability checks pass, so
    a model that spams a forbidden name burns no budget. That is correct -- nothing ran --
    but it means the per-turn tool budget is not a bound on gate invocations, and the
    denial ledger grows once per attempt.
    """
    toolset, turn = _toolset(AgentRole.SHOPPING, InMemoryBackend(MerchantStore()), budget=2)
    for _ in range(20):
        result = toolset.gate(StubTool("checkout_submit_approved"), {}, StubToolContext())
        assert result and result["ok"] is False
    assert turn.tool_calls_admitted == 0
    assert len(turn.denials) == 20
    # The two real calls the turn was budgeted for are still available.
    assert (
        toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, StubToolContext()) is None
    )
    assert (
        toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, StubToolContext()) is None
    )
    assert toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, StubToolContext())


@pytest.mark.asyncio
async def test_the_budget_cannot_be_reset_from_inside_a_tool_call() -> None:
    """Session state is the only mapping a tool writes, and the counter does not live there.

    A tool closure captures the ``TurnContext``; the model captures nothing. The only thing
    a tool call can influence is ``tool_context.state``, so the attack is to plant keys
    there that a careless implementation might read the budget back from.
    """
    backend = InMemoryBackend(MerchantStore())
    toolset, turn = _toolset(AgentRole.SHOPPING, backend, budget=2)
    ctx = StubToolContext()

    search = toolset.get("search")
    assert toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, ctx) is None
    await search.func(query="milk", tool_context=ctx)
    assert turn.tool_calls_admitted == 1

    # Everything a hostile tool could plausibly plant to buy itself more calls.
    ctx.state.update(
        {
            "tool_calls_admitted": 0,
            "max_tool_calls": 9999,
            "budget": 9999,
            "turn": None,
            "principal": {"capabilities": sorted(ALL_CAPABILITIES | NON_AGENT_ACTIONS)},
        }
    )
    assert turn.tool_calls_admitted == 1
    assert turn.max_tool_calls == 2
    assert toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, ctx) is None
    denied = toolset.gate(_bound_tool(toolset, "search"), {"query": "milk"}, ctx)
    assert denied and denied["reason_key"] == REASON_TOOL_BUDGET_EXHAUSTED

    # And the turn record itself is not reachable through the state a tool writes.
    assert not any(isinstance(value, TurnContext) for value in ctx.state.values())


def test_the_budget_is_per_turn_and_a_spent_turn_stays_spent() -> None:
    """A new message gets a new counter; the exhausted turn does not come back to life."""
    backend = InMemoryBackend(MerchantStore())
    first_set, first_turn = _toolset(AgentRole.SHOPPING, backend, budget=1)
    ctx = StubToolContext()
    assert first_set.gate(_bound_tool(first_set, "search"), {"query": "milk"}, ctx) is None
    spent = first_set.gate(_bound_tool(first_set, "search"), {"query": "milk"}, ctx)
    assert spent and spent["reason_key"] == REASON_TOOL_BUDGET_EXHAUSTED

    # The next turn shares the session state (same ctx) but not the counter.
    second_set, second_turn = _toolset(AgentRole.SHOPPING, backend, budget=1)
    assert second_turn is not first_turn
    assert second_turn.tool_calls_admitted == 0
    assert second_set.gate(_bound_tool(second_set, "search"), {"query": "milk"}, ctx) is None

    still_spent = first_set.gate(_bound_tool(first_set, "search"), {"query": "milk"}, ctx)
    assert still_spent and still_spent["reason_key"] == REASON_TOOL_BUDGET_EXHAUSTED


# ==================================================================== 5. the harness
# ==================================================================== boundary, statically

_HARNESS_SOURCES: Final[tuple[Path, ...]] = tuple(
    sorted(Path(str(harness_pkg.__file__)).parent.glob("*.py"))
)
_CAPABILITY_SOURCES: Final[tuple[Path, ...]] = tuple(
    sorted(Path(str(capabilities_pkg.__file__)).parent.glob("*.py"))
)


def _imported_roots(source: str) -> set[str]:
    """Every top-level package this source imports, however it spells the import.

    An ``ast`` walk rather than a regex, because a regex over the text misses
    ``importlib.import_module("google.adk...")``, an import nested inside a function or a
    ``try`` block, and an aliased one. Relative imports are skipped: they are this package.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            target = node.func
            dynamic = (isinstance(target, ast.Attribute) and target.attr == "import_module") or (
                isinstance(target, ast.Name) and target.id == "__import__"
            )
            if dynamic:
                roots.update(
                    argument.value.split(".")[0]
                    for argument in node.args
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                )
    return roots


@pytest.mark.parametrize(
    "path", [*_HARNESS_SOURCES, *_CAPABILITY_SOURCES], ids=lambda path: path.name
)
def test_no_harness_or_gate_module_imports_a_model_sdk(path: Path) -> None:
    """The harnesses and the gate are pure Python: no model lives inside either one.

    This is the load-bearing claim of the whole design. If a harness could call a model,
    "agents propose, deterministic systems authorize" would be a description of intent
    rather than of the code.
    """
    assert _HARNESS_SOURCES, "no harness sources were found; the glob has drifted"
    roots = _imported_roots(path.read_text(encoding="utf-8"))
    assert not roots & MODEL_SDK_ROOTS, f"{path.name} imports {sorted(roots & MODEL_SDK_ROOTS)}"


def test_importing_the_harness_loads_no_model_sdk_in_a_fresh_interpreter() -> None:
    """The transitive proof the source walk cannot give: nothing *reachable* pulls a model in.

    A fresh interpreter, so a model SDK another test in this session imported cannot make
    this pass or fail. Offline: it imports local packages and prints ``sys.modules``.
    """
    program = (
        "import json, sys\n"
        "import agent_runtime.harness\n"
        "import agent_runtime.capabilities\n"
        "print(json.dumps(sorted({name.split('.')[0] for name in sys.modules})))\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, this interpreter
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120, check=False
    )
    assert completed.returncode == 0, completed.stderr
    roots = set(json.loads(completed.stdout))
    leaked = sorted(roots & MODEL_SDK_ROOTS)
    assert not leaked, f"importing the harness pulled in {leaked}"


def test_the_commerce_backend_declares_exactly_the_agent_operations() -> None:
    """Ten operations, and the money verbs are not among them. Absence, not filtering.

    ``basket_propose_line`` is surface without abstractness: its default refuses with a
    501 problem, so a backend with no buyer surface inherits a refusal instead of being
    forced to fake a proposal it cannot build.
    """
    assert set(CommerceBackend.__abstractmethods__) == set(AGENT_OPERATIONS) - {
        "basket_propose_line"
    }
    assert hasattr(CommerceBackend, "basket_propose_line")
    assert not set(AGENT_OPERATIONS) & NEVER_ON_AGENT_SURFACE


@pytest.mark.parametrize("verb", sorted(NEVER_ON_AGENT_SURFACE))
@pytest.mark.parametrize("surface", [CommerceBackend, InMemoryBackend], ids=lambda s: s.__name__)
def test_no_agent_reachable_backend_exposes_an_executing_verb(surface: type, verb: str) -> None:
    """There is no method here for a capability gate to forget to guard.

    Both protocols and the concrete in-memory backend, both as classes and as members, so
    a method added to an implementation but not to the interface is caught too.
    """
    assert not hasattr(surface, verb), f"{surface.__name__} exposes {verb!r}"
    assert verb not in dir(surface)
    assert verb not in vars(surface)


def test_registry_b_lives_on_a_separate_object_no_agent_can_reach() -> None:
    """``InMemoryTrustedSurface`` can approve. It is not a backend and no tool holds one."""
    backend = InMemoryBackend(MerchantStore())
    surface = InMemoryTrustedSurface(backend)
    assert hasattr(surface, "approve"), "the fixture must really be able to approve"
    assert not isinstance(surface, CommerceBackend)
    # The surface holds the backend; the backend must not hold the surface back.
    reachable = {
        getattr(backend, attribute) for attribute in dir(backend) if not attribute.startswith("_")
    }
    assert surface not in reachable
    assert not any(isinstance(value, InMemoryTrustedSurface) for value in reachable)


def _captured(func: ToolFunc) -> list[object]:
    """Everything one factory closure holds, plus the backend inside its FactoryContext."""
    values = [cell.cell_contents for cell in (func.__closure__ or ())]
    return [*values, *[getattr(value, "backend", None) for value in values]]


@pytest.mark.parametrize("role", list(AgentRole))
def test_no_factory_closure_captures_anything_that_can_approve_or_refund(
    role: AgentRole,
) -> None:
    """A tool closure captures identity and a backend. Neither can execute money.

    Walking ``__closure__`` is the structural version of the claim: whatever the model
    says, the objects a tool actually has in hand expose no Registry B or C operation.
    """
    backend = InMemoryBackend(MerchantStore())
    toolset, _ = _toolset(role, backend)
    for tool in toolset:
        for value in _captured(tool.func):
            if value is None:
                continue
            exposed = {verb for verb in NEVER_ON_AGENT_SURFACE if hasattr(value, verb)}
            assert not exposed, f"{role.value}/{tool.name} holds {type(value).__name__}: {exposed}"
            assert not isinstance(value, InMemoryTrustedSurface)


def test_an_approve_method_added_to_a_backend_buys_the_agent_nothing() -> None:
    """The factory reads the registry, never the backend. Adding a method is not a capability.

    Both routes are tried: a subclass that declares ``approve``, and an instance
    monkeypatched with one at runtime. In both cases no capability exists for the name, no
    tool is built, and the gate refuses a call to it as unregistered.
    """
    subclassed = BackendWithApprove(MerchantStore())
    assert callable(subclassed.approve), "the attack backend must really have the method"

    patched = InMemoryBackend(MerchantStore())
    patched.approve = subclassed.approve  # type: ignore[attr-defined]
    assert hasattr(patched, "approve")

    for backend in (subclassed, patched):
        for role in AgentRole:
            toolset, _ = _toolset(role, backend)
            assert "approve" not in toolset.names
            assert "approve" not in toolset.unbuilt
            assert not {tool.name for tool in toolset} & NEVER_ON_AGENT_SURFACE
            denial = toolset.gate(
                StubTool("approve"), {"checkout_id": "c1", "version": 1}, StubToolContext()
            )
            assert denial and denial["reason_key"] == REASON_TOOL_NOT_REGISTERED
            assert denial["capability"] is None
    assert capability_for("approve") is None


@pytest.mark.parametrize("verb", MONEY_VERBS)
def test_the_factory_refuses_to_build_a_tool_outside_registry_a(verb: str) -> None:
    """The last door: hand the factory a builder for ``approve`` and it refuses to hold it."""

    async def rogue(tool_context: Any) -> dict[str, Any]:  # pragma: no cover - never built
        del tool_context
        return {"ok": True}

    principal = derive_principal(_principal(ALL_CAPABILITIES), AgentRole.CHECKOUT)
    turn = TurnContext(language=Language.EN, principal=principal)
    with pytest.raises(ValueError, match="not a Registry A tool"):
        build_toolset(
            AgentRole.CHECKOUT,
            InMemoryBackend(MerchantStore()),
            turn,
            principal=principal,
            session_id="session-1",
            extra_builders={verb: lambda _ctx: rogue},
        )


@pytest.mark.parametrize("role", list(AgentRole))
def test_every_tool_a_specialist_holds_came_from_the_factory_by_identity(
    role: AgentRole,
) -> None:
    """The toolset's ``bound_tools`` set is exactly the names it built, and nothing else."""
    toolset, _ = _toolset(role, InMemoryBackend(MerchantStore()))
    assert set(toolset.names) <= set(tools_for_role(role))
    assert not set(toolset.names) & set(toolset.unbuilt)
    for name in toolset.names:
        # A name in the toolset resolves, is a roster row for this role, and its closure is
        # the object the factory returned rather than a look-alike registered beside it.
        assert capability_for(name) is REGISTRY_A[name]
        assert toolset.get(name) is next(tool for tool in toolset if tool.name == name)
        assert inspect.iscoroutinefunction(toolset.get(name).func)


# ==================================================================== 6. multilingual


@pytest.mark.parametrize("language", list(Language))
@pytest.mark.parametrize("reason", GATE_REASONS)
def test_enforcement_does_not_vary_with_the_buyer_language(language: Language, reason: str) -> None:
    """A Hindi or Hinglish session is refused by exactly the same structure as an English one.

    The turn carries the language; the gate never reads it. Asserting that here means a
    later change that rendered denials per language would have to keep ``reason_key`` a
    machine key, or this fails.
    """
    held = ALL_CAPABILITIES
    bound = frozenset({"search"})
    tool_name = "search"
    budget = 8
    if reason == REASON_TOOL_NOT_REGISTERED:
        tool_name = "refund"
    elif reason == REASON_TOOL_NOT_BOUND:
        bound = frozenset()
    elif reason == REASON_CAPABILITY_MISSING:
        held = ALL_CAPABILITIES - {Capability.CATALOG_SEARCH.value}
    else:
        budget = 0

    principal = derive_principal(_principal(held), AgentRole.SHOPPING)
    turn = TurnContext(language=language, principal=principal, max_tool_calls=budget)
    gate = make_capability_gate(principal, turn, agent_name="shopping", bound_tools=bound)
    result = gate(StubTool(tool_name), {"query": "doodh"}, StubToolContext())

    english, _ = _forced_denial(reason)
    assert result is not None
    assert bool(result) is True
    assert result["reason_key"] == english["reason_key"] == reason
    assert result["ok"] is english["ok"] is False
    assert result["denied"] is english["denied"] is True
    assert result["capability"] == english["capability"]
    assert turn.language is language, "the turn knows the language; the gate did not use it"


def test_a_denial_carries_no_translated_prose_the_enforcement_depends_on() -> None:
    """Every machine-read field is ASCII structure; only ``instruction`` is prose.

    The surface renders the reason key into the buyer's language. Nothing downstream may
    have to parse the sentence, so the sentence is the only free-text field there is.
    """
    result, _ = _forced_denial(REASON_TOOL_NOT_BOUND)
    machine_fields = {key: value for key, value in result.items() if key != "instruction"}
    for key, value in machine_fields.items():
        assert isinstance(key, str) and key.isascii()
        if isinstance(value, str):
            assert value.isascii(), key
    assert set(machine_fields) == {
        "denied",
        "ok",
        "capability",
        "reason_key",
        "tool",
        "principal_id",
        "function_call_id",
    }


@pytest.mark.parametrize("language", list(Language))
def test_a_specialist_is_bound_identically_in_every_language(language: Language) -> None:
    """Language selects a script and a display locale, never an authority."""
    backend = InMemoryBackend(MerchantStore())
    toolset, turn = _toolset(AgentRole.CHECKOUT, backend, language=language)
    baseline, _ = _toolset(AgentRole.CHECKOUT, backend, language=Language.EN)
    assert toolset.names == baseline.names
    assert toolset.principal.capabilities == baseline.principal.capabilities
    assert not toolset.principal.capabilities & NON_AGENT_ACTIONS
    assert turn.language is language

"""The five specialists as data: the roster is exact, the reads are reads, no ADK leaks.

These tests need no model runtime and no network. They pin what ``docs/briefs/AGENT_ROSTER.md``
says each specialist may hold, that the three tables which must agree on it do (the spec,
the registry allowlist, the harness allowlist), that nothing on any roster is a Registry
B, C or D verb, that the Case Specialist cannot mutate anything, that every grounding rule
starts from a tool on its own roster, and -- by reading source -- that the
``specialists`` package never imports ``google.adk`` or ``google.genai``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agent_runtime import specialists
from agent_runtime.backends.base import NEVER_ON_AGENT_SURFACE
from agent_runtime.capabilities.registry import (
    AGENT_ALLOWLIST,
    KERNEL_INTERNAL_OPERATIONS,
    PRESENTATION_TOOLS,
    REGISTRY_A,
    TRUSTED_OPERATOR_ACTIONS,
    TRUSTED_SURFACE_ACTIONS,
    AgentRole,
    tools_for_role,
)
from agent_runtime.core.fencing import MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE
from agent_runtime.core.grounding_rules import (
    DEFAULT_LEXICON,
    GroundingRule,
    GroundingState,
    first_rule,
    rules_for,
)
from agent_runtime.harness.base import ROLE_CAPABILITIES
from agent_runtime.harness.routing import Specialist
from agent_runtime.specialists import (
    ACTIONS,
    CARD_TOOLS,
    SPECS,
    ActionKind,
    SpecialistSpec,
    Surface,
    action_for_tool,
    case,
    checkout,
    shopping,
    spec_for,
)

#: docs/briefs/AGENT_ROSTER.md. Checkout also carries ``checkout.read``: ADR 0004 §1.2
#: starts every checkout turn with a read of the checkout, and the registry allowlist, the
#: harness allowlist and the grounding rules all give Checkout that read.
ROSTER: dict[str, frozenset[str]] = {
    "shopping_specialist": frozenset(
        {
            "catalog.search",
            "catalog.get_product",
            "inventory.check",
            "basket.create",
            "basket.update",
            "basket.propose_line",
            "quote.request",
            "reservation.request",
        }
    ),
    "checkout_specialist": frozenset(
        {
            "quote.request",
            "reservation.request",
            "checkout.submit_for_approval",
            "checkout.submit_approved",
            "checkout.read",
            "order.track",
            "order.propose_cancel",
            "refund.propose",
        }
    ),
    "support_specialist": frozenset(
        {
            "order.track",
            "checkout.read",
            "policy.search",
            "resolution.evaluate",
            "support.escalate",
            "support.case.read",
        }
    ),
    "growth_specialist": frozenset(
        {
            "merchant.catalogue_health.read",
            "merchant.inventory_anomalies.read",
            "merchant.checkout_metrics.read",
            "merchant.growth_proposal.create",
        }
    ),
    "case_specialist": frozenset({"support.case.read"}),
}

FORBIDDEN_ACTIONS = TRUSTED_SURFACE_ACTIONS | KERNEL_INTERNAL_OPERATIONS | TRUSTED_OPERATOR_ACTIONS
SPECIALISTS_DIR = Path(specialists.__file__).parent


# --------------------------------------------------------------------------- roster


@pytest.mark.parametrize("name", sorted(ROSTER))
def test_specialist_lists_exactly_its_roster_tools(name: str) -> None:
    spec = spec_for(name)
    assert frozenset(spec.actions) == ROSTER[name], f"{name} roster drifted"
    assert len(spec.actions) == len(ROSTER[name]), "no duplicate actions"


@pytest.mark.parametrize("spec", SPECS, ids=[spec.name for spec in SPECS])
def test_spec_registry_and_harness_agree_on_the_roster(spec: SpecialistSpec) -> None:
    """Three tables grant the same thing, or authority is granted differently in two places."""
    role = AgentRole(spec.role)
    assert frozenset(c.value for c in AGENT_ALLOWLIST[role]) == spec.capabilities
    assert ROLE_CAPABILITIES[Specialist(spec.role)] == spec.capabilities
    # Every tool the factory offers this role is on the spec's tool list, so the adapter
    # never has to filter -- and refuses if it ever would.
    assert set(tools_for_role(role)) <= set(spec.tool_names), spec.name
    for tool_name in spec.action_tool_names:
        if tool_name in REGISTRY_A:
            assert REGISTRY_A[tool_name].value in spec.actions
            assert action_for_tool(tool_name) is not None
            assert action_for_tool(tool_name).name == REGISTRY_A[tool_name].value  # type: ignore[union-attr]


def test_case_specialist_holds_case_read_and_only_reads() -> None:
    spec = spec_for("case_specialist")
    assert spec.actions == ("support.case.read",)
    assert spec.is_read_only
    assert spec.mutating_actions == ()
    assert spec.tool_names == ("support_case_read", "present_case")
    assert spec.rules == ()


def test_five_specialists_two_surfaces() -> None:
    assert [spec.name for spec in SPECS] == [
        "shopping_specialist",
        "checkout_specialist",
        "support_specialist",
        "growth_specialist",
        "case_specialist",
    ]
    assert {spec.surface for spec in specialists.BUYER_SPECIALISTS} == {Surface.BUYER}
    assert {spec.surface for spec in specialists.MERCHANT_SPECIALISTS} == {Surface.MERCHANT}
    assert len(specialists.BUYER_SPECIALISTS) == 3
    assert len(specialists.MERCHANT_SPECIALISTS) == 2
    for spec in SPECS:
        assert Specialist(spec.role).value == AgentRole(spec.role).value == spec.role


def test_lookup_by_name_or_role() -> None:
    assert spec_for("shopping") is shopping.SPEC
    assert spec_for("checkout_specialist") is checkout.SPEC
    with pytest.raises(KeyError):
        spec_for("coordinator")


# --------------------------------------------------------------------------- registries


def test_no_action_is_a_registry_b_c_or_d_name() -> None:
    assert not (set(ACTIONS) & FORBIDDEN_ACTIONS)
    for spec in SPECS:
        assert not (set(spec.actions) & FORBIDDEN_ACTIONS), spec.name


def test_no_write_action_carries_a_money_verb() -> None:
    """A proposal may name the verb it proposes; a write may not be that verb."""
    for action in ACTIONS.values():
        segments = set(re.split(r"[._]", action.name)) | set((action.tool_name or "").split("_"))
        if action.kind is ActionKind.PROPOSE:
            assert "propose" in segments or "proposal" in segments, action.name
            continue
        assert not (segments & NEVER_ON_AGENT_SURFACE), f"{action.name} names a money verb"


def test_conversation_proposals_have_no_tool() -> None:
    """Proposing costs nothing and executes nothing: no tool, no backend method."""
    for name in ("order.propose_cancel", "refund.propose"):
        assert ACTIONS[name].kind is ActionKind.PROPOSE
        assert ACTIONS[name].tool_name is None
    assert "order_propose_cancel" not in REGISTRY_A and "refund_propose" not in REGISTRY_A


def test_there_is_no_apply_verb_for_growth() -> None:
    growth_spec = spec_for("growth")
    assert growth_spec.mutating_actions == ("merchant.growth_proposal.create",)
    assert ACTIONS["merchant.growth_proposal.create"].kind is ActionKind.PROPOSE
    assert not any("apply" in name for name in ACTIONS)


# --------------------------------------------------------------------------- gates


def test_every_write_runs_under_the_session_lock_and_provenance() -> None:
    for action in ACTIONS.values():
        if action.kind is ActionKind.WRITE:
            assert "write_lock" in action.gates, action.name
    basket_update = ACTIONS["basket.update"]
    assert {"sku_provenance", "quantity", "line_count"} <= set(basket_update.gates)
    assert "checkout_provenance" in ACTIONS["checkout.submit_approved"].gates
    for name in ("order.propose_cancel", "refund.propose", "resolution.evaluate"):
        assert "order_provenance" in ACTIONS[name].gates, name
    assert "proposal_guardrails" in ACTIONS["merchant.growth_proposal.create"].gates


def test_tool_names_are_unique_identifiers_and_round_trip() -> None:
    names = [action.tool_name for action in ACTIONS.values() if action.tool_name]
    assert len(set(names)) == len(names)
    for action in ACTIONS.values():
        if action.tool_name is None:
            continue
        assert action.tool_name.isidentifier()
        assert action_for_tool(action.tool_name) is action
    assert action_for_tool("approve") is None
    assert set(CARD_TOOLS.values()) == PRESENTATION_TOOLS


# --------------------------------------------------------------------------- spec invariants


def _spec(**overrides: object) -> SpecialistSpec:
    base: dict[str, object] = {
        "name": "probe_specialist",
        "role": "probe",
        "surface": Surface.BUYER,
        "description": "probe",
        "actions": ("catalog.search",),
        "rules": (),
        "cards": (),
        "fallback_instruction": "probe",
    }
    base.update(overrides)
    return SpecialistSpec(**base)  # type: ignore[arg-type]


def test_spec_refuses_an_action_outside_registry_a() -> None:
    with pytest.raises(ValueError, match="not in Registry A"):
        _spec(actions=("approval.record",))
    with pytest.raises(ValueError, match="not in Registry A"):
        _spec(actions=("payment.create_order",))


def test_spec_refuses_a_rule_that_starts_outside_its_roster() -> None:
    rule = GroundingRule("probe", "order_track", lambda _l, _t, _s: {})
    with pytest.raises(ValueError, match="not on this specialist's roster"):
        _spec(rules=(rule,))


def test_spec_refuses_duplicates_bad_names_and_unknown_cards() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _spec(actions=("catalog.search", "catalog.search"))
    with pytest.raises(ValueError, match="identifiers"):
        _spec(name="probe-specialist")
    with pytest.raises(ValueError, match="unknown cards"):
        _spec(cards=("invoice",))


def test_fallback_instruction_names_every_action_and_no_marker() -> None:
    for spec in SPECS:
        text = spec.fallback_instruction
        for name in spec.actions:
            assert name in text, f"{spec.name} fallback does not name {name}"
        assert "never" in text.lower()
        for fence in (MERCHANT_DATA_FENCE, OPERATOR_DATA_FENCE):
            assert fence.open not in text and fence.close not in text


# --------------------------------------------------------------------------- import boundary


def test_specialists_package_imports_no_model_runtime() -> None:
    for path in sorted(SPECIALISTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+google", source, re.MULTILINE), path.name
        assert "google.adk" not in source and "google.genai" not in source, path.name


def test_specialists_reach_only_into_core() -> None:
    """A specialist is data; the one thing it borrows is the rule table in ``core``."""
    allowed = {"..core.grounding_rules"}
    for path in sorted(SPECIALISTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*from\s+(\.\.[\w.]*)\s+import", source, re.MULTILINE):
            assert match.group(1) in allowed, f"{path.name} imports {match.group(1)}"


# --------------------------------------------------------------------------- rules


@pytest.mark.parametrize("spec", SPECS, ids=[spec.name for spec in SPECS])
def test_rules_are_the_core_tables_and_start_on_the_roster(spec: SpecialistSpec) -> None:
    assert spec.rules == rules_for(spec.role)
    for rule in spec.rules:
        assert rule.tool in spec.tool_names
        action = action_for_tool(rule.tool)
        assert action is not None
        assert rule.tool not in {"basket_set_line", "checkout_submit_approved", "basket_create"}


def test_rules_fire_through_the_specialist_wiring() -> None:
    lexicon = DEFAULT_LEXICON
    fired = first_rule(shopping.SPEC.rules, lexicon, "add AMUL-DAIRY-001", GroundingState())
    assert (
        fired is not None and fired[0].tool == "product" and fired[1] == {"sku": "AMUL-DAIRY-001"}
    )
    seen = GroundingState(seen_skus=frozenset({"amul-dairy-001"}))
    assert first_rule(shopping.SPEC.rules, lexicon, "add AMUL-DAIRY-001", seen) is None

    with_checkout = GroundingState(basket_id="b1", checkout_id="c1")
    fired = first_rule(checkout.SPEC.rules, lexicon, "anything at all", with_checkout)
    assert fired is not None and fired[0].name == "state" and fired[0].tool == "checkout_get"
    fired = first_rule(
        checkout.SPEC.rules,
        lexicon,
        "why is the delivery fee so high?",
        GroundingState(basket_id="b1"),
    )
    assert fired is not None and fired[0].tool == "basket_get"

    with_order = GroundingState(order_id="o1")
    fired = first_rule(spec_for("support").rules, lexicon, "I want a refund please", with_order)
    assert (
        fired is not None and fired[0].tool == "resolution_evaluate" and not fired[0].prefetchable
    )

    fired = first_rule(
        spec_for("growth").rules, lexicon, "how are sales this week?", GroundingState()
    )
    assert fired is not None and fired[0].tool == "checkout_metrics_read"
    assert first_rule(case.SPEC.rules, lexicon, "open CASE-1", GroundingState()) is None

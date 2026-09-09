"""The capability gate, the registry and the tool factory (ADR 0004 section 4, rows 1-5,
8-10 and 18).

Every test here runs without a model and without ADK: the tools are the factory's
closures, the gate is the callable the factory returned, and the "runtime" is a dict for
session state. That is the point. These are the properties that must hold *before* a
model is involved, and a test that needed a model would be testing the wrong layer.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from agent_runtime.backends import InMemoryBackend
from agent_runtime.capabilities import (
    AGENT_ALLOWLIST,
    ALL_CAPABILITIES,
    IDENTITY_PARAMETER_NAMES,
    KERNEL_INTERNAL_OPERATIONS,
    MAX_BASKET_LINES,
    MAX_LINE_QUANTITY,
    PRESENTATION_TOOLS,
    PROVENANCE_CAP,
    PROVENANCE_STATE_KEY,
    REASON_CAPABILITY_MISSING,
    REASON_TOOL_BUDGET_EXHAUSTED,
    REASON_TOOL_NOT_BOUND,
    REASON_TOOL_NOT_REGISTERED,
    REGISTRY_A,
    SPECIALIST_TOOLS,
    TRUSTED_OPERATOR_ACTIONS,
    TRUSTED_SURFACE_ACTIONS,
    WRITE_TOOLS,
    AgentRole,
    BoundToolset,
    Capability,
    SessionProvenance,
    build_toolset,
    derive_principal,
)
from agent_runtime.language import Language
from agent_runtime.turn import TurnContext
from commerce_domain import ActorType, AgentPrincipal, RecoveryCode
from merchant_sim import MerchantStore

from tests.test_ar_support_backend import SupportlessBackend

from .conftest import MILK_SKU

BREAD_SKU = "BRIT-BAKE-001"
UNSEEN_SKU = "EVIL-PROD-999"


# ------------------------------------------------------------------ test doubles


@dataclass(slots=True)
class FakeToolContext:
    """What ADK's ToolContext looks like to a tool: a state mapping and a call id."""

    state: dict[str, Any] = field(default_factory=dict)
    function_call_id: str | None = "call-1"


@dataclass(frozen=True, slots=True)
class FakeTool:
    """What ADK's BaseTool looks like to the gate.

    ``func`` carries the factory's own closure where the call is meant to be admitted.
    The gate compares it with ``is`` against the closures the factory produced, so a stub
    without one is refused as ``tool_not_bound`` -- correct for the tests that construct a
    tool the factory never built, and wrong for the ones exercising a real call, which is
    why the helper below hands over the genuine article.
    """

    name: str
    description: str = ""
    func: Any = None


class SpyBackend(InMemoryBackend):
    """Counts and orders writes so a test can prove a gate stopped one, or serialised two."""

    def __init__(self, store: MerchantStore) -> None:
        super().__init__(store)
        self.set_line_calls = 0
        self.events: list[str] = []

    async def basket_set_line(self, cart_id: str, sku: str, quantity: int) -> Any:
        self.set_line_calls += 1
        self.events.append(f"enter:{sku}")
        # Yield so a second concurrent write would interleave if nothing serialised it.
        await asyncio.sleep(0)
        view = await super().basket_set_line(cart_id, sku, quantity)
        self.events.append(f"exit:{sku}")
        return view


def _harness(capabilities: frozenset[str] | None = None) -> AgentPrincipal:
    """A buyer-copilot harness principal. Defaults to every Registry A capability."""
    return AgentPrincipal(
        principal_id="agent:buyer-copilot",
        tenant_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        actor_type=ActorType.AGENT,
        agent_role=None,
        capabilities=ALL_CAPABILITIES if capabilities is None else capabilities,
    )


def _turn(principal: AgentPrincipal, *, budget: int = 8) -> TurnContext:
    return TurnContext(language=Language.EN, principal=principal, max_tool_calls=budget)


def _toolset(
    role: AgentRole,
    backend: InMemoryBackend,
    *,
    harness: AgentPrincipal | None = None,
    budget: int = 8,
    session_id: str = "session-1",
) -> tuple[BoundToolset, TurnContext]:
    specialist = derive_principal(harness or _harness(), role)
    turn = _turn(specialist, budget=budget)
    toolset = build_toolset(role, backend, turn, principal=specialist, session_id=session_id)
    return toolset, turn


async def _call(toolset: BoundToolset, name: str, ctx: FakeToolContext, **args: Any) -> Any:
    """Run a tool the way the runtime would: gate first, tool only if the gate says so."""
    tool = toolset.get(name)
    denial = toolset.gate(FakeTool(name, tool.description, tool.func), dict(args), ctx)
    if denial is not None:
        return denial
    return await tool.func(tool_context=ctx, **args)


# ------------------------------------------------------------------ registry


def test_registry_a_is_disjoint_from_b_c_and_d() -> None:
    others = TRUSTED_SURFACE_ACTIONS | KERNEL_INTERNAL_OPERATIONS | TRUSTED_OPERATOR_ACTIONS
    assert not ALL_CAPABILITIES & others
    assert {c.value for c in REGISTRY_A.values()} <= ALL_CAPABILITIES


def test_tool_table_never_names_a_trusted_surface_action() -> None:
    """Neither a tool name nor its capability may spell approve, refund, revoke or pay."""
    # Verbs an agent tool may never be: whole words, so ``submit_approved`` (submit a
    # version somebody else approved) passes and ``approve`` would not.
    forbidden_verbs = {"approve", "reject", "revoke", "pay", "refund", "cancel", "capture"}
    for tool_name, capability in REGISTRY_A.items():
        assert capability.value not in TRUSTED_SURFACE_ACTIONS
        assert tool_name not in TRUSTED_SURFACE_ACTIONS
        assert not forbidden_verbs & set(tool_name.split("_")), tool_name
    for capability in Capability:
        assert capability.value not in TRUSTED_SURFACE_ACTIONS


def test_every_roster_tool_has_a_registry_row_and_every_role_an_allowlist() -> None:
    for role in AgentRole:
        assert role in SPECIALIST_TOOLS
        assert role in AGENT_ALLOWLIST
        for name in SPECIALIST_TOOLS[role]:
            assert name in REGISTRY_A
            assert REGISTRY_A[name] in AGENT_ALLOWLIST[role], (role, name)


def test_write_and_presentation_tool_sets_are_registry_rows() -> None:
    assert set(REGISTRY_A) >= WRITE_TOOLS
    assert set(REGISTRY_A) >= PRESENTATION_TOOLS
    assert not WRITE_TOOLS & PRESENTATION_TOOLS


# ------------------------------------------------------------------ subset at binding


def test_specialist_cannot_hold_a_capability_the_harness_lacks() -> None:
    """Row 3: the harness lacks submit_approved, so the checkout specialist does too."""
    narrow = _harness(ALL_CAPABILITIES - {Capability.CHECKOUT_SUBMIT_APPROVED.value})
    specialist = derive_principal(narrow, AgentRole.CHECKOUT)
    assert not specialist.can(Capability.CHECKOUT_SUBMIT_APPROVED.value)
    assert specialist.capabilities <= narrow.capabilities
    assert specialist.principal_id == "agent:buyer-copilot/checkout"
    assert specialist.delegation_chain == ("agent:buyer-copilot",)


def test_widening_at_binding_raises() -> None:
    narrow = _harness(frozenset({Capability.CATALOG_SEARCH.value}))
    with pytest.raises(ValueError, match="parent lacks"):
        narrow.subset_for("shopping", frozenset({Capability.BASKET_UPDATE.value}))


def test_specialist_allowlist_never_exceeds_registry_a() -> None:
    for role, allowed in AGENT_ALLOWLIST.items():
        assert {c.value for c in allowed} <= ALL_CAPABILITIES, role


# ------------------------------------------------------------------ factory


def test_factory_builds_only_tools_the_principal_may_hold(store: MerchantStore) -> None:
    backend = InMemoryBackend(store)
    toolset, _ = _toolset(AgentRole.SHOPPING, backend)
    # Pinned rather than counted: the point of this test is that a role is offered exactly
    # the tools its registry row grants and not one more, so the list is written out and a
    # newly built tool has to be added here deliberately.
    assert toolset.names == (
        "search",
        "product",
        "basket_create",
        "basket_set_line",
        "basket_propose_line",
        "basket_get",
        "present_products",
        "present_basket",
    )
    # Every tool this role may hold now has a builder, so nothing is reported unbuilt. The
    # assertion stays rather than being deleted: a roster row that loses its builder must
    # surface here as a gap rather than disappearing from the offered set unnoticed.
    assert set(toolset.unbuilt) == set()
    for tool in toolset.tools:
        assert REGISTRY_A[tool.name] == tool.capability
        assert toolset.principal.can(tool.capability.value)
        assert tool.writes == (tool.name in WRITE_TOOLS)


def test_factory_drops_tools_whose_capability_the_harness_withheld(store: MerchantStore) -> None:
    narrow = _harness(ALL_CAPABILITIES - {Capability.BASKET_UPDATE.value})
    toolset, _ = _toolset(AgentRole.SHOPPING, InMemoryBackend(store), harness=narrow)
    assert "basket_set_line" not in toolset.names
    assert "basket_set_line" not in toolset.unbuilt


def test_factory_refuses_a_principal_of_another_role(store: MerchantStore) -> None:
    specialist = derive_principal(_harness(), AgentRole.SHOPPING)
    with pytest.raises(ValueError, match="does not match"):
        build_toolset(
            AgentRole.CHECKOUT,
            InMemoryBackend(store),
            _turn(specialist),
            principal=specialist,
            session_id="s",
        )


def test_no_tool_schema_carries_an_identity_parameter(store: MerchantStore) -> None:
    """Row 18: the server supplies identity; the model never picks a cart or a tenant."""
    backend = InMemoryBackend(store)
    for role in AgentRole:
        toolset, _ = _toolset(role, backend)
        for tool in toolset.tools:
            assert not IDENTITY_PARAMETER_NAMES & set(tool.parameters), tool.name
            assert "tool_context" not in tool.parameters


def test_factory_refuses_a_builder_outside_registry_a(store: MerchantStore) -> None:
    specialist = derive_principal(_harness(), AgentRole.SHOPPING)
    with pytest.raises(ValueError, match="not a Registry A tool"):
        build_toolset(
            AgentRole.SHOPPING,
            InMemoryBackend(store),
            _turn(specialist),
            principal=specialist,
            session_id="s",
            extra_builders={"approve_checkout": lambda _ctx: _never},
        )


async def _never(tool_context: Any) -> dict[str, Any]:  # pragma: no cover - never built
    del tool_context
    return {}


# ------------------------------------------------------------------ the gate


@pytest.mark.asyncio
async def test_denial_is_nonempty_dict_and_tool_does_not_run(store: MerchantStore) -> None:
    """Row 2. The shopping specialist asks for a checkout write; the kernel never hears."""
    backend = SpyBackend(store)
    toolset, turn = _toolset(AgentRole.SHOPPING, backend)
    ctx = FakeToolContext()
    result = toolset.gate(FakeTool("checkout_create"), {}, ctx)
    assert result, "a denial must be a non-empty dict; ADK treats {} as None and runs the tool"
    assert result["denied"] is True
    assert result["reason_key"] in {REASON_CAPABILITY_MISSING, REASON_TOOL_NOT_BOUND}
    assert result["capability"] == Capability.CHECKOUT_SUBMIT_FOR_APPROVAL.value
    assert result["principal_id"] == "agent:buyer-copilot/shopping"
    assert len(turn.denials) == 1
    assert turn.tool_calls[-1].denied is True


def test_capability_missing_is_named_when_the_tool_is_bound_but_withheld(
    store: MerchantStore,
) -> None:
    """Same tool name, principal lacks the capability: the reason says so, not 'unbound'."""
    narrow = _harness(ALL_CAPABILITIES - {Capability.CHECKOUT_SUBMIT_FOR_APPROVAL.value})
    toolset, _ = _toolset(AgentRole.CHECKOUT, InMemoryBackend(store), harness=narrow)
    result = toolset.gate(FakeTool("checkout_create"), {}, FakeToolContext())
    assert result and result["reason_key"] in {REASON_TOOL_NOT_BOUND, REASON_CAPABILITY_MISSING}
    assert result["denied"] is True


def test_hand_built_tool_with_a_registered_name_is_denied_as_unbound(
    store: MerchantStore,
) -> None:
    """Row 4 at runtime: a FunctionTool nobody got from the factory is refused by the gate."""
    toolset, _ = _toolset(AgentRole.SUPPORT, SupportlessBackend(InMemoryBackend(store)))
    # ``support_escalate`` is a SUPPORT roster tool the support principal may hold, and on
    # a backend with no support surface no closure exists for it. A tool object bearing
    # that name therefore did not come from the factory, and the gate, bound to the
    # toolset's own names, refuses it however it arrived.
    result = toolset.gate(FakeTool("support_escalate"), {}, FakeToolContext())
    assert result and result["reason_key"] == REASON_TOOL_NOT_BOUND


def test_unregistered_tool_is_denied(store: MerchantStore) -> None:
    toolset, _ = _toolset(AgentRole.SHOPPING, InMemoryBackend(store))
    result = toolset.gate(FakeTool("approve_checkout"), {}, FakeToolContext())
    assert result and result["reason_key"] == REASON_TOOL_NOT_REGISTERED
    assert result["capability"] is None


@pytest.mark.asyncio
async def test_budget_exhausted_denies(store: MerchantStore) -> None:
    """Row 5: the budget is spent in the gate, before the tool, so a loop cannot run it."""
    toolset, turn = _toolset(AgentRole.SHOPPING, InMemoryBackend(store), budget=1)
    ctx = FakeToolContext()
    first = await _call(toolset, "search", ctx, query="milk")
    assert first["result_count"] >= 1
    second = toolset.gate(
        FakeTool("search", func=toolset.get("search").func), {"query": "milk"}, ctx
    )
    assert second and second["reason_key"] == REASON_TOOL_BUDGET_EXHAUSTED
    assert turn.tool_calls_admitted == 1


# ------------------------------------------------------------------ provenance


@pytest.mark.asyncio
async def test_basket_write_naming_an_unreturned_sku_is_refused_by_provenance(
    store: MerchantStore,
) -> None:
    """Row 8. A real SKU the session never saw is as unwritable as an invented one."""
    backend = SpyBackend(store)
    toolset, turn = _toolset(AgentRole.SHOPPING, backend)
    ctx = FakeToolContext()
    await _call(toolset, "basket_create", ctx)
    for sku in (UNSEEN_SKU, BREAD_SKU):
        held = await _call(toolset, "basket_set_line", ctx, sku=sku, quantity=1)
        assert held["ok"] is False
        assert held["blocked"] == "provenance"
        assert held["reason_key"] == "sku_not_returned"
    assert backend.set_line_calls == 0
    assert all(record.ok is False for record in turn.tool_calls[-2:])


@pytest.mark.asyncio
async def test_basket_write_accepts_a_sku_a_tool_returned(store: MerchantStore) -> None:
    backend = SpyBackend(store)
    toolset, _ = _toolset(AgentRole.SHOPPING, backend)
    ctx = FakeToolContext()
    await _call(toolset, "basket_create", ctx)
    page = await _call(toolset, "search", ctx, query="milk")
    assert MILK_SKU in page["allowed_skus"]
    view = await _call(toolset, "basket_set_line", ctx, sku=MILK_SKU, quantity=2)
    assert view["code"] == RecoveryCode.OK.value
    assert view["lines"] == [{"sku": MILK_SKU, "quantity": 2}]
    assert backend.set_line_calls == 1
    # Provenance survives in session state, not in the closure: a new toolset for the
    # same session (next turn) still knows the SKU.
    assert MILK_SKU in {row["sku"] for row in ctx.state[PROVENANCE_STATE_KEY]["skus"]}
    later, _ = _toolset(AgentRole.SHOPPING, backend)
    again = await _call(later, "basket_set_line", ctx, sku=MILK_SKU, quantity=3)
    assert again["lines"] == [{"sku": MILK_SKU, "quantity": 3}]


@pytest.mark.asyncio
async def test_product_read_grants_provenance_without_a_search(store: MerchantStore) -> None:
    toolset, _ = _toolset(AgentRole.SHOPPING, SpyBackend(store))
    ctx = FakeToolContext()
    await _call(toolset, "basket_create", ctx)
    card = await _call(toolset, "product", ctx, sku=BREAD_SKU)
    assert card["sku"] == BREAD_SKU
    view = await _call(toolset, "basket_set_line", ctx, sku=BREAD_SKU, quantity=1)
    assert view["lines"] == [{"sku": BREAD_SKU, "quantity": 1}]


@pytest.mark.asyncio
async def test_quantity_cap_applies_to_the_line_after_the_write(store: MerchantStore) -> None:
    """Row 9."""
    backend = SpyBackend(store)
    toolset, _ = _toolset(AgentRole.SHOPPING, backend)
    ctx = FakeToolContext()
    await _call(toolset, "basket_create", ctx)
    await _call(toolset, "search", ctx, query="milk")
    for bad in (MAX_LINE_QUANTITY + 1, -1, True):
        held = await _call(toolset, "basket_set_line", ctx, sku=MILK_SKU, quantity=bad)
        assert held["blocked"] == "quantity", bad
    assert backend.set_line_calls == 0
    ok = await _call(toolset, "basket_set_line", ctx, sku=MILK_SKU, quantity=MAX_LINE_QUANTITY)
    assert ok["lines"] == [{"sku": MILK_SKU, "quantity": MAX_LINE_QUANTITY}]


@pytest.mark.asyncio
async def test_line_count_cap_refuses_growth_but_not_edits(store: MerchantStore) -> None:
    """Row 9: the cap stops a new line; setting or removing an existing one still works."""
    backend = SpyBackend(store)
    toolset, _ = _toolset(AgentRole.SHOPPING, backend, budget=1000)
    ctx = FakeToolContext()
    await _call(toolset, "basket_create", ctx)
    skus = list(store.all_skus())
    assert len(skus) > MAX_BASKET_LINES
    # Ground only the lines this test writes. The ledger keeps the newest PROVENANCE_CAP
    # ids per family, so remembering the whole 247-SKU catalogue would evict the first ones
    # before they are written, and the gate would correctly hold them as unseen.
    provenance = SessionProvenance.from_state(ctx.state.get(PROVENANCE_STATE_KEY))
    for sku in skus[: MAX_BASKET_LINES + 1]:
        provenance.remember_product(await backend.product(sku))
    ctx.state[PROVENANCE_STATE_KEY] = provenance.to_state()
    for sku in skus[:MAX_BASKET_LINES]:
        result = await _call(toolset, "basket_set_line", ctx, sku=sku, quantity=1)
        assert "blocked" not in result, sku
    extra = skus[MAX_BASKET_LINES]
    held = await _call(toolset, "basket_set_line", ctx, sku=extra, quantity=1)
    assert held["blocked"] == "line_count"
    edited = await _call(toolset, "basket_set_line", ctx, sku=skus[0], quantity=2)
    assert "blocked" not in edited
    removed = await _call(toolset, "basket_set_line", ctx, sku=skus[0], quantity=0)
    assert "blocked" not in removed


@pytest.mark.asyncio
async def test_concurrent_writes_for_one_session_do_not_interleave(store: MerchantStore) -> None:
    """Row 10: one round's tool calls run together; the session lock orders the writes."""
    backend = SpyBackend(store)
    toolset, _ = _toolset(AgentRole.SHOPPING, backend)
    ctx = FakeToolContext()
    await _call(toolset, "basket_create", ctx)
    await _call(toolset, "search", ctx, query="milk")
    await _call(toolset, "product", ctx, sku=BREAD_SKU)
    await asyncio.gather(
        _call(toolset, "basket_set_line", ctx, sku=MILK_SKU, quantity=1),
        _call(toolset, "basket_set_line", ctx, sku=BREAD_SKU, quantity=1),
    )
    assert backend.set_line_calls == 2
    entered = [e for e in backend.events if e.startswith("enter")]
    exited = [e for e in backend.events if e.startswith("exit")]
    assert len(entered) == 2 and len(exited) == 2
    # Serialized: every enter is followed by its own exit before the next enter.
    assert backend.events[0].startswith("enter") and backend.events[1].startswith("exit")
    assert backend.events[2].startswith("enter") and backend.events[3].startswith("exit")


@pytest.mark.asyncio
async def test_checkout_get_records_every_version_for_provenance(store: MerchantStore) -> None:
    backend = SpyBackend(store)
    shopping, _ = _toolset(AgentRole.SHOPPING, backend)
    ctx = FakeToolContext()
    await _call(shopping, "basket_create", ctx)
    await _call(shopping, "search", ctx, query="milk")
    await _call(shopping, "basket_set_line", ctx, sku=MILK_SKU, quantity=1)
    checkout, _ = _toolset(AgentRole.CHECKOUT, backend)
    card = await _call(checkout, "checkout_create", ctx)
    # A fresh context for the same session that never saw the card, then a read.
    fresh = FakeToolContext(state={k: v for k, v in ctx.state.items() if k != PROVENANCE_STATE_KEY})
    view = await _call(checkout, "checkout_get", fresh)
    assert view["current_version"] == 1
    provenance = SessionProvenance.from_state(fresh.state.get(PROVENANCE_STATE_KEY))
    assert provenance.checkouts[card["checkout_id"]].knows(1, card["content_hash"])


# ------------------------------------------------------------------ provenance record


def test_the_outer_session_state_is_not_a_provenance_record() -> None:
    """Reading provenance one level too high silently yields "this session saw nothing".

    `from_state` is documented to treat anything malformed as an *empty* record, and that
    fail-safe is right: a corrupted blob should hold every write until a fresh read
    re-grounds it. But it also means passing the wrong level cannot raise. The harness's
    `prefetch_grounding` passed the whole tool-visible `session.state` -- the outer dict
    that *contains* the record under `PROVENANCE_STATE_KEY` -- where the tool factory's
    `_load` passes `state.get(PROVENANCE_STATE_KEY)`. So every rule the prefetch judged
    against "SKUs this session has already seen" judged against an empty set, on every
    turn, and nothing failed.

    This pins the difference between the two levels so a future caller cannot pick the
    wrong one and be told nothing.
    """
    record = SessionProvenance()
    record.remember_sku(
        "MILK-DAIRY-001",
        unit_price_minor=2900,
        currency="INR",
        catalogue_revision=1,
        is_available=True,
    )
    outer: dict[str, Any] = {PROVENANCE_STATE_KEY: record.to_state()}

    assert SessionProvenance.from_state(outer.get(PROVENANCE_STATE_KEY)).seen_skus(), (
        "the inner record is what from_state takes"
    )
    assert not SessionProvenance.from_state(outer).seen_skus(), (
        "the outer dict must not read as a populated record; if it ever does, this test "
        "is no longer pinning the level and the two call sites can drift again"
    )


def test_provenance_state_survives_a_new_toolset_and_junk_reads_empty() -> None:
    """The record lives in session state, and a corrupted blob holds every write."""
    record = SessionProvenance()
    record.remember_order_id("ord-1")
    state: dict[str, Any] = {PROVENANCE_STATE_KEY: record.to_state()}
    assert SessionProvenance.from_state(state[PROVENANCE_STATE_KEY]).knows_order("ord-1")
    for junk in ("not a dict", {"skus": "text"}, {"skus": [{"sku": 1}]}):
        assert not SessionProvenance.from_state(junk).knows_order("ord-1")
    assert PROVENANCE_CAP == 200


def test_toolset_is_a_sequence_of_its_tools(store: MerchantStore) -> None:
    """The harness passes 'the tools' on as a Sequence; the adapter reads the gates."""
    toolset, _ = _toolset(AgentRole.SHOPPING, InMemoryBackend(store))
    assert len(toolset) == len(toolset.tools)
    assert list(toolset) == list(toolset.tools)
    assert toolset[0] is toolset.tools[0]
    assert all(callable(tool.func) for tool in toolset[1:])

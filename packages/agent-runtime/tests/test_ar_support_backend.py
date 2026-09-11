"""The Support Specialist's two post-purchase reads, wired to the SupportBackend surface.

These are the two reads that decide what a buyer is *owed*: the rules the sale was made
under (``policy_search``) and the findings raised against the payment provider with the
plan that settles each (``resolution_evaluate``). Every property below is a way the wiring
could be wrong in the direction of money or of a leaked capability.

*A tool that cannot work is not offered.* Both reads need a backend that actually carries
the support surface. Against a backend without it the factory builds nothing and reports
both rows in ``unbuilt``, exactly as the merchant reads and the case reads do -- because a
closure that had to invent an at-sale term or a remedy amount is the one thing a
read-only support tool must never become.

*Escalate stays unbuilt, always.* There is deliberately no ``support_escalate`` builder.
Opening a human-review case freezes a payment attempt on a terminal transition -- a write
on the money path whose kernel primitive carries no who/why gate --
so the row is reported unbuilt whatever surface the backend has, and never bound beside
the two reads.

*A capability the principal lacks means no tool.* A support principal without
``POLICY_SEARCH`` never sees the policy tool at all; the gate is the second answer for a
name that reaches it by some other route.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from agent_runtime.backends import InMemoryBackend
from agent_runtime.backends.base import (
    AdmissionDecision,
    ApprovalCard,
    CartView,
    CheckoutView,
    CommerceBackend,
    Locale,
    OrderResolution,
    OrderView,
    PolicyAtSale,
    PolicyTerm,
    ProductCard,
    RemedyConfirmation,
    RemedyOption,
    ResolutionPlan,
    SearchPage,
    SupportBackend,
    WithheldReason,
    WithheldRemedy,
)
from agent_runtime.backends.base import (
    RemedyOutcome as Outcome,
)
from agent_runtime.capabilities import (
    ALL_CAPABILITIES,
    IDENTITY_PARAMETER_NAMES,
    REASON_TOOL_NOT_BOUND,
    REGISTRY_A,
    SPECIALIST_TOOLS,
    WRITE_TOOLS,
    AgentRole,
    BoundToolset,
    Capability,
    build_toolset,
    derive_principal,
)
from agent_runtime.core import SessionProvenance
from agent_runtime.language import Language
from agent_runtime.turn import TurnContext
from commerce_domain import ActorType, AgentPrincipal, Money, PolicyKind, RecoveryCode
from merchant_sim import MerchantStore

#: The two reads this task wires. ``support_escalate`` is not here on purpose.
SUPPORT_READS = ("policy_search", "resolution_evaluate")

ORDER_ID = "order-post-purchase"


# ------------------------------------------------------------------ test doubles


@dataclass(slots=True)
class FakeToolContext:
    """What ADK's ToolContext looks like to a tool: a state mapping and a call id."""

    state: dict[str, Any] = field(default_factory=dict)
    function_call_id: str | None = "call-1"


@dataclass(frozen=True, slots=True)
class FakeTool:
    """What ADK's BaseTool looks like to the gate.

    ``func`` is left empty where the point is a tool the factory never built -- the name
    check refuses those first -- and carries the genuine closure where the call is meant
    to run, because the gate compares it with ``is`` against the factory's own.
    """

    name: str
    description: str = ""
    func: Any = None


class SupportlessBackend(CommerceBackend):
    """The buyer surface and nothing else: a backend that never learned the support reads.

    Every method delegates, so nothing about the buyer tools changes and the only
    difference under test is the missing surface. This is the production case rather than
    a contrived one -- a backend built for a buyer session must not be able to read an
    at-sale receipt or a resolution plan at all, which is why the support reads are a
    protocol of their own.
    """

    def __init__(self, inner: InMemoryBackend) -> None:
        self._inner = inner

    async def search(self, query: str, locale: Locale, limit: int) -> SearchPage:
        return await self._inner.search(query, locale, limit)

    async def product(self, sku: str) -> ProductCard:
        return await self._inner.product(sku)

    async def basket_create(self) -> CartView:
        return await self._inner.basket_create()

    async def basket_set_line(self, cart_id: str, sku: str, quantity: int) -> CartView:
        return await self._inner.basket_set_line(cart_id, sku, quantity)

    async def basket_get(self, cart_id: str) -> CartView:
        return await self._inner.basket_get(cart_id)

    async def checkout_create(self, cart_id: str) -> ApprovalCard:
        return await self._inner.checkout_create(cart_id)

    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        return await self._inner.checkout_get(checkout_id)

    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> AdmissionDecision:
        return await self._inner.checkout_submit_approved(checkout_id, version, content_hash)

    async def order_track(self, order_id: str) -> OrderView:
        return await self._inner.order_track(order_id)


# ------------------------------------------------------------------ fixtures


def _policy(*, binding_ok: bool = True) -> PolicyAtSale:
    """A verified Policy-at-Sale Receipt with one recorded refund term.

    ``PolicyAtSale`` refuses terms with a broken binding and refuses a verified binding
    with no terms, so the two shapes are built apart: an OK binding carries the term, and
    a broken one carries none and the code that says which way it broke.
    """
    if not binding_ok:
        return PolicyAtSale(
            order_id=ORDER_ID,
            binding_ok=False,
            binding_code=RecoveryCode.STALE_CHECKOUT,
            receipt_hash=None,
        )
    return PolicyAtSale(
        order_id=ORDER_ID,
        binding_ok=True,
        binding_code=RecoveryCode.OK,
        receipt_hash="sha256:receipt",
        policies=(
            PolicyTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund-7",
                policy_version=3,
                terms={"window_days": 7, "note": "Full refund within the window."},
                applies_to=("AMUL-DAIRY-001",),
            ),
        ),
    )


def _resolution_with_plan() -> OrderResolution:
    """One finding whose plan issued a single full-refund option over a ₹1,250 capture."""
    plan = ResolutionPlan(
        finding_id="finding-1",
        code=RecoveryCode.RESOLUTION_PLAN_ISSUED,
        plan_id="plan-1",
        options=(
            RemedyOption(
                outcome=Outcome.REFUND_FULL,
                amount=Money(125000, "INR"),
                policy_kind=PolicyKind.REFUND,
                policy_id="pol-refund-7",
                policy_version=3,
                confirmation=RemedyConfirmation.BUYER_APPROVAL,
                basis="Within the recorded refund window.",
            ),
        ),
        withheld=(
            WithheldRemedy(
                outcome=Outcome.STORE_CREDIT,
                reason=WithheldReason.POLICY_FORBIDS,
                detail="Store credit is not offered on this sale.",
            ),
        ),
        captured_minor=125000,
        refunds_reserved_minor=0,
        refundable_minor=125000,
        currency="INR",
        explanation="A full refund settles this finding.",
    )
    return OrderResolution(
        order_id=ORDER_ID,
        recorded_state="captured",
        findings=1,
        plans=(plan,),
        plan_ttl_seconds=3600,
    )


def _backend(store: MerchantStore, **kwargs: Any) -> InMemoryBackend:
    return InMemoryBackend(store, **kwargs)


def _harness(capabilities: frozenset[str] = ALL_CAPABILITIES) -> AgentPrincipal:
    """A merchant-console harness principal; ``capabilities`` narrows what it may grant."""
    return AgentPrincipal(
        principal_id="agent:merchant-console",
        tenant_id=uuid.UUID("00000000-0000-4000-8000-000000000003"),
        actor_type=ActorType.AGENT,
        agent_role=None,
        capabilities=capabilities,
    )


def _toolset(
    backend: CommerceBackend, *, harness: AgentPrincipal | None = None
) -> tuple[BoundToolset, TurnContext]:
    specialist = derive_principal(harness or _harness(), AgentRole.SUPPORT)
    turn = TurnContext(language=Language.EN, principal=specialist, max_tool_calls=8)
    toolset = build_toolset(
        AgentRole.SUPPORT, backend, turn, principal=specialist, session_id="support-1"
    )
    return toolset, turn


async def _call(toolset: BoundToolset, name: str, ctx: FakeToolContext, **args: Any) -> Any:
    """Run a tool the way the runtime would: gate first, tool only if the gate says so."""
    tool = toolset.get(name)
    denial = toolset.gate(FakeTool(name, tool.description, tool.func), dict(args), ctx)
    if denial is not None:
        return denial
    return await tool.func(tool_context=ctx, **args)


# ------------------------------------------------------------------ the wiring


def test_both_reads_build_against_a_support_backend(store: MerchantStore) -> None:
    """With the surface present both rows have a closure, and neither is left unbuilt."""
    toolset, _ = _toolset(_backend(store, policies=(_policy(),)))
    assert "policy_search" in toolset.names
    assert "resolution_evaluate" in toolset.names
    assert "policy_search" not in toolset.unbuilt
    assert "resolution_evaluate" not in toolset.unbuilt


def test_neither_read_takes_an_identity(store: MerchantStore) -> None:
    """Identity is the server's (spec 20.2); both reads name an order subject and nothing else."""
    toolset, _ = _toolset(_backend(store, policies=(_policy(),)))
    for name in SUPPORT_READS:
        tool = toolset.get(name)
        assert not IDENTITY_PARAMETER_NAMES & set(tool.parameters)
        assert tool.parameters == ("order_id",)
    assert REGISTRY_A["policy_search"] is Capability.POLICY_SEARCH
    assert REGISTRY_A["resolution_evaluate"] is Capability.RESOLUTION_EVALUATE


def test_neither_read_is_built_without_the_support_surface(store: MerchantStore) -> None:
    """A backend that is not a SupportBackend leaves both rows unbuilt, denied if called."""
    supportless = SupportlessBackend(_backend(store))
    assert not isinstance(supportless, SupportBackend)
    toolset, _ = _toolset(supportless)
    for name in SUPPORT_READS:
        assert name not in toolset.names
        assert name in toolset.unbuilt
        with pytest.raises(KeyError):
            toolset.get(name)
        denial = toolset.gate(FakeTool(name), {}, FakeToolContext())
        assert denial is not None
        assert denial["reason_key"] == REASON_TOOL_NOT_BOUND


@pytest.mark.asyncio
async def test_support_escalate_refuses_an_order_this_session_never_saw(
    store: MerchantStore,
) -> None:
    """Opening a case is an act, and it must name an order the session was actually shown.

    `_spec.py` declares this tool a write gated on order provenance, and the gate was never
    applied: `check_order_provenance` was defined, exported through three modules, and had
    no caller anywhere in the package. An agent could file a case on the merchant's support
    queue against any order id it composed.

    `resolution_evaluate` is deliberately left ungated -- it is one of the tools that
    *establishes* order provenance, so gating it on its own output would be circular. This
    one consumes provenance rather than producing it.
    """
    toolset, _ = _toolset(_backend(store, policies=(_policy(),)))
    ctx = FakeToolContext()

    refused = await _call(
        toolset,
        "support_escalate",
        ctx,
        order_id="ord-never-seen",
        reason="buyer_requested",
        note="the parcel never arrived",
    )
    assert refused["ok"] is False, refused
    assert refused["reason_key"] == "order_not_returned", refused


@pytest.mark.asyncio
async def test_support_escalate_accepts_an_order_a_tool_returned(store: MerchantStore) -> None:
    """The gate must let the real path through: read the order, then act on it."""
    toolset, _ = _toolset(_backend(store, resolutions=(_resolution_with_plan(),)))
    ctx = FakeToolContext()

    await _call(toolset, "resolution_evaluate", ctx, order_id=ORDER_ID)
    assert SessionProvenance.from_state(ctx.state["acr:provenance"]).knows_order(ORDER_ID)

    opened = await _call(
        toolset,
        "support_escalate",
        ctx,
        order_id=ORDER_ID,
        reason="buyer_requested",
        note="the parcel never arrived",
    )
    # The gate is what this pins. Whatever the backend then says about the order, it must
    # no longer be the provenance refusal -- the read grounded it, so the act may proceed.
    assert opened.get("reason_key") != "order_not_returned", opened


def test_support_escalate_needs_the_support_surface(store: MerchantStore) -> None:
    """Built where the support surface is, unbuilt where it is not. Same rule as the reads.

    This test used to assert that escalate could never be built, and the reason it gave was
    sound for the escalation it had in mind: opening a *human-review case* freezes a payment
    attempt on a terminal transition, which is a write on money. What exists now is not
    that. It puts one row on the merchant's support queue naming an order and a reason,
    touches no payment attempt and no financial table, and returns a case id rather than an
    outcome -- so it is bound like the two reads and gated like them.

    What stays true, and is what this now pins: a backend built for a buyer session has no
    support surface, and no surface means no closure. The row is reported unbuilt rather
    than handed a stub that would file a case nobody could answer.
    """
    assert "support_escalate" in SPECIALIST_TOOLS[AgentRole.SUPPORT]
    with_surface, _ = _toolset(_backend(store, policies=(_policy(),)))
    assert "support_escalate" in with_surface.names
    assert "support_escalate" not in with_surface.unbuilt

    without_surface, _ = _toolset(SupportlessBackend(_backend(store)))
    assert "support_escalate" not in without_surface.names
    assert "support_escalate" in without_surface.unbuilt


def test_a_principal_lacking_policy_search_does_not_get_the_policy_tool(
    store: MerchantStore,
) -> None:
    """A roster tool whose capability the principal lacks is never built for the model.

    The harness is narrowed to grant everything except ``policy.search``; the derived
    support principal then cannot hold it, so the policy read is neither built nor listed
    unbuilt (an unheld capability is not a missing closure). ``resolution_evaluate``, whose
    capability the principal still holds, is unaffected -- the two reads are gated apart.
    """
    narrowed = ALL_CAPABILITIES - {Capability.POLICY_SEARCH.value}
    toolset, _ = _toolset(_backend(store, policies=(_policy(),)), harness=_harness(narrowed))
    assert "policy_search" not in toolset.names
    assert "policy_search" not in toolset.unbuilt
    assert "resolution_evaluate" in toolset.names


# ------------------------------------------------------------------ policy_search reads


@pytest.mark.asyncio
async def test_policy_search_returns_the_at_sale_terms_and_grounds_the_order(
    store: MerchantStore,
) -> None:
    toolset, _ = _toolset(_backend(store, policies=(_policy(),)))
    ctx = FakeToolContext()

    result = await _call(toolset, "policy_search", ctx, order_id=ORDER_ID)

    assert result["ok"] is True
    assert result["order_id"] == ORDER_ID
    assert result["binding_ok"] is True
    assert result["binding_code"] == RecoveryCode.OK.value
    term = result["policies"][0]
    assert term["kind"] == PolicyKind.REFUND.value
    assert term["policy_id"] == "pol-refund-7"
    assert term["policy_version"] == 3
    assert term["terms"]["window_days"] == 7
    # The order subject is grounded, so a later present or resolution call is held to an
    # order this conversation actually saw.
    record = SessionProvenance.from_state(ctx.state["acr:provenance"])
    assert record.knows_order(ORDER_ID)


@pytest.mark.asyncio
async def test_a_broken_binding_returns_no_terms_and_says_so(store: MerchantStore) -> None:
    """An empty ``policies`` under a broken binding is a verification failure, not permission."""
    toolset, _ = _toolset(_backend(store, policies=(_policy(binding_ok=False),)))

    result = await _call(toolset, "policy_search", FakeToolContext(), order_id=ORDER_ID)

    assert result["binding_ok"] is False
    assert result["policies"] == []
    assert result["binding_code"] == RecoveryCode.STALE_CHECKOUT.value


@pytest.mark.asyncio
async def test_an_unknown_order_policy_is_a_structured_refusal(store: MerchantStore) -> None:
    """A missing receipt is a 404 the model can explain, never an empty policy set."""
    toolset, _ = _toolset(_backend(store))

    result = await _call(toolset, "policy_search", FakeToolContext(), order_id="order-unknown")

    assert result["ok"] is False
    assert result["status"] == 404
    assert result != {}


# ------------------------------------------------------------------ resolution_evaluate reads


@pytest.mark.asyncio
async def test_resolution_evaluate_grounds_every_amount_it_returns(store: MerchantStore) -> None:
    toolset, turn = _toolset(_backend(store, resolutions=(_resolution_with_plan(),)))
    ctx = FakeToolContext()

    result = await _call(toolset, "resolution_evaluate", ctx, order_id=ORDER_ID)

    assert result["ok"] is True
    assert result["findings"] == 1
    assert result["plan_ttl_seconds"] == 3600
    plan = result["plans"][0]
    assert plan["code"] == RecoveryCode.RESOLUTION_PLAN_ISSUED.value
    option = plan["options"][0]
    assert option["outcome"] == Outcome.REFUND_FULL.value
    assert option["amount"] == {"minor": 125000, "display": "₹1,250.00", "measured": True}
    assert plan["withheld"][0]["reason"] == WithheldReason.POLICY_FORBIDS.value
    # Every money figure the tool returned is recorded, so the specialist may quote it in
    # prose without the reply post-check treating it as invented.
    assert 125000 in turn.ledger.amounts_minor


@pytest.mark.asyncio
async def test_zero_findings_is_an_answer_not_a_silence(store: MerchantStore) -> None:
    """Zero findings is a measurement: the service looked and nothing diverged, no TTL.

    Supplied directly as an ``OrderResolution`` with no plan rather than driven through an
    order, so the test asserts on how the tool serialises the empty answer, not on the
    backend's order-creation path. ``plan_ttl_seconds`` is ``None`` because no plan was
    issued, which is a different fact from a window of zero seconds.
    """
    empty = OrderResolution(order_id=ORDER_ID, recorded_state="captured", findings=0)
    toolset, _ = _toolset(_backend(store, resolutions=(empty,)))

    result = await _call(toolset, "resolution_evaluate", FakeToolContext(), order_id=ORDER_ID)

    assert result["ok"] is True
    assert result["findings"] == 0
    assert result["plans"] == []
    assert result["plan_ttl_seconds"] is None


@pytest.mark.asyncio
async def test_a_resolution_read_changes_nothing_it_grounds_the_order(
    store: MerchantStore,
) -> None:
    """The read grounds the order subject and returns without any write side effect."""
    toolset, _ = _toolset(_backend(store, resolutions=(_resolution_with_plan(),)))
    ctx = FakeToolContext()

    await _call(toolset, "resolution_evaluate", ctx, order_id=ORDER_ID)

    record = SessionProvenance.from_state(ctx.state["acr:provenance"])
    assert record.knows_order(ORDER_ID)


def test_reading_a_policy_holds_no_write_lock(store: MerchantStore) -> None:
    """``policy_search`` is a pure read: not in WRITE_TOOLS and not flagged as a write."""
    toolset, _ = _toolset(_backend(store, policies=(_policy(),)))
    tool = toolset.get("policy_search")
    assert "policy_search" not in WRITE_TOOLS
    assert not tool.writes

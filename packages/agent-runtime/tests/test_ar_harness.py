"""The harness, driven with a scripted specialist. No model, no network, no ADK.

What is under test is everything a harness owns and a specialist must not: who may enter,
what a specialist is bound to, what survives across turns, what is refused before a model
would have run, and what is corrected after one has. The scripted runner stands in for
the whole model layer; it records what it was handed and writes whatever structured
records the case needs onto the turn, exactly as a factory-built tool would.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from agent_runtime.backends import InMemoryBackend
from agent_runtime.backends.base import CartView, CommerceBackend, Provenance, UnavailableLine
from agent_runtime.capabilities.registry import (
    KERNEL_INTERNAL_OPERATIONS,
    TRUSTED_OPERATOR_ACTIONS,
    TRUSTED_SURFACE_ACTIONS,
)
from agent_runtime.grounding.payloads import decision_payload
from agent_runtime.harness import (
    REGISTRY_A_CAPABILITIES,
    ROLE_CAPABILITIES,
    Binding,
    BoundSpecialist,
    CopilotSession,
    HandBack,
    HarnessConfigurationError,
    Modality,
    PrincipalRefusedError,
    RazorAI,
    SessionRefusedError,
    Specialist,
    SpecialistInput,
    SpecialistReply,
    bind,
    enforce_conversational_rules,
    to_sse,
)
from agent_runtime.harness.base import (
    CORRECTION_DECISION,
    CORRECTION_FALLBACK,
    CORRECTION_INTERIM,
    CORRECTION_RECOVERY,
    CORRECTION_UNAVAILABLE,
    CORRECTION_UNGROUNDED,
)
from agent_runtime.language import Language
from agent_runtime.rendering import recovery_text, render_fallback
from agent_runtime.turn import Denial, TurnContext
from transaction_kernel import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Delta,
    KernelDecision,
    RecoveryCode,
)

TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_TENANT = uuid.UUID("00000000-0000-4000-8000-000000000002")
MERCHANT = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
CORRELATION = uuid.UUID("00000000-0000-4000-8000-0000000000cc")

#: Verbs only Registry B, C or D may carry, compared as whole tokens so that
#: ``submit_approved`` and ``submit_for_approval`` (a human approves; the agent submits)
#: are not mistaken for approving. Registry B's ``approval.record`` is caught by the
#: disjointness assertion instead.
FORBIDDEN_TOKENS = frozenset(
    {"revoke", "confirm", "execute", "payment", "webhook", "reconcile", "issue", "consume"}
)


def buyer_principal(capabilities: frozenset[str] = REGISTRY_A_CAPABILITIES) -> AgentPrincipal:
    return AgentPrincipal(
        principal_id="agent:buyer-copilot",
        tenant_id=TENANT,
        actor_type=ActorType.AGENT,
        agent_role="razorai",
        buyer_ref="buyer:pseudonymous-1",
        capabilities=capabilities,
        correlation_id=CORRELATION,
    )


def merchant_principal(capabilities: frozenset[str] = REGISTRY_A_CAPABILITIES) -> AgentPrincipal:
    return AgentPrincipal(
        principal_id="agent:merchant-copilot",
        tenant_id=TENANT,
        actor_type=ActorType.AGENT,
        agent_role="merchant_copilot",
        merchant_id=MERCHANT,
        capabilities=capabilities,
    )


@dataclass
class Handed:
    bound: BoundSpecialist
    message: SpecialistInput
    turn: TurnContext
    session: CopilotSession


@dataclass
class ScriptedRunner:
    """Stands in for the model layer. ``script`` is called with the turn to act on it."""

    reply: str = "ok"
    script: Any = None
    handback: HandBack | None = None
    delay_s: float = 0.0
    raise_exc: Exception | None = None
    handed: list[Handed] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    async def __call__(
        self,
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        self.handed.append(Handed(bound, message, turn, session))
        self.order.append(f"runner:{bound.specialist.value}")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.script is not None:
            self.script(turn)
        handback = self.handback
        if handback is not None and len(self.handed) > 1:
            handback = None  # hand back once, then answer
        return SpecialistReply(
            text=self.reply, structured={"from": bound.specialist.value}, handback=handback
        )


@dataclass
class SpyToolset:
    """Stands in for the factory. Returns a distinct marker per call and records the binding."""

    calls: list[tuple[Binding, TurnContext]] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def __call__(
        self, bound: Binding, backend: CommerceBackend, turn: TurnContext, session: CopilotSession
    ) -> Sequence[object]:
        del backend, session
        self.calls.append((bound, turn))
        self.order.append(f"tools:{bound.specialist.value}")
        return [object()]


def price_change_decision() -> KernelDecision:
    """The demonstration's refusal: version 1 approved at ₹50.00, milk now ₹62.00."""
    return KernelDecision(
        decision_id=uuid.uuid4(),
        allowed=False,
        code=RecoveryCode.REAPPROVAL_REQUIRED,
        explanation="PRICE_CHANGED",
        checkout=CheckoutRef(uuid.uuid4(), 1, "hash-v1"),
        deltas=(
            Delta("lines[AMUL-DAIRY-001].unit_price_minor", 5000, 6200, "PRICE_CHANGED"),
            Delta("total_minor", 5000, 6200, "TOTAL_CHANGED"),
        ),
        next_version=2,
    )


# ------------------------------------------------------------------- binding


@pytest.mark.parametrize("specialist", list(Specialist))
def test_specialist_principal_is_always_a_subset(specialist: Specialist) -> None:
    harness = buyer_principal()
    binding = bind(harness, specialist)
    assert binding.principal.capabilities <= harness.capabilities
    assert binding.principal.capabilities == ROLE_CAPABILITIES[specialist]
    assert binding.principal.tenant_id == harness.tenant_id
    assert binding.principal.delegation_chain == (harness.principal_id,)
    assert binding.principal.correlation_id == harness.correlation_id


def test_binding_is_the_intersection_when_the_harness_holds_less() -> None:
    narrow = buyer_principal(frozenset({"catalog.search", "basket.update", "order.track"}))
    shopping = bind(narrow, Specialist.SHOPPING).principal.capabilities
    assert shopping == {"catalog.search", "basket.update"}
    support = bind(narrow, Specialist.SUPPORT).principal.capabilities
    assert support == {"order.track"}


def test_widening_is_impossible_by_every_route() -> None:
    """Prove the subset rule by trying to break it, three ways."""
    harness = buyer_principal(frozenset({"catalog.search"}))
    # 1. Through the kernel's own derivation.
    with pytest.raises(ValueError, match="parent lacks"):
        harness.subset_for("shopping", frozenset({"catalog.search", "checkout.submit_approved"}))
    # 2. Through a tampered allowlist naming a Registry B verb and a capability the
    #    harness lacks: the bound principal gains neither.
    tampered = {
        **ROLE_CAPABILITIES,
        Specialist.SHOPPING: frozenset({"catalog.search", "approval.record", "basket.update"}),
    }
    bound = bind(harness, Specialist.SHOPPING, allowlist=tampered).principal
    assert bound.capabilities == {"catalog.search"}
    # 3. The bound principal cannot widen itself either.
    with pytest.raises(ValueError):
        bound.subset_for("child", frozenset({"basket.update"}))


def test_role_capabilities_are_registry_a_and_name_no_money_verb() -> None:
    others = TRUSTED_SURFACE_ACTIONS | KERNEL_INTERNAL_OPERATIONS | TRUSTED_OPERATOR_ACTIONS
    for specialist, capabilities in ROLE_CAPABILITIES.items():
        assert capabilities <= REGISTRY_A_CAPABILITIES, specialist
        assert not capabilities & others, specialist
        for capability in capabilities:
            tokens = set(re.split(r"[._]", capability))
            assert not tokens & FORBIDDEN_TOKENS, capability


# ---------------------------------------------------------------- acceptance


@pytest.mark.asyncio
async def test_buyer_harness_refuses_a_merchant_principal(backend: InMemoryBackend) -> None:
    harness = RazorAI(runner=ScriptedRunner(), tools=SpyToolset())
    with pytest.raises(PrincipalRefusedError):
        await harness.run("s1", merchant_principal(), "I want milk", backend)


@pytest.mark.asyncio
async def test_principal_holding_a_non_registry_a_capability_is_refused(
    backend: InMemoryBackend,
) -> None:
    forged = buyer_principal(REGISTRY_A_CAPABILITIES | {"approval.record"})
    harness = RazorAI(runner=ScriptedRunner(), tools=SpyToolset())
    with pytest.raises(PrincipalRefusedError, match="approval.record"):
        await harness.run("s1", forged, "I want milk", backend)


@pytest.mark.asyncio
async def test_session_cannot_be_continued_by_another_tenant(backend: InMemoryBackend) -> None:
    harness = RazorAI(runner=ScriptedRunner(), tools=SpyToolset())
    await harness.run("s1", buyer_principal(), "I want milk", backend)
    intruder = AgentPrincipal(
        principal_id="agent:buyer-copilot",
        tenant_id=OTHER_TENANT,
        actor_type=ActorType.AGENT,
        buyer_ref="buyer:x",
        capabilities=REGISTRY_A_CAPABILITIES,
    )
    with pytest.raises(SessionRefusedError):
        await harness.run("s1", intruder, "add bread", backend)


# ------------------------------------------------------------ across turns


@pytest.mark.asyncio
async def test_session_tenant_and_correlation_survive_three_turns(
    backend: InMemoryBackend,
) -> None:
    runner = ScriptedRunner()
    harness = RazorAI(runner=runner, tools=SpyToolset())
    principal = buyer_principal()
    texts = ("I want milk", "add two more", "let's pay")
    results = [await harness.run("s1", principal, text, backend) for text in texts]

    assert {r.session_id for r in results} == {"s1"}
    assert {r.tenant_id for r in results} == {TENANT}
    assert {r.correlation_id for r in results} == {CORRELATION}
    # Causation is a chain: each turn points at the one before it.
    assert results[0].causation_id is None
    assert results[1].causation_id == results[0].turn_id
    assert results[2].causation_id == results[1].turn_id
    assert len({r.turn_id for r in results}) == 3
    # Every specialist ran under the same tenant and correlation, on the same session.
    for handed in runner.handed:
        assert handed.bound.principal.tenant_id == TENANT
        assert handed.bound.principal.correlation_id == CORRELATION
        assert handed.session.session_id == "s1"
    session = harness.session("s1")
    assert session is not None and session.turns == 3
    assert len(harness.transcript("s1")) == 3
    assert [r.specialist for r in results] == ["shopping", "shopping", "checkout"]


@pytest.mark.asyncio
async def test_modality_comes_from_context_and_sticks(backend: InMemoryBackend) -> None:
    harness = RazorAI(runner=ScriptedRunner(), tools=SpyToolset())
    principal = buyer_principal()
    first = await harness.run(
        "s1",
        principal,
        "I want milk",
        backend,
        context={"modality": "voice", "transcript": "final"},
    )
    assert first.events[0].data == {"text": "I want milk", "modality": "voice"}
    second = await harness.run("s1", principal, "and bread", backend)
    assert second.structured["session"]["modality"] == Modality.VOICE.value


# --------------------------------------------------------------- refusals


@pytest.mark.asyncio
async def test_interim_transcript_is_refused_before_routing(backend: InMemoryBackend) -> None:
    runner = ScriptedRunner()
    tools = SpyToolset()
    harness = RazorAI(runner=runner, tools=tools)
    result = await harness.run(
        "s1",
        buyer_principal(),
        "pay for",
        backend,
        context={"modality": "voice", "transcript": "interim"},
    )
    assert result.refused is True
    assert result.specialist is None
    assert result.routing_reason == "refused:interim_transcript"
    assert result.corrections == (CORRECTION_INTERIM,)
    assert result.stop_reason == "refused"
    assert runner.handed == [] and tools.calls == []
    assert result.tool_calls == ()


@pytest.mark.asyncio
async def test_unroutable_message_gets_a_clarifying_question_and_no_model(
    backend: InMemoryBackend,
) -> None:
    runner = ScriptedRunner()
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "qwerty zxcv", backend)
    assert result.specialist is None
    assert result.routing_reason == "unroutable"
    assert result.stop_reason == "clarify"
    assert result.reply_text
    assert runner.handed == []


# ------------------------------------------------------- what a specialist gets


@pytest.mark.asyncio
async def test_specialist_receives_bound_principal_factory_tools_and_grounding_first(
    backend: InMemoryBackend,
) -> None:
    order: list[str] = []
    runner = ScriptedRunner(order=order)
    tools = SpyToolset(order=order)

    async def grounding(
        text: str, session: CopilotSession, turn: TurnContext, toolset: Sequence[object]
    ) -> str:
        del session, turn
        order.append("grounding")
        assert toolset is tools_handed[0]
        return f"<fenced prefetch for {text!r}>"

    tools_handed: list[Sequence[object]] = []
    original = tools.__call__

    def capturing(
        bound: Binding, backend: CommerceBackend, turn: TurnContext, session: CopilotSession
    ) -> Sequence[object]:
        built = original(bound, backend, turn, session)
        tools_handed.append(built)
        return built

    harness = RazorAI(runner=runner, tools=capturing, grounding=grounding)
    result = await harness.run("s1", buyer_principal(), "I want milk", backend)

    assert order == ["tools:shopping", "grounding", "runner:shopping"]
    handed = runner.handed[0]
    assert handed.bound.principal.capabilities == ROLE_CAPABILITIES[Specialist.SHOPPING]
    assert handed.bound.principal.principal_id == "agent:buyer-copilot/shopping"
    assert handed.bound.tools is tools_handed[0]  # identity: the factory's list, unchanged
    assert handed.turn.principal is handed.bound.principal
    assert handed.turn is tools.calls[0][1]  # the same TurnContext the tools captured
    assert handed.message.preamble == "<fenced prefetch for 'I want milk'>"
    assert handed.message.language is Language.EN
    assert handed.message.facts["cart_id"] is None
    assert result.specialist == "shopping"


@pytest.mark.asyncio
async def test_language_is_detected_by_the_harness_per_turn(backend: InMemoryBackend) -> None:
    runner = ScriptedRunner()
    harness = RazorAI(runner=runner, tools=SpyToolset())
    principal = buyer_principal()
    hi = await harness.run("s1", principal, "मुझे दूध चाहिए", backend)
    latn = await harness.run("s1", principal, "aur bread bhi chahiye", backend)
    assert (hi.language, latn.language) == (Language.HI, Language.HI_LATN)
    assert [h.turn.language for h in runner.handed] == [Language.HI, Language.HI_LATN]


# ------------------------------------------------- specification 6.1 as code


@pytest.mark.asyncio
async def test_reply_that_drops_a_price_change_is_corrected(backend: InMemoryBackend) -> None:
    decision = price_change_decision()

    def submit(turn: TurnContext) -> None:
        decision_payload(decision, turn)  # what checkout_submit_approved's closure does
        turn.record_call(
            "checkout",
            "checkout_submit_approved",
            {"version": 1},
            ok=True,
            summary={"allowed": False, "code": "REAPPROVAL_REQUIRED"},
        )

    runner = ScriptedRunner(reply="All set, your milk is on its way for ₹50.00.", script=submit)
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "pay now", backend)

    assert CORRECTION_DECISION in result.corrections
    assert "₹62.00" in result.reply_text  # the new price the model left out
    assert "₹50.00" in result.reply_text  # and the approved one it did mention
    assert "AMUL-DAIRY-001" in result.reply_text
    assert recovery_text(RecoveryCode.REAPPROVAL_REQUIRED, Language.EN) in result.reply_text
    assert result.structured["decisions"][0]["deltas"][1] == {
        "field_path": "total_minor",
        "approved": 5000,
        "current": 6200,
        "reason": "TOTAL_CHANGED",
    }


@pytest.mark.asyncio
async def test_reply_that_states_every_delta_is_left_alone(backend: InMemoryBackend) -> None:
    decision = price_change_decision()
    told = (
        "The price of AMUL-DAIRY-001 moved from Rs 50 to Rs 62, so the total is now "
        "₹62.00 instead of ₹50.00. Version 1 is invalidated; version 2 needs approval."
    )
    runner = ScriptedRunner(reply=told, script=lambda turn: decision_payload(decision, turn))
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "pay now", backend)
    assert result.reply_text == told
    assert result.corrections == ()


def test_non_ok_recovery_code_on_a_basket_result_is_restored() -> None:
    turn = TurnContext(language=Language.HI_LATN, principal=buyer_principal())
    turn.record_call(
        "shopping",
        "basket_set_line",
        {"sku": "AMUL-DAIRY-001", "quantity": 2},
        ok=True,
        summary={"code": "RESERVATION_EXPIRED", "total_minor": None},
    )
    reply, corrections = enforce_conversational_rules("Done, added.", [turn], Language.HI_LATN)
    assert corrections == (CORRECTION_RECOVERY,)
    assert recovery_text(RecoveryCode.RESERVATION_EXPIRED, Language.HI_LATN) in reply
    assert reply.startswith("Done, added.")


def test_unavailable_line_the_reply_omits_is_named() -> None:
    turn = TurnContext(language=Language.EN, principal=buyer_principal())
    view = CartView(
        cart_id="bsk",
        code=RecoveryCode.RESERVATION_EXPIRED,
        lines=(("AMUL-DAIRY-001", 2),),
        quote=None,
        unavailable=(UnavailableLine("AMUL-DAIRY-001", 2, 0, True),),
        stale=False,
        provenance=Provenance("merchant-sim", 1),
    )
    turn.ledger.record_cart(view)
    reply, corrections = enforce_conversational_rules("Added to your cart.", [turn], Language.EN)
    assert corrections == (CORRECTION_UNAVAILABLE,)
    assert "AMUL-DAIRY-001" in reply
    # Naming it is enough; the rule is that the buyer sees it, not how.
    reply, corrections = enforce_conversational_rules(
        "AMUL-DAIRY-001 is out of stock.", [turn], Language.EN
    )
    assert corrections == ()


def test_admitted_decision_needs_no_correction() -> None:
    turn = TurnContext(language=Language.EN, principal=buyer_principal())
    turn.decisions.append(
        KernelDecision(uuid.uuid4(), True, RecoveryCode.OK, "OK", grant_id=uuid.uuid4())
    )
    reply, corrections = enforce_conversational_rules(
        "Admitted; not yet paid.", [turn], Language.EN
    )
    assert (reply, corrections) == ("Admitted; not yet paid.", ())


@pytest.mark.asyncio
async def test_ungrounded_amount_is_dropped_and_success_claim_removed(
    backend: InMemoryBackend,
) -> None:
    runner = ScriptedRunner(reply="Your total is ₹999.00. Payment successful! Anything else?")
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "pay now", backend)
    assert CORRECTION_UNGROUNDED in result.corrections
    assert "₹999.00" not in result.reply_text
    assert "successful" not in result.reply_text
    assert result.reply_text == "Anything else?"


@pytest.mark.asyncio
async def test_reply_with_nothing_provable_becomes_the_fallback(backend: InMemoryBackend) -> None:
    runner = ScriptedRunner(reply="Charged ₹999.00.")
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "pay now", backend)
    assert result.reply_text == render_fallback(Language.EN)
    assert result.corrections == (CORRECTION_UNGROUNDED, CORRECTION_FALLBACK)


# ---------------------------------------------------------- the tool-call log


@pytest.mark.asyncio
async def test_tool_call_log_records_every_call_and_denial_in_order(
    backend: InMemoryBackend,
) -> None:
    def script(turn: TurnContext) -> None:
        turn.record_call(
            "shopping", "search", {"query": "milk"}, ok=True, summary={"skus": ["AMUL-DAIRY-001"]}
        )
        turn.record_denial(
            Denial(
                "shopping",
                "checkout_submit_approved",
                "checkout.submit_approved",
                "capability_missing",
                turn.principal.principal_id,
            ),
            {"version": 1},
        )
        turn.record_call(
            "shopping", "product", {"sku": "XX-YY-999"}, ok=False, reason_key="unknown_sku"
        )

    runner = ScriptedRunner(reply="Found milk.", script=script)
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "find milk", backend)

    assert [r.tool for r in result.tool_calls] == ["search", "checkout_submit_approved", "product"]
    assert [d.tool for d in result.denials] == ["checkout_submit_approved"]
    kinds = [(e.type, e.data.get("tool"), e.data.get("status")) for e in result.events]
    assert kinds == [
        ("user_message", None, None),
        ("tool_call", "search", None),
        ("tool_result", "search", "ok"),
        ("tool_call", "checkout_submit_approved", None),
        ("tool_result", "checkout_submit_approved", "blocked"),
        ("tool_call", "product", None),
        ("tool_result", "product", "error"),
        ("text_delta", None, None),
        ("turn_complete", None, None),
    ]
    blocked = result.events[4]
    assert blocked.data["reason"] == "capability_missing"
    assert blocked.data["is_error"] is False  # a gate working is not an error
    # Call ids pair each result with its call and are scoped to the turn.
    assert result.events[1].data["id"] == result.events[2].data["id"] == f"{result.turn_id}:0"
    frame = to_sse(blocked)
    assert frame.startswith("event: tool_result\ndata: {") and frame.endswith("\n\n")
    assert '"status": "blocked"' in frame
    turn = harness.transcript("s1").turns[0]
    assert turn.tool_names == ("search", "checkout_submit_approved", "product")
    assert turn.denied_tools == ("checkout_submit_approved",)


# ----------------------------------------------------------- failure paths


@pytest.mark.asyncio
async def test_timeout_renders_fallback_and_leaves_state_unchanged(
    backend: InMemoryBackend,
) -> None:
    runner = ScriptedRunner(reply="never", delay_s=0.5)
    harness = RazorAI(runner=runner, tools=SpyToolset(), turn_timeout_s=0.01)
    principal = buyer_principal()
    revision = backend.store.revision
    result = await harness.run("s1", principal, "pay now", backend)
    assert result.stop_reason == "timeout"
    assert result.reply_text == render_fallback(Language.EN)
    assert result.corrections == (CORRECTION_FALLBACK,)
    assert backend.submit_calls == 0 and backend.store.revision == revision
    session = harness.session("s1")
    assert session is not None
    assert (session.tenant_id, session.correlation_id, session.turns) == (TENANT, CORRELATION, 1)
    # The next turn continues the same session as if nothing happened.
    again = await harness.run("s1", principal, "pay now", backend)
    assert again.causation_id == result.turn_id


@pytest.mark.asyncio
async def test_runtime_exception_never_escapes_the_harness(backend: InMemoryBackend) -> None:
    runner = ScriptedRunner(raise_exc=RuntimeError("model exploded"))
    harness = RazorAI(runner=runner, tools=SpyToolset())
    result = await harness.run("s1", buyer_principal(), "I want milk", backend)
    assert result.stop_reason == "error"
    assert result.reply_text == render_fallback(Language.EN)


@pytest.mark.asyncio
async def test_misconfiguration_is_loud_not_a_fallback(backend: InMemoryBackend) -> None:
    harness = RazorAI(tools=SpyToolset())  # no runner
    with pytest.raises(HarnessConfigurationError, match="runner="):
        await harness.run("s1", buyer_principal(), "I want milk", backend)


# ---------------------------------------------------------------- hand-back


@pytest.mark.asyncio
async def test_typed_handback_reroutes_once_within_the_turn(backend: InMemoryBackend) -> None:
    runner = ScriptedRunner(
        reply="Here is your order status.", handback=HandBack(Specialist.SUPPORT, "post-purchase")
    )
    tools = SpyToolset()
    harness = RazorAI(runner=runner, tools=tools)
    result = await harness.run("s1", buyer_principal(), "I want to know about my milk", backend)
    assert [h.bound.specialist for h in runner.handed] == [Specialist.SHOPPING, Specialist.SUPPORT]
    assert result.specialist == "support"
    assert result.routing_reason == "handback:shopping->support:post-purchase"
    # Each hop was bound and tooled separately, each as a subset.
    assert [b.specialist for b, _ in tools.calls] == [Specialist.SHOPPING, Specialist.SUPPORT]
    assert tools.calls[1][0].principal.capabilities == ROLE_CAPABILITIES[Specialist.SUPPORT]
    session = harness.session("s1")
    assert session is not None and session.last_specialist == "support"


# -------------------------------------------------------------- session facts


@pytest.mark.asyncio
async def test_session_facts_come_from_records_never_from_prose(backend: InMemoryBackend) -> None:
    def create(turn: TurnContext) -> None:
        turn.record_call("shopping", "basket_create", {}, ok=True, summary={"cart_id": "bsk_real"})

    runner = ScriptedRunner(reply="Your cart id is bsk_fake.", script=create)
    harness = RazorAI(runner=runner, tools=SpyToolset())
    principal = buyer_principal()
    await harness.run("s1", principal, "I want milk", backend)
    session = harness.session("s1")
    assert session is not None and session.cart_id == "bsk_real"
    # The next specialist sees the fact in its dynamic block, and routing sees it too.
    runner.script = None
    again = await harness.run("s1", principal, "ok", backend)
    assert runner.handed[1].message.facts["cart_id"] == "bsk_real"
    assert again.structured["session"]["cart_id"] == "bsk_real"


@pytest.mark.asyncio
async def test_server_context_ids_become_session_facts(backend: InMemoryBackend) -> None:
    harness = RazorAI(runner=ScriptedRunner(), tools=SpyToolset())
    principal = buyer_principal()
    await harness.run("s1", principal, "hi", backend, context={"checkout_id": "chk_9"})
    result = await harness.run("s1", principal, "ok", backend)
    assert result.routing_reason == "session:continuity"
    assert result.structured["session"]["checkout_id"] == "chk_9"

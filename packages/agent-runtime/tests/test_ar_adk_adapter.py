"""The ADK adapter, driven by a scripted model through a real ``Runner`` and the harness.

What these prove, with no network and no key: every tool on every built agent wraps a
closure the factory returned and the gate is the toolset's single callable; no Registry
B, C or D name reaches an agent; the Case agent cannot mutate; the specialist principal
is a subset of the harness principal; a denied call returns a non-empty dict and the tool
never runs; the model is plain text on Vertex or nothing; and -- the hero moment -- a
``REAPPROVAL_REQUIRED`` decision reaches the buyer with every delta, byte for byte from
the deterministic template, both through the agent alone and through the Buyer Copilot
harness with :class:`AdkSpecialistRunner` as its model seam.

Skipped, with the reason stated, where ``google-adk`` is not installed.
"""

from __future__ import annotations

import re
import uuid
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("google.adk", reason="google-adk is not installed in this environment")

from agent_runtime import runtime_adk  # noqa: E402
from agent_runtime.backends import InMemoryBackend, InMemoryTrustedSurface  # noqa: E402
from agent_runtime.capabilities.registry import (  # noqa: E402
    ALL_CAPABILITIES,
    KERNEL_INTERNAL_OPERATIONS,
    TRUSTED_OPERATOR_ACTIONS,
    TRUSTED_SURFACE_ACTIONS,
    capability_for,
)
from agent_runtime.capabilities.tools import (  # noqa: E402
    STATE_BASKET_ID,
    STATE_CHECKOUT_ID,
    BoundToolset,
    build_toolset,
)
from agent_runtime.core.provenance import PROVENANCE_STATE_KEY  # noqa: E402
from agent_runtime.grounding import verify_reply  # noqa: E402
from agent_runtime.harness import BuyerCopilot, Specialist, bind  # noqa: E402
from agent_runtime.harness.base import ROLE_CAPABILITIES  # noqa: E402
from agent_runtime.language import Language  # noqa: E402
from agent_runtime.rendering import render_decision  # noqa: E402
from agent_runtime.rendering.money import display_delta_value  # noqa: E402
from agent_runtime.runtime_adk import adapter  # noqa: E402
from agent_runtime.runtime_adk.adapter import (  # noqa: E402
    DEFAULT_MODEL,
    MODEL_ENV,
    AdkSpecialistRunner,
    BuiltSpecialist,
    SpecialistToolingError,
    VertexNotConfiguredError,
    build_agent,
    build_specialist,
    text_run_config,
    vertex_configured,
)
from agent_runtime.specialists import (  # noqa: E402
    CARD_TOOLS,
    SPECS,
    ActionKind,
    action_for_tool,
    spec_for,
)
from agent_runtime.turn import TurnContext  # noqa: E402
from commerce_domain import Money  # noqa: E402
from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.run_config import StreamingMode  # type: ignore[attr-defined]  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools.function_tool import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402
from merchant_sim import ScenarioController  # noqa: E402
from transaction_kernel import ActorType, AgentPrincipal, RecoveryCode  # noqa: E402

from tests.ar_scripted_model import (  # noqa: E402
    ScriptedModel,
    call,
    echo_field,
    latest_function_response,
    say,
)

MILK = "GRO-DAIRY-001"
TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
FORBIDDEN = TRUSTED_SURFACE_ACTIONS | KERNEL_INTERNAL_OPERATIONS | TRUSTED_OPERATOR_ACTIONS
RUNTIME_ADK_DIR = Path(runtime_adk.__file__).parent
VERTEX_ENV = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")
NON_MUTATING_TOOLS = frozenset(CARD_TOOLS.values())


def _harness(capabilities: frozenset[str] = ALL_CAPABILITIES) -> AgentPrincipal:
    """The Buyer Copilot's principal: the harness holds every Registry A capability."""
    return AgentPrincipal(
        principal_id="agent:buyer-copilot",
        tenant_id=TENANT,
        actor_type=ActorType.AGENT,
        agent_role="buyer_copilot",
        capabilities=capabilities,
        buyer_ref="buyer-1",
    )


def _build(
    name: str,
    backend: InMemoryBackend,
    prompts_dir: Path,
    *,
    model: ScriptedModel | None = None,
    harness: AgentPrincipal | None = None,
    turn: TurnContext | None = None,
) -> BuiltSpecialist:
    return build_specialist(
        spec_for(name),
        harness or _harness(),
        backend,
        turn=turn,
        session_id="s1",
        model=model or ScriptedModel(),
        prompts_dir=prompts_dir,
    )


async def _run(built: BuiltSpecialist, message: str, state: Mapping[str, Any] | None = None) -> str:
    """One text turn through ADK's runner; the agent's text parts, joined."""
    service = InMemorySessionService()
    await service.create_session(
        app_name="ar-test", user_id="buyer", session_id="s1", state=dict(state or {})
    )
    runner = Runner(app_name="ar-test", agent=built.agent, session_service=service)
    texts: list[str] = []
    async for event in runner.run_async(
        user_id="buyer",
        session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        run_config=text_run_config(),
    ):
        if event.author != built.agent.name or event.content is None:
            continue
        texts.extend(part.text for part in event.content.parts or [] if part.text)
    return "\n".join(texts)


def _system_text(request_config: Any) -> str:
    instruction = getattr(request_config, "system_instruction", None)
    if instruction is None:
        return ""
    if isinstance(instruction, str):
        return instruction
    if isinstance(instruction, types.Content):
        return "".join(part.text or "" for part in instruction.parts or [])
    return str(instruction)


async def _approved_checkout_with_moved_price(
    backend: InMemoryBackend, surface: InMemoryTrustedSurface, scenario: ScenarioController
) -> tuple[str, Any]:
    basket = await backend.basket_create()
    await backend.basket_set_line(basket.basket_id, MILK, 2)
    card = await backend.checkout_create(basket.basket_id)
    surface.approve(
        card.checkout_id, card.version, content_hash=card.content_hash, total_minor=card.total.minor
    )
    milk = await backend.product(MILK)
    scenario.set_price(MILK, Money(milk.unit_price.minor + 700, "INR"))
    return basket.basket_id, card


def _assert_every_delta_told(reply: str, decision: Any) -> None:
    assert decision.allowed is False
    assert decision.code is RecoveryCode.REAPPROVAL_REQUIRED
    assert decision.deltas, "a material change carries at least one delta"
    assert any(d.field_path == f"lines[{MILK}].unit_price_minor" for d in decision.deltas)
    for delta in decision.deltas:
        assert delta.field_path in reply
        assert display_delta_value(delta.field_path, delta.approved, "INR") in reply
        assert display_delta_value(delta.field_path, delta.current, "INR") in reply
    assert decision.checkout is not None
    assert str(decision.checkout.version) in reply and str(decision.next_version) in reply


# --------------------------------------------------------------------------- factory-only


def test_every_tool_on_every_agent_wraps_a_factory_closure(
    monkeypatch: pytest.MonkeyPatch, backend: InMemoryBackend, tmp_path: Path
) -> None:
    produced: list[BoundToolset] = []
    real = build_toolset

    def spy(*args: Any, **kwargs: Any) -> BoundToolset:
        toolset = real(*args, **kwargs)
        produced.append(toolset)
        return toolset

    monkeypatch.setattr(adapter, "build_toolset", spy)
    for spec in SPECS:
        built = _build(spec.name, backend, tmp_path)
        assert isinstance(built.agent, LlmAgent)
        assert built.agent.name == spec.name
        assert built.toolset is produced[-1]
        assert list(built.agent.tools) == list(built.tools)
        assert len(built.tools) == len(built.toolset)
        for tool, bound in zip(built.tools, built.toolset, strict=True):
            assert isinstance(tool, FunctionTool)
            assert tool.name == bound.name
            assert tool.func is bound.func, f"{tool.name} does not wrap the factory closure"
            assert tool.name in spec.tool_names, f"{tool.name} is off {spec.name}'s roster"


def test_a_tool_the_spec_does_not_list_is_refused_not_filtered(backend: InMemoryBackend) -> None:
    binding = bind(_harness(), Specialist.SHOPPING)
    turn = TurnContext(language=Language.EN, principal=binding.principal, agent_name="shopping")
    toolset = build_toolset(binding, backend, turn, session_id="s1", agent_name="shopping")
    checkout_spec = spec_for("checkout")
    with pytest.raises(SpecialistToolingError, match="binding is for"):
        build_specialist(checkout_spec, binding, toolset=toolset, model=ScriptedModel())


def test_gate_is_the_toolsets_single_callable(backend: InMemoryBackend, tmp_path: Path) -> None:
    for spec in SPECS:
        built = _build(spec.name, backend, tmp_path)
        gate: object = built.agent.before_tool_callback
        error_gate: object = built.agent.on_tool_error_callback
        assert callable(gate) and not isinstance(gate, list)
        assert gate is built.toolset.gate
        assert error_gate is built.toolset.error_gate


# --------------------------------------------------------------------------- registries


def test_no_registry_b_c_or_d_name_reaches_any_agent(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    for spec in SPECS:
        built = _build(spec.name, backend, tmp_path)
        for tool in built.tools:
            assert tool.name not in FORBIDDEN
            capability = capability_for(tool.name)
            assert capability is not None, f"{tool.name} has no Registry A row"
            assert capability.value not in FORBIDDEN
            assert built.principal.can(capability.value)
        assert not (set(spec.actions) & FORBIDDEN)


def test_case_agent_holds_no_mutating_tool(backend: InMemoryBackend, tmp_path: Path) -> None:
    built = _build("case_specialist", backend, tmp_path)
    assert built.spec.is_read_only
    for tool in built.tools:
        if tool.name in NON_MUTATING_TOOLS:
            continue
        action = action_for_tool(tool.name)
        assert action is not None and action.kind is ActionKind.READ, tool.name


# --------------------------------------------------------------------------- binding


def test_specialist_principal_is_a_subset_of_the_harness(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    harness = _harness()
    for spec in SPECS:
        built = _build(spec.name, backend, tmp_path, harness=harness)
        assert built.principal.capabilities <= harness.capabilities
        assert built.principal.capabilities == spec.capabilities & harness.capabilities
        assert built.principal.principal_id == f"{harness.principal_id}/{spec.role}"
        assert harness.principal_id in built.principal.delegation_chain
        assert built.principal.tenant_id == harness.tenant_id
    with pytest.raises(ValueError, match="gain capabilities"):
        harness.subset_for("rogue", frozenset({"approval.record"}))


def test_a_narrowed_harness_narrows_the_specialist_and_its_tools(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    narrowed = _harness(ALL_CAPABILITIES - {"catalog.search"})
    built = _build("shopping_specialist", backend, tmp_path, harness=narrowed)
    assert not built.principal.can("catalog.search")
    assert "search" not in built.toolset.names
    assert "search" not in {tool.name for tool in built.tools}
    assert built.principal.capabilities == ROLE_CAPABILITIES[Specialist.SHOPPING] - {
        "catalog.search"
    }


@pytest.mark.asyncio
async def test_denied_tool_returns_a_nonempty_dict_and_never_runs(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    """The per-turn budget is the gate's own refusal: one call allowed, the second denied."""
    binding = bind(_harness(), Specialist.SHOPPING)
    turn = TurnContext(
        language=Language.EN, principal=binding.principal, max_tool_calls=1, agent_name="shopping"
    )
    model = ScriptedModel(
        steps=[call("search", query="milk"), call("search", query="bread"), say("Two searches.")]
    )
    built = build_specialist(
        spec_for("shopping"),
        binding,
        backend,
        turn=turn,
        session_id="s1",
        model=model,
        prompts_dir=tmp_path,
    )

    reply = await _run(built, "milk and bread please")

    assert reply == "Two searches."
    assert len(model.requests) == 3
    first = latest_function_response(model.requests[1])
    assert first is not None and "denied" not in first and first.get("allowed_skus")
    denied = latest_function_response(model.requests[2])
    assert denied, "a denial must be a NON-EMPTY dict; ADK treats {} as None and runs the tool"
    assert denied["denied"] is True
    assert denied["reason_key"] == "tool_budget_exhausted"
    assert denied["tool"] == "search"
    assert len(turn.denials) == 1
    assert [record.denied for record in turn.tool_calls] == [False, True]


@pytest.mark.asyncio
async def test_a_tool_the_model_was_never_given_is_an_error_result_not_a_crash(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    narrowed = _harness(ALL_CAPABILITIES - {"catalog.search"})
    model = ScriptedModel(steps=[call("search", query="milk"), say("I cannot search.")])
    built = _build("shopping_specialist", backend, tmp_path, model=model, harness=narrowed)
    reply = await _run(built, "milk")
    assert reply == "I cannot search."
    response = latest_function_response(model.requests[1])
    assert response and response["denied"] is True
    assert built.turn.tool_failures and built.turn.tool_failures[0]["tool"] == "search"


# --------------------------------------------------------------------------- the hero moment


@pytest.mark.asyncio
async def test_reapproval_required_reaches_the_buyer_with_every_delta(
    backend: InMemoryBackend,
    surface: InMemoryTrustedSurface,
    scenario: ScenarioController,
    tmp_path: Path,
) -> None:
    basket_id, card = await _approved_checkout_with_moved_price(backend, surface, scenario)
    # The model reads the checkout first, as the prompt and the grounding rule require:
    # that read is what puts the checkout's hashes into session provenance.
    model = ScriptedModel(
        steps=[
            call("checkout_get"),
            call("checkout_submit_approved", version=card.version, content_hash=card.content_hash),
            echo_field("rendered_for_buyer"),
        ]
    )
    built = _build("checkout_specialist", backend, tmp_path, model=model)
    state = {STATE_BASKET_ID: basket_id, STATE_CHECKOUT_ID: card.checkout_id}

    reply = await _run(built, "go ahead and submit", state)

    assert len(built.turn.decisions) == 1
    decision = built.turn.decisions[0]
    assert decision.next_version == card.version + 1
    _assert_every_delta_told(reply, decision)
    assert reply == render_decision(decision, Language.EN)

    payload = latest_function_response(model.requests[2])
    assert payload is not None and payload["code"] == "REAPPROVAL_REQUIRED"
    assert len(payload["deltas"]) == len(decision.deltas)
    assert payload["include_rendered_verbatim"] is True

    check = verify_reply(reply, built.turn.ledger)
    assert not check.rewritten and check.ungrounded_amounts_minor == ()

    view = await backend.checkout_get(card.checkout_id)
    assert [v.status.value for v in view.versions] == ["INVALIDATED", "PENDING_APPROVAL"]
    assert view.payment is None, "nothing was charged"


@pytest.mark.asyncio
async def test_submit_without_a_session_read_is_held_by_provenance(
    backend: InMemoryBackend,
    surface: InMemoryTrustedSurface,
    scenario: ScenarioController,
    tmp_path: Path,
) -> None:
    """A submit for a checkout no tool returned this session is held: the kernel never sees it."""
    basket_id, card = await _approved_checkout_with_moved_price(backend, surface, scenario)
    model = ScriptedModel(
        steps=[
            call("checkout_submit_approved", version=card.version, content_hash=card.content_hash),
            say("held"),
        ]
    )
    built = _build("checkout_specialist", backend, tmp_path, model=model)
    state = {STATE_BASKET_ID: basket_id, STATE_CHECKOUT_ID: card.checkout_id}

    await _run(built, "submit now", state)

    response = latest_function_response(model.requests[1])
    assert response and response.get("ok") is False
    assert response.get("blocked") == "provenance"
    assert built.turn.decisions == []
    assert backend.submit_calls == 0, "the backend was never asked"
    view = await backend.checkout_get(card.checkout_id)
    assert [v.status.value for v in view.versions] == ["APPROVED"], "state untouched"


@pytest.mark.asyncio
async def test_reapproval_through_the_buyer_copilot_harness(
    backend: InMemoryBackend,
    surface: InMemoryTrustedSurface,
    scenario: ScenarioController,
    tmp_path: Path,
) -> None:
    """Harness -> AdkSpecialistRunner -> ADK -> factory tool -> kernel decision -> buyer."""
    basket_id, card = await _approved_checkout_with_moved_price(backend, surface, scenario)
    model = ScriptedModel(
        steps=[
            call("checkout_get"),
            call("checkout_submit_approved", version=card.version, content_hash=card.content_hash),
            echo_field("rendered_for_buyer"),
        ]
    )
    runner = AdkSpecialistRunner(model=model, prompts_dir=tmp_path, require_vertex=False)
    copilot = BuyerCopilot(runner=runner)

    result = await copilot.run(
        "sess-1",
        _harness(),
        "please submit my approved checkout",
        backend,
        context={"checkout_id": card.checkout_id, "basket_id": basket_id},
    )

    assert result.specialist == "checkout"
    assert result.stop_reason == "end_turn"
    submitted = [r for r in result.tool_calls if r.tool == "checkout_submit_approved"]
    assert len(submitted) == 1 and submitted[0].ok
    assert submitted[0].summary["code"] == "REAPPROVAL_REQUIRED"
    assert result.structured["runtime"] == "google-adk"
    assert result.structured["prompt_source"] == "fallback"
    decision_turns = [t for t in result.tool_calls if t.tool == "checkout_submit_approved"]
    assert decision_turns
    session = copilot.session("sess-1")
    assert session is not None
    assert session.checkout_version == card.version + 1
    assert PROVENANCE_STATE_KEY in session.state, "provenance came back from the model session"
    # The template reached the buyer with every delta; the harness would have restored it
    # had the model dropped one, and reports that it did not have to.
    assert "decision_deltas_restored" not in result.corrections
    assert str(card.version + 1) in result.reply_text


# --------------------------------------------------------------------------- prompt and config


@pytest.mark.asyncio
async def test_static_instruction_carries_the_prompt_and_the_fence_notice(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    model = ScriptedModel(steps=[say("hello")])
    built = _build("shopping_specialist", backend, tmp_path, model=model)
    assert built.prompt.source == "fallback"
    await _run(built, "hi")
    request = model.requests[0]
    system = _system_text(request.config)
    assert built.spec.fallback_instruction.strip() in system
    assert "<merchant_data>" in system
    declared = {
        declaration.name
        for tool in request.config.tools or []
        for declaration in getattr(tool, "function_declarations", None) or []
    }
    assert declared == {tool.name for tool in built.tools}
    assert not any(name in {"tenant_id", "session_id", "basket_id"} for name in declared)


def test_prompt_file_is_installed_when_present(backend: InMemoryBackend, tmp_path: Path) -> None:
    (tmp_path / "shopping_specialist.md").write_text("# Gemini's prompt\n\nBe helpful.\n")
    built = _build("shopping_specialist", backend, tmp_path)
    assert built.prompt.source == "file"
    assert str(built.agent.static_instruction).startswith("# Gemini's prompt")


def test_text_only_and_no_transfer(backend: InMemoryBackend, tmp_path: Path) -> None:
    for spec in SPECS:
        agent = _build(spec.name, backend, tmp_path).agent
        config = agent.generate_content_config
        assert config is not None and config.response_modalities == ["TEXT"]
        assert agent.sub_agents == []
        assert agent.disallow_transfer_to_parent and agent.disallow_transfer_to_peers
    assert text_run_config().streaming_mode is StreamingMode.NONE
    for path in RUNTIME_ADK_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for banned in ("run_live", "require_confirmation", "AUDIO", "speech_config"):
            assert banned not in source, f"{path.name} mentions {banned}"


def test_a_real_model_needs_the_vertex_environment(
    monkeypatch: pytest.MonkeyPatch, backend: InMemoryBackend, tmp_path: Path
) -> None:
    for name in (*VERTEX_ENV, MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    assert not vertex_configured()
    with pytest.raises(VertexNotConfiguredError):
        build_agent(spec_for("shopping"), _harness(), backend, prompts_dir=tmp_path)

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    assert vertex_configured()
    agent = build_agent(spec_for("shopping"), _harness(), backend, prompts_dir=tmp_path)
    assert agent.model == DEFAULT_MODEL == "gemini-3.8-flash"

    monkeypatch.setenv(MODEL_ENV, "gemini-3.8-flash-lite")
    agent = build_agent(spec_for("shopping"), _harness(), backend, prompts_dir=tmp_path)
    assert agent.model == "gemini-3.8-flash-lite"
    assert not vertex_configured({"GOOGLE_GENAI_USE_VERTEXAI": "true"})


def test_unbuilt_roster_tools_are_reported_not_faked(
    backend: InMemoryBackend, tmp_path: Path
) -> None:
    """The factory does not build every roster tool yet; the gap is visible, never papered over."""
    report: list[str] = []
    for spec in SPECS:
        built = _build(spec.name, backend, tmp_path)
        offered = {tool.name for tool in built.tools}
        assert set(built.unbuilt) <= set(spec.tool_names)
        assert not (offered & set(built.unbuilt))
        assert offered | set(built.unbuilt) == set(spec.tool_names)
        assert set(built.toolset.unbuilt) <= set(built.unbuilt)
        if built.unbuilt:
            report.append(f"{spec.name}: {', '.join(built.unbuilt)}")
    if report:
        warnings.warn(
            "roster tools no factory builder exists for yet -- " + "; ".join(report),
            stacklevel=1,
        )


def test_runtime_adk_is_the_only_package_importing_the_model_runtime() -> None:
    package_root = RUNTIME_ADK_DIR.parent
    offenders: list[str] = []
    for path in package_root.rglob("*.py"):
        if RUNTIME_ADK_DIR in path.parents:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+google\.(adk|genai)", source, re.MULTILINE):
            offenders.append(str(path.relative_to(package_root)))
    assert offenders == [], offenders

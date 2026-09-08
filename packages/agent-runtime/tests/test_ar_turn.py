"""``run_turn``: the one entry point, and the boundary it keeps.

Three properties. The harness is chosen from the principal the server minted, so a body
cannot pick one. The harness layer imports nothing from ``google.*`` (ADR 0004 gate 25):
a source scan, because an import boundary that is only a convention is not one. And the
seams fail loudly when nothing is behind them, so a misconfigured deployment cannot
answer a buyer with a fallback sentence forever.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from agent_runtime import turn as turn_module
from agent_runtime.backends import InMemoryBackend
from agent_runtime.capabilities.tools import BoundToolset
from agent_runtime.harness import (
    BUYER_SPECIALISTS,
    REGISTRY_A_CAPABILITIES,
    ROLE_CAPABILITIES,
    BoundSpecialist,
    CopilotSession,
    HarnessConfigurationError,
    PrincipalRefusedError,
    RazorAI,
    Specialist,
    SpecialistInput,
    SpecialistReply,
)
from agent_runtime.turn import Copilots, TurnContext, copilots, run_turn
from commerce_domain import ActorType, AgentPrincipal

TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
MERCHANT = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def buyer_principal() -> AgentPrincipal:
    return AgentPrincipal(
        principal_id="agent:buyer-copilot",
        tenant_id=TENANT,
        actor_type=ActorType.AGENT,
        agent_role="razorai",
        buyer_ref="buyer:1",
        capabilities=REGISTRY_A_CAPABILITIES,
    )


def merchant_principal() -> AgentPrincipal:
    return AgentPrincipal(
        principal_id="agent:merchant-copilot",
        tenant_id=TENANT,
        actor_type=ActorType.AGENT,
        agent_role="merchant_copilot",
        merchant_id=MERCHANT,
        capabilities=REGISTRY_A_CAPABILITIES,
    )


async def echo_runner(
    bound: BoundSpecialist, message: SpecialistInput, turn: TurnContext, session: CopilotSession
) -> SpecialistReply:
    del turn, session
    return SpecialistReply(text=f"{bound.specialist.value} heard: {message.text}")


def no_tools(bound: object, backend: object, turn: object, session: object) -> list[object]:
    del bound, backend, turn, session
    return []


@pytest.fixture
def pair() -> Copilots:
    return Copilots(runner=echo_runner, tools=no_tools)


# ---------------------------------------------------------------- the choice


@pytest.mark.asyncio
async def test_run_turn_chooses_the_harness_from_the_principal(
    backend: InMemoryBackend, pair: Copilots
) -> None:
    buyer = await run_turn("s-b", buyer_principal(), "I want milk", backend, pair=pair)
    assert buyer.specialist in {s.value for s in BUYER_SPECIALISTS}
    assert buyer.reply_text == "shopping heard: I want milk"
    assert isinstance(pair.buyer, RazorAI)


@pytest.mark.asyncio
async def test_a_buyer_principal_cannot_reach_the_merchant_harness(
    backend: InMemoryBackend, pair: Copilots
) -> None:
    # Through the chooser: a buyer principal lands on the buyer harness every time.
    assert pair.for_principal(buyer_principal()) is pair.buyer
    # Directly: a merchant principal is refused by the buyer harness.
    with pytest.raises(PrincipalRefusedError):
        await pair.buyer.run("s-x", merchant_principal(), "how are sales", backend)
    with pytest.raises(PrincipalRefusedError):
        await run_turn("s-m", merchant_principal(), "how are sales", backend, pair=pair)


def test_process_wide_pair_is_built_once_and_can_be_reset() -> None:
    first = copilots(reset=True, runner=echo_runner, tools=no_tools)
    assert copilots() is first
    second = copilots(reset=True, runner=echo_runner, tools=no_tools)
    assert second is not first
    assert copilots() is second


# ------------------------------------------------------------------- seams


@pytest.mark.asyncio
async def test_no_runtime_configured_raises_rather_than_answering(backend: InMemoryBackend) -> None:
    pair = Copilots(tools=no_tools)
    with pytest.raises(HarnessConfigurationError):
        await run_turn("s", buyer_principal(), "I want milk", backend, pair=pair)


@pytest.mark.asyncio
async def test_default_tools_come_from_the_factory_bound_to_the_specialist(
    backend: InMemoryBackend,
) -> None:
    """With no ``tools=`` given, every specialist's tools are the factory's, with its gate."""
    handed: list[BoundSpecialist] = []

    async def capture(
        bound: BoundSpecialist, message: SpecialistInput, turn: TurnContext, session: CopilotSession
    ) -> SpecialistReply:
        del message, turn, session
        handed.append(bound)
        return SpecialistReply(text="ok")

    pair = Copilots(runner=capture)
    await run_turn("s", buyer_principal(), "I want milk", backend, pair=pair)
    toolset = handed[0].tools
    assert isinstance(toolset, BoundToolset)
    assert toolset.principal is handed[0].principal  # the bound one, not the harness's
    assert toolset.principal.capabilities == ROLE_CAPABILITIES[Specialist.SHOPPING]
    assert callable(toolset.gate) and callable(toolset.error_gate)
    assert "search" in toolset.names and "checkout_submit_approved" not in toolset.names


# ---------------------------------------------------------- import boundary

_HARNESS_DIR = Path(turn_module.__file__).parent / "harness"
_GOOGLE_IMPORT = re.compile(r"^\s*(?:from|import)\s+google\b", re.MULTILINE)


@pytest.mark.parametrize(
    "path", [*sorted(_HARNESS_DIR.glob("*.py")), Path(turn_module.__file__)], ids=lambda p: p.name
)
def test_harness_layer_imports_no_google_module(path: Path) -> None:
    assert not _GOOGLE_IMPORT.search(path.read_text(encoding="utf-8")), path.name


# ------------------------------------------------------------- the result


@pytest.mark.asyncio
async def test_result_carries_routing_and_session_facts_as_structure(
    backend: InMemoryBackend, pair: Copilots
) -> None:
    result = await run_turn(
        "s-b", buyer_principal(), "let's pay", backend, pair=pair, context={"checkout_id": "chk_1"}
    )
    assert result.structured["routing"] == {
        "specialist": "checkout",
        "reason": "context:checkout_id",
    }
    assert result.structured["session"]["checkout_id"] == "chk_1"
    assert result.structured["decisions"] == []
    assert result.structured["corrections"] == []
    assert result.events[-1].type == "turn_complete"
    assert result.events[-1].data["specialist"] == "checkout"

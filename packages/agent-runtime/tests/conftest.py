"""Fixtures for the agent-runtime suite.

Everything here is deterministic and in-process. No database, no network and no model
call: the behaviours under test are the ones that must hold *before* a model is involved,
so a test that needed a model to run would be testing the wrong thing.

``asyncio_mode`` is not set repository-wide -- this is the first async suite in the
project -- so async tests carry ``pytest.mark.asyncio`` explicitly rather than relying on
a global setting that a later package would inherit without asking for it.
"""

from __future__ import annotations

import uuid

import pytest
from agent_runtime.backends import InMemoryBackend, InMemoryTrustedSurface
from agent_runtime.core import SessionProvenance
from merchant_sim import MerchantStore, ScenarioController
from transaction_kernel import ActorType, AgentPrincipal

#: A stock SKU with a non-zero price and no tax, so a changed price moves the total by
#: exactly the amount the test changed. Taken from the merchant-sim fixture catalogue.
MILK_SKU = "AMUL-DAIRY-001"


@pytest.fixture
def store() -> MerchantStore:
    """A fresh merchant with the fixture catalogue at revision 0."""
    return MerchantStore()


@pytest.fixture
def backend(store: MerchantStore) -> InMemoryBackend:
    return InMemoryBackend(store)


@pytest.fixture
def surface(backend: InMemoryBackend) -> InMemoryTrustedSurface:
    """Registry B. Held by the test, never by anything that stands in for an agent."""
    return InMemoryTrustedSurface(backend)


@pytest.fixture
def scenario(store: MerchantStore) -> ScenarioController:
    """Moves merchant state under a checkout, which is how a refusal is provoked."""
    return ScenarioController(store)


@pytest.fixture
def provenance() -> SessionProvenance:
    """An empty session record: every write is held until a read grounds it."""
    return SessionProvenance()


@pytest.fixture
def principal() -> AgentPrincipal:
    return AgentPrincipal(
        principal_id="agent:test",
        tenant_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        actor_type=ActorType.AGENT,
        agent_role="shopping",
        capabilities=frozenset({"catalog.search", "catalog.get_product"}),
    )

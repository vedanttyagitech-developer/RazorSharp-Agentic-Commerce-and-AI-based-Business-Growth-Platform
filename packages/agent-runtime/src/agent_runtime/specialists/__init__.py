"""The specialists, as runtime-agnostic data.

Two harnesses route to them: the RazorAI, to shopping, checkout and support, and the
Merchant Copilot, to operations. Only the
specialists are models. Nothing in this package imports a model runtime: ``runtime_adk/``
is the one adapter that turns a :class:`SpecialistSpec` into an ``LlmAgent``, and a source
test proves the boundary.

Three tables must agree on what each specialist may hold, and a test proves they do:
``SpecialistSpec.capabilities`` here, ``AGENT_ALLOWLIST`` in ``capabilities/registry.py``
and ``ROLE_CAPABILITIES`` in ``harness/base.py``.

The merchant surface had two more -- Growth and Case -- and they were removed with the
merchant copilot they answered on. Operations is the first back, and it declares its
audience the same way the buyer three do: ``Surface.MERCHANT``, which is what decides
which party the fence treats as untrusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from . import checkout, operations, shopping, support
from ._spec import (
    ACTIONS,
    CARD_TOOLS,
    Action,
    ActionKind,
    SpecialistSpec,
    Surface,
    action_for_tool,
)

__all__ = [
    "ACTIONS",
    "BUYER_SPECIALISTS",
    "MERCHANT_SPECIALISTS",
    "CARD_TOOLS",
    "SPECS",
    "SPECS_BY_NAME",
    "SPECS_BY_ROLE",
    "Action",
    "ActionKind",
    "SpecialistSpec",
    "Surface",
    "action_for_tool",
    "spec_for",
]

#: Every specialist this runtime ships, in roster order.
SPECS: Final[tuple[SpecialistSpec, ...]] = (
    shopping.SPEC,
    checkout.SPEC,
    support.SPEC,
    operations.SPEC,
)

BUYER_SPECIALISTS: Final[tuple[SpecialistSpec, ...]] = tuple(
    spec for spec in SPECS if spec.surface is Surface.BUYER
)

#: Derived from the declared surface rather than listed by hand, so a specialist cannot be
#: added to one copilot's roster while declaring itself to be on the other's.
MERCHANT_SPECIALISTS: Final[tuple[SpecialistSpec, ...]] = tuple(
    spec for spec in SPECS if spec.surface is Surface.MERCHANT
)

SPECS_BY_NAME: Final[Mapping[str, SpecialistSpec]] = MappingProxyType(
    {spec.name: spec for spec in SPECS}
)
SPECS_BY_ROLE: Final[Mapping[str, SpecialistSpec]] = MappingProxyType(
    {spec.role: spec for spec in SPECS}
)


def spec_for(name_or_role: str) -> SpecialistSpec:
    """Look a specialist up by its name (``shopping_specialist``) or role (``shopping``)."""
    spec = SPECS_BY_NAME.get(name_or_role) or SPECS_BY_ROLE.get(name_or_role)
    if spec is None:
        raise KeyError(f"no specialist named {name_or_role!r}")
    return spec

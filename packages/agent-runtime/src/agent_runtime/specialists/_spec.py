"""What a specialist is, as data: its roster actions, rules, gates and cards.

A :class:`SpecialistSpec` is the whole runtime-agnostic description of one specialist.
The ADK adapter reads it to build an ``LlmAgent``; the harness binds a principal to the
same roster through its own allowlist; the tests prove the three agree. It holds no
model, no prompt text beyond a built-in fallback, and no identity: tenant, session and
principal are the harness's and never appear here (specification 20.2).

Actions are named as the roster and Registry A name them (``basket.update``), which is
also the capability string a principal must hold. Each action maps to at most one
model-facing function name (``basket_set_line``), the name the factory in
``capabilities/tools.py`` builds; ``order.propose_cancel`` and ``refund.propose`` are
proposals made in conversation and have no tool, so their function name is ``None``. The
mapping is a frozen table: a new action is a reviewed row, never a default.

Approve, pay, refund, revoke, cancel and capture are absent from :data:`ACTIONS` by
construction. ``__post_init__`` refuses a spec that names an action the table lacks, so
no specialist can be declared with a verb the platform never gave an agent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..core.grounding_rules import GroundingRule

__all__ = [
    "ACTIONS",
    "CARD_TOOLS",
    "Action",
    "ActionKind",
    "SpecialistSpec",
    "Surface",
    "action_for_tool",
]


class ActionKind(StrEnum):
    """What an action does to platform state. None of them moves money."""

    #: Reads state. Safe to prefetch, safe to force, safe to repeat.
    READ = "read"
    #: Changes platform state through the backend (a basket line, a checkout version, a
    #: plan or case record). Guarded by provenance gates and the session write lock.
    WRITE = "write"
    #: Records a proposal for a human to act on. Changes nothing the proposal describes.
    PROPOSE = "propose"


class Surface(StrEnum):
    """Which fence applies to third-party text the specialist reads (``core/fencing.py``)."""

    #: Buyer side: merchant catalogue text is the untrusted party.
    BUYER = "buyer"
    #: Merchant side: buyer messages and pasted text are the untrusted party.
    MERCHANT = "merchant"


@dataclass(frozen=True, slots=True)
class Action:
    """One Registry A action: its roster name, its function name, and the gates on it.

    ``gates`` names the pure checks in ``core/provenance.py`` the tool closure runs
    before the backend. They are named here so a spec can be tested for "every write is
    gated" without importing the gate implementation.
    """

    name: str
    tool_name: str | None
    kind: ActionKind
    gates: tuple[str, ...] = ()

    @property
    def mutates(self) -> bool:
        return self.kind is not ActionKind.READ


#: Gate names, as ``core/provenance.py`` exposes them. Strings, so this table is data.
GATE_SKU_PROVENANCE: Final[str] = "sku_provenance"
GATE_CHECKOUT_PROVENANCE: Final[str] = "checkout_provenance"
GATE_ORDER_PROVENANCE: Final[str] = "order_provenance"
GATE_PROPOSAL_GUARDRAILS: Final[str] = "proposal_guardrails"
GATE_QUANTITY: Final[str] = "quantity"
GATE_LINE_COUNT: Final[str] = "line_count"
GATE_WRITE_LOCK: Final[str] = "write_lock"


def _read(name: str, tool_name: str) -> Action:
    return Action(name, tool_name, ActionKind.READ)


def _write(name: str, tool_name: str, *gates: str) -> Action:
    return Action(name, tool_name, ActionKind.WRITE, (*gates, GATE_WRITE_LOCK))


def _propose(name: str, tool_name: str | None, *gates: str) -> Action:
    return Action(name, tool_name, ActionKind.PROPOSE, gates)


_ACTION_ROWS: Final[tuple[Action, ...]] = (
    # --- catalogue and basket (shopping) -------------------------------------
    _read("catalog.search", "search"),
    _read("catalog.get_product", "product"),
    _read("inventory.check", "inventory_check"),
    _write("basket.create", "basket_create"),
    _write("basket.update", "basket_set_line", GATE_SKU_PROVENANCE, GATE_QUANTITY, GATE_LINE_COUNT),
    _propose("basket.propose_line", "basket_propose_line", GATE_SKU_PROVENANCE, GATE_QUANTITY),
    _read("quote.request", "basket_get"),
    _write("reservation.request", "reservation_request"),
    # --- checkout ---------------------------------------------------------------
    _write("checkout.submit_for_approval", "checkout_create"),
    _write("checkout.submit_approved", "checkout_submit_approved", GATE_CHECKOUT_PROVENANCE),
    _read("checkout.read", "checkout_get"),
    _read("order.track", "order_track"),
    # Proposals made in conversation: no tool, no backend method, nothing to execute.
    _propose("order.propose_cancel", None, GATE_ORDER_PROVENANCE),
    _propose("refund.propose", None, GATE_ORDER_PROVENANCE),
    # --- support ----------------------------------------------------------------
    _read("policy.search", "policy_search"),
    _write("resolution.evaluate", "resolution_evaluate", GATE_ORDER_PROVENANCE),
    _write("support.escalate", "support_escalate", GATE_ORDER_PROVENANCE),
    # --- merchant ---------------------------------------------------------------
)

#: Roster action name -> Action. The closed set of everything any specialist may hold.
ACTIONS: Final[Mapping[str, Action]] = MappingProxyType({row.name: row for row in _ACTION_ROWS})

_TOOL_TO_ACTION: Final[Mapping[str, Action]] = MappingProxyType(
    {row.tool_name: row for row in _ACTION_ROWS if row.tool_name is not None}
)

#: Card kind -> the ``present_*`` tool that selects it (ADR 0004 section 1.7). A present
#: tool takes ids only; the card is built from the turn ledger by ``rendering/cards.py``.
CARD_TOOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "product": "present_products",
        "basket": "present_basket",
        "approval": "present_approval",
        "decision": "present_decision",
        "plan": "present_plan",
    }
)


def action_for_tool(tool_name: str) -> Action | None:
    """The action behind a model-facing function name, or None for a name no row has."""
    return _TOOL_TO_ACTION.get(tool_name)


@dataclass(frozen=True, slots=True)
class SpecialistSpec:
    """The runtime-agnostic description of one specialist (ADR 0004 section 5, ``specs``).

    ``name`` is the agent name and the prompt basename (``shopping_specialist``); ``role``
    is the short role the registry allowlist, the harness router and the principal's
    delegation chain all use. ``actions`` is the exact roster. ``rules`` are
    ``core/grounding_rules.py``'s, in precedence order, and each names a tool on this
    roster. ``cards`` names the presentation components the specialist may select.
    ``fallback_instruction`` is what the model is told when the specialist's prompt file
    is absent; it is minimal on purpose, because the prompt is not this package's to write.
    """

    name: str
    role: str
    surface: Surface
    description: str
    actions: tuple[str, ...]
    rules: tuple[GroundingRule, ...]
    cards: tuple[str, ...]
    fallback_instruction: str
    max_tool_calls: int = 8
    max_rounds: int = 6

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or not self.role.isidentifier():
            raise ValueError("specialist name and role must be identifiers")
        unknown = [name for name in self.actions if name not in ACTIONS]
        if unknown:
            raise ValueError(f"{self.name}: actions not in Registry A table: {unknown}")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError(f"{self.name}: duplicate actions")
        bad_cards = [card for card in self.cards if card not in CARD_TOOLS]
        if bad_cards:
            raise ValueError(f"{self.name}: unknown cards {bad_cards}")
        offered = set(self.tool_names)
        for rule in self.rules:
            if rule.tool not in offered:
                raise ValueError(
                    f"{self.name}: rule {rule.name!r} starts with {rule.tool!r}, "
                    "which is not on this specialist's roster"
                )
        if self.max_tool_calls <= 0 or self.max_rounds <= 0:
            raise ValueError("budgets must be positive")

    # ---- derived views -----------------------------------------------------

    @property
    def capabilities(self) -> frozenset[str]:
        """The Registry A capability strings this specialist needs. Same as its actions."""
        return frozenset(self.actions)

    @property
    def action_tool_names(self) -> tuple[str, ...]:
        """Function names of the actions that have a tool, in roster order."""
        return tuple(tool for name in self.actions if (tool := ACTIONS[name].tool_name) is not None)

    @property
    def card_tool_names(self) -> tuple[str, ...]:
        return tuple(CARD_TOOLS[card] for card in self.cards)

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Every model-facing function name: action tools, then presentation tools."""
        return (*self.action_tool_names, *self.card_tool_names)

    def action(self, name: str) -> Action:
        if name not in self.actions:
            raise KeyError(f"{self.name} does not hold {name}")
        return ACTIONS[name]

    @property
    def mutating_actions(self) -> tuple[str, ...]:
        return tuple(name for name in self.actions if ACTIONS[name].mutates)

    @property
    def read_actions(self) -> tuple[str, ...]:
        return tuple(name for name in self.actions if not ACTIONS[name].mutates)

    @property
    def is_read_only(self) -> bool:
        return not self.mutating_actions

    @property
    def prompt_name(self) -> str:
        return self.name

"""Registry A as code: the capability each specialist tool requires.

Specification 5.3 defines four registries. Agents may only ever hold Registry A. This
module is the frozen table from tool name to the Registry A capability it needs, the
roster of tools each specialist (``docs/briefs/AGENT_ROSTER.md``) may be offered, and the
per-role allowlist the capability broker intersects with the harness principal.

Registries B (trusted buyer-surface), C (kernel-internal) and D (operator) are listed here
so a test can prove no tool maps to them. They are asserted disjoint from Registry A at
import time: a merge that put ``approval.record`` in :class:`Capability` would fail to
import this package rather than fail in a review.

Why a capability string, not a boolean per tool: the same capability guards more than one
tool (``checkout.read`` guards both the read and the card that presents it), and the
principal carries capabilities, not tool names. Spec 5.4 intersects sets of capabilities;
the tool table is how a tool name is translated into that vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "AGENT_ALLOWLIST",
    "ALL_CAPABILITIES",
    "KERNEL_INTERNAL_OPERATIONS",
    "PRESENTATION_TOOLS",
    "REGISTRY_A",
    "SPECIALIST_TOOLS",
    "TRUSTED_OPERATOR_ACTIONS",
    "TRUSTED_SURFACE_ACTIONS",
    "WRITE_TOOLS",
    "AgentRole",
    "Capability",
    "capability_for",
    "tools_for_role",
]


class Capability(StrEnum):
    """Registry A, specification 5.3, verbatim. Closed set; the enum is the registry."""

    CATALOG_SEARCH = "catalog.search"
    CATALOG_GET_PRODUCT = "catalog.get_product"
    INVENTORY_CHECK = "inventory.check"
    BASKET_CREATE = "basket.create"
    BASKET_UPDATE = "basket.update"
    BASKET_PROPOSE_LINE = "basket.propose_line"
    QUOTE_REQUEST = "quote.request"
    RESERVATION_REQUEST = "reservation.request"
    CHECKOUT_SUBMIT_FOR_APPROVAL = "checkout.submit_for_approval"
    CHECKOUT_SUBMIT_APPROVED = "checkout.submit_approved"
    ORDER_TRACK = "order.track"
    ORDER_PROPOSE_CANCEL = "order.propose_cancel"
    REFUND_PROPOSE = "refund.propose"
    SUPPORT_ESCALATE = "support.escalate"
    MERCHANT_CATALOGUE_HEALTH_READ = "merchant.catalogue_health.read"
    MERCHANT_INVENTORY_ANOMALIES_READ = "merchant.inventory_anomalies.read"
    MERCHANT_CHECKOUT_METRICS_READ = "merchant.checkout_metrics.read"
    MERCHANT_GROWTH_PROPOSAL_CREATE = "merchant.growth_proposal.create"
    CHECKOUT_READ = "checkout.read"
    POLICY_SEARCH = "policy.search"
    RESOLUTION_EVALUATE = "resolution.evaluate"
    SUPPORT_CASE_READ = "support.case.read"


class AgentRole(StrEnum):
    """The five specialists of the roster. Copilots are harnesses and have no role here."""

    SHOPPING = "shopping"
    CHECKOUT = "checkout"
    SUPPORT = "support"
    GROWTH = "growth"
    CASE = "case"


#: Tool name -> required capability. Frozen: a new tool is a reviewed row here, never a
#: default. Notes on the less obvious rows:
#:
#: * ``basket_get`` re-quotes, so it is ``quote.request`` and not a basket write.
#: * ``checkout_create`` produces version 1 for the trusted surface to approve; that is
#:   what ``checkout.submit_for_approval`` means. It cannot approve anything.
#: * ``present_*`` tools take ids only and require the capability that could have read
#:   the thing being shown, so a specialist can never present what it could not fetch.
REGISTRY_A: Final[Mapping[str, Capability]] = MappingProxyType(
    {
        # shopping
        "search": Capability.CATALOG_SEARCH,
        "product": Capability.CATALOG_GET_PRODUCT,
        "basket_create": Capability.BASKET_CREATE,
        "basket_set_line": Capability.BASKET_UPDATE,
        # A proposal, not a write: it reads the product and re-quotes the basket to bind
        # price, revision and content hash, and returns a record the buyer's instruction
        # turns into a basket write on the trusted surface. Nothing is persisted, which is
        # why it is not in WRITE_TOOLS and why a reads-only bridge may hold it.
        "basket_propose_line": Capability.BASKET_PROPOSE_LINE,
        "basket_get": Capability.QUOTE_REQUEST,
        "present_products": Capability.CATALOG_GET_PRODUCT,
        "present_basket": Capability.QUOTE_REQUEST,
        # checkout
        "checkout_create": Capability.CHECKOUT_SUBMIT_FOR_APPROVAL,
        "checkout_get": Capability.CHECKOUT_READ,
        "checkout_submit_approved": Capability.CHECKOUT_SUBMIT_APPROVED,
        "order_track": Capability.ORDER_TRACK,
        "present_approval": Capability.CHECKOUT_READ,
        "present_decision": Capability.CHECKOUT_READ,
        # support
        "policy_search": Capability.POLICY_SEARCH,
        "resolution_evaluate": Capability.RESOLUTION_EVALUATE,
        "support_escalate": Capability.SUPPORT_ESCALATE,
        "support_case_read": Capability.SUPPORT_CASE_READ,
        "present_plan": Capability.RESOLUTION_EVALUATE,
        # growth
        "catalogue_health_read": Capability.MERCHANT_CATALOGUE_HEALTH_READ,
        "inventory_anomalies_read": Capability.MERCHANT_INVENTORY_ANOMALIES_READ,
        "checkout_metrics_read": Capability.MERCHANT_CHECKOUT_METRICS_READ,
        "growth_proposal_create": Capability.MERCHANT_GROWTH_PROPOSAL_CREATE,
        "present_metrics": Capability.MERCHANT_CHECKOUT_METRICS_READ,
        # case
        "present_case": Capability.SUPPORT_CASE_READ,
    }
)

#: The roster (ADR 0004 section 5): which tools each specialist is offered, in the order
#: the model sees them. Every name is a row of :data:`REGISTRY_A`; import asserts it.
SPECIALIST_TOOLS: Final[Mapping[AgentRole, tuple[str, ...]]] = MappingProxyType(
    {
        AgentRole.SHOPPING: (
            "search",
            "product",
            "basket_create",
            "basket_set_line",
            "basket_propose_line",
            "basket_get",
            "present_products",
            "present_basket",
        ),
        AgentRole.CHECKOUT: (
            "basket_get",
            "checkout_create",
            "checkout_get",
            "checkout_submit_approved",
            "order_track",
            "present_approval",
            "present_decision",
        ),
        AgentRole.SUPPORT: (
            "order_track",
            "checkout_get",
            "policy_search",
            "resolution_evaluate",
            "support_escalate",
            "support_case_read",
            "present_plan",
        ),
        AgentRole.GROWTH: (
            "catalogue_health_read",
            "inventory_anomalies_read",
            "checkout_metrics_read",
            "growth_proposal_create",
            "present_metrics",
        ),
        AgentRole.CASE: ("support_case_read", "present_case"),
    }
)

#: Built-in allowlist per role (specification 5.4, intersection input 1). The broker
#: intersects this with the harness principal; it never widens it. ``order.propose_cancel``
#: and ``refund.propose`` are proposals made in conversation and have no tool; the
#: capability still exists so the checkout principal records what it may propose.
AGENT_ALLOWLIST: Final[Mapping[AgentRole, frozenset[Capability]]] = MappingProxyType(
    {
        AgentRole.SHOPPING: frozenset(
            {
                Capability.CATALOG_SEARCH,
                Capability.CATALOG_GET_PRODUCT,
                Capability.INVENTORY_CHECK,
                Capability.BASKET_CREATE,
                Capability.BASKET_UPDATE,
                Capability.BASKET_PROPOSE_LINE,
                Capability.QUOTE_REQUEST,
                Capability.RESERVATION_REQUEST,
            }
        ),
        AgentRole.CHECKOUT: frozenset(
            {
                Capability.QUOTE_REQUEST,
                Capability.RESERVATION_REQUEST,
                Capability.CHECKOUT_SUBMIT_FOR_APPROVAL,
                Capability.CHECKOUT_SUBMIT_APPROVED,
                Capability.CHECKOUT_READ,
                Capability.ORDER_TRACK,
                Capability.ORDER_PROPOSE_CANCEL,
                Capability.REFUND_PROPOSE,
            }
        ),
        AgentRole.SUPPORT: frozenset(
            {
                Capability.ORDER_TRACK,
                Capability.CHECKOUT_READ,
                Capability.POLICY_SEARCH,
                Capability.RESOLUTION_EVALUATE,
                Capability.SUPPORT_ESCALATE,
                Capability.SUPPORT_CASE_READ,
            }
        ),
        AgentRole.GROWTH: frozenset(
            {
                Capability.MERCHANT_CATALOGUE_HEALTH_READ,
                Capability.MERCHANT_INVENTORY_ANOMALIES_READ,
                Capability.MERCHANT_CHECKOUT_METRICS_READ,
                Capability.MERCHANT_GROWTH_PROPOSAL_CREATE,
            }
        ),
        AgentRole.CASE: frozenset({Capability.SUPPORT_CASE_READ}),
    }
)

#: Tools that change state somewhere. Each one's closure holds the session write lock and
#: passes a provenance gate before the backend is called.
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "basket_create",
        "basket_set_line",
        "checkout_create",
        "checkout_submit_approved",
        "resolution_evaluate",
        "support_escalate",
        "growth_proposal_create",
    }
)

#: Tools whose only arguments are ids the ledger already knows (ADR 0004 section 1.7).
PRESENTATION_TOOLS: Final[frozenset[str]] = frozenset(
    name for name in REGISTRY_A if name.startswith("present_")
)

ALL_CAPABILITIES: Final[frozenset[str]] = frozenset(capability.value for capability in Capability)

#: Registry B. Recorded so a test can prove the tool table never names one.
TRUSTED_SURFACE_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "approval.record",
        "approval.reject",
        "authority.revoke",
        "order.confirm_cancel",
        "refund.confirm",
    }
)

#: Registry C.
KERNEL_INTERNAL_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "checkout.revalidate",
        "inventory.reserve_commit",
        "inventory.reserve_release",
        "authority.consume",
        "execution_grant.issue",
        "execution_grant.consume",
        "payment.create_order",
        "payment.handoff_create",
        "payment.result_verify",
        "payment.reconcile",
        "refund.execute",
        "refund.reconcile",
        "webhook.apply",
        "order.cancel_execute",
        "resolution.plan_issue",
    }
)

#: Registry D. Empty in P0 by specification; named so the disjointness check covers it.
TRUSTED_OPERATOR_ACTIONS: Final[frozenset[str]] = frozenset(
    {"review.decision.record", "review.case.assign", "review.case.resolve"}
)


def _assert_registries_disjoint() -> None:
    """Fail at import if Registry A ever overlaps B, C or D, or the roster names a stray.

    Import time rather than test time: a package that cannot be imported cannot be
    deployed, whereas a red test can be skipped under deadline pressure.
    """
    others = TRUSTED_SURFACE_ACTIONS | KERNEL_INTERNAL_OPERATIONS | TRUSTED_OPERATOR_ACTIONS
    overlap = ALL_CAPABILITIES & others
    if overlap:
        raise RuntimeError(f"Registry A names non-agent actions: {sorted(overlap)}")
    missing = {name for tools in SPECIALIST_TOOLS.values() for name in tools} - set(REGISTRY_A)
    if missing:
        raise RuntimeError(f"roster tools without a Registry A row: {sorted(missing)}")


_assert_registries_disjoint()


def capability_for(tool_name: str) -> Capability | None:
    """The capability a tool requires, or None for a tool this registry never granted."""
    return REGISTRY_A.get(tool_name)


def tools_for_role(role: AgentRole) -> tuple[str, ...]:
    """Tool names a role may be offered, in roster order, restricted to its allowlist.

    The roster and the allowlist are maintained together, so the restriction is a
    tautology today; it is applied anyway so a roster edit can never offer a tool the
    role's own allowlist would deny at call time.
    """
    allowed = AGENT_ALLOWLIST[role]
    return tuple(name for name in SPECIALIST_TOOLS[role] if REGISTRY_A[name] in allowed)

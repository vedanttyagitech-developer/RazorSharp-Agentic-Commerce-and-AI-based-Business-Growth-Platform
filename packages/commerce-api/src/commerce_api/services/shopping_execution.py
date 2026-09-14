"""Model-free Shopping execution: the capability behind any conversational surface.

A single Gemini main agent reasons; this module executes. It never invokes a model,
loads a prompt, or writes a sentence -- it validates a structured command against
server-side identity, runs it through the same services the REST routes call, and
returns evidence with a machine-readable outcome. Narration belongs to the caller.

What this module is not: a second catalogue engine, a second cart engine, or a
payment pathway. Search, product detail and cart reads go through
:mod:`commerce_api.services.catalogue_service` and :mod:`commerce_api.services.cart_service`;
proposals are built by :func:`line_proposal_record`, the same function the buyer panel
and the voice gateway already act on. Two entries -- this structured one and the
existing conversational fast paths -- converge on those services, never on each other.

Execution honesty, stated once:

* A proposal is not a mutation. ``stage="proposed"`` means the trusted surface still
  has to press; ``stage="mutated"`` never appears here because this module never
  writes a cart. The write happens on ``PUT /v1/carts/{id}/lines/{sku}`` under the
  cart lock, keyed durably in ``idempotency_records``.
* ``operation_id`` is carried and echoed, never invented here. Retry identity for the
  mutation itself is the ``Idempotency-Key`` the surface sends with the write; a
  repeated logical instruction is a new operation, a transport retry reuses its key.
* Reference resolution reads the displayed shelf the caller hands over. A stale,
  missing or ambiguous shelf is clarification, never a guess.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from . import catalogue_service

if TYPE_CHECKING:  # No runtime dependency on the conversational layer.
    from .agent_service import ToolExecutor

__all__ = [
    "OutcomeStatus",
    "QuantityMode",
    "ShoppingAction",
    "ShoppingCommand",
    "ShoppingContractError",
    "ShoppingExecutionAdapter",
    "ShoppingOutcome",
]


class ShoppingAction(StrEnum):
    """The supported shopping operations. Discovery is not single-SKU work."""

    SEARCH = "search"
    GET_PRODUCT = "get_product"
    READ_CART = "read_cart"
    PROPOSE = "propose"


class QuantityMode(StrEnum):
    """What the amount means. Absolute quantities survive a retry unchanged."""

    ADD = "add"
    SET = "set"
    REMOVE = "remove"


class OutcomeStatus(StrEnum):
    """What happened, for a main agent that must narrate without guessing."""

    COMPLETED = "completed"
    CLARIFICATION_REQUIRED = "clarification_required"
    REQUIRES_APPROVAL = "requires_approval"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


#: Returned discoveries per call when the caller does not say. Matches the copilot's
#: shelf: direct matches first, then same-category neighbours.
DEFAULT_SEARCH_LIMIT: Final[int] = 12


class ShoppingContractError(ValueError):
    """A structured command that fails server-side validation.

    ``reason_code`` is machine-readable; the caller renders it. A contract refusal is
    not an execution failure: nothing ran, nothing changed.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ShoppingCommand:
    """One validated shopping instruction.

    Identity, tenant, buyer, merchant and permissions come from the authenticated
    server context the executor carries -- never from these fields, which name only
    *what* to do. ``operation_id`` names the logical instruction: a retry resends it,
    a new instruction mints a new one. The model never decides which case applies;
    the request lifecycle does.
    """

    action: ShoppingAction
    operation_id: uuid.UUID
    query: str = ""
    sku: str | None = None
    mode: QuantityMode = QuantityMode.ADD
    amount: int = 1
    cart_id: uuid.UUID | None = None
    #: Zero-based index into ``displayed`` for "second wala" style references.
    ordinal: int | None = None
    max_results: int = DEFAULT_SEARCH_LIMIT

    def validated(self) -> ShoppingCommand:
        """Reject what cannot execute before anything runs. Returns ``self``."""
        # `type(...) is` rather than `in`: a plain string equal to a member name
        # ("search") must not pass as the member itself.
        if type(self.action) is not ShoppingAction:
            raise ShoppingContractError("unknown_action", f"unsupported action: {self.action!r}")
        if self.max_results < 1 or self.max_results > catalogue_service.MAX_SEARCH_LIMIT:
            raise ShoppingContractError(
                "invalid_limit",
                f"max_results must be between 1 and {catalogue_service.MAX_SEARCH_LIMIT}",
            )
        if self.action is ShoppingAction.SEARCH and not self.query.strip():
            raise ShoppingContractError("empty_query", "search needs a query")
        if self.action is ShoppingAction.GET_PRODUCT and not (self.sku or "").strip():
            raise ShoppingContractError("missing_sku", "get_product needs a SKU")
        if self.action is ShoppingAction.PROPOSE:
            if self.mode is not QuantityMode.REMOVE and self.amount < 1:
                raise ShoppingContractError(
                    "invalid_amount", "add and set need an amount of at least 1"
                )
            if not ((self.sku or "").strip() or self.query.strip() or self.ordinal is not None):
                raise ShoppingContractError(
                    "missing_target",
                    "propose needs a SKU, a query, or an ordinal into the displayed shelf",
                )
        return self


@dataclass(frozen=True, slots=True)
class ShoppingOutcome:
    """Backend evidence for one executed command. No prose, only facts.

    ``stage`` names how far the world moved: ``"observed"`` for reads,
    ``"proposed"`` for a staged cart write the trusted surface still has to press.
    There is no ``"mutated"`` stage here because this module never mutates.
    """

    status: OutcomeStatus
    reason_code: str
    operation_id: uuid.UUID
    action: ShoppingAction
    evidence: Mapping[str, Any] = field(default_factory=dict)
    stage: str = "observed"
    next_steps: tuple[str, ...] = ()
    #: True when the underlying read was refused by the capability gate. Preserved
    #: so callers render a denial sentence rather than an outage sentence.
    denied: bool = False

    def to_evidence(self) -> dict[str, Any]:
        """The wire shape handed back to a main agent."""
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "operation_id": str(self.operation_id),
            "action": self.action.value,
            "stage": self.stage,
            "denied": self.denied,
            "next_steps": list(self.next_steps),
            **dict(self.evidence),
        }


class ShoppingExecutionAdapter:
    """Shopping as a capability: structured in, evidence out, no model inside.

    Constructed without arguments on purpose -- everything a call needs arrives per
    call, so no request state can leak between turns. ``tools`` is the caller's own
    gated executor: every read below passes the same capability gate and lands in
    the same ledger as the conversational paths, which is what keeps one grounding
    rule for both entries. ``displayed`` is the visible shelf in visible order,
    read server-side from whoever owns display memory; this module never stores it.
    """

    def execute(
        self,
        command: ShoppingCommand,
        *,
        tools: ToolExecutor,
        displayed: Sequence[str] = (),
    ) -> ShoppingOutcome:
        """Validate, run, and report. Raises :class:`ShoppingContractError`."""
        command = command.validated()
        if command.action is ShoppingAction.SEARCH:
            return self._search(command, tools=tools)
        if command.action is ShoppingAction.GET_PRODUCT:
            return self._get_product(command, tools=tools)
        if command.action is ShoppingAction.READ_CART:
            return self._read_cart(command, tools=tools)
        return self._propose(command, tools=tools, displayed=tuple(displayed))

    def plan(
        self,
        message: str,
        *,
        tools: ToolExecutor,
        language: str,
        operation_id: uuid.UUID,
    ) -> ShoppingOutcome:
        """Deterministic adaptive plans: comparisons, replacements, budgeted bundles.

        The plan shapes (``comparison_plan``, ``replacement_plan``) and their
        execution are pure functions over the catalogue -- no model, no memory.
        A request no deterministic plan covers is clarification, never a guess,
        and a follow-up without its plan context does not inherit one. The reply
        and structured block are relayed verbatim from the deterministic renderer;
        this method generates no prose of its own.
        """
        # Deferred like every other conversational-layer import here: adaptive
        # planning renders sentences, and this module must not depend on that
        # layer at import time.
        from .adaptive_shopping import (
            comparison_plan,
            replacement_plan,
            stated_budget,
        )
        from .adaptive_shopping import execute as execute_plan

        plan = replacement_plan(message, None) or comparison_plan(message)
        if plan is None:
            plan = {
                "mode": "clarify",
                "needs": [],
                "clarification": "",
                "unverified_requirements": [],
            }
        turn = execute_plan(plan, tools, language, stated_budget(message), message)
        structured = dict(turn.structured or {})
        status = (
            OutcomeStatus.CLARIFICATION_REQUIRED
            if structured.get("planning_status") == "clarification_required"
            else OutcomeStatus.COMPLETED
        )
        return ShoppingOutcome(
            status=status,
            reason_code=str(structured.get("planning_status", "planned")),
            operation_id=operation_id,
            action=ShoppingAction.SEARCH,
            stage="observed",
            evidence={
                "reply": turn.reply,
                "structured": structured,
            },
            next_steps=("choose",),
        )

    # ---- reads -----------------------------------------------------------

    def _search(self, command: ShoppingCommand, *, tools: ToolExecutor) -> ShoppingOutcome:
        result = tools.call("catalog.search", query=command.query, limit=command.max_results)
        if not result.ok:
            # Uncertain, not failed: the store may not have been reached. The caller
            # must recover from status, never blindly re-run.
            return ShoppingOutcome(
                status=OutcomeStatus.UNKNOWN,
                reason_code=result.reason_key or "search_unavailable",
                operation_id=command.operation_id,
                action=command.action,
                next_steps=("retry_status_check",),
                denied=result.denied,
            )
        hits = result.payload.get("hits", [])
        # The whole tool payload rides along under ``result`` so conversational
        # rendering keeps its exact shape (query, locale, freshness included);
        # ``to_evidence`` is the flattened main-agent view of the same outcome.
        return ShoppingOutcome(
            status=OutcomeStatus.COMPLETED,
            reason_code="search_completed" if hits else "no_results",
            operation_id=command.operation_id,
            action=command.action,
            evidence={
                "result": result.payload,
                "hits": hits,
                "skus": result.payload.get("skus", [hit.get("sku") for hit in hits]),
            },
            next_steps=("choose",) if hits else ("rephrase",),
            denied=result.denied,
        )

    def _get_product(self, command: ShoppingCommand, *, tools: ToolExecutor) -> ShoppingOutcome:
        sku = (command.sku or "").strip()
        result = tools.call("catalog.get_product", sku=sku)
        if not result.ok:
            return ShoppingOutcome(
                status=OutcomeStatus.REJECTED,
                # The tool's own reason, not a renamed one: callers render
                # "not found" versus "unavailable" off exactly this key.
                reason_code=result.reason_key or "unknown_sku",
                operation_id=command.operation_id,
                action=command.action,
                evidence={"sku": sku},
                next_steps=("search",),
                denied=result.denied,
            )
        return ShoppingOutcome(
            status=OutcomeStatus.COMPLETED,
            reason_code="product_found",
            operation_id=command.operation_id,
            action=command.action,
            evidence={"product": result.payload, "sku": result.payload.get("sku", sku)},
        )

    def _read_cart(self, command: ShoppingCommand, *, tools: ToolExecutor) -> ShoppingOutcome:
        if command.cart_id is None:
            return ShoppingOutcome(
                status=OutcomeStatus.CLARIFICATION_REQUIRED,
                reason_code="no_basket",
                operation_id=command.operation_id,
                action=command.action,
                next_steps=("create_basket",),
            )
        result = tools.call("cart.read", cart_id=command.cart_id)
        if not result.ok:
            return ShoppingOutcome(
                status=OutcomeStatus.UNKNOWN,
                reason_code=result.reason_key or "cart_unreadable",
                operation_id=command.operation_id,
                action=command.action,
                next_steps=("retry_status_check",),
                denied=result.denied,
            )
        return ShoppingOutcome(
            status=OutcomeStatus.COMPLETED,
            reason_code="cart_read",
            operation_id=command.operation_id,
            action=command.action,
            evidence={"cart": result.payload},
        )

    # ---- proposals (staged writes, never writes) --------------------------

    def _resolve_target(
        self,
        command: ShoppingCommand,
        *,
        tools: ToolExecutor,
        displayed: tuple[str, ...],
    ) -> tuple[str | None, list[dict[str, Any]], str | None]:
        """The SKU to propose, or (None, choices, reason) asking for help.

        An ordinal resolves against the visible shelf in visible order -- never a
        fresh search ordering. A query proposes directly only on a unique
        product-name match; accessories stay visible on discovery instead of being
        silently chosen. Anything else is clarification, never a guess.
        """
        from .discovery import prefer_named_hits

        resolved = (command.sku or "").strip()
        if resolved:
            return resolved, [], None
        if command.ordinal is not None:
            if 0 <= command.ordinal < len(displayed):
                return displayed[command.ordinal], [], None
            return None, [], "stale_shelf"
        result = tools.call("catalog.search", query=command.query, limit=command.max_results)
        if not result.ok:
            return None, [], "search_unavailable"
        direct = prefer_named_hits(command.query, result.payload.get("hits", []), strict=True)
        if len(direct) == 1:
            return direct[0]["sku"], [], None
        if not direct:
            return None, [], "no_matching_product"
        return None, list(direct), "ambiguous_product"

    def _propose(
        self,
        command: ShoppingCommand,
        *,
        tools: ToolExecutor,
        displayed: tuple[str, ...],
    ) -> ShoppingOutcome:
        # Imported here so this module never depends on the conversational layer at
        # import time: the proposal shape is shared, the turn machinery is not.
        from .agent_service import line_proposal_record

        sku, choices, problem = self._resolve_target(command, tools=tools, displayed=displayed)
        if problem == "search_unavailable":
            return ShoppingOutcome(
                status=OutcomeStatus.UNKNOWN,
                reason_code=problem,
                operation_id=command.operation_id,
                action=command.action,
                next_steps=("retry_status_check",),
            )
        if problem is not None or sku is None:
            return ShoppingOutcome(
                status=OutcomeStatus.CLARIFICATION_REQUIRED,
                reason_code=problem or "no_matching_product",
                operation_id=command.operation_id,
                action=command.action,
                evidence={"choices": choices, "query": command.query},
                next_steps=("choose",),
            )
        product = tools.call("catalog.get_product", sku=sku)
        # SET bypasses the availability gate, mirroring the conversational path: an
        # absolute quantity is the buyer's statement about their line, and the write
        # endpoint re-checks the world under the cart lock when they press.
        if not product.ok or (
            not product.payload.get("is_available") and command.mode is not QuantityMode.SET
        ):
            return ShoppingOutcome(
                status=OutcomeStatus.REJECTED,
                reason_code=(
                    product.reason_key if product.reason_key is not None else "unknown_sku"
                )
                if not product.ok
                else "unavailable_product",
                operation_id=command.operation_id,
                action=command.action,
                evidence={"sku": sku},
                next_steps=("search",),
                denied=product.denied,
            )
        if sku not in tools.ledger.seen_skus:
            # Provenance is per turn and non-negotiable: a proposal may name only a
            # SKU a tool returned during this turn.
            return ShoppingOutcome(
                status=OutcomeStatus.REJECTED,
                reason_code="ungrounded_sku",
                operation_id=command.operation_id,
                action=command.action,
                evidence={"sku": sku},
                next_steps=("search",),
            )
        current, cart_denied = self._line_quantity(tools, command.cart_id, sku)
        if current is None:
            return ShoppingOutcome(
                status=OutcomeStatus.UNKNOWN,
                reason_code="cart_unreadable",
                operation_id=command.operation_id,
                action=command.action,
                evidence={"sku": sku},
                next_steps=("retry_status_check",),
                denied=cart_denied,
            )
        if command.mode is QuantityMode.ADD:
            absolute: int = current + command.amount
            target: int | None = None
        elif command.mode is QuantityMode.SET:
            absolute, target = command.amount, command.amount
        else:
            absolute, target = 0, 0
        proposal = line_proposal_record(
            product.payload,
            absolute - current,
            command.cart_id,
            tools,
            target_quantity=target,
        )
        proposal["operation_id"] = str(command.operation_id)
        blocked = proposal.get("blocked_by")
        if blocked not in (None, "no_basket"):
            return ShoppingOutcome(
                status=OutcomeStatus.REJECTED,
                reason_code=f"proposal_{blocked}",
                operation_id=command.operation_id,
                action=command.action,
                evidence={"sku": sku, "proposal": proposal},
                next_steps=("refresh_cart",),
            )
        return ShoppingOutcome(
            status=OutcomeStatus.COMPLETED,
            reason_code="proposed",
            operation_id=command.operation_id,
            action=command.action,
            stage="proposed",
            evidence={
                "sku": sku,
                "proposal": proposal,
                "current_quantity": current,
                "absolute_quantity": proposal.get("quantity"),
            },
            next_steps=("confirm",),
        )

    @staticmethod
    def _line_quantity(
        tools: ToolExecutor, cart_id: uuid.UUID | None, sku: str
    ) -> tuple[int | None, bool]:
        """The line's current quantity and whether its read was denied.

        ``(None, _)`` means the cart could not be read: uncertain, never zero.
        Zero is a fact about an empty line, not a failed read.
        """
        if cart_id is None:
            return 0, False
        read = tools.call("cart.read", cart_id=cart_id)
        if not read.ok:
            return None, read.denied
        return (
            next(
                (
                    int(line["quantity"])
                    for line in read.payload.get("lines", ())
                    if line.get("sku") == sku
                ),
                0,
            ),
            False,
        )

"""The tool factory: the only place a specialist's tools are constructed.

``build_toolset`` binds one principal, one backend, one session and one turn into a
:class:`BoundToolset`: the tools the principal may hold, the capability gate that runs
before each of them, and the error gate that turns an exception into a result. The
runtime adapter wraps each :class:`BoundTool` in its own tool class (ADK's
``FunctionTool(tool.func)``) and registers the two gates; it never builds a tool of its
own. A test asserts every specialist's tools come from here, by object identity.

Tools are closures so that the principal, session and turn ledger are captured in code,
not passed as model-visible arguments. A model cannot hand a tool a different tenant,
session, basket or principal because there is no parameter for one (specification 20.2:
the server supplies identity). The session's basket and checkout are read from session
state; the model names SKUs, quantities, versions and hashes, and nothing else.

Every tool result is a JSON-safe dict. Merchant text inside it is fenced on the way out
(``grounding/payloads.py``); an id on the way in passes a provenance gate
(``core/provenance.py``) before the backend; failures are structured (``ok: False`` with
``reason_key``) rather than exceptions, so the model always gets something it can explain
and the turn always gets a record.

This module imports nothing from ``google.adk`` or ``google.genai``: the closures take a
``tool_context`` that satisfies :class:`~agent_runtime.capabilities.broker.ToolContextLike`,
which ADK's ``ToolContext`` does structurally.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, overload

from transaction_kernel import AgentPrincipal

from ..backends.base import BackendError, CommerceBackend
from ..core.provenance import (
    GATE_PROVENANCE,
    PROVENANCE_STATE_KEY,
    Held,
    SessionProvenance,
    check_checkout_provenance,
    check_line_count,
    check_quantity,
    check_sku_provenance,
    session_write_lock,
)
from ..grounding.payloads import (
    approval_payload,
    basket_payload,
    checkout_payload,
    decision_payload,
    order_payload,
    product_payload,
    search_payload,
)
from ..rendering.cards import (
    approval_card,
    basket_card,
    decision_card,
    plan_card,
    product_card,
)
from ..turn import TurnContext
from .broker import (
    ToolContextLike,
    ToolErrorGate,
    ToolGate,
    make_capability_gate,
    make_tool_error_gate,
)
from .registry import REGISTRY_A, WRITE_TOOLS, AgentRole, Capability, tools_for_role

__all__ = [
    "IDENTITY_PARAMETER_NAMES",
    "STATE_BASKET_ID",
    "STATE_CHECKOUT_HASH",
    "STATE_CHECKOUT_ID",
    "STATE_CHECKOUT_VERSION",
    "AgentRole",
    "BindingLike",
    "BoundTool",
    "BoundToolset",
    "FactoryContext",
    "ToolBuilder",
    "ToolFunc",
    "build_tools",
    "build_toolset",
]

#: Session-state keys the tools maintain. IDs only; never product data.
STATE_BASKET_ID: Final[str] = "basket_id"

#: How many ids one present call may name. A model asked to show the options that dumps
#: forty SKUs onto the screen has stopped choosing; the card reports the full count so a
#: surface can say how many were left out rather than silently truncating.
MAX_PRESENTED_IDS: Final[int] = 8
STATE_CHECKOUT_ID: Final[str] = "checkout_id"
STATE_CHECKOUT_VERSION: Final[str] = "checkout_version"
STATE_CHECKOUT_HASH: Final[str] = "checkout_content_hash"

#: Parameter names no tool schema may carry. Identity is the server's (spec 20.2); a tool
#: that took one of these would let the model choose whose basket it writes to.
IDENTITY_PARAMETER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "tenant",
        "tenant_id",
        "merchant",
        "merchant_id",
        "session",
        "session_id",
        "principal",
        "principal_id",
        "user",
        "user_id",
        "buyer",
        "buyer_id",
        "buyer_ref",
        "basket_id",
        "checkout_id",
    }
)

#: The runtime injects its context under this parameter name; it is not a schema field.
_CONTEXT_PARAMETER: Final[str] = "tool_context"

_MAX_SEARCH_LIMIT: Final[int] = 10
_DEFAULT_SEARCH_LIMIT: Final[int] = 5

ToolFunc = Callable[..., Awaitable[dict[str, Any]]]


class BindingLike(Protocol):
    """What the harness hands the factory: which specialist, and its bound principal."""

    @property
    def specialist(self) -> Any: ...

    @property
    def principal(self) -> AgentPrincipal: ...


@dataclass(frozen=True, slots=True)
class FactoryContext:
    """Everything a tool closure captures. Built once per turn by :func:`build_toolset`."""

    principal: AgentPrincipal
    backend: CommerceBackend
    turn: TurnContext
    session_id: str
    agent_name: str


ToolBuilder = Callable[[FactoryContext], ToolFunc]


@dataclass(frozen=True, slots=True)
class BoundTool:
    """One tool as the factory produced it: name, capability, and the closure to call."""

    name: str
    capability: Capability
    func: ToolFunc
    writes: bool

    @property
    def description(self) -> str:
        return inspect.getdoc(self.func) or ""

    @property
    def parameters(self) -> tuple[str, ...]:
        """Model-visible parameter names: the signature minus the injected context."""
        return tuple(
            name for name in inspect.signature(self.func).parameters if name != _CONTEXT_PARAMETER
        )


@dataclass(frozen=True, slots=True)
class BoundToolset(Sequence[BoundTool]):
    """The factory's whole output for one specialist and one turn.

    A ``Sequence`` of :class:`BoundTool` so the harness can hand it on as "the tools"
    without knowing the shape, plus the two gates the runtime must register beside them.
    ``unbuilt`` lists roster tools the principal may hold but no builder exists for yet
    (presentation tools and the support, growth and case backends land in other units);
    they are reported rather than silently dropped so a test can see the gap.
    """

    agent_name: str
    principal: AgentPrincipal
    tools: tuple[BoundTool, ...]
    gate: ToolGate
    error_gate: ToolErrorGate
    unbuilt: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def get(self, name: str) -> BoundTool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise KeyError(name)

    def __len__(self) -> int:
        return len(self.tools)

    def __iter__(self) -> Iterator[BoundTool]:
        return iter(self.tools)

    @overload
    def __getitem__(self, index: int) -> BoundTool: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[BoundTool]: ...

    def __getitem__(self, index: int | slice) -> BoundTool | Sequence[BoundTool]:
        return self.tools[index]


# ---------------------------------------------------------------------------- helpers


def _failure(
    ctx: FactoryContext, tool: str, args: dict[str, Any], exc: BackendError
) -> dict[str, Any]:
    problem = exc.problem
    reason = problem.reason_key
    ctx.turn.record_failure(ctx.agent_name, tool, reason, str(exc))
    ctx.turn.record_call(ctx.agent_name, tool, args, ok=False, reason_key=reason)
    payload: dict[str, Any] = {
        "ok": False,
        "error": True,
        "reason_key": reason,
        "status": problem.status,
        "title": problem.title,
        "detail": problem.detail,
    }
    if reason == "unknown_sku":
        # Specification 20.4: an unknown product is rejected loudly, and the model is
        # told to search for grounded alternatives rather than guess.
        payload["code"] = "UNKNOWN_CATALOGUE_ITEM"
        payload["instruction"] = (
            "This SKU is not in the merchant catalogue. Do not use it. Search for the "
            "item and offer only SKUs the search returns."
        )
    return payload


def _held(ctx: FactoryContext, tool: str, args: dict[str, Any], held: Held) -> dict[str, Any]:
    ctx.turn.record_call(
        ctx.agent_name,
        tool,
        args,
        ok=False,
        reason_key=held.reason_key,
        summary={"blocked": held.gate},
    )
    return held.to_result()


def _missing(reason_key: str, instruction: str) -> dict[str, Any]:
    return {"ok": False, "error": True, "reason_key": reason_key, "instruction": instruction}


def _load(tool_context: ToolContextLike) -> SessionProvenance:
    """The session's provenance record. Malformed state reads as empty: writes are held."""
    return SessionProvenance.from_state(tool_context.state.get(PROVENANCE_STATE_KEY))


def _save(tool_context: ToolContextLike, record: SessionProvenance) -> None:
    tool_context.state[PROVENANCE_STATE_KEY] = record.to_state()


# ---------------------------------------------------------------------------- builders


def _build_search(ctx: FactoryContext) -> ToolFunc:
    async def search(
        query: str, tool_context: ToolContextLike, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any]:
        """Search the merchant catalogue for products matching the buyer's words.

        Searches Hindi, Hinglish and English together; pass the buyer's own words. Returns
        live price, stock and availability for each result. Only SKUs in `allowed_skus`
        may be mentioned or added to the basket afterwards.

        Args:
            query: The product the buyer wants, in the buyer's own words.
            limit: Maximum results, 1-10.
        """
        bounded = max(1, min(int(limit), _MAX_SEARCH_LIMIT))
        args = {"query": query, "limit": bounded, "locale": ctx.turn.language.locale.value}
        try:
            page = await ctx.backend.search(query, ctx.turn.language.locale, bounded)
        except BackendError as exc:
            return _failure(ctx, "search", args, exc)
        payload = search_payload(page, ctx.turn, tool="search")
        record = _load(tool_context)
        record.remember_search(page)
        _save(tool_context, record)
        ctx.turn.record_call(
            ctx.agent_name, "search", args, ok=True, summary={"skus": list(page.skus())}
        )
        return payload

    return search


def _build_product(ctx: FactoryContext) -> ToolFunc:
    async def product(sku: str, tool_context: ToolContextLike) -> dict[str, Any]:
        """Live detail for one catalogue SKU.

        Use this to resolve a SKU the buyer named directly; text search does not match
        SKUs. An unknown SKU is reported as `UNKNOWN_CATALOGUE_ITEM`, never as out of stock.

        Args:
            sku: A catalogue SKU, e.g. AMUL-DAIRY-001.
        """
        args = {"sku": sku}
        try:
            card = await ctx.backend.product(sku)
        except BackendError as exc:
            return _failure(ctx, "product", args, exc)
        payload = product_payload(card, ctx.turn, tool="product")
        record = _load(tool_context)
        record.remember_product(card)
        _save(tool_context, record)
        ctx.turn.record_call(ctx.agent_name, "product", args, ok=True, summary={"sku": card.sku})
        return payload

    return product


def _build_basket_create(ctx: FactoryContext) -> ToolFunc:
    async def basket_create(tool_context: ToolContextLike) -> dict[str, Any]:
        """Create a new, empty basket for this session. Call once, then add lines."""
        async with session_write_lock(ctx.session_id):
            existing = str(tool_context.state.get(STATE_BASKET_ID, ""))
            if existing:
                # Idempotent on purpose: a model that calls this twice must not orphan a
                # basket the buyer already filled.
                try:
                    view = await ctx.backend.basket_get(existing)
                except BackendError as exc:
                    return _failure(ctx, "basket_create", {"existing": existing}, exc)
            else:
                try:
                    view = await ctx.backend.basket_create()
                except BackendError as exc:
                    return _failure(ctx, "basket_create", {}, exc)
                tool_context.state[STATE_BASKET_ID] = view.basket_id
            record = _load(tool_context)
            record.remember_basket(view)
            _save(tool_context, record)
        payload = basket_payload(view, ctx.turn, tool="basket_create")
        ctx.turn.record_call(
            ctx.agent_name, "basket_create", {}, ok=True, summary={"basket_id": view.basket_id}
        )
        return payload

    return basket_create


def _build_basket_set_line(ctx: FactoryContext) -> ToolFunc:
    async def basket_set_line(
        sku: str, quantity: int, tool_context: ToolContextLike
    ) -> dict[str, Any]:
        """Set the quantity of one SKU in the basket; 0 removes it. Returns the exact quote.

        Only a SKU that search or product returned in this session can be written. The
        quote is computed by the merchant's deterministic fee engine: read every price,
        tax, delivery fee, total and free-delivery gap from it; never compute one.

        Args:
            sku: A SKU returned by search or product in this session.
            quantity: Whole units, 0 to remove, at most 50.
        """
        basket_id = str(tool_context.state.get(STATE_BASKET_ID, ""))
        args = {"basket_id": basket_id, "sku": sku, "quantity": quantity}
        if not basket_id:
            return _missing("no_basket", "Call basket_create first.")
        if held := check_quantity(quantity):
            return _held(ctx, "basket_set_line", args, held)
        record = _load(tool_context)
        async with session_write_lock(ctx.session_id):
            try:
                current = await ctx.backend.basket_get(basket_id)
            except BackendError as exc:
                return _failure(ctx, "basket_set_line", args, exc)
            current_skus = tuple(line_sku for line_sku, _ in current.lines)
            if held := check_sku_provenance(record, sku, basket_lines=current_skus):
                return _held(ctx, "basket_set_line", args, held)
            if held := check_line_count(current_skus, sku, quantity):
                return _held(ctx, "basket_set_line", args, held)
            try:
                view = await ctx.backend.basket_set_line(basket_id, sku, quantity)
            except BackendError as exc:
                return _failure(ctx, "basket_set_line", args, exc)
            record.remember_basket(view)
            _save(tool_context, record)
        payload = basket_payload(view, ctx.turn, tool="basket_set_line")
        ctx.turn.record_call(
            ctx.agent_name,
            "basket_set_line",
            args,
            ok=True,
            summary={
                "code": view.code.value,
                "total_minor": None if view.quote is None else view.quote.total.minor,
            },
        )
        return payload

    return basket_set_line


def _build_basket_get(ctx: FactoryContext) -> ToolFunc:
    async def basket_get(tool_context: ToolContextLike) -> dict[str, Any]:
        """Re-quote the session's basket and report whether merchant state moved since."""
        basket_id = str(tool_context.state.get(STATE_BASKET_ID, ""))
        args = {"basket_id": basket_id}
        if not basket_id:
            return _missing("no_basket", "Call basket_create first.")
        try:
            view = await ctx.backend.basket_get(basket_id)
        except BackendError as exc:
            return _failure(ctx, "basket_get", args, exc)
        payload = basket_payload(view, ctx.turn, tool="basket_get")
        record = _load(tool_context)
        record.remember_basket(view)
        _save(tool_context, record)
        ctx.turn.record_call(
            ctx.agent_name, "basket_get", args, ok=True, summary={"stale": view.stale}
        )
        return payload

    return basket_get


def _build_checkout_create(ctx: FactoryContext) -> ToolFunc:
    async def checkout_create(tool_context: ToolContextLike) -> dict[str, Any]:
        """Create checkout version 1 from the session's basket and return its approval card.

        The card states exactly what the buyer will approve on the trusted surface. This
        tool cannot approve anything.
        """
        basket_id = str(tool_context.state.get(STATE_BASKET_ID, ""))
        args = {"basket_id": basket_id}
        if not basket_id:
            return _missing("no_basket", "No basket exists yet.")
        async with session_write_lock(ctx.session_id):
            try:
                card = await ctx.backend.checkout_create(basket_id)
            except BackendError as exc:
                return _failure(ctx, "checkout_create", args, exc)
            tool_context.state[STATE_CHECKOUT_ID] = card.checkout_id
            tool_context.state[STATE_CHECKOUT_VERSION] = card.version
            tool_context.state[STATE_CHECKOUT_HASH] = card.content_hash
            record = _load(tool_context)
            record.remember_approval(card)
            _save(tool_context, record)
        payload = approval_payload(card, ctx.turn, tool="checkout_create")
        ctx.turn.record_call(
            ctx.agent_name,
            "checkout_create",
            args,
            ok=True,
            summary={"checkout_id": card.checkout_id, "version": card.version},
        )
        return payload

    return checkout_create


def _build_checkout_get(ctx: FactoryContext) -> ToolFunc:
    async def checkout_get(tool_context: ToolContextLike) -> dict[str, Any]:
        """Read the session's checkout: every version, its approval status, payment state.

        Only a payment state of CAPTURED means the buyer has paid.
        """
        checkout_id = str(tool_context.state.get(STATE_CHECKOUT_ID, ""))
        args = {"checkout_id": checkout_id}
        if not checkout_id:
            return _missing("no_checkout", "No checkout exists yet.")
        try:
            view = await ctx.backend.checkout_get(checkout_id)
        except BackendError as exc:
            return _failure(ctx, "checkout_get", args, exc)
        tool_context.state[STATE_CHECKOUT_VERSION] = view.current_version
        tool_context.state[STATE_CHECKOUT_HASH] = view.current.content_hash
        record = _load(tool_context)
        record.remember_checkout(view)
        _save(tool_context, record)
        payload = checkout_payload(view, ctx.turn, tool="checkout_get")
        ctx.turn.record_call(
            ctx.agent_name,
            "checkout_get",
            args,
            ok=True,
            summary={
                "current_version": view.current_version,
                "status": view.current.status.value,
                "payment_state": None if view.payment is None else view.payment.state,
            },
        )
        return payload

    return checkout_get


def _build_checkout_submit_approved(ctx: FactoryContext) -> ToolFunc:
    async def checkout_submit_approved(
        version: int, content_hash: str, tool_context: ToolContextLike
    ) -> dict[str, Any]:
        """Submit an already-approved version of the session's checkout for admission.

        The kernel decides. Its decision is returned verbatim with a `rendered_for_buyer`
        text you must include unchanged. An allowed decision means admitted, not paid.

        Args:
            version: The approved version number, as shown on its card this session.
            content_hash: The content hash of that version, exactly as shown on its card.
        """
        checkout_id = str(tool_context.state.get(STATE_CHECKOUT_ID, ""))
        args = {"checkout_id": checkout_id, "version": version, "content_hash": content_hash}
        if not checkout_id:
            return _missing("no_checkout", "No checkout exists yet.")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return _missing("invalid_version", "version is a positive whole number.")
        record = _load(tool_context)
        if held := check_checkout_provenance(record, checkout_id, version, content_hash):
            return _held(ctx, "checkout_submit_approved", args, held)
        async with session_write_lock(ctx.session_id):
            try:
                decision = await ctx.backend.checkout_submit_approved(
                    checkout_id, version, content_hash
                )
            except BackendError as exc:
                return _failure(ctx, "checkout_submit_approved", args, exc)
            if decision.next_version is not None:
                tool_context.state[STATE_CHECKOUT_VERSION] = decision.next_version
        payload = decision_payload(decision, ctx.turn)
        ctx.turn.record_call(
            ctx.agent_name,
            "checkout_submit_approved",
            args,
            ok=True,
            summary={
                "allowed": decision.allowed,
                "code": decision.code.value,
                "deltas": len(decision.deltas),
                "next_version": decision.next_version,
            },
        )
        return payload

    return checkout_submit_approved


def _build_order_track(ctx: FactoryContext) -> ToolFunc:
    async def order_track(order_id: str, tool_context: ToolContextLike) -> dict[str, Any]:
        """Read one order: state, verified payment evidence and refunds already issued.

        Read-only. Payment and refund state here come from Razorpay's own record; quote
        them, never restate them from memory.

        Args:
            order_id: The order reference the buyer or a tool gave you.
        """
        args = {"order_id": order_id}
        try:
            view = await ctx.backend.order_track(order_id)
        except BackendError as exc:
            return _failure(ctx, "order_track", args, exc)
        record = _load(tool_context)
        record.remember_order(view)
        _save(tool_context, record)
        payload = order_payload(view, ctx.turn, tool="order_track")
        ctx.turn.record_call(
            ctx.agent_name,
            "order_track",
            args,
            ok=True,
            summary={"order_id": view.order_id, "state": view.state.value},
        )
        return payload

    return order_track


#: Builders for every tool this unit can construct. Presentation tools (Unit D) and the
#: support, growth and case tools (their backend operations are not on
#: :class:`CommerceBackend` yet) arrive through ``extra_builders``.
# ------------------------------------------------------------------ presentation tools
#
# ADR 0004 section 1.7: the model *selects* a component and names ids; every fact on the
# card is joined here from the backend's own records. These tools therefore take
# identifiers and nothing else. There is no argument through which a price, a name, a
# total or a state could arrive from the model, which is a stronger guarantee than
# validating one away afterwards -- a card looks like the platform speaking, and a buyer
# reads a number on one as a fact about their money.
#
# Each re-reads its subject rather than replaying whatever the ledger happened to hold.
# The provenance gate has already established that this session legitimately saw the id;
# re-reading means the card shows what is true now, which matters most on exactly the
# screens where a stale figure would be worst.


def _build_present_products(ctx: FactoryContext) -> ToolFunc:
    async def present_products(skus: list[str], tool_context: ToolContextLike) -> dict[str, Any]:
        """Show product cards for SKUs this conversation has already resolved.

        Use after search or product to put the options in front of the buyer. Every fact
        shown is read from the catalogue; you choose which products, not what they say.

        Args:
            skus: Catalogue SKUs a tool returned in this conversation, e.g. AMUL-DAIRY-001.
        """
        args = {"skus": list(skus)}
        record = _load(tool_context)
        unknown = [sku for sku in skus if not record.knows_sku(sku)]
        if unknown:
            return _held(
                ctx,
                "present_products",
                args,
                Held(
                    GATE_PROVENANCE,
                    "sku_not_returned",
                    f"These SKUs were not returned by any tool in this conversation: {unknown}. "
                    "Resolve each with product or search first, then present only what came back.",
                    {"skus": unknown},
                ),
            )
        if not skus:
            return _missing("no_ids", "Name at least one SKU this conversation resolved.")
        cards = []
        for sku in skus[:MAX_PRESENTED_IDS]:
            try:
                cards.append(await ctx.backend.product(sku))
            except BackendError as exc:
                return _failure(ctx, "present_products", args, exc)
        payload = product_card(cards)
        ctx.turn.record_call(
            ctx.agent_name, "present_products", args, ok=True, summary={"count": len(cards)}
        )
        return payload

    return present_products


def _build_present_basket(ctx: FactoryContext) -> ToolFunc:
    async def present_basket(tool_context: ToolContextLike) -> dict[str, Any]:
        """Show the buyer their basket: every line, the quote, and anything unavailable.

        Takes no arguments. The basket is the one this session created, so naming one
        would be a way to look at somebody else's.
        """
        basket_id = str(tool_context.state.get(STATE_BASKET_ID, ""))
        if not basket_id:
            return _missing("no_basket", "This session has no basket yet. Call basket_create.")
        try:
            view = await ctx.backend.basket_get(basket_id)
        except BackendError as exc:
            return _failure(ctx, "present_basket", {}, exc)
        record = _load(tool_context)
        record.remember_basket(view)
        _save(tool_context, record)
        payload = basket_card(view)
        ctx.turn.record_call(
            ctx.agent_name,
            "present_basket",
            {},
            ok=True,
            summary={"lines": len(view.quote.lines) if view.quote else 0},
        )
        return payload

    return present_basket


def _build_present_approval(ctx: FactoryContext) -> ToolFunc:
    async def present_approval(tool_context: ToolContextLike) -> dict[str, Any]:
        """Show the approval card for this session's checkout: the version awaiting consent.

        Takes no arguments. The checkout is the one this session opened, exactly as
        checkout_get resolves it -- a checkout identifier is an identity, and a tool that
        let a model choose one would be a way to read somebody else's.

        The card names the trusted surface as where approval happens, because it does. You
        cannot approve, and neither can this card.
        """
        checkout_id = str(tool_context.state.get(STATE_CHECKOUT_ID, ""))
        args = {"checkout_id": checkout_id}
        if not checkout_id:
            return _missing("no_checkout", "No checkout exists yet. Call checkout_create.")
        try:
            view = await ctx.backend.checkout_get(checkout_id)
        except BackendError as exc:
            return _failure(ctx, "present_approval", args, exc)
        record = _load(tool_context)
        record.remember_checkout(view)
        _save(tool_context, record)
        payload = approval_card(view.current)
        ctx.turn.record_call(
            ctx.agent_name,
            "present_approval",
            args,
            ok=True,
            summary={"version": view.current_version},
        )
        return payload

    return present_approval


def _build_present_decision(ctx: FactoryContext) -> ToolFunc:
    async def present_decision(tool_context: ToolContextLike) -> dict[str, Any]:
        """Show the kernel's most recent decision, with every field that moved if it refused.

        Takes no arguments: it renders the decision this turn already produced. A decision
        is the one object holding both what the buyer approved and what is current now, so
        it cannot be reconstructed from a later read of the checkout.
        """
        if not ctx.turn.decisions:
            return _missing(
                "no_decision",
                "No kernel decision has been made in this turn. Submit an approved checkout first.",
            )
        decision = ctx.turn.decisions[-1]
        view = None
        checkout_id = str(tool_context.state.get(STATE_CHECKOUT_ID, ""))
        if checkout_id:
            try:
                view = await ctx.backend.checkout_get(checkout_id)
            except BackendError:
                # The decision alone carries the refusal and every delta, so a checkout the
                # backend cannot serve right now must not take the card down with it. The
                # card simply omits the fields that would have come from the read.
                view = None
        payload = decision_card(decision, view)
        ctx.turn.record_call(
            ctx.agent_name,
            "present_decision",
            {},
            ok=True,
            summary={"allowed": decision.allowed, "deltas": len(decision.deltas)},
        )
        return payload

    return present_decision


def _build_present_plan(ctx: FactoryContext) -> ToolFunc:
    async def present_plan(order_id: str, tool_context: ToolContextLike) -> dict[str, Any]:
        """Show what is verified about an order and what the platform will do next.

        The card offers no control that resolves anything, because in P0 nothing in the
        product does; a reviewer acts elsewhere. Do not promise a timeline it does not carry.

        Args:
            order_id: An order this conversation has already read.
        """
        args = {"order_id": order_id}
        record = _load(tool_context)
        if not record.knows_order(order_id):
            return _held(
                ctx,
                "present_plan",
                args,
                Held(
                    GATE_PROVENANCE,
                    "order_not_returned",
                    f"Order {order_id} was not returned by any tool in this conversation. "
                    "Read it with order_track first.",
                    args,
                ),
            )
        try:
            view = await ctx.backend.order_track(order_id)
        except BackendError as exc:
            return _failure(ctx, "present_plan", args, exc)
        record.remember_order(view)
        _save(tool_context, record)
        payload = plan_card(view)
        ctx.turn.record_call(
            ctx.agent_name, "present_plan", args, ok=True, summary={"state": str(view.state)}
        )
        return payload

    return present_plan


_BUILDERS: Final[Mapping[str, ToolBuilder]] = {
    "search": _build_search,
    "product": _build_product,
    "basket_create": _build_basket_create,
    "basket_set_line": _build_basket_set_line,
    "basket_get": _build_basket_get,
    "checkout_create": _build_checkout_create,
    "checkout_get": _build_checkout_get,
    "checkout_submit_approved": _build_checkout_submit_approved,
    "order_track": _build_order_track,
    "present_products": _build_present_products,
    "present_basket": _build_present_basket,
    "present_approval": _build_present_approval,
    "present_decision": _build_present_decision,
    "present_plan": _build_present_plan,
}


def _check_schema(name: str, func: ToolFunc) -> None:
    """Refuse a builder whose signature would let the model choose an identity."""
    params = inspect.signature(func).parameters
    leaked = IDENTITY_PARAMETER_NAMES & set(params)
    if leaked:
        raise ValueError(f"tool {name!r} exposes identity parameters {sorted(leaked)}")
    if _CONTEXT_PARAMETER not in params:
        raise ValueError(f"tool {name!r} must take {_CONTEXT_PARAMETER} to reach session state")


def _session_key(turn: TurnContext, session_id: str | None) -> str:
    """The write-lock key. The harness passes its session id; without one, the principal.

    A principal is created per session (specification 5.4), so its id is a per-session
    key too; the correlation id is preferred when present because it is what the audit
    row carries.
    """
    if session_id:
        return session_id
    if turn.principal.correlation_id is not None:
        return str(turn.principal.correlation_id)
    return turn.principal.principal_id


def build_toolset(
    binding: BindingLike | AgentRole,
    backend: CommerceBackend,
    turn: TurnContext,
    *,
    principal: AgentPrincipal | None = None,
    session_id: str | None = None,
    agent_name: str | None = None,
    extra_builders: Mapping[str, ToolBuilder] | None = None,
) -> BoundToolset:
    """Tools for a specialist that its bound principal may hold, with their gates.

    ``binding`` is what the harness produced (its ``specialist`` names the role, its
    ``principal`` is already ``harness ∩ allowlist`` through
    :func:`~agent_runtime.capabilities.broker.derive_principal` or the harness's own
    ``bind``), or a bare :class:`AgentRole` with ``principal`` given (or taken from the
    turn). A principal whose role disagrees with the role is refused, because a toolset
    built for one role and offered under another is how a capability leaks between
    specialists.

    A roster tool whose capability the principal lacks is not built at all: the model
    never sees a tool it would be denied. The gate still checks every call, so a tool that
    reached the model by some other route is denied as ``tool_not_bound``.
    """
    if isinstance(binding, AgentRole):
        role = binding
        bound = principal if principal is not None else turn.principal
    else:
        role = AgentRole(str(getattr(binding.specialist, "value", binding.specialist)))
        bound = binding.principal
        if principal is not None and principal != bound:
            raise ValueError("principal disagrees with the binding's principal")
    if bound.agent_role is not None and bound.agent_role != role.value:
        raise ValueError(
            f"principal role {bound.agent_role!r} does not match toolset role {role.value!r}"
        )
    name = agent_name or f"{role.value}_specialist"
    ctx = FactoryContext(
        principal=bound,
        backend=backend,
        turn=turn,
        session_id=_session_key(turn, session_id),
        agent_name=name,
    )
    builders: dict[str, ToolBuilder] = dict(_BUILDERS)
    for extra_name, builder in (extra_builders or {}).items():
        if extra_name not in REGISTRY_A:
            raise ValueError(f"builder for {extra_name!r}: not a Registry A tool")
        builders[extra_name] = builder

    tools: list[BoundTool] = []
    unbuilt: list[str] = []
    for tool_name in tools_for_role(role):
        capability = REGISTRY_A[tool_name]
        if not bound.can(capability.value):
            continue
        make = builders.get(tool_name)
        if make is None:
            unbuilt.append(tool_name)
            continue
        func = make(ctx)
        _check_schema(tool_name, func)
        tools.append(
            BoundTool(
                name=tool_name,
                capability=capability,
                func=func,
                writes=tool_name in WRITE_TOOLS,
            )
        )

    names = frozenset(tool.name for tool in tools)
    return BoundToolset(
        agent_name=name,
        principal=bound,
        tools=tuple(tools),
        gate=make_capability_gate(bound, turn, agent_name=name, bound_tools=names),
        error_gate=make_tool_error_gate(turn, agent_name=name),
        unbuilt=tuple(unbuilt),
    )


def build_tools(
    *,
    role: AgentRole,
    backend: CommerceBackend,
    turn: TurnContext,
    agent_name: str,
    session_id: str | None = None,
) -> list[BoundTool]:
    """The tools alone, for a caller that registers the gates itself.

    Kept for the ADK adapter's existing seam. The principal is the turn's, which the
    adapter has already bound; the gates are rebuilt there with the same principal and
    turn, so a denial recorded by either lands on the same evidence record. Prefer
    :func:`build_toolset`, which hands back the gates the factory built.
    """
    toolset = build_toolset(
        role, backend, turn, principal=turn.principal, session_id=session_id, agent_name=agent_name
    )
    return list(toolset.tools)

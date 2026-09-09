"""The tool factory: the only place a specialist's tools are constructed.

``build_toolset`` binds one principal, one backend, one session and one turn into a
:class:`BoundToolset`: the tools the principal may hold, the capability gate that runs
before each of them, and the error gate that turns an exception into a result. The
runtime adapter wraps each :class:`BoundTool` in its own tool class (ADK's
``FunctionTool(tool.func)``) and registers the two gates; it never builds a tool of its
own. A test asserts every specialist's tools come from here, by object identity.

Tools are closures so that the principal, session and turn ledger are captured in code,
not passed as model-visible arguments. A model cannot hand a tool a different tenant,
session, cart or principal because there is no parameter for one (specification 20.2:
the server supplies identity). The session's cart and checkout are read from session
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

from commerce_domain import AgentPrincipal, Money

from ..backends.base import (
    BackendError,
    CommerceBackend,
    PolicyTerm,
    ResolutionPlan,
    SupportBackend,
)
from ..core.provenance import (
    GATE_PROVENANCE,
    PROVENANCE_STATE_KEY,
    Held,
    SessionProvenance,
    check_line_count,
    check_order_provenance,
    check_quantity,
    check_sku_provenance,
    session_write_lock,
)
from ..grounding.fence import fence_untrusted
from ..grounding.payloads import (
    approval_payload,
    cart_payload,
    checkout_payload,
    order_payload,
    product_payload,
    search_payload,
)
from ..rendering.cards import (
    approval_card,
    cart_card,
    plan_card,
    product_card,
)
from ..rendering.money import display_minor
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
    "STATE_CART_ID",
    "STATE_CHECKOUT_HASH",
    "STATE_CHECKOUT_ID",
    "STATE_CHECKOUT_VERSION",
    "AgentRole",
    "BindingLike",
    "BoundTool",
    "BoundToolset",
    "FactoryContext",
    "SupportToolBuilder",
    "ToolBuilder",
    "ToolFunc",
    "build_tools",
    "build_toolset",
]

#: Session-state keys the tools maintain. IDs only; never product data.
STATE_CART_ID: Final[str] = "cart_id"

#: How many ids one present call may name. A model asked to show the options that dumps
#: forty SKUs onto the screen has stopped choosing; the card reports the full count so a
#: surface can say how many were left out rather than silently truncating.
MAX_PRESENTED_IDS: Final[int] = 8
STATE_CHECKOUT_ID: Final[str] = "checkout_id"
STATE_CHECKOUT_VERSION: Final[str] = "checkout_version"
STATE_CHECKOUT_HASH: Final[str] = "checkout_content_hash"


#: Parameter names no tool schema may carry. Identity is the server's (spec 20.2); a tool
#: that took one of these would let the model choose whose cart it writes to.
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
        "cart_id",
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

#: A builder that needs the two post-purchase reads, taken as an argument for the same
#: reason the other two are: the factory hands a
#: :class:`~agent_runtime.backends.base.SupportBackend` over only when the backend really
#: carries that surface, so the closure holds a checked reference rather than a cast the
#: type checker was talked out of. A backend that is a bare ``CommerceBackend`` leaves the
#: rows in ``unbuilt`` instead, which is the honest report that no closure was built.
SupportToolBuilder = Callable[[FactoryContext, SupportBackend], ToolFunc]


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
    ``unbuilt`` lists roster tools the principal may hold but that this toolset could not
    construct: the remaining support tools, whose backend operations do not exist yet. They are
    reported rather than silently dropped, because a roster row with no closure is a real
    gap and a test should be able to see it. A tool that is offered and fails when called
    is the same gap discovered later, by whoever asked the question.
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
        may be mentioned or added to the cart afterwards.

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
        """Create a new, empty cart for this session. Call once, then add lines."""
        async with session_write_lock(ctx.session_id):
            existing = str(tool_context.state.get(STATE_CART_ID, ""))
            if existing:
                # Idempotent on purpose: a model that calls this twice must not orphan a
                # cart the buyer already filled.
                try:
                    view = await ctx.backend.basket_get(existing)
                except BackendError as exc:
                    return _failure(ctx, "basket_create", {"existing": existing}, exc)
            else:
                try:
                    view = await ctx.backend.basket_create()
                except BackendError as exc:
                    return _failure(ctx, "basket_create", {}, exc)
                tool_context.state[STATE_CART_ID] = view.cart_id
            record = _load(tool_context)
            record.remember_cart(view)
            _save(tool_context, record)
        payload = cart_payload(view, ctx.turn, tool="basket_create")
        ctx.turn.record_call(
            ctx.agent_name, "basket_create", {}, ok=True, summary={"cart_id": view.cart_id}
        )
        return payload

    return basket_create


def _build_basket_set_line(ctx: FactoryContext) -> ToolFunc:
    async def basket_set_line(
        sku: str, quantity: int, tool_context: ToolContextLike
    ) -> dict[str, Any]:
        """Set the quantity of one SKU in the cart; 0 removes it. Returns the exact quote.

        Only a SKU that search or product returned in this session can be written. The
        quote is computed by the merchant's deterministic fee engine: read every price,
        tax, delivery fee, total and free-delivery gap from it; never compute one.

        Args:
            sku: A SKU returned by search or product in this session.
            quantity: Whole units, 0 to remove, at most 50.
        """
        cart_id = str(tool_context.state.get(STATE_CART_ID, ""))
        args = {"cart_id": cart_id, "sku": sku, "quantity": quantity}
        if not cart_id:
            return _missing("no_basket", "Call basket_create first.")
        if held := check_quantity(quantity):
            return _held(ctx, "basket_set_line", args, held)
        record = _load(tool_context)
        async with session_write_lock(ctx.session_id):
            try:
                current = await ctx.backend.basket_get(cart_id)
            except BackendError as exc:
                return _failure(ctx, "basket_set_line", args, exc)
            current_skus = tuple(line_sku for line_sku, _ in current.lines)
            if held := check_sku_provenance(record, sku, cart_lines=current_skus):
                return _held(ctx, "basket_set_line", args, held)
            if held := check_line_count(current_skus, sku, quantity):
                return _held(ctx, "basket_set_line", args, held)
            try:
                view = await ctx.backend.basket_set_line(cart_id, sku, quantity)
            except BackendError as exc:
                return _failure(ctx, "basket_set_line", args, exc)
            record.remember_cart(view)
            _save(tool_context, record)
        payload = cart_payload(view, ctx.turn, tool="basket_set_line")
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


def _build_basket_propose_line(ctx: FactoryContext) -> ToolFunc:
    async def basket_propose_line(
        sku: str, quantity: int, tool_context: ToolContextLike
    ) -> dict[str, Any]:
        """Stage adding units of one SKU to the buyer's cart. It changes nothing itself.

        Use this when the buyer asks to add, buy, or take something you have already read
        with search or product. The record it returns is what the buyer's surface acts on:
        the platform performs the add on the buyer's own instruction, re-checking the price
        and the stock under the cart's lock as it does. You never add anything yourself,
        so report it as being added -- "I'm adding it to your cart" -- and never as done
        until the surface has confirmed it.

        Args:
            sku: A SKU returned by search or product in this session.
            quantity: Whole units to add on top of what the cart already holds, 1 to 50.
        """
        cart_id = str(tool_context.state.get(STATE_CART_ID, ""))
        args = {"cart_id": cart_id, "sku": sku, "quantity": quantity}
        if quantity < 1:
            return _missing("quantity", "Propose at least one unit.")
        if held := check_quantity(quantity):
            return _held(ctx, "basket_propose_line", args, held)
        record = _load(tool_context)
        if held := check_sku_provenance(record, sku):
            return _held(ctx, "basket_propose_line", args, held)
        try:
            proposal = await ctx.backend.basket_propose_line(cart_id or None, sku, quantity)
        except BackendError as exc:
            return _failure(ctx, "basket_propose_line", args, exc)
        ctx.turn.record_call(
            ctx.agent_name,
            "basket_propose_line",
            args,
            ok=True,
            summary={
                "quantity": proposal.get("quantity"),
                "blocked_by": proposal.get("blocked_by"),
            },
        )
        return {"kind": "proposal", **proposal}

    return basket_propose_line


def _build_basket_get(ctx: FactoryContext) -> ToolFunc:
    async def basket_get(tool_context: ToolContextLike) -> dict[str, Any]:
        """Re-quote the session's cart and report whether merchant state moved since."""
        cart_id = str(tool_context.state.get(STATE_CART_ID, ""))
        args = {"cart_id": cart_id}
        if not cart_id:
            return _missing("no_basket", "Call basket_create first.")
        try:
            view = await ctx.backend.basket_get(cart_id)
        except BackendError as exc:
            return _failure(ctx, "basket_get", args, exc)
        payload = cart_payload(view, ctx.turn, tool="basket_get")
        record = _load(tool_context)
        record.remember_cart(view)
        _save(tool_context, record)
        ctx.turn.record_call(
            ctx.agent_name, "basket_get", args, ok=True, summary={"stale": view.stale}
        )
        return payload

    return basket_get


def _build_checkout_create(ctx: FactoryContext) -> ToolFunc:
    async def checkout_create(tool_context: ToolContextLike) -> dict[str, Any]:
        """Create checkout version 1 from the session's cart and return its approval card.

        The card states exactly what the buyer will approve on the trusted surface. This
        tool cannot approve anything.
        """
        cart_id = str(tool_context.state.get(STATE_CART_ID, ""))
        args = {"cart_id": cart_id}
        if not cart_id:
            return _missing("no_basket", "No cart exists yet.")
        async with session_write_lock(ctx.session_id):
            try:
                card = await ctx.backend.checkout_create(cart_id)
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


#: Builders for every tool this unit can construct against a bare
#: :class:`CommerceBackend`. Tools whose backend operations do not exist yet arrive
#: through ``extra_builders`` when they do.
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
        """Show the buyer their cart: every line, the quote, and anything unavailable.

        Takes no arguments. The cart is the one this session created, so naming one
        would be a way to look at somebody else's.
        """
        cart_id = str(tool_context.state.get(STATE_CART_ID, ""))
        if not cart_id:
            return _missing("no_basket", "This session has no cart yet. Call basket_create.")
        try:
            view = await ctx.backend.basket_get(cart_id)
        except BackendError as exc:
            return _failure(ctx, "present_basket", {}, exc)
        record = _load(tool_context)
        record.remember_cart(view)
        _save(tool_context, record)
        payload = cart_card(view)
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


# ------------------------------------------------------------------ post-purchase reads
#
# The two reads a Support Specialist needs before it may say what a buyer is owed: the
# rules the sale was actually made under, and the findings the reconciliation service
# raised against the payment provider with the plan that settles each. Both mirror
# ``order_track`` and the case reads precisely -- they take a subject and nothing else,
# they populate the turn's grounding ledger so a reply may quote what was read, and a
# backend error comes back as the same structured refusal every other read gives.
#
# Neither writes. There is deliberately no ``support_escalate`` builder beside them:
# opening a human-review case freezes a payment attempt on a terminal transition, which
# is a write on the money path whose kernel primitive carries no who/why gate, so it
# stays in ``unbuilt`` (docs/KNOWN_GAPS.md) rather than being handed a closure here.


def _build_policy_search(ctx: FactoryContext, support: SupportBackend) -> ToolFunc:
    async def policy_search(order_id: str, tool_context: ToolContextLike) -> dict[str, Any]:
        """Read the rules one sale was made under: the Policy-at-Sale Receipt, never today's.

        Name the order and nothing else. What comes back is the document frozen when the
        sale was made -- the merchant's return, refund, cancellation and substitution
        terms as they stood then -- not the merchant's current catalogue rules, which may
        have changed since. Quote a term against the `policy_id` and `policy_version`
        beside it, so a dispute can be argued against the version the buyer was shown.

        Check `binding_ok` before you rely on any term. When it is false the platform
        could not re-derive the checkout/receipt binding from stored rows, so `policies`
        is empty and `binding_code` says which way it broke. An empty list there is a
        verification failure, not permission: never read "no terms" as "no rules apply"
        and never tell a buyer a sale had no return policy on the strength of it.

        Each term's `terms` mapping is the receipt's own wording, carried through and
        never summarised here. It is text a merchant wrote, so it arrives fenced; report
        what it says and cite the version, and do not reword a term into a rule of your
        own that would then have no document behind it.

        This reads and changes nothing. It cannot open a case, promise a refund or
        escalate anything; a remedy comes from resolution_evaluate and an amount lives on
        a plan there, never here.

        Args:
            order_id: The order reference the buyer or a tool gave you.
        """
        args = {"order_id": order_id}
        try:
            policy = await support.order_policy(order_id)
        except BackendError as exc:
            return _failure(ctx, "policy_search", args, exc)
        # The subject is grounded from the argument the backend answered for, exactly as
        # ``order_track`` remembers the order it read: a later present or resolution call
        # is then held to an order this conversation actually saw, and the reply
        # post-check will let the specialist name it in prose.
        record = _load(tool_context)
        record.remember_order_id(policy.order_id)
        _save(tool_context, record)
        payload: dict[str, Any] = {
            "ok": True,
            "order_id": policy.order_id,
            # The answer, not a status beside it: when the binding did not verify the
            # platform returns no terms, and reading `policies` without first reading
            # this boolean is how a verification failure gets read as permission.
            "binding_ok": policy.binding_ok,
            "binding_code": policy.binding_code.value,
            "receipt_hash": policy.receipt_hash,
            "policies": [_policy_term_row(ctx, term) for term in policy.policies],
        }
        ctx.turn.record_call(
            ctx.agent_name,
            "policy_search",
            args,
            ok=True,
            summary={
                "order_id": policy.order_id,
                "binding_ok": policy.binding_ok,
                "kinds": sorted(term.kind.value for term in policy.policies),
            },
        )
        return payload

    return policy_search


def _policy_term_row(ctx: FactoryContext, term: PolicyTerm) -> dict[str, Any]:
    """One at-sale rule as the model reads it: closed vocabulary verbatim, wording fenced.

    `kind`, `policy_id` and `policy_version` are the platform's own identifiers and are
    carried through unchanged, because a term cited against a version the buyer can look
    up is the whole point of a receipt. `terms` is the merchant's own wording, so every
    string in it arrives inside the fence with a flag on the row when one trips a pattern
    -- a receipt is a place a merchant's text could carry an instruction aimed at the
    agent, and the model must not mistake what a merchant wrote for something addressed
    to it. Numbers and booleans are left as they are, so a term's amount or flag stays a
    value rather than becoming a string.
    """
    fenced_terms: dict[str, Any] = {}
    flags: list[str] = []
    for key, value in term.terms.items():
        if value is None or isinstance(value, bool | int):
            fenced_terms[key] = value
            continue
        fenced = fence_untrusted(str(value))
        fenced_terms[key] = fenced.text
        flags.extend(fenced.flags)
    if flags:
        ctx.turn.record_flag("policy_search", term.policy_id, tuple(flags))
    return {
        "kind": term.kind.value,
        "policy_id": term.policy_id,
        "policy_version": term.policy_version,
        "applies_to": list(term.applies_to),
        "terms": fenced_terms,
        "quarantined": bool(flags),
    }


def _build_resolution_evaluate(ctx: FactoryContext, support: SupportBackend) -> ToolFunc:
    async def resolution_evaluate(order_id: str, tool_context: ToolContextLike) -> dict[str, Any]:
        """Read every finding on one order and the plan that would settle each.

        Name the order and nothing else. What comes back is what the reconciliation
        service found comparing this sale against the payment provider's own record, and
        for each finding the recovery `code` and, only where a plan was actually issued,
        the remedy options with their exact amounts.

        `findings` is reported alongside the list on purpose: zero findings is a
        measurement -- the service looked and nothing diverged -- and is a different, and
        better, answer than "no evaluation happened". Say "nothing was found to be wrong",
        never "nothing is wrong", and never read an empty list as either without checking
        the count.

        Every amount is the platform's, in integer minor units, and is recorded as a
        grounded fact so you may quote it. Do no arithmetic on one and never present an
        amount larger than a plan's `refundable_minor`; the plan already refuses that, and
        an option you compute yourself has no plan behind it. A plan that was issued
        reports `plan_ttl_seconds`, the window the amount stands for: quote the amount
        with the window, never on its own. A `withheld` remedy carries a closed reason it
        was not offered -- report the reason as given; it is not a remedy you may talk the
        buyer back into.

        This reads and evaluates; it settles nothing. No refund is issued and no case is
        opened here. A buyer acts on a plan through the surface that presents it, and a
        stuck payment goes to human review on its own gated seam, not from this tool.

        Args:
            order_id: The order reference the buyer or a tool gave you.
        """
        args = {"order_id": order_id}
        # Deliberately NOT gated on order provenance, though `_spec.py` lists it. This tool
        # is one of the three that *establish* order provenance -- it calls
        # `remember_order_id` on the way out -- so gating it on its own output is circular:
        # the only tools that could ground the order are the ones the gate would refuse.
        # `test_a_resolution_read_changes_nothing_it_grounds_the_order` encodes exactly
        # that role. `support_escalate` is the one that acts on an order rather than
        # reading it, and that one is gated.
        try:
            resolution = await support.order_resolution(order_id)
        except BackendError as exc:
            return _failure(ctx, "resolution_evaluate", args, exc)
        # Grounded from the argument the backend answered for, as ``order_track`` and
        # ``policy_search`` do: the subject a later call is held to is the order this
        # read actually saw, not whatever string the model happened to pass.
        record = _load(tool_context)
        record.remember_order_id(resolution.order_id)
        _save(tool_context, record)
        payload: dict[str, Any] = {
            "ok": True,
            "order_id": resolution.order_id,
            "recorded_state": resolution.recorded_state,
            # The count travels beside the list so an empty `plans` reads as "looked and
            # found nothing" rather than "nothing was looked at"; the domain type refuses
            # the two disagreeing, so a reader can trust they match.
            "findings": resolution.findings,
            # ``None`` and not ``0``: a backend that issued no plan reported no window, and
            # zero seconds would read as an amount that expired the instant it was read.
            "plan_ttl_seconds": resolution.plan_ttl_seconds,
            "plans": [_resolution_plan_row(ctx, plan) for plan in resolution.plans],
        }
        ctx.turn.record_call(
            ctx.agent_name,
            "resolution_evaluate",
            args,
            ok=True,
            summary={
                "order_id": resolution.order_id,
                "findings": resolution.findings,
                "codes": sorted(plan.code.value for plan in resolution.plans),
            },
        )
        return payload

    return resolution_evaluate


def _amount_field(minor: int | None, currency: str, turn: TurnContext) -> dict[str, Any]:
    """One money figure for a tool result: the integer, its display string, or neither."""
    if minor is None:
        return {"minor": None, "display": None, "measured": False}
    turn.ledger.record_money(Money(minor, currency))
    return {"minor": minor, "display": display_minor(minor, currency), "measured": True}


def _resolution_plan_row(ctx: FactoryContext, plan: ResolutionPlan) -> dict[str, Any]:
    """One finding's plan as the model reads it, every amount grounded before it is shown.

    The recovery `code` and the three capture-ledger figures are the platform's own, and
    each money figure goes through :func:`_amount_field` so it is recorded on the turn's
    grounding ledger -- what lets the specialist quote it in prose without the reply
    post-check treating it as invented. `explanation` and each option's `basis` are text
    the platform wrote rather than a third party, but they arrive fenced anyway: a tool result is
    model-visible, and fencing a platform string costs nothing while guessing which
    strings are safe to leave open is how the one that was not gets through.
    """
    return {
        "finding_id": plan.finding_id,
        "code": plan.code.value,
        "plan_id": plan.plan_id,
        "recorded": plan.recorded,
        "valid_until": plan.valid_until.isoformat() if plan.valid_until is not None else None,
        "captured": _amount_field(plan.captured_minor, plan.currency, ctx.turn),
        "refunds_reserved": _amount_field(plan.refunds_reserved_minor, plan.currency, ctx.turn),
        "refundable": _amount_field(plan.refundable_minor, plan.currency, ctx.turn),
        "explanation": fence_untrusted(plan.explanation).text,
        "options": [
            {
                "outcome": option.outcome.value,
                "amount": _amount_field(option.amount.minor, option.amount.currency, ctx.turn),
                "policy_kind": option.policy_kind.value,
                "policy_id": option.policy_id,
                "policy_version": option.policy_version,
                "confirmation": option.confirmation.value,
                "basis": fence_untrusted(option.basis).text,
            }
            for option in plan.options
        ],
        "withheld": [
            {
                "outcome": withheld.outcome.value,
                "reason": withheld.reason.value,
                "detail": fence_untrusted(withheld.detail).text,
            }
            for withheld in plan.withheld
        ],
    }


_BUILDERS: Final[Mapping[str, ToolBuilder]] = {
    "search": _build_search,
    "product": _build_product,
    "basket_create": _build_basket_create,
    "basket_set_line": _build_basket_set_line,
    "basket_propose_line": _build_basket_propose_line,
    "basket_get": _build_basket_get,
    "checkout_create": _build_checkout_create,
    "checkout_get": _build_checkout_get,
    "order_track": _build_order_track,
    "present_products": _build_present_products,
    "present_basket": _build_present_basket,
    "present_approval": _build_present_approval,
    "present_plan": _build_present_plan,
}


#: Builders that need the two post-purchase reads. A backend without that surface leaves both
#: rows in ``unbuilt`` rather than being handed a closure that would have to invent a rule
def _build_support_escalate(ctx: FactoryContext, support: SupportBackend) -> ToolFunc:
    async def support_escalate(
        order_id: str, reason: str, note: str, tool_context: ToolContextLike
    ) -> dict[str, Any]:
        """Hand this order to a person, and decide nothing yourself.

        This is the end of what you may do about a buyer who wants money back. You may
        read the sale's terms with `policy_search` and tell the buyer what the merchant
        promised; you may not work out what they are owed, and you cannot pay it. A person
        on the merchant's side reads the case and settles it.

        What comes back is a `case_id` and nothing else that resembles an outcome. Give the
        buyer that reference and say a person will answer. Do not add an amount, a date, a
        likelihood or a reassurance beside it: none of those has been decided, and a
        sentence that sounds like a decision is one the merchant then has to honour or
        withdraw.

        Ask twice and you get the same case back rather than a second one, so it is safe to
        retry. `reason` must be one the store recognises -- the same words the buyer's own
        order screen uses -- and an invented one is refused rather than filed.

        Args:
            order_id: The order the buyer is asking about.
            reason: One of buyer_requested, item_not_delivered, item_damaged, wrong_item,
                ordered_by_mistake.
            note: What the buyer said, in their words. Passed to a person unchanged.
        """
        args = {"order_id": order_id, "reason": reason}
        record = _load(tool_context)
        if held := check_order_provenance(record, order_id):
            return _held(ctx, "support_escalate", args, held)
        # Under the session write lock, as the spec declares: opening a case is a write,
        # and two turns racing would otherwise open two cases for one complaint.
        async with session_write_lock(ctx.session_id):
            try:
                case = await support.open_support_case(order_id, reason, note)
            except BackendError as exc:
                return _failure(ctx, "support_escalate", args, exc)
        record.remember_order_id(case.order_id)
        _save(tool_context, record)
        payload: dict[str, Any] = {
            "ok": True,
            "case_id": case.case_id,
            "order_id": case.order_id,
            "reason": case.reason,
            "status": case.status,
        }
        ctx.turn.record_call(
            ctx.agent_name,
            "support_escalate",
            args,
            ok=True,
            summary={"case_id": case.case_id, "order_id": case.order_id},
        )
        return payload

    return support_escalate


#: or an amount.
#:
#: ``support_escalate`` is here now, and the reason it used to be absent is worth keeping
#: because it is still true of the thing it described: opening a *human-review case*
#: freezes a payment attempt on a terminal transition, which is a write on the money path
#: and belongs behind its own gate. This is not that. It writes one row on the merchant's
#: support queue naming an order and a reason, touches no payment attempt and no financial
#: table, and returns a case id rather than an outcome. The model's reach here ends at
#: "a person should look at this".
_SUPPORT_BUILDERS: Final[Mapping[str, SupportToolBuilder]] = {
    "policy_search": _build_policy_search,
    "resolution_evaluate": _build_resolution_evaluate,
    "support_escalate": _build_support_escalate,
}


def _bind_support(build: SupportToolBuilder, support: SupportBackend) -> ToolBuilder:
    """Fix a checked support backend into a builder so the factory's loop stays one shape."""

    def bound(ctx: FactoryContext) -> ToolFunc:
        return build(ctx, support)

    return bound


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
    wrap: Callable[[BoundTool], BoundTool] | None = None,
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

    ``wrap`` decorates each built tool *before* the gate is bound, and exists because the
    alternative does not work. A caller that wants to observe results -- the API's bridge
    wraps every closure to catch a proposal on its way out -- used to rewrap the finished
    toolset with :func:`dataclasses.replace`. That keeps the gate object, which holds the
    *original* closures in ``bound_callables``, while every tool now carries a new one, so
    the identity check below refused the factory's own tools and every model tool call was
    denied ``tool_not_bound``. Wrapping here instead means the gate is built from the
    closures that will actually run, and the factory keeps its monopoly: the hook belongs
    to whoever is already allowed to build the toolset, so a hand-made tool still cannot
    reach the gate by this route.
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
    if isinstance(backend, SupportBackend):
        # The same rule a third time, for the two post-purchase reads. A backend that does
        # not carry the support surface leaves ``policy_search`` and ``resolution_evaluate``
        # in ``unbuilt`` rather than being handed a closure that would have to invent an
        # at-sale term or a remedy amount -- the two things on this platform that decide
        # what a buyer is owed, and precisely what a shopping backend must not reach. Note
        # what is not looped here: there is no ``support_escalate`` builder to bind, so it
        # is reported unbuilt whatever surface the backend has.
        for support_name, support_builder in _SUPPORT_BUILDERS.items():
            builders[support_name] = _bind_support(support_builder, backend)
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

    if wrap is not None:
        # Before `names` and `bound_callables` are taken, so both describe the tools the
        # runtime will call rather than the ones this function happened to construct.
        tools = [wrap(tool) for tool in tools]

    names = frozenset(tool.name for tool in tools)
    return BoundToolset(
        agent_name=name,
        principal=bound,
        tools=tuple(tools),
        gate=make_capability_gate(
            bound,
            turn,
            agent_name=name,
            bound_tools=names,
            bound_callables=tuple(tool.func for tool in tools),
        ),
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

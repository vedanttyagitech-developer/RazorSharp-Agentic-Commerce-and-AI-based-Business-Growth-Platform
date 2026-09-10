"""The seam: ``agent_runtime``'s harness runner, presented as this service's ``TurnRunner``.

Two halves of this product grew to different contracts and nothing forced them to meet.
``agent_runtime.harness.SpecialistRunner`` is ``async __call__(bound, message, turn,
session) -> SpecialistReply``; ``agent_service.TurnRunner`` is ``run(turn, chosen, tools)
-> TurnOutcome``, synchronous. Different name, different arity, different types, different
colour. Until this module existed no agent in this product had ever called a model, and
``app.py`` declined to attach the ADK runner and said why.

This is the adapter, and it lives here rather than in ``agent_runtime`` because it is the
only object that has to know **both** vocabularies -- the API's tool names and payloads on
one side, Registry A's tool names and frozen dataclasses on the other. Putting it in the
runtime would make the runtime depend on the service it is supposed to be independent of.

It imports nothing from ``google.*``. The model runtime arrives as a
:class:`~agent_runtime.harness.base.SpecialistRunner`, injected by ``app.py`` behind its
lazy import, so this module -- and every test of it -- runs where ``google-adk`` is not
installed and Vertex is not configured.

**What it bridges, and what it deliberately does not.**

Shopping is model-backed: :attr:`~agent_service.Specialist.SHOPPING`. Every other route
keeps the deterministic runner, and that is a structural choice rather than a staging plan:

* Checkout and Support remain deterministic: Checkout needs a ``checkout.read`` string
  this service does not mint, and Support's grounding rules force writes and terms.
* The translation table :data:`READS_ONLY_CAPABILITIES` translates a buyer principal's
  ``catalogue.read`` and ``basket.write`` into Registry A's reads and proposal capability.

:meth:`SpecialistBridge.run` therefore delegates every other specialist to
:class:`~agent_service.DeterministicRunner` **without** the "reasoning layer unavailable"
sentence.

**What is enforced here.**

* *The tool ledger records every call and every denial.* Every read a model makes lands on
  this service's :class:`~agent_service.ToolExecutor`, through
  :meth:`~agent_service.ToolExecutor.call`, through every gate it already has, and onto
  the same :class:`~agent_service.TurnLedger` the panel renders. The bridge holds no
  database handle and no service import: :class:`_MerchantReads` can reach a row only by
  asking the executor for it.
* *The grounding post-check still runs.* :func:`~agent_runtime.grounding.verify_reply` and
  :func:`~agent_runtime.harness.base.enforce_conversational_rules` are called here, in the
  order ``Harness.run`` calls them, over the ``TurnContext`` the factory's tools filled.
  A bridged turn that skipped them would be strictly worse than the template it replaces,
  and invisibly so, because an invented total reads exactly like a counted one.
* *No tool signature gains an identity parameter.* The tools are the factory's own,
  built by :func:`~agent_runtime.capabilities.tools.build_toolset`, which refuses any
  closure naming one. This module builds no tool of its own.
* *A raising bridge is still a deterministic answer.* ``run_turn``'s ``except`` is what
  specification 30 promises, and everything here raises rather than returning a half-turn.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

from agent_runtime.backends.base import (
    ApprovalCard,
    BackendError,
    CartQuote,
    CartView,
    CheckoutView,
    CommerceBackend,
    OrderView,
    PricedLine,
    ProductCard,
    Provenance,
    SearchPage,
    UnavailableLine,
    backend_problem,
)
from agent_runtime.capabilities.registry import REGISTRY_A, WRITE_TOOLS, Capability
from agent_runtime.capabilities.tools import STATE_CART_ID, BoundTool, BoundToolset, build_toolset
from agent_runtime.grounding import verify_reply
from agent_runtime.harness import BUYER_SPECIALISTS
from agent_runtime.harness.base import (
    BoundSpecialist,
    SpecialistInput,
    SpecialistRunner,
    bind,
    enforce_conversational_rules,
    prefetch_grounding,
)
from agent_runtime.harness.session import CopilotSession, Modality
from agent_runtime.language import Language
from agent_runtime.rendering import render_fallback
from agent_runtime.specialists import spec_for
from agent_runtime.turn import TurnContext
from commerce_domain import AdmissionDecision, Money, RecoveryCode
from merchant_sim import Locale

from .agent_service import (
    DeterministicRunner,
    Route,
    Specialist,
    ToolExecutor,
    ToolResult,
    TurnInput,
    TurnOutcome,
    line_proposal_record,
)

__all__ = [
    "BRIDGED_SPECIALISTS",
    "CURRENCY",
    "READS_ONLY_CAPABILITIES",
    "TURN_TIMEOUT_S",
    "BridgeUnavailableError",
    "SpecialistBridge",
    "registry_a_capabilities",
]

_log = logging.getLogger("commerce_api.agent.bridge")

#: The specialists this bridge answers with a model.
#:
#: Shopping is bridged because :data:`READS_ONLY_CAPABILITIES` translates a buyer principal
#: through :func:`registry_a_capabilities` to hold ``catalog.search``, ``catalog.get_product``,
#: ``quote.request``, and ``basket.propose_line``, which intersects Shopping's allowlist
#: non-empty and builds the necessary tools.
#:
#: What it deliberately does not build is the reason this is safe rather than merely
#: possible. ``basket.create`` and ``basket.update`` are withheld by the table, so
#: ``build_toolset`` never constructs ``basket_create`` or ``basket_set_line`` and the model
#: is never shown a tool it would be denied. A cart line still changes only when the buyer
#: presses a proposal the route re-checks under the cart's lock. The model gained the
#: ability to *reason* about a cart, not to *move* one.
#:
#: Support and Checkout remain deterministic:
#:
#: * Support's own grounding rules force ``resolution_evaluate`` on a refund request, and
#:   that tool is a write -- it takes the session write lock and passes a provenance gate.
#: * Checkout needs a ``checkout.read`` string this service does not mint, so it would bind
#:   with no ``checkout_get``.
BRIDGED_SPECIALISTS: Final[frozenset[Specialist]] = frozenset({Specialist.SHOPPING})

#: Registry A capabilities that a reads-only translation may never grant.
#:
#: Derived from :data:`~agent_runtime.capabilities.registry.WRITE_TOOLS` rather than written
#: out, which is the whole point: a tool that becomes a write later moves into that set at
#: its own definition site and is refused here at import, with no second list to remember to
#: update. ``resolution_evaluate`` is the row that makes this worth doing -- it reads like a
#: question and is a write, because its closure takes the session write lock and passes a
#: provenance gate before the backend sees it.
_WRITE_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    REGISTRY_A[name] for name in WRITE_TOOLS
)

#: Capabilities that some tool in Registry A actually requires. A capability outside this
#: set can be granted without granting anything, which is a worse failure than being denied:
#: ``bind`` succeeds, the toolset comes back short, and the model answers from nothing.
_TOOLED_CAPABILITIES: Final[frozenset[Capability]] = frozenset(REGISTRY_A.values())

#: This service's capability strings -> the Registry A capabilities a **model-backed** turn
#: may hold under them. The reviewed decision the module docstring says is not written here.
#:
#: It is written as reads-only, and the reason is not caution about models. The buyer's
#: writes already have a better path than a tool call: a proposal the buyer presses. The
#: line proposal card sends the absolute quantity and the binding the agent proposed, the
#: route compares that binding under the cart's lock, and a stale proposal is refused as
#: ``proposal_superseded`` rather than silently applied. A model calling ``basket_set_line``
#: directly would bypass a mechanism that already exists and works, to arrive at the same
#: cart with less evidence and no press.
#:
#: Row by row, because each is a decision and not a rename:
#:
#: * ``catalogue.read`` -> ``catalog.search`` and ``catalog.get_product``. The two halves of
#:   discovery, and both are reads in every sense. ``inventory.check`` is deliberately absent:
#:   no row of Registry A requires it, so granting it would widen the principal's printed
#:   capability list without widening what it can do.
#: * ``basket.write`` -> ``quote.request`` **only**. This is the row the docstring meant.
#:   ``basket.write`` is one string on this side and five capabilities on the other, and the
#:   honest reading for a model is the one that lets it *see* a cart without *changing*
#:   one. ``quote.request`` is what ``basket_get`` and ``present_basket`` require, and
#:   ``basket_get`` re-quotes rather than mutating -- Registry A says so at its own row.
#:   ``basket.create`` and ``basket.update`` are withheld, so ``build_toolset`` never
#:   constructs those closures and the model is never shown a tool it would be denied.
#: * ``order.read`` -> ``order.track``. A buyer asking where an order is, answered from the
#:   order.
#:
#: Two of this service's agent strings map to nothing and are absent rather than empty:
#: ``checkout.create`` (``checkout.submit_for_approval``) and ``checkout.submit_approved``
#: are both writes. ``checkout.read`` has no string on this side at all, which is why the
#: Checkout specialist cannot be bridged by this table alone -- it would come back with a
#: roster and no ``checkout_get``, and a checkout specialist that cannot read a checkout is
#: not a specialist. Widening this service's vocabulary by one read string is a separate
#: reviewed change.
READS_ONLY_CAPABILITIES: Final[Mapping[str, frozenset[Capability]]] = MappingProxyType(
    {
        "catalogue.read": frozenset({Capability.CATALOG_SEARCH, Capability.CATALOG_GET_PRODUCT}),
        # ``basket.write`` grants the re-quote and the line PROPOSAL, never the write
        # itself. The proposal is a record the buyer presses; the route re-checks its
        # binding under the cart's lock. Without it a bridged specialist can read the
        # catalogue and nothing else, so it answers "here is the milk" to "add milk" --
        # which is what happened, and why this line names two capabilities.
        "basket.write": frozenset({Capability.QUOTE_REQUEST, Capability.BASKET_PROPOSE_LINE}),
        "order.read": frozenset({Capability.ORDER_TRACK}),
    }
)

_GRANTED: Final[frozenset[Capability]] = frozenset(
    capability for row in READS_ONLY_CAPABILITIES.values() for capability in row
)

# Both refusals are at import, in the spirit of the registry's own disjointness assertion: a
# merge that makes this table grant a write, or grant a capability no tool requires, fails to
# import this module rather than failing in a review.
if _GRANTED & _WRITE_CAPABILITIES:
    raise RuntimeError(
        "the reads-only capability table grants a write capability: "
        f"{sorted(_GRANTED & _WRITE_CAPABILITIES)}"
    )
if not _GRANTED <= _TOOLED_CAPABILITIES:
    raise RuntimeError(
        "the reads-only capability table grants a capability no tool requires: "
        f"{sorted(_GRANTED - _TOOLED_CAPABILITIES)}"
    )


def registry_a_capabilities(service_capabilities: Iterable[str]) -> frozenset[Capability]:
    """Translate this service's capability strings into Registry A's, reads only.

    An unknown string is dropped rather than raising. The caller is passing a session's own
    capability set, which is minted by ``deps`` and may legitimately hold strings that
    belong to Registry B -- ``checkout.approve`` is the important one. Those are not
    translatable by design, and refusing the whole turn because a buyer session also holds
    the right to consent would be the wrong answer to the right observation.
    """
    return frozenset(
        capability
        for name in service_capabilities
        for capability in READS_ONLY_CAPABILITIES.get(name, frozenset())
    )


#: How long one model turn may take before the bridge abandons it and the deterministic
#: answer is rendered instead.
#:
#: The guard has to be here at all because ``Harness._run_specialist`` is what applies one
#: in the reference path, and a ``TurnRunner`` reaches the runner without going through
#: ``Harness.run``: without this line a model could hold a request thread indefinitely.
#:
#: Thirty rather than something tighter, and the number is measured rather than chosen. It
#: was twelve first, on the reasoning that a synchronous request should not wait half a
#: minute -- and twelve abandoned a perfectly good live turn against ``gemini-3.8-flash``
#: that answered correctly in fourteen. A grounded question that reads two tools takes about
#: ten seconds; one that reads three and writes a table takes longer, and a merchant whose
#: correct answer was thrown away at second twelve is worse off than one who waited. So it
#: matches ``Harness``'s own thirty, which is also the point: two halves of one product
#: disagreeing about how long a turn may take is the same class of defect as the seam this
#: module exists to close.
#: A bridged turn now reads the catalogue, proposes a line and looks for something that goes
#: with it, so four tool calls in a turn is ordinary rather than exceptional. At thirty
#: seconds those turns were timing out and falling back to the deterministic runner, which
#: answers the same sentence with a disambiguation card instead of the proposal the buyer's
#: press needs -- so the item silently did not go in the cart. Forty-five leaves room for the
#: work and still lands inside the storefront proxy's sixty-second ceiling for this route.
TURN_TIMEOUT_S: Final[float] = 45.0

#: The currency the reply post-check reads amounts in. One currency exists on this
#: platform; the constant is here so the two post-check calls cannot drift from each other.
CURRENCY: Final[str] = "INR"

#: The ``kind`` a proposal record carries. The observer below recognises a proposal by this
#: rather than by which tool produced it, so nothing here knows a tool's signature.
_PROPOSAL_KIND: Final[str] = "proposal"

#: Stands in when a bound principal carries no correlation id. Every request this service
#: builds has one, so the live path never reads it; it exists because ``CopilotSession``
#: requires the field, and minting a fresh uuid per turn would put an identifier in the
#: audit trail that joins nothing to anything.
_NO_CORRELATION: Final[uuid.UUID] = uuid.UUID(int=0)

#: HTTP statuses for the executor's reason keys, so a refused read reaches the model as the
#: structured failure ``agent_runtime`` renders rather than as an exception that ends a turn.
_STATUS_FOR_REASON: Final[Mapping[str, int]] = {
    "tool_not_registered": 404,
    "tool_budget_exhausted": 429,
    "capability_missing": 403,
    "not_found": 404,
    "not_permitted": 403,
    "invalid_argument": 422,
    "tool_unavailable": 503,
    "tool_failed": 502,
}


class BridgeUnavailableError(RuntimeError):
    """The bridge cannot run this turn. Raised, never rendered.

    ``run_turn`` catches it and answers deterministically with the sentence that names the
    missing layer, which is specification 30's contract and is the same answer a Vertex
    outage produces. Returning a degraded reply from here instead would put a model failure
    inside a response that claims a model succeeded.
    """


# --------------------------------------------------------------------- the backend


def _refuse(tool: str, result: ToolResult) -> BackendError:
    """The executor's refusal, as the structured failure the factory's tools render.

    The reason key travels through unchanged -- ``backend_problem`` builds
    ``urn:acr:problem:<reason>`` and ``Problem.reason_key`` reads the last segment back --
    so ``capability_missing`` on this side is ``capability_missing`` on the model's side,
    and the denial the panel shows and the sentence the model reads name one event.
    """
    reason = result.reason_key or "tool_failed"
    return backend_problem(
        reason,
        status=_STATUS_FOR_REASON.get(reason, 502),
        title="Read refused" if result.denied else "Read unavailable",
        detail=f"{tool} did not return a result on this turn.",
        tool=tool,
    )


# --------------------------------------------------- the buyer reads and their translation


def _provenance(block: Mapping[str, Any]) -> Provenance:
    """Registry A's :class:`Provenance` from this service's ``freshness`` block.

    Every buyer read this service returns carries provenance in a nested ``freshness``
    object -- ``source``, ``catalogue_revision``, ``observed_at`` -- because the buyer
    surface's whole freshness story (specification 6.2, 20.1) is a comparison of the
    revision a quote was priced at against the revision a later read reports. It is a hard
    read rather than a defaulted one: a card that rendered with a blank source would look
    like an answer while being a guess about how old it is, which on the buyer surface is
    worse than not rendering. ``observed_at`` is optional because ``Provenance`` allows it,
    and a missing timestamp is a fact about the read, not a contract violation.
    """
    return Provenance(
        source=str(block["source"]),
        catalogue_revision=int(block["catalogue_revision"]),
        observed_at=_iso(block.get("observed_at")),
    )


def _iso(value: Any) -> datetime | None:
    """An ISO-8601 string as a datetime, or ``None``. Anything else is dropped, not raised.

    A malformed timestamp is a fact about freshness that failed to render, and the read it
    rides on is still a real read; losing the timestamp is not a reason to lose the card.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _product_card(row: Mapping[str, Any]) -> ProductCard:
    """One ``ProductOut``/``SearchHitOut`` payload as Registry A's frozen :class:`ProductCard`.

    ``display_name`` rather than ``name_en``: this service has already applied the locale
    the buyer's request named, and a Hindi speaker should see the Hindi name. The extra
    fields a search hit carries -- ``score``, ``matched_terms`` -- are read by neither the
    card nor the grounding ledger and are dropped here rather than smuggled onto a shape
    that has no field for them. ``unit_price`` is a minor-unit integer read from
    ``unit_price_minor``; the ``unit_price`` sub-object's decimal ``display`` string is a
    rendering the buyer surface owns and is never the number the agent may quote.
    """
    currency = str(row["currency"])
    return ProductCard(
        sku=str(row["sku"]),
        # Untrusted merchant text, handed over unfenced: ``agent_runtime``'s own payload
        # builder fences it before a model sees it, exactly as the merchant surface hands
        # over a raw name. Pre-fencing here would put the fence markers inside the card.
        name=str(row["display_name"]),
        # The catalogue routes carry no description; empty is the honest reading of a field
        # this service does not send, and it stays a non-fatal absence rather than a key error.
        description=str(row.get("description") or ""),
        category=str(row["category"]),
        unit_label=str(row["unit_label"]),
        unit_price=Money(int(row["unit_price_minor"]), currency),
        stock_units=int(row["stock_units"]),
        is_listed=bool(row["is_listed"]),
        is_available=bool(row["is_available"]),
        provenance=_provenance(row["freshness"]),
    )


def _opt_str(value: object) -> str | None:
    """A nullable string from the wire, without turning ``None`` into ``"None"``."""
    return None if value is None else str(value)


def _priced_line(row: Mapping[str, Any], currency: str) -> PricedLine:
    """One ``QuoteLineOut`` as a frozen :class:`PricedLine`, every amount a minor-unit int.

    ``tax_bp`` is the rate the fee engine applied and is an integer basis-point count, not
    money. A live quote (the only kind a re-quote produces) always states it; the ``None``
    a rebuilt-from-approved-bytes line would carry cannot occur on this path, so reading it
    strictly is correct rather than lossy.
    """
    return PricedLine(
        sku=str(row["sku"]),
        name=str(row["name"]),
        quantity=int(row["quantity"]),
        unit_price=Money(int(row["unit_price_minor"]), currency),
        subtotal=Money(int(row["subtotal_minor"]), currency),
        tax_bp=int(row["tax_bp"]),
        tax=Money(int(row["tax_minor"]), currency),
    )


def _cart_quote(block: Mapping[str, Any]) -> CartQuote:
    """One ``QuoteOut`` payload as a frozen :class:`CartQuote`.

    Provenance is spread flat across a quote -- ``source`` and ``catalogue_revision`` sit
    beside the amounts rather than in a ``freshness`` block -- because a quote is hashed and
    signed as one flat content object, and :func:`_provenance` reads a nested block, so this
    reads the two fields directly. :class:`CartQuote` re-checks that its total equals its
    components on construction, which is the point of building the dataclass at all: an HTTP
    or executor payload hands over a total this service did not compute here, and the agent
    must not repeat one the components do not support. A contradiction raises ``ValueError``
    from the dataclass; the caller turns that into a backend refusal rather than a 500.

    ``gap_to_free_delivery`` is ``0`` when the quote does not state one -- a free-delivery
    quote reports no gap -- and ``free_delivery_threshold`` is absent from this service's
    ``QuoteOut`` entirely, so it stays ``None``. Every figure is minor units.
    """
    currency = str(block["currency"])
    lines = tuple(_priced_line(line, currency) for line in block["lines"])
    gap = block.get("gap_to_free_delivery_minor")
    return CartQuote(
        lines=lines,
        items_subtotal=Money(int(block["items_subtotal_minor"]), currency),
        items_tax=Money(int(block["items_tax_minor"]), currency),
        delivery_fee=Money(int(block["delivery_fee_minor"]), currency),
        delivery_tax=Money(int(block["delivery_tax_minor"]), currency),
        total=Money(int(block["total_minor"]), currency),
        free_delivery_applied=bool(block["free_delivery_applied"]),
        gap_to_free_delivery=Money(0 if gap is None else int(gap), currency),
        currency=currency,
        content_hash=str(block["content_hash"]),
        provenance=Provenance(
            source=str(block["source"]),
            catalogue_revision=int(block["catalogue_revision"]),
        ),
        discount=Money(int(block["discount_minor"]), currency),
        offer_label=_opt_str(block.get("offer_label")),
    )


def _cart_view(payload: Mapping[str, Any]) -> CartView:
    """One ``CartOut`` payload as a frozen :class:`CartView`, re-quoted, never mutated.

    ``code`` goes through :class:`RecoveryCode` so a recovery state this platform cannot
    produce is refused rather than acted on (specification 6.7), and :class:`CartView`
    itself refuses the two incoherent shapes -- a non-empty OK cart with no quote, and a
    refused cart that names no unavailable line -- on construction. A ``ValueError`` from
    either raises here and the caller renders a refusal, which is a deterministic answer
    rather than a crash.
    """
    lines = tuple((str(line["sku"]), int(line["quantity"])) for line in payload["lines"])
    unavailable = tuple(
        UnavailableLine(
            sku=str(item["sku"]),
            requested=int(item["requested"]),
            available_units=int(item["available_units"]),
            listed=bool(item["listed"]),
        )
        for item in payload["unavailable"]
    )
    quote_block = payload.get("quote")
    return CartView(
        cart_id=str(payload["cart_id"]),
        code=RecoveryCode(str(payload["code"])),
        lines=lines,
        quote=None if quote_block is None else _cart_quote(quote_block),
        unavailable=unavailable,
        stale=bool(payload["stale"]),
        provenance=_provenance(payload["freshness"]),
    )


class _BuyerReads(CommerceBackend):
    """Registry A's buyer surface over this service's own gated executor, reads only.

    The mirror image of :class:`_MerchantReads`, and for the same reason: it asks
    :meth:`~agent_service.ToolExecutor.call` for a payload and turns that payload into the
    frozen dataclass ``agent_runtime``'s tool closures expect, holding no ``Session``, no
    ``RequestContext`` and no ``MerchantRegistry``. The executor holds all three, which is
    what keeps "every tool call is gated" a property of the object graph rather than of
    discipline -- a buyer read reaches a row only by asking the executor, through every gate
    it already has, onto the same ledger the panel renders.

    Building the frozen dataclasses is load-bearing here for exactly the reason it is in the
    merchant backend: ``TurnContext.ledger``, which ``verify_reply`` reads, is filled only
    by ``agent_runtime``'s ``search_payload`` and ``cart_payload`` builders, and both take
    these dataclasses. A backend that forwarded this service's JSON would preserve the
    ledger the *panel* reads and leave the grounding ledger empty -- and an empty grounding
    ledger does not fail open, it drops every sentence naming a SKU, a price or a stock
    count. So this backend re-quotes and re-prices into ``SearchPage``, ``ProductCard`` and
    ``CartView``, and no float ever appears: every amount is a minor-unit integer read
    from a ``*_minor`` field, and :class:`CartQuote` re-checks that the total it was handed
    equals its components before the agent is allowed to repeat it.

    **Three reads are implemented; four writes raise, and none of the four is reachable.**
    The shopping principal, translated through :func:`registry_a_capabilities`, holds
    ``catalog.search``, ``catalog.get_product`` and ``quote.request`` and nothing else, so
    ``build_toolset`` never constructs ``basket_create`` or ``basket_set_line`` -- the model
    is never shown a tool it would be denied. ``checkout_create`` and
    ``checkout_submit_approved`` are the checkout roster's, not shopping's, and are here only
    because :class:`~agent_runtime.backends.base.CommerceBackend` declares them abstract.
    Each write raises through the same :meth:`~_MerchantReads._absent` helper the merchant
    backend uses: raising is the honest form of "not inside a read transaction", because a
    cart or checkout write needs an ``Idempotency-Key`` and the kernel role that an agent
    turn holds neither of. The buyer's real writes have a better path anyway -- a line
    proposal the buyer presses, which the route re-checks under the cart's lock -- so the
    model gained the ability to *reason* about a cart, never to *move* one.
    """

    def __init__(self, tools: ToolExecutor) -> None:
        self._tools = tools
        self._cards: dict[str, dict[str, Any]] = {}

    # ---- observations the panel renders ------------------------------------

    @property
    def cards(self) -> Mapping[str, dict[str, Any]]:
        """The last payload each buyer read returned, keyed by this service's ``kind``.

        The bridge builds ``TurnOutcome.structured`` from these, so a model-backed shopping
        turn renders through the same panel components the deterministic runner draws a
        search page, a product card or a cart through -- the ``products``, ``product`` and
        ``cart`` kinds ``DeterministicRunner._shopping`` emits, verbatim.
        """
        return self._cards

    # ---- the gated read ----------------------------------------------------

    def _read(self, kind: str, tool: str, **args: Any) -> dict[str, Any]:
        """One executor call, refusal turned into a structured backend problem.

        ``kind`` is this service's own card kind and is stored separately from the payload,
        because the buyer payloads -- unlike the merchant ones -- do not already carry one:
        a search page is spread flat under ``products`` rather than nested, matching what
        ``DeterministicRunner._shopping`` emits.
        """
        result = self._tools.call(tool, **args)
        if not result.ok:
            raise _refuse(tool, result)
        self._cards.pop(kind, None)
        self._cards[kind] = {"kind": kind, **result.payload}
        return result.payload

    # ---- buyer reads -------------------------------------------------------

    async def search(
        self,
        query: str,
        # The executor searched in the turn's own locale and names it back in the payload,
        # which is what the page below reports. This one is the ABC's parameter, and using
        # it would be a second opinion that could disagree with the search that ran.
        locale: Locale,  # noqa: ARG002 - the executor's own locale is authoritative
        limit: int,
    ) -> SearchPage:
        payload = self._read("products", "catalog.search", query=query, limit=limit)
        return SearchPage(
            query=str(payload["query"]),
            # The executor already searched in the request's own locale and named it back;
            # ``locale`` here is that same one, not a second opinion. Read from the payload
            # so the page reports the locale it was actually searched in.
            locale=Locale(str(payload["locale"])),
            hits=tuple(_product_card(hit) for hit in payload["hits"]),
            provenance=_provenance(payload["freshness"]),
        )

    async def product(self, sku: str) -> ProductCard:
        payload = self._read("product", "catalog.get_product", sku=sku)
        return _product_card(payload)

    async def basket_get(self, cart_id: str) -> CartView:
        # ``cart.read`` is a re-quote, not a write: Registry A maps it to ``quote.request``
        # and the executor's handler re-prices the stored lines against live merchant state
        # rather than mutating them. The id is coerced to a ``UUID`` because the executor's
        # handler and the cart row expect one, exactly as the REST layer parses the path.
        payload = self._read("cart", "cart.read", cart_id=uuid.UUID(cart_id))
        return _cart_view(payload)

    async def basket_propose_line(
        self, cart_id: str | None, sku: str, delta: int
    ) -> Mapping[str, Any]:
        # The product read goes through the executor so the SKU lands in its ledger, which is
        # the provenance the deterministic runner's own proposal path requires. The record is
        # then built by that same path, so a model-backed turn and a deterministic one propose
        # byte-identical cards and the route re-checks one binding at the press. Nothing is
        # written here: the buyer presses, or does not.
        product = self._read("product", "catalog.get_product", sku=sku)
        return line_proposal_record(
            product, delta, None if cart_id is None else uuid.UUID(cart_id), self._tools
        )

    # ---- writes: present because the ABC requires it, never reachable ------

    @staticmethod
    def _absent(operation: str, **asked: Any) -> BackendError:
        return backend_problem(
            "not_on_this_surface",
            status=501,
            title="Operation unavailable",
            detail=f"{operation} is not available on this surface.",
            operation=operation,
            **asked,
        )

    async def basket_create(self) -> CartView:
        raise self._absent("basket_create")

    async def basket_set_line(self, cart_id: str, sku: str, quantity: int) -> CartView:
        raise self._absent("basket_set_line", cart_id=cart_id, sku=sku, quantity=quantity)

    async def checkout_create(self, cart_id: str) -> ApprovalCard:
        raise self._absent("checkout_create", cart_id=cart_id)

    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        # Not a write, but not on the shopping roster and not translatable by the committed
        # table -- this service mints no ``checkout.read`` string -- so a shopping principal
        # never holds it and ``build_toolset`` never builds this closure. Raising is the
        # same honest answer: this backend is the shopping surface, and a checkout read is
        # the checkout specialist's, which is deterministic.
        raise self._absent("checkout_get", checkout_id=checkout_id)

    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> AdmissionDecision:
        raise self._absent(
            "checkout_submit_approved",
            checkout_id=checkout_id,
            version=version,
            content_hash=content_hash,
        )

    async def order_track(self, order_id: str) -> OrderView:
        # ``order.read`` is a real read this service mints, but it is the checkout and
        # support rosters' tool, not shopping's. A shopping principal is not granted it, so
        # this closure is never built; the checkout/support specialists that would use it
        # are deterministic. Raising keeps the shopping backend honest about its own surface.
        raise self._absent("order_track", order_id=order_id)


#: This service's ``kind`` for each buyer read, so a bridged shopping turn's ``structured``
#: block is the block ``DeterministicRunner._shopping`` emits and the panel already renders:
#: ``products`` for a search page, ``product`` for one product, ``cart`` for a re-quote.
_BUYER_CARD_KIND: Final[frozenset[str]] = frozenset({"products", "product", "cart"})


# ------------------------------------------------------------------ observation


class _Observer:
    """Keeps the one tool result the panel needs and that no ledger carries.

    Every read a tool makes reaches this service's ledger through the executor, and every
    figure reaches the grounding ledger through the factory's payload builders. A proposal
    record reaches neither: ``basket_propose_line`` assembles it and hands it to the model,
    and ``TurnContext.record_call`` keeps only a summary. So the toolset's closures are
    wrapped on the way out of the factory, and a result is recognised as a proposal by its
    own ``kind`` field rather than by which tool returned it.
    """

    def __init__(self) -> None:
        self.proposal: dict[str, Any] | None = None
        self.presented_products: list[dict[str, Any]] | None = None

    def watch(self, tool: BoundTool) -> BoundTool:
        """Decorate one tool. Handed to ``build_toolset(wrap=...)``, never applied after.

        Applying it afterwards is what used to happen, and it silently disarmed the whole
        toolset: ``replace(toolset, tools=...)`` keeps the gate, the gate holds the
        original closures in ``bound_callables``, and the capability gate compares
        ``tool.func`` by identity -- so every tool the model called was refused
        ``tool_not_bound``. Wrapping inside the factory means the gate is built from these
        closures instead.
        """
        return self._watch(tool)

    def _watch(self, tool: BoundTool) -> BoundTool:
        observe = self._record

        @functools.wraps(tool.func)
        async def watched(*args: Any, **kwargs: Any) -> dict[str, Any]:
            started = time.monotonic()
            try:
                result = await tool.func(*args, **kwargs)
                observe(result)
                return result
            finally:
                _log.info(
                    "agent tool name=%s elapsed_ms=%d",
                    tool.name,
                    round((time.monotonic() - started) * 1000),
                )

        return replace(tool, func=watched)

    def _record(self, result: Any) -> None:
        if isinstance(result, Mapping) and result.get("kind") == _PROPOSAL_KIND:
            self.proposal = dict(result)
        if isinstance(result, Mapping) and result.get("ok") is True:
            card = result.get("card")
            if isinstance(card, Mapping) and card.get("kind") == "product":
                self.presented_products = [
                    {**item, "display_name": item.get("name", "")}
                    for item in card.get("items", [])
                    if isinstance(item, dict) and item.get("sku")
                ]


# ----------------------------------------------------------------- the bridge


def _facts(session: CopilotSession, language: Language) -> dict[str, Any]:
    """Session facts for the specialist's dynamic block: a language, a modality, a clock.

    Not ``harness.base._facts``, and not because it is private. That one is buyer-shaped --
    cart, checkout, checkout version, order and case ids -- and every one of those is
    ``None`` on a merchant turn. Naming them anyway would tell a merchant's specialist
    about a cart in a console that has none, which is how a model comes to ask about one.

    The clock is rounded to the hour so the block is byte-stable within it (ADR 0004 §1.5).
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return {
        "language": language.label,
        "modality": session.modality.value,
        "clock_hour_utc": now.isoformat(),
    }


class SpecialistBridge:
    """This service's :class:`~agent_service.TurnRunner`, backed by a harness runner.

    Construct it with the model runtime -- ``AdkSpecialistRunner``, or a scripted double --
    and attach it to ``app.state.agent_runner``. ``routers.agent`` reads it from there and
    never imports it, which is what keeps that module free of ``google.*``.

    **The sync/async boundary sits inside :meth:`run`, at the single point where the
    specialist is awaited.** ``buyer_turn`` and ``merchant_turn`` are ``def``, not ``async
    def``, so FastAPI dispatches them through anyio's thread pool and this code already
    runs in a worker thread with no event loop: :func:`asyncio.run` builds one, runs the
    turn and tears it down. No thread is spawned per turn, and that is a decision rather
    than an omission -- the ``ToolExecutor`` closes over the request's SQLAlchemy
    ``Session``, and a second thread would touch a transaction it does not own. Moving the
    boundary up instead, by making the handler ``async def``, would put blocking psycopg
    calls on the event loop for every tool the model calls.
    """

    def __init__(
        self,
        runner: SpecialistRunner,
        *,
        bridged: frozenset[Specialist] = BRIDGED_SPECIALISTS,
        timeout_s: float = TURN_TIMEOUT_S,
        fast_discovery: bool = False,
    ) -> None:
        self._runner = runner
        self._bridged = bridged
        self._timeout_s = timeout_s
        self.fast_discovery = fast_discovery
        self._planning_timeout_s = 6.0
        self._discovery_lock = threading.Lock()
        self._recent_discovery: OrderedDict[str, tuple[float, tuple[str, ...], str | None]] = (
            OrderedDict()
        )
        self._shopping_plans: OrderedDict[
            str, tuple[float, dict[str, Any], tuple[int, bool] | None]
        ] = OrderedDict()
        self._fallback = DeterministicRunner()
        # None until a bridged specialist's call to the model has completed at least once.
        # ``None`` is "no turn has asked yet"; that is not the same claim as "unreachable",
        # and this process never guesses one from the other. Construction reads three
        # environment variables and touches no credential -- see the module docstring's
        # note on ``bridged`` versus this -- so this is the only fact that can tell a
        # process with Vertex configured but no Application Default Credentials apart from
        # one that is genuinely answering. `run_turn` writes it from the branch that already
        # distinguishes a wiring defect from an outage; this object only remembers the
        # answer for `GET /v1/config` to read.
        self.model_reached: bool | None = None

    @property
    def bridged(self) -> frozenset[Specialist]:
        """Which specialists this bridge answers with a model."""
        return self._bridged

    def remember_discovery(
        self, principal_id: str, skus: Sequence[str], preferred_sku: str | None = None
    ) -> None:
        """Bounded display context, never cached price/stock or authorization evidence."""
        with self._discovery_lock:
            self._recent_discovery[principal_id] = (
                time.monotonic(),
                tuple(skus[:5]),
                preferred_sku,
            )
            self._recent_discovery.move_to_end(principal_id)
            while len(self._recent_discovery) > 256:
                self._recent_discovery.popitem(last=False)

    def _take_discovery(self, principal_id: str) -> tuple[str, ...]:
        with self._discovery_lock:
            previous = self._recent_discovery.pop(principal_id, None)
        return previous[1] if previous and time.monotonic() - previous[0] < 300 else ()

    def displayed_products(self, principal_id: str) -> tuple[str, ...]:
        """Read bounded display references; callers must fetch fresh commercial facts."""
        with self._discovery_lock:
            previous = self._recent_discovery.get(principal_id)
        if not previous or time.monotonic() - previous[0] >= 300:
            return ()
        return (previous[2],) if previous[2] in previous[1] else previous[1]

    def displayed_order(self, principal_id: str) -> tuple[str, ...]:
        with self._discovery_lock:
            previous = self._recent_discovery.get(principal_id)
        return previous[1] if previous and time.monotonic() - previous[0] < 300 else ()

    def run(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome:
        if chosen.specialist not in self._bridged:
            # Not a degraded answer and not announced as one. There is no model path for
            # this specialist to have failed, so prefixing the reply with the sentence that
            # names a missing reasoning layer would report an outage that did not happen.
            return self._fallback.run(turn, chosen, tools)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Reached only if a future edit makes the turn handler ``async def``. Said out
            # loud rather than left to ``asyncio.run``'s own message, because the fix is to
            # move the await, not to wrap this call.
            raise BridgeUnavailableError(
                "the bridge runs inside a synchronous request thread; a running event loop "
                "means the turn handler became async and the await belongs in it"
            )
        return asyncio.run(self._turn(turn, chosen, tools))

    async def _turn(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome:
        """One model turn: bind, build tools, ground, run, post-check.

        The order is the harness's own (``Harness.run`` and ``Harness._run_specialist``),
        because that order is the harness's whole job. The grounding rules run once, before
        the specialist; the post-check runs once, after it. Neither runs twice: this
        service's turn path contributes neither, which is the finding that made this
        function necessary rather than optional.
        """
        specialist = chosen.specialist
        language = turn.language
        from .adaptive_shopping import (
            comparison_plan,
            eligible_request,
            execute,
            is_followup,
            replacement_plan,
            stated_budget,
        )

        planner = getattr(self._runner, "plan_shopping", None)
        if callable(planner) and specialist == Specialist.SHOPPING:
            key = tools.principal.principal_id
            with self._discovery_lock:
                saved = self._shopping_plans.get(key)
            previous_plan = saved[1] if saved and time.monotonic() - saved[0] < 300 else None
            followup = is_followup(turn.message)
            if not followup:
                previous_plan = None
            if eligible_request(turn.message, previous_plan):
                plan = replacement_plan(turn.message, previous_plan) or comparison_plan(
                    turn.message
                )
                rounds = 0
                if plan is None:
                    try:
                        async with asyncio.timeout(min(self._timeout_s, self._planning_timeout_s)):
                            plan = await planner(turn.message, previous_plan)
                    except TimeoutError:
                        # Do not enter a second, unbounded tool loop after a slow planner.
                        outcome = execute(
                            {
                                "mode": "clarify",
                                "needs": [],
                                "clarification": "",
                                "unverified_requirements": [],
                            },
                            tools,
                            language.value,
                            stated_budget(turn.message),
                            turn.message,
                        )
                        assert outcome.structured is not None
                        outcome.structured.update(model_rounds=1, planning_status="planner_timeout")
                        return outcome
                    rounds = 1
                budget = stated_budget(turn.message)
                if (
                    budget is None
                    and previous_plan is not None
                    and saved
                    and is_followup(turn.message)
                ):
                    budget = saved[2]
                original = (
                    str(previous_plan.get("original_request", previous_plan.get("request", "")))
                    if previous_plan
                    else ""
                )
                outcome = execute(
                    plan, tools, language.value, budget, original + " " + turn.message
                )
                assert outcome.structured is not None
                context = {"plan": plan, "request": turn.message}
                if followup and previous_plan:
                    context["original_request"] = previous_plan.get(
                        "original_request", previous_plan.get("request", "")
                    )
                with self._discovery_lock:
                    self._shopping_plans[key] = (time.monotonic(), context, budget)
                    self._shopping_plans.move_to_end(key)
                    while len(self._shopping_plans) > 256:
                        self._shopping_plans.popitem(last=False)
                outcome.structured["model_rounds"] = rounds
                return outcome
        spec = spec_for(specialist.value)

        # ``harness.base.bind``, not a hand-built ``Binding``. It re-derives the specialist
        # principal as ``harness ∩ ROLE_CAPABILITIES`` through ``subset_for``, so a
        # capability this service granted that Registry A does not know is dropped here
        # rather than reaching a tool, and any widening raises. It is the second,
        # independent proof of the invariant a prompt-injection attack would most like to
        # break, and constructing the dataclass by hand would skip it.
        #
        # A buyer principal is translated on the way in, and this is the one place the
        # committed :func:`registry_a_capabilities` table earns its keep. A buyer
        # principal holds this service's ``catalogue.read`` and ``basket.write``, which
        # Registry A does not name directly, so the translation replaces the capability
        # set -- ``principal_id`` and the delegation chain are preserved -- and ``bind``
        # then intersects the translated set with ``ROLE_CAPABILITIES[SHOPPING]`` to the
        # reads the shopping roster actually builds.
        buyer = specialist in BUYER_SPECIALISTS
        principal = tools.principal
        if buyer:
            principal = replace(
                principal, capabilities=registry_a_capabilities(principal.capabilities)
            )
        binding = bind(principal, specialist)

        # The backend is the surface the bound principal can read, and nothing more.
        # ``_BuyerReads`` reads the catalogue and re-quotes a cart over the same executor.
        backend: CommerceBackend = _BuyerReads(tools)
        turn_ctx = TurnContext(
            language=language,
            principal=binding.principal,
            max_tool_calls=spec.max_tool_calls,
            agent_name=specialist.value,
        )
        session = self._session(tools, language, turn)
        observer = _Observer()
        toolset = build_toolset(
            binding,
            backend,
            turn_ctx,
            session_id=session.session_id,
            agent_name=specialist.value,
            wrap=observer.watch,
        )
        if not toolset.tools:
            # The failure this bridge exists to make impossible, asserted rather than
            # assumed. ``build_toolset`` skips every tool whose capability the bound
            # principal lacks, so a vocabulary mismatch produces an empty toolset, an
            # ``LlmAgent`` with nothing to call, and a confident answer from nothing --
            # and it produces it silently, because ``unbuilt`` is only ever carried in a
            # structured field nobody raises on.
            raise BridgeUnavailableError(
                f"{specialist.value} bound to no tools: its principal holds "
                f"{sorted(binding.capabilities)}, which builds none of "
                f"{list(spec.tool_names)}"
            )
        bound = BoundSpecialist(specialist=specialist, binding=binding, tools=toolset)

        try:
            async with asyncio.timeout(self._timeout_s):
                preamble = await prefetch_grounding(turn.message, session, turn_ctx, toolset)
                previous = self.displayed_products(tools.principal.principal_id)
                if previous:
                    preamble = (preamble or "") + (
                        "\nThe preceding discovery displayed these product references in order: "
                        + ", ".join(previous)
                        + ". These are context only, not current price, stock or authorization. "
                        "Read the selected product before proposing any action; clarify ambiguity."
                    )
                preamble = (preamble or "") + (
                    "\nResolve the user's intent before choosing an action. A literal search miss "
                    "is not proof the store lacks the item: try a shorter brand/category query "
                    "and compare actual catalogue names, including speech spelling variants. "
                    "Keep the user's category and constraints; do not replace them with unrelated "
                    "suggestions. If identity or pack remains ambiguous, show candidates and ask "
                    "one specific question. Never invent a SKU or silently substitute an item. "
                    "For an explicit cart request, use the validated proposal tool after reading "
                    "the product. A proposal is not a completed cart update; do not claim success "
                    "until a backend cart result confirms it."
                )
                message = SpecialistInput(
                    text=turn.message,
                    language=language,
                    preamble=preamble,
                    facts=_facts(session, language),
                )
                reply = await self._runner(bound, message, turn_ctx, session)
        finally:
            # In a ``finally`` because a refusal is evidence whether or not the turn
            # finished. A model that was denied a tool and then timed out still had that
            # denial recorded against it, and the deterministic answer ``run_turn`` builds
            # after this raises runs over the same ledger, so the merchant sees both.
            self._mirror_refusals(turn_ctx, tools)

        text, corrections = self._checked(reply.text, turn_ctx, language)
        structured = self._structured(backend, observer, toolset, reply, corrections, turn_ctx)
        if corrections and not observer.proposal and structured.get("kind") == "products":
            # Removing unsafe sentences may strand references such as "this option".
            # Rebuild from the selected live cards instead of presenting broken prose.
            rows = structured.get("hits", [])
            if rows:
                text = self._recovered_product_reply(rows, language)
                text, _ = self._checked(text, turn_ctx, language)
                structured["response_rebuilt_from_cards"] = True
        _log.info(
            "bridged turn session=%s specialist=%s tools=%d denials=%d corrections=%s",
            session.tag,
            specialist.value,
            len(turn_ctx.tool_calls),
            len(turn_ctx.denials),
            ",".join(corrections) or "none",
        )
        return TurnOutcome(reply=text, structured=structured)

    @staticmethod
    def _recovered_product_reply(rows: list[dict[str, Any]], language: Language) -> str:
        facts = []
        for row in rows[:5]:
            money = row.get("unit_price") or {}
            name = row.get("display_name", row.get("name", ""))
            if name and money.get("display"):
                facts.append(f"{name}: {money['display']}.")
        closing = {
            Language.HI: (
                "ये अलग-अलग उत्पाद हैं; पूरे बजट, डिलीवरी, टैक्स और जरूरी मात्रा "
                "की पुष्टि बाकी है। कौन सा विकल्प देखें?"
            ),
            Language.HI_LATN: (
                "Ye alag products hain; poora budget, delivery, tax aur quantity "
                "abhi verify nahi hue. Kaunsa option dekhein?"
            ),
        }.get(
            language,
            "These are individual product options; the complete budget, delivery, tax "
            "and required quantities still need verification. Which option should we explore?",
        )
        return " ".join(facts + [closing])

    # ---- the ledger --------------------------------------------------------

    @staticmethod
    def _mirror_refusals(turn_ctx: TurnContext, tools: ToolExecutor) -> None:
        """Put the refusals only the runtime saw onto the ledger the panel renders.

        Every *read* a bridged turn makes already lands on this service's ledger, because
        the only way to a row is :meth:`~agent_service.ToolExecutor.call`. Two kinds of
        refusal never get that far, and both are exactly the kind of event the panel exists
        to show:

        * a capability gate denial -- the model named a tool its principal does not hold, or
          spent the runtime's per-turn budget. ``agent_runtime``'s gate records it and
          returns a non-empty dict, so the tool never runs and this service never hears of
          it.
        * a guardrail hold -- the model asked to stage a proposal from figures this
          conversation has not read. Nothing was read, so nothing was recorded here.

        Neither is mirrored twice. A backend failure is not among them: it began
        as an executor refusal, which is already a chip.

        A denial is copied as a denial, with its capability, so it appears in the response's
        ``denials`` list as well as its tool log. A hold is copied as a failed call and not
        as a denial, because a provenance guardrail is not a capability refusal.
        """
        for denial in turn_ctx.denials:
            tools.ledger.deny(
                denial.capability or denial.reason_key,
                denial.reason_key,
                tool=denial.tool,
                summary=f"refused: {denial.agent} may not call {denial.tool}",
            )
        for record in turn_ctx.tool_calls:
            blocked = record.summary.get("blocked")
            if record.ok or record.denied or not isinstance(blocked, str):
                continue
            tools.ledger.record(
                record.tool,
                f"held: {blocked} ({record.reason_key})",
                ok=False,
                reason_key=record.reason_key,
            )

    # ---- the post-check ----------------------------------------------------

    @staticmethod
    def _checked(
        reply: str, turn_ctx: TurnContext, language: Language
    ) -> tuple[str, tuple[str, ...]]:
        """The two calls ``Harness.run`` makes after a specialist answers, in its order.

        ``verify_reply`` drops every sentence asserting a SKU, an amount or a stock count
        the turn's grounding ledger cannot prove, and removes scarcity language that no
        tool on this platform can ground. ``enforce_conversational_rules`` puts back, whole,
        any material change a tool reported and the prose summarised away. An empty reply
        after both is the fallback template: a blank bubble is not an answer, and a model
        whose every sentence was dropped has said nothing the merchant may act on.
        """
        corrections: list[str] = []
        check = verify_reply(reply, turn_ctx.ledger, currency=CURRENCY, language=language)
        text = check.reply
        if check.rewritten:
            corrections.append("ungrounded_sentences_dropped")
        if check.pressure_removed or check.ungrounded_stock_counts:
            corrections.append("sales_pressure_removed")
        text, restored = enforce_conversational_rules(text, [turn_ctx], language, currency=CURRENCY)
        corrections.extend(restored)
        if not text.strip():
            text = render_fallback(language)
            corrections.append("fallback_rendered")
        return text, tuple(dict.fromkeys(corrections))

    # ---- what the panel renders --------------------------------------------

    @staticmethod
    def _structured(
        backend: CommerceBackend,
        observer: _Observer,
        toolset: BoundToolset,
        reply: Any,
        corrections: Sequence[str],
        turn_ctx: TurnContext,
    ) -> dict[str, Any]:
        """The response's ``structured`` block: a card the panel already knows, and a receipt.

        The card is the payload of the last read this turn made, under this service's own
        ``kind``, so a model-backed turn renders through the same component a deterministic
        one does -- a ``products``/``product``/``cart`` card for Shopping, keyed exactly as
        ``DeterministicRunner`` keys them. The proposal, when the model staged one, is the record
        :func:`~commerce_api.services.agent_service.line_proposal_record` assembled -- the same
        record the deterministic runner emits.

        ``bridge`` is the receipt, and it is always present. A turn that a model answered
        should say so in the body and not only in a log line: which specialist ran, which
        tools it was offered, which roster rows had no closure, what the post-check
        corrected, and a hand-back that had nowhere to go. This service's turn path is
        one-shot -- ``route`` chooses once and the executor is bound to that choice -- so a
        typed hand-back is dropped rather than followed, and it is dropped in writing.
        """
        # The backend keeps each read's payload keyed by this service's ``kind``; the card
        # is the last one keyed, so a turn that searched and then read one product shows the
        # product. Both surfaces' kinds are searched because the receipt is one shape and the
        # bridge does not branch on which specialist ran to render it.
        cards = getattr(backend, "cards", {})
        card: dict[str, Any] = {}
        for kind, payload in cards.items():
            if kind in _BUYER_CARD_KIND:
                card = payload
        structured: dict[str, Any] = dict(card)
        if observer.presented_products is not None:
            structured = {"kind": "products", "hits": observer.presented_products}
        if observer.proposal is not None:
            structured["proposal"] = observer.proposal
        receipt: dict[str, Any] = {
            "runtime": "agent_runtime",
            "specialist": turn_ctx.agent_name,
            "tools_offered": list(toolset.names),
            "tools_unbuilt": list(toolset.unbuilt),
            "corrections": list(corrections),
            "tool_calls": len(turn_ctx.tool_calls),
        }
        handback = getattr(reply, "handback", None)
        if handback is not None:
            receipt["handback_ignored"] = handback.to.value
        structured["bridge"] = receipt
        return structured

    # ---- the session -------------------------------------------------------

    @staticmethod
    def _session(tools: ToolExecutor, language: Language, turn: TurnInput) -> CopilotSession:
        """The harness session for this turn, built from the bound principal alone.

        ``TurnInput`` carries no identity by design and ``ToolExecutor`` exposes no request
        context, so every field here comes off the principal the executor was bound to. The
        session id is the principal id, which this service mints as ``session:<session id>``
        and already returns in every turn response: per-session by construction
        (specification 5.4), unique across tenants, and not a credential.

        It is rebuilt per turn, and for a read-only specialist that costs less than it
        looks. The tool-visible state a specialist accumulates -- which metrics this
        conversation has read, which proposals it staged -- lives in the model runtime's own
        session store under this same id, so a merchant who read the metrics on one turn and
        asks for a proposal on the next is not held. What a per-turn session does lose is
        ``causation_id``: with no previous turn there is nothing to point back at, so an
        audit reader walks the correlation id instead. The session store that fixes it
        properly is cross-request mutable state in a service whose turn path has none, and
        it belongs with the buyer side, where session provenance gates a cart write.
        """
        principal = tools.principal
        return CopilotSession(
            session_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            correlation_id=principal.correlation_id or _NO_CORRELATION,
            buyer_ref=principal.buyer_ref,
            merchant_id=principal.merchant_id,
            # Decided by the transport and never by the model. The merchant console is
            # typed; the voice gateway calls the buyer endpoint, which this bridge does not
            # answer, and ``TurnRequest`` forbids unknown fields, so a spoken merchant turn
            # could not tell this bridge it was spoken even if one existed.
            modality=Modality.TEXT,
            language=language,
            # The cart the buyer is looking at. The factory's cart tools read it from the
            # tool state, so without it ``basket_propose_line`` proposes against no cart and
            # the card comes back blocked_by=no_basket -- a proposal the buyer cannot press.
            cart_id=None if turn.cart_id is None else str(turn.cart_id),
            state={} if turn.cart_id is None else {STATE_CART_ID: str(turn.cart_id)},
        )

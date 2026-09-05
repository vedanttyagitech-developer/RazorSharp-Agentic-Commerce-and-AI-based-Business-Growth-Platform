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

One specialist is model-backed: :attr:`~agent_service.Specialist.GROWTH`. Every other route
keeps the deterministic runner, and that is a structural choice rather than a staging plan:

* Growth is read-only. Its one non-read action stages a proposal a person applies, so
  nothing here strains the app role's read transaction. The buyer specialists are offered
  ``basket_create``, ``basket_set_line``, ``checkout_create`` and
  ``checkout_submit_approved`` by their roster, and none of those has an honest
  implementation inside a read transaction that holds no ``Idempotency-Key`` and no kernel
  role.
* Growth's capability vocabulary already matches on both sides. The four
  ``merchant.*`` strings this service mints are the same four strings
  ``agent_runtime.capabilities.registry`` names. The buyer side overlaps in exactly one
  string -- this service says ``catalogue.read`` and ``basket.write`` where Registry A says
  ``catalog.search``, ``catalog.get_product``, ``basket.create``, ``basket.update`` and
  ``quote.request`` -- so binding a shopping principal through
  :func:`~agent_runtime.harness.base.bind` yields an empty intersection and a toolset with
  no tools in it. A model handed no tools answers from nothing, and it does so fluently.
  The translation table that fixes it is a reviewed decision about what ``basket.write``
  means, not a dict comprehension, and it is not written here.
* Growth's proposal record is already shared. ``agent_runtime.capabilities.proposals``
  assembles it for both the ``growth_proposal_create`` tool and this service's
  deterministic runner, so a merchant console parses one record whichever half answered.

:meth:`SpecialistBridge.run` therefore delegates every other specialist to
:class:`~agent_service.DeterministicRunner` **without** the "reasoning layer unavailable"
sentence. That sentence would be a lie: for shopping there is no model path to have failed.

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
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

from agent_runtime.backends.base import (
    ApprovalCard,
    BackendError,
    BasketView,
    CatalogueHealth,
    CheckoutMetrics,
    CheckoutView,
    CommerceBackend,
    InventoryAnomaly,
    MerchantBackend,
    OrderView,
    ProductCard,
    SearchPage,
    backend_problem,
)
from agent_runtime.capabilities.proposals import (
    ANOMALY_DELISTED_WITH_STOCK,
    ANOMALY_LISTED_OUT_OF_STOCK,
)
from agent_runtime.capabilities.tools import BoundTool, BoundToolset, build_toolset
from agent_runtime.grounding import verify_reply
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
from merchant_sim import Locale
from transaction_kernel import KernelDecision

from .agent_service import (
    DeterministicRunner,
    Route,
    Specialist,
    ToolExecutor,
    ToolResult,
    TurnInput,
    TurnOutcome,
)

__all__ = [
    "BRIDGED_SPECIALISTS",
    "CURRENCY",
    "TURN_TIMEOUT_S",
    "BridgeUnavailableError",
    "SpecialistBridge",
]

_log = logging.getLogger("commerce_api.agent.bridge")

#: The specialists this bridge answers with a model. See the module docstring for why the
#: buyer three and the case specialist are not among them; the short version is that their
#: capability strings do not match Registry A's and their rosters contain writes this
#: service's read transaction cannot honestly perform.
BRIDGED_SPECIALISTS: Final[frozenset[Specialist]] = frozenset({Specialist.GROWTH})

#: How long one model turn may take before the bridge abandons it. ``Harness`` applies the
#: same guard at 30 seconds around its own runner call; a ``TurnRunner`` bypasses
#: ``Harness.run`` entirely and would inherit no timeout at all, so it is applied here.
#: Twelve rather than thirty because this runs inside a synchronous HTTP request on a
#: single-worker process (ADR 0003 D14): a turn that holds a worker thread for half a
#: minute has already failed the merchant, and the deterministic answer below it is
#: immediate and correct.
TURN_TIMEOUT_S: Final[float] = 12.0

#: The currency the reply post-check reads amounts in. One currency exists on this
#: platform; the constant is here so the two post-check calls cannot drift from each other.
CURRENCY: Final[str] = "INR"

#: This service's ``kind`` for each merchant read, so a bridged turn's ``structured`` block
#: is the same block the deterministic runner emits and the panel already renders. Spelled
#: against the API's tool names because those are what the executor answers to.
_CARD_KIND: Final[Mapping[str, str]] = {
    "merchant.catalogue_health.read": "catalogue_health",
    "merchant.inventory_anomalies.read": "inventory_anomalies",
    "merchant.checkout_metrics.read": "checkout_metrics",
}

#: Registry A's third anomaly kind. The other two are imported from
#: ``agent_runtime.capabilities.proposals``, which names them because a proposal record
#: cites one as its basis; this one grounds no proposal, so it is spelled here.
#:
#: Both halves count the same shelf states and spell two of them differently: this service
#: says ``out_of_stock`` and ``delisted`` where ``agent_runtime`` says
#: ``listed_out_of_stock`` and ``delisted_with_stock``. That rename is mechanical. The
#: fourth case is not: a product that is delisted *and* empty is an anomaly here and is not
#: one there, because there is nothing to restock and nothing to relist. Registry A's
#: vocabulary is closed on purpose -- a specialist that could report a fourth kind could
#: invent one -- so such a row is dropped rather than given a name the proposal drafts do
#: not recognise, and the count of what was dropped travels in the turn's ``structured``
#: block rather than vanishing.
_LOW_STOCK: Final[str] = "low_stock"

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
        title="Merchant read refused" if result.denied else "Merchant read unavailable",
        detail=f"{tool} did not return a result on this turn.",
        tool=tool,
    )


def _anomaly_kind(row: Mapping[str, Any]) -> str | None:
    """Registry A's word for one row of this service's anomaly read, or ``None``.

    Derived from ``is_listed`` and ``stock_units`` rather than from this service's own
    ``anomaly`` string, because those two are the facts and the string is one naming of
    them. A row neither vocabulary shares is ``None``; see :data:`_LOW_STOCK`.
    """
    listed = bool(row.get("is_listed"))
    units = row.get("stock_units")
    if not isinstance(units, int) or isinstance(units, bool):
        return None
    if listed and units == 0:
        return ANOMALY_LISTED_OUT_OF_STOCK
    if not listed and units > 0:
        return ANOMALY_DELISTED_WITH_STOCK
    if listed and units > 0:
        return _LOW_STOCK
    return None


class _MerchantReads(CommerceBackend, MerchantBackend):
    """Registry A's merchant surface over this service's own gated executor.

    Every method here is a translation and nothing more: it asks
    :meth:`~agent_service.ToolExecutor.call` for a payload, and turns that payload into the
    frozen dataclass ``agent_runtime``'s tool closures expect. It holds no ``Session``, no
    ``RequestContext`` and no ``MerchantRegistry``; the executor holds all three, which is
    what keeps "every tool call is gated" a property of the object graph.

    The alternative -- wrapping the executor as a set of tool closures directly -- looks
    cheaper and is a trap. ``TurnContext.ledger``, which the reply post-check reads, is
    filled only by ``agent_runtime.grounding.payloads`` and by ``_amount_field``, and both
    take these dataclasses. A wrapper returning the API's JSON preserves the ledger the
    *panel* reads and leaves the ledger the *post-check* reads empty -- and an empty
    grounding ledger does not fail open, it drops every sentence naming a figure. Only the
    second ledger is load-bearing for correctness, so this is the shape that gets built.

    It subclasses :class:`~agent_runtime.backends.base.CommerceBackend` because
    :func:`~agent_runtime.capabilities.tools.build_toolset` is typed on it and finds the
    merchant surface by ``isinstance``. The nine buyer operations are abstract there and so
    must exist here; each one raises. None of them is reachable: the Growth roster lists
    none of them, and the growth principal holds none of their capabilities, so
    ``build_toolset`` never constructs a closure over one. Raising rather than answering is
    the honest form of "this surface does not do that inside a read transaction".
    """

    def __init__(self, tools: ToolExecutor) -> None:
        self._tools = tools
        self._cards: dict[str, dict[str, Any]] = {}
        self._dropped_anomalies = 0

    # ---- observations the panel renders ------------------------------------

    @property
    def cards(self) -> Mapping[str, dict[str, Any]]:
        """The last payload each merchant read returned, keyed by this service's ``kind``.

        The bridge builds ``TurnOutcome.structured`` from these, so a model-backed turn
        renders through the panel components a deterministic turn already renders through.
        The alternative was the ADK runner's own ``structured``, which is runtime telemetry
        -- runtime, model, prompt source, tool names -- and carries no ``kind`` at all.
        """
        return self._cards

    @property
    def dropped_anomalies(self) -> int:
        """Anomaly rows Registry A has no ``kind`` for. See :data:`_LOW_STOCK`."""
        return self._dropped_anomalies

    # ---- the gated read ----------------------------------------------------

    def _read(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self._tools.call(tool, **args)
        if not result.ok:
            raise _refuse(tool, result)
        kind = _CARD_KIND.get(tool)
        if kind is not None:
            self._cards[kind] = {"kind": kind, **result.payload}
        return result.payload

    # ---- merchant surface --------------------------------------------------

    async def catalogue_health(self) -> CatalogueHealth:
        payload = self._read("merchant.catalogue_health.read")
        return CatalogueHealth(
            total=int(payload["products"]),
            listed=int(payload["listed"]),
            delisted=int(payload["delisted"]),
            available=int(payload["available"]),
            out_of_stock=int(payload["out_of_stock"]),
            by_category=dict(payload["by_category"]),
            catalogue_revision=int(payload["catalogue_revision"]),
        )

    async def inventory_anomalies(self, limit: int = 20) -> tuple[InventoryAnomaly, ...]:
        payload = self._read("merchant.inventory_anomalies.read")
        rows: list[InventoryAnomaly] = []
        dropped = 0
        for row in payload["anomalies"]:
            kind = _anomaly_kind(row)
            if kind is None:
                dropped += 1
                continue
            rows.append(
                InventoryAnomaly(
                    sku=str(row["sku"]),
                    # The merchant's own name, unfenced. ``_subject_of`` fences it on the
                    # way into a proposal record and ``_subject_label`` draws the raw one
                    # on a card; handing a pre-fenced name to either would put the fence
                    # markers inside the record and inside the card.
                    name=str(row["name"] or ""),
                    kind=kind,
                    detail={"stock_units": int(row["stock_units"])},
                )
            )
        # Most costly first, and the ordering is the roster's rather than this service's:
        # a listed product with an empty shelf is losing a sale now, a delisted product
        # holding stock is hidden, and a low shelf is neither yet. ``_choose_subject``
        # takes the first row when the merchant names no SKU, so this order decides what a
        # proposal is about.
        order = {ANOMALY_LISTED_OUT_OF_STOCK: 0, ANOMALY_DELISTED_WITH_STOCK: 1, _LOW_STOCK: 2}
        rows.sort(key=lambda anomaly: (order.get(anomaly.kind, 9), anomaly.sku))
        self._dropped_anomalies = dropped
        return tuple(rows[: max(1, limit)])

    async def checkout_metrics(self) -> CheckoutMetrics:
        payload = self._read("merchant.checkout_metrics.read")
        return CheckoutMetrics(
            orders_total=int(payload["orders"]),
            # Empty, and empty is the honest answer rather than a convenient one. This
            # service counts *checkouts* by state and does not count orders by state; the
            # two are different populations, and putting ``PENDING_APPROVAL`` on a card row
            # labelled "Orders in ..." would make a merchant with five abandoned checkouts
            # and one sale read six of something beside a total of one. A state absent says
            # nobody counted; a state at zero says none. Neither is invented here.
            orders_by_state={},
            refunds_by_state={},
            # ``None``, not ``0``. This service sums no amount on an agent turn at all --
            # retained revenue is the evidence endpoint's to derive, row by row -- so both
            # figures reach the model as ``measured: false`` with no number beside them,
            # and the specialist is told plainly not to read that as zero.
            captured_minor=None,
            refunded_minor=None,
            currency=CURRENCY,
        )

    # ---- buyer surface: present because the ABC requires it, never reachable ----

    @staticmethod
    def _absent(operation: str, **asked: Any) -> BackendError:
        """The refusal every buyer operation answers with, naming what was asked for.

        The arguments travel into the problem's extensions rather than being discarded.
        None of these methods can be called today -- the Growth roster lists no buyer tool
        and the growth principal holds no buyer capability, so ``build_toolset`` builds no
        closure over one -- and if that ever stops being true the failure record should say
        which operation was attempted with which arguments, not merely that one was.
        """
        return backend_problem(
            "not_on_this_surface",
            status=501,
            title="No buyer surface",
            detail=(
                "The merchant copilot's backend has no buyer surface. A basket or checkout "
                "write needs an Idempotency-Key and the kernel role, and an agent turn holds "
                "neither: a write is proposed to the trusted surface, never executed here."
            ),
            operation=operation,
            **asked,
        )

    async def search(self, query: str, locale: Locale, limit: int) -> SearchPage:
        raise self._absent("search", query=query, locale=locale.value, limit=limit)

    async def product(self, sku: str) -> ProductCard:
        raise self._absent("product", sku=sku)

    async def basket_create(self) -> BasketView:
        raise self._absent("basket_create")

    async def basket_set_line(self, basket_id: str, sku: str, quantity: int) -> BasketView:
        raise self._absent("basket_set_line", basket_id=basket_id, sku=sku, quantity=quantity)

    async def basket_get(self, basket_id: str) -> BasketView:
        raise self._absent("basket_get", basket_id=basket_id)

    async def checkout_create(self, basket_id: str) -> ApprovalCard:
        raise self._absent("checkout_create", basket_id=basket_id)

    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        raise self._absent("checkout_get", checkout_id=checkout_id)

    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> KernelDecision:
        raise self._absent(
            "checkout_submit_approved",
            checkout_id=checkout_id,
            version=version,
            content_hash=content_hash,
        )

    async def order_track(self, order_id: str) -> OrderView:
        raise self._absent("order_track", order_id=order_id)


# ------------------------------------------------------------------ observation


class _Observer:
    """Keeps the one tool result the panel needs and that no ledger carries.

    Every read a tool makes reaches this service's ledger through the executor, and every
    figure reaches the grounding ledger through the factory's payload builders. A proposal
    record reaches neither: ``growth_proposal_create`` assembles it from
    ``agent_runtime.capabilities.proposals`` and hands it to the model, and
    ``TurnContext.record_call`` keeps only a summary -- lever, id and subject -- which is
    right for an evidence record and is not the record the merchant console applies.

    So the toolset's closures are wrapped on the way out of the factory, and a result is
    recognised as a proposal by its own ``kind`` field rather than by which tool returned
    it. Nothing here knows a tool's name or signature: ``functools.wraps`` carries
    ``__wrapped__``, so ``inspect.signature`` -- which is what the identity-parameter check
    and ADK's function declaration both read -- still sees the factory's own closure.
    """

    def __init__(self) -> None:
        self.proposal: dict[str, Any] | None = None

    def watching(self, toolset: BoundToolset) -> BoundToolset:
        return replace(toolset, tools=tuple(self._watch(tool) for tool in toolset))

    def _watch(self, tool: BoundTool) -> BoundTool:
        observe = self._record

        @functools.wraps(tool.func)
        async def watched(*args: Any, **kwargs: Any) -> dict[str, Any]:
            result = await tool.func(*args, **kwargs)
            observe(result)
            return result

        return replace(tool, func=watched)

    def _record(self, result: Any) -> None:
        if isinstance(result, Mapping) and result.get("kind") == _PROPOSAL_KIND:
            self.proposal = dict(result)


# ----------------------------------------------------------------- the bridge


def _facts(session: CopilotSession, language: Language) -> dict[str, Any]:
    """Session facts for the specialist's dynamic block: a language, a modality, a clock.

    Not ``harness.base._facts``, and not because it is private. That one is buyer-shaped --
    basket, checkout, checkout version, order and case ids -- and every one of those is
    ``None`` on a merchant turn. Naming them anyway would tell a merchant's specialist
    about a basket in a console that has none, which is how a model comes to ask about one.

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
    ) -> None:
        self._runner = runner
        self._bridged = bridged
        self._timeout_s = timeout_s
        self._fallback = DeterministicRunner()

    @property
    def bridged(self) -> frozenset[Specialist]:
        """Which specialists this bridge answers with a model."""
        return self._bridged

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
        spec = spec_for(specialist.value)

        # ``harness.base.bind``, not a hand-built ``Binding``. It re-derives the specialist
        # principal as ``harness ∩ ROLE_CAPABILITIES`` through ``subset_for``, so a
        # capability this service granted that Registry A does not know is dropped here
        # rather than reaching a tool, and any widening raises. It is the second,
        # independent proof of the invariant a prompt-injection attack would most like to
        # break, and constructing the dataclass by hand would skip it.
        binding = bind(tools.principal, specialist)

        backend = _MerchantReads(tools)
        turn_ctx = TurnContext(
            language=language,
            principal=binding.principal,
            max_tool_calls=spec.max_tool_calls,
            agent_name=specialist.value,
        )
        session = self._session(tools, language)
        toolset = build_toolset(
            binding,
            backend,
            turn_ctx,
            session_id=session.session_id,
            agent_name=specialist.value,
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
        observer = _Observer()
        watched = observer.watching(toolset)
        bound = BoundSpecialist(specialist=specialist, binding=binding, tools=watched)

        async with asyncio.timeout(self._timeout_s):
            preamble = await prefetch_grounding(turn.message, session, turn_ctx, watched)
            message = SpecialistInput(
                text=turn.message,
                language=language,
                preamble=preamble,
                facts=_facts(session, language),
            )
            reply = await self._runner(bound, message, turn_ctx, session)

        text, corrections = self._checked(reply.text, turn_ctx, language)
        structured = self._structured(backend, observer, toolset, reply, corrections, turn_ctx)
        _log.info(
            "bridged turn session=%s specialist=%s tools=%d denials=%d corrections=%s",
            session.tag,
            specialist.value,
            len(turn_ctx.tool_calls),
            len(turn_ctx.denials),
            ",".join(corrections) or "none",
        )
        return TurnOutcome(reply=text, structured=structured)

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
        backend: _MerchantReads,
        observer: _Observer,
        toolset: BoundToolset,
        reply: Any,
        corrections: Sequence[str],
        turn_ctx: TurnContext,
    ) -> dict[str, Any]:
        """The response's ``structured`` block: a card the panel already knows, and a receipt.

        The card is the payload of the last merchant read this turn made, under this
        service's own ``kind``, so a model-backed growth turn renders through the same
        component a deterministic one does. The proposal, when the model staged one, is the
        record ``agent_runtime.capabilities.proposals`` assembled -- the same record the
        deterministic runner emits, which is why the console parses one shape.

        ``bridge`` is the receipt, and it is always present. A turn that a model answered
        should say so in the body and not only in a log line: which specialist ran, which
        tools it was offered, which roster rows had no closure, what the post-check
        corrected, and a hand-back that had nowhere to go. This service's turn path is
        one-shot -- ``route`` chooses once and the executor is bound to that choice -- so a
        typed hand-back is dropped rather than followed, and it is dropped in writing.
        """
        cards = backend.cards
        card: dict[str, Any] = {}
        for kind in _CARD_KIND.values():
            if kind in cards:
                card = cards[kind]
        structured: dict[str, Any] = dict(card)
        if observer.proposal is not None:
            structured["proposal"] = observer.proposal
        receipt: dict[str, Any] = {
            "runtime": "agent_runtime",
            "specialist": turn_ctx.agent_name,
            "tools_offered": list(toolset.names),
            "tools_unbuilt": list(toolset.unbuilt),
            "corrections": list(corrections),
            "tool_calls": len(turn_ctx.tool_calls),
            "anomalies_without_a_kind": backend.dropped_anomalies,
        }
        handback = getattr(reply, "handback", None)
        if handback is not None:
            receipt["handback_ignored"] = handback.to.value
        structured["bridge"] = receipt
        return structured

    # ---- the session -------------------------------------------------------

    @staticmethod
    def _session(tools: ToolExecutor, language: Language) -> CopilotSession:
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
        it belongs with the buyer side, where session provenance gates a basket write.
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
        )

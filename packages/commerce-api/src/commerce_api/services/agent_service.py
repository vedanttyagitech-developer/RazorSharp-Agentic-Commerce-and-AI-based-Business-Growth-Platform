"""One conversational turn, from the authenticated session to a structured answer.

This is the HTTP-facing half of the agent layer: it turns a bearer session into an agent
principal, binds that principal to one specialist's tools, runs the turn, and hands back
a record the panel can render -- reply, specialist, why that specialist, every tool call,
every refusal, and a structured payload built from tool results. What it never does is
touch money. Every function here runs as the ``commerce_app`` role inside the one read
transaction the router owns, and nothing in this module writes a row.

Three rules the shape of this module exists to make true:

**The principal comes from the session and is only ever narrowed.** The session row says
what this caller may do. The agent principal is that set intersected with
:data:`AGENT_SURFACE`, then intersected again with the specialist's allowlist, both through
:meth:`~transaction_kernel.AgentPrincipal.subset_for`, which raises on any widening. A
request body cannot name a capability at all -- the request model forbids unknown fields --
and a message that asks for one is answered with a denial, not a tool.

**Approve, pay, refund and revoke are absent by construction.** :data:`ABSENT_VERBS` maps
the words a buyer uses for those actions to the capability they would need; none of those
capabilities is in :data:`AGENT_SURFACE`, so no tool exists for them and the executor has
nothing to gate. A request for one is recorded as a denial with the reason, carried in an
HTTP 200: a refusal is the system working (ADR 0003 D15).

**A copilot is a harness.** Routing is :func:`route`, a lexicon over the message and the
identifiers the caller is looking at. It calls no model. The specialist that runs may be a
model (``agent_runtime``'s ADK adapter, plugged in through :class:`TurnRunner`) or, when no
model is configured, :class:`DeterministicRunner`, which grounds every sentence in a tool
result and proposes writes for the trusted surface to execute. The fallback is not a
degraded mode of the same thing; it is specification 6.1's "keep the transaction unchanged,
preserve the deterministic state card" made into code.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol

from agent_runtime.harness import BUYER_SPECIALISTS, MERCHANT_SPECIALISTS, Specialist
from agent_runtime.harness import session_tag as _harness_session_tag
from agent_runtime.language import Language, detect_language
from agent_runtime.rendering import render_denial
from platform_db import Checkout, Order
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from transaction_kernel import ActorType, AgentPrincipal

from ..deps import (
    AGENT_CAPABILITIES,
    MERCHANT_AGENT_CAPABILITIES,
    SUPPORT_AGENT_CAPABILITIES,
    RequestContext,
    assert_owner,
)
from ..errors import ProblemError
from ..merchants import MerchantRegistry
from ..schemas import FreshnessOut, ProductOut, SearchHitOut
from . import basket_service, catalogue_service, checkout_service
from .refund_service import load_order, order_payload

__all__ = [
    "ABSENT_VERBS",
    "AGENT_SURFACE",
    "COPILOT_SPECIALISTS",
    "MAX_TOOL_CALLS",
    "MERCHANT_AGENT_CAPABILITIES",
    "SPECIALIST_ALLOWLIST",
    "TOOLS",
    "Binding",
    "Copilot",
    "DeterministicRunner",
    "Route",
    "Specialist",
    "ToolCall",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
    "TurnInput",
    "TurnLedger",
    "TurnOutcome",
    "TurnResult",
    "TurnRunner",
    "bind",
    "copilot_for",
    "language_for",
    "route",
    "run_turn",
    "session_tag",
]

_log = logging.getLogger("commerce_api.agent")

#: Specification 20.2: a fixed maximum number of tool calls per turn, counted before the
#: tool runs so a runner looping on a tool cannot run it.
MAX_TOOL_CALLS: Final[int] = 8

#: How many hits a fallback search shows. A conversation can act on a handful.
_SEARCH_LIMIT: Final[int] = 5

#: Below this many units a listed product is reported as low stock on the merchant side.
_LOW_STOCK_UNITS: Final[int] = 5


# ------------------------------------------------------------------------ vocabulary


class Copilot(StrEnum):
    """The two harnesses. Neither is a model."""

    BUYER = "buyer"
    MERCHANT = "merchant"

    @property
    def harness_slug(self) -> str:
        """The segment this harness contributes to a delegated principal id.

        Written out rather than derived from the member name, because the buyer harness
        is called RazorAI on both surfaces and a principal id is read by a human
        auditing a delegation chain. ``session:<id>/razorai/shopping`` says which harness
        bound the tools; a slug that disagreed with the product name would make an audit
        trail harder to follow, which is the one thing it exists to make easy.
        """
        return "razorai" if self is Copilot.BUYER else f"{self.value}_copilot"


#: The five specialists of ``docs/briefs/AGENT_ROSTER.md``, and the one enumeration of
#: them: the harness's ``Specialist`` is the registry's ``AgentRole``, re-exported here so
#: the API, the harness, the tool factory and the specialist specs cannot disagree about
#: what "shopping" is. Only these may be models.


#: Every capability an agent principal may ever hold, in the vocabulary ``api_sessions``
#: stores. ``checkout.approve``, ``checkout.reject``, ``checkout.cancel``,
#: ``payment.verify`` and ``refund.request`` are Registry B, buyer consent, and are not
#: here -- which is what makes the absence of an approve or refund tool structural rather
#: than a matter of which tools happen to be registered.
AGENT_SURFACE: Final[frozenset[str]] = (
    AGENT_CAPABILITIES | MERCHANT_AGENT_CAPABILITIES | SUPPORT_AGENT_CAPABILITIES
)

#: Per-specialist allowlist (specification 5.4, intersection input 1), from the roster's
#: tool lists translated into the session vocabulary.
SPECIALIST_ALLOWLIST: Final[Mapping[Specialist, frozenset[str]]] = MappingProxyType(
    {
        Specialist.SHOPPING: frozenset({"catalogue.read", "basket.write"}),
        Specialist.CHECKOUT: frozenset(
            {"catalogue.read", "checkout.create", "checkout.submit_approved", "order.read"}
        ),
        Specialist.SUPPORT: frozenset({"order.read"}) | SUPPORT_AGENT_CAPABILITIES,
        Specialist.GROWTH: MERCHANT_AGENT_CAPABILITIES,
        Specialist.CASE: frozenset({"support.case.read"}),
    }
)

#: Which specialists each harness may route to. A buyer session can never reach the
#: merchant specialists and the reverse, whatever the message says.
COPILOT_SPECIALISTS: Final[Mapping[Copilot, tuple[Specialist, ...]]] = MappingProxyType(
    {
        Copilot.BUYER: (Specialist.SHOPPING, Specialist.CHECKOUT, Specialist.SUPPORT),
        Copilot.MERCHANT: (Specialist.GROWTH, Specialist.CASE),
    }
)

# The harness owns which specialists each copilot may reach; this table only fixes their
# order for the capabilities view. A disagreement is a bug, so it fails at import.
if frozenset(COPILOT_SPECIALISTS[Copilot.BUYER]) != BUYER_SPECIALISTS:
    raise RuntimeError("RazorAI specialists disagree with agent_runtime.harness")
if frozenset(COPILOT_SPECIALISTS[Copilot.MERCHANT]) != MERCHANT_SPECIALISTS:
    raise RuntimeError("merchant copilot specialists disagree with agent_runtime.harness")

#: Words a buyer uses for the actions no agent may take, and the Registry B capability
#: each would need. Matched as whole words in English, Hinglish and Hindi. ``pay`` is
#: consent too: payment happens on the trusted surface after the buyer approves, and an
#: agent that could "pay" would be an agent that could consent.
ABSENT_VERBS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "approve": "checkout.approve",
        "pay": "checkout.approve",
        "bhugtan": "checkout.approve",
        "मंज़ूर": "checkout.approve",
        "मंजूर": "checkout.approve",
        "reject": "checkout.reject",
        "revoke": "authority.revoke",
    }
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One read tool the fallback runner may call, and the capability it needs.

    Writes are deliberately not tools here: this module runs as the app role in a read
    transaction, and a mutation needs an ``Idempotency-Key`` and the kernel role. A write
    the runner wants is a *proposal* in ``structured`` that the trusted surface executes
    through the mutation endpoints, which is exactly the propose-then-authorize rule.
    """

    name: str
    capability: str
    specialists: frozenset[Specialist]


def _spec(name: str, capability: str, *specialists: Specialist) -> tuple[str, ToolSpec]:
    return name, ToolSpec(name=name, capability=capability, specialists=frozenset(specialists))


#: The closed tool table. A tool absent from here does not exist, whatever a runner asks
#: for; :meth:`ToolExecutor.call` answers ``tool_not_registered``.
TOOLS: Final[Mapping[str, ToolSpec]] = MappingProxyType(
    dict(
        [
            _spec("catalog.search", "catalogue.read", Specialist.SHOPPING, Specialist.CHECKOUT),
            _spec(
                "catalog.get_product", "catalogue.read", Specialist.SHOPPING, Specialist.CHECKOUT
            ),
            _spec("basket.read", "catalogue.read", Specialist.SHOPPING, Specialist.CHECKOUT),
            _spec("checkout.read", "order.read", Specialist.CHECKOUT, Specialist.SUPPORT),
            _spec("order.track", "order.read", Specialist.CHECKOUT, Specialist.SUPPORT),
            _spec("support.case.read", "support.case.read", Specialist.SUPPORT, Specialist.CASE),
            _spec(
                "merchant.catalogue_health.read",
                "merchant.catalogue_health.read",
                Specialist.GROWTH,
            ),
            _spec(
                "merchant.inventory_anomalies.read",
                "merchant.inventory_anomalies.read",
                Specialist.GROWTH,
            ),
            _spec(
                "merchant.checkout_metrics.read",
                "merchant.checkout_metrics.read",
                Specialist.GROWTH,
            ),
        ]
    )
)


# --------------------------------------------------------------------------- binding


@dataclass(frozen=True, slots=True)
class Binding:
    """A session's principal, narrowed to the harness and then to each specialist.

    ``harness`` is the session's capabilities intersected with :data:`AGENT_SURFACE`;
    each entry of ``specialists`` is that intersected with the specialist's allowlist.
    Both derivations go through ``AgentPrincipal.subset_for``, so the chain of narrowing
    is recorded on each principal's ``delegation_chain`` and any widening raises.
    """

    copilot: Copilot
    harness: AgentPrincipal
    specialists: Mapping[Specialist, AgentPrincipal]

    def principal_for(self, specialist: Specialist) -> AgentPrincipal:
        return self.specialists[specialist]


def bind(ctx: RequestContext, copilot: Copilot) -> Binding:
    """Grant authority: the one place a session becomes an agent principal.

    The intersection is computed here and handed to ``subset_for``, which checks it
    again. Computing it and then having the kernel type re-check the subset relation is
    the point: a future edit that computed a union instead would raise, not widen.
    """
    session_capabilities = ctx.principal.capabilities
    harness = ctx.principal.subset_for(copilot.harness_slug, session_capabilities & AGENT_SURFACE)
    specialists = {
        specialist: harness.subset_for(
            specialist.value, harness.capabilities & SPECIALIST_ALLOWLIST[specialist]
        )
        for specialist in COPILOT_SPECIALISTS[copilot]
    }
    return Binding(copilot=copilot, harness=harness, specialists=MappingProxyType(specialists))


def session_tag(session_id: uuid.UUID) -> str:
    """A log-safe handle for a session: the harness's tag over the id's string form.

    One algorithm on both sides of the API boundary, so an operator can correlate this
    service's lines with the harness's for the same session.
    """
    return _harness_session_tag(str(session_id))


# ------------------------------------------------------------------------ the ledger


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One attempted tool call as the panel shows it: a chip, not a transcript."""

    name: str
    summary: str
    ok: bool
    reason_key: str | None = None
    denied: bool = False


@dataclass(frozen=True, slots=True)
class Denial:
    """A capability refusal. Recorded before the tool would have run; it never runs."""

    capability: str
    reason_key: str
    tool: str | None = None


@dataclass(slots=True)
class TurnLedger:
    """What one turn did and was refused, plus the SKUs a tool returned this turn.

    ``seen_skus`` is the per-turn provenance record: a proposal may name a SKU only if a
    tool returned it during this turn (specification 20.4). Session-scoped provenance is
    ``agent_runtime``'s concern; the fallback runner's proposals are per turn and so is
    this.
    """

    tool_calls: list[ToolCall] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    seen_skus: set[str] = field(default_factory=set)
    admitted: int = 0

    def deny(self, capability: str, reason_key: str, *, tool: str | None, summary: str) -> None:
        self.denials.append(Denial(capability=capability, reason_key=reason_key, tool=tool))
        self.tool_calls.append(
            ToolCall(
                name=tool or capability,
                summary=summary,
                ok=False,
                reason_key=reason_key,
                denied=True,
            )
        )

    def record(self, name: str, summary: str, *, ok: bool, reason_key: str | None = None) -> None:
        self.tool_calls.append(ToolCall(name=name, summary=summary, ok=ok, reason_key=reason_key))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a runner gets back from a tool: always a value, never an exception.

    ``payload`` is the same JSON the REST read endpoint for that resource returns, so a
    ``structured`` block built from it renders through the panel's existing components.
    """

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    reason_key: str | None = None
    denied: bool = False


# ------------------------------------------------------------------- tool executor


class ToolExecutor:
    """Gated, in-process tools for one specialist in one turn.

    The gate order is fixed: is the tool registered, does this specialist carry it, is
    the per-turn budget left, does the specialist's principal hold the capability. Only
    then does the service run, and a failure inside it becomes a ``ToolResult`` with a
    reason key rather than an exception: a tool error never ends a turn.

    The executor is the runner's only handle on the database and the merchant registry.
    A runner cannot reach a service except through :meth:`call`, which is what makes
    "every tool call is gated" a property of the object graph rather than of discipline.
    """

    def __init__(
        self,
        *,
        session: Session,
        ctx: RequestContext,
        registry: MerchantRegistry,
        principal: AgentPrincipal,
        specialist: Specialist,
        language: Language,
        ledger: TurnLedger,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._registry = registry
        self._principal = principal
        self._specialist = specialist
        self._language = language
        self._ledger = ledger
        self._handlers: dict[str, Callable[..., tuple[dict[str, Any], str]]] = {
            "catalog.search": self._search,
            "catalog.get_product": self._product,
            "basket.read": self._basket,
            "checkout.read": self._checkout,
            "order.track": self._order,
            "support.case.read": self._support_case,
            "merchant.catalogue_health.read": self._catalogue_health,
            "merchant.inventory_anomalies.read": self._inventory_anomalies,
            "merchant.checkout_metrics.read": self._checkout_metrics,
        }

    @property
    def specialist(self) -> Specialist:
        return self._specialist

    @property
    def principal(self) -> AgentPrincipal:
        return self._principal

    @property
    def ledger(self) -> TurnLedger:
        return self._ledger

    def call(self, name: str, **args: Any) -> ToolResult:
        """Run one tool through every gate. See the class docstring for the order."""
        spec = TOOLS.get(name)
        if spec is None or self._specialist not in spec.specialists:
            self._ledger.record(
                name,
                f"refused: no such tool for {self._specialist.value}",
                ok=False,
                reason_key="tool_not_registered",
            )
            return ToolResult(ok=False, reason_key="tool_not_registered")
        if self._ledger.admitted >= MAX_TOOL_CALLS:
            self._ledger.record(
                name, "refused: tool budget exhausted", ok=False, reason_key="tool_budget_exhausted"
            )
            return ToolResult(ok=False, reason_key="tool_budget_exhausted")
        if not self._principal.can(spec.capability):
            self._ledger.deny(
                spec.capability,
                "capability_missing",
                tool=name,
                summary=f"refused: {self._specialist.value} does not hold {spec.capability}",
            )
            return ToolResult(ok=False, reason_key="capability_missing", denied=True)
        self._ledger.admitted += 1
        try:
            payload, summary = self._handlers[name](**args)
        except ProblemError as exc:
            reason = _reason_for_status(exc.status)
            self._ledger.record(name, f"{name}: {exc.title.lower()}", ok=False, reason_key=reason)
            return ToolResult(ok=False, reason_key=reason)
        except Exception:  # noqa: BLE001 - the error gate: a tool failure never ends a turn
            _log.exception("tool %s failed for session %s", name, session_tag(self._ctx.session_id))
            self._ledger.record(name, f"{name}: failed", ok=False, reason_key="tool_failed")
            return ToolResult(ok=False, reason_key="tool_failed")
        self._ledger.record(name, summary, ok=True)
        return ToolResult(ok=True, payload=payload)

    # --- buyer-side reads: byte-identical to the REST read models --------------------

    def _search(self, *, query: str, limit: int = _SEARCH_LIMIT) -> tuple[dict[str, Any], str]:
        locale = self._language.locale
        results = catalogue_service.search_catalogue(
            self._registry,
            merchant_id=self._ctx.merchant_id,
            query=query,
            locale=locale,
            limit=max(1, min(limit, catalogue_service.MAX_SEARCH_LIMIT)),
        )
        devanagari = locale.uses_devanagari
        hits = [SearchHitOut.of_hit(hit, devanagari=devanagari) for hit in results.hits]
        skus = list(results.skus())
        self._ledger.seen_skus.update(skus)
        payload = {
            "query": results.query,
            "normalized_query": results.normalized_query,
            "locale": results.locale.value,
            "hits": [hit.model_dump(mode="json") for hit in hits],
            "skus": skus,
            "freshness": FreshnessOut.of(results.freshness).model_dump(mode="json"),
        }
        return payload, f"searched catalogue: {query} ({len(hits)} hits)"

    def _product(self, *, sku: str) -> tuple[dict[str, Any], str]:
        view = catalogue_service.product_view(
            self._registry, merchant_id=self._ctx.merchant_id, sku=sku
        )
        self._ledger.seen_skus.add(view.product.sku)
        payload = ProductOut.of(view, devanagari=self._language.locale.uses_devanagari)
        return payload.model_dump(mode="json"), f"read product {view.product.sku}"

    def _basket(self, *, basket_id: uuid.UUID) -> tuple[dict[str, Any], str]:
        body = basket_service.read_basket(self._session, self._ctx, self._registry, basket_id)
        for line in body.get("lines", ()):
            sku = line.get("sku")
            if isinstance(sku, str):
                self._ledger.seen_skus.add(sku)
        return body, f"read basket ({len(body.get('lines', ()))} lines)"

    def _checkout(self, *, checkout_id: uuid.UUID) -> tuple[dict[str, Any], str]:
        body = checkout_service.read_checkout(self._session, self._ctx, checkout_id)
        return body, f"read checkout v{body['current_version']} {body['state']}"

    def _order(self, *, order_id: uuid.UUID) -> tuple[dict[str, Any], str]:
        order = load_order(self._session, self._ctx, order_id=order_id)
        assert_owner(self._session, self._ctx, order.checkout_id)
        body = order_payload(self._session, self._ctx, order).model_dump(mode="json")
        return body, f"tracked order {body['state']}"

    def _support_case(self, **_args: Any) -> tuple[dict[str, Any], str]:
        # The Human Review queue (specification 6.4.3) has no read model on this API yet.
        # Refusing here, inside the error gate, is honest; inventing a case is not.
        raise ProblemError(
            503, "Support cases unavailable", "The support-case queue is not on this API yet."
        )

    # --- merchant-side reads: counts, never amounts ------------------------------------

    def _catalogue_health(self) -> tuple[dict[str, Any], str]:
        store = self._registry.store(self._ctx.merchant_id)
        views = [store.get_product(sku) for sku in store.all_skus()]
        listed = [view for view in views if view.is_listed]
        payload = {
            "synthetic": True,
            "source": "merchant-sim",
            "catalogue_revision": store.revision,
            "sample_size": len(views),
            "products": len(views),
            "listed": len(listed),
            "delisted": len(views) - len(listed),
            "out_of_stock": sum(1 for view in listed if view.stock_units == 0),
            "low_stock": sum(1 for view in listed if 0 < view.stock_units < _LOW_STOCK_UNITS),
        }
        return payload, f"read catalogue health ({len(views)} products)"

    def _inventory_anomalies(self) -> tuple[dict[str, Any], str]:
        store = self._registry.store(self._ctx.merchant_id)
        anomalies: list[dict[str, Any]] = []
        for sku in store.all_skus():
            view = store.get_product(sku)
            kind = None
            if not view.is_listed:
                kind = "delisted"
            elif view.stock_units == 0:
                kind = "out_of_stock"
            elif view.stock_units < _LOW_STOCK_UNITS:
                kind = "low_stock"
            if kind is not None:
                anomalies.append(
                    {
                        "sku": view.product.sku,
                        "name": view.display_name(devanagari=False),
                        "stock_units": view.stock_units,
                        "is_listed": view.is_listed,
                        "anomaly": kind,
                    }
                )
        payload = {
            "synthetic": True,
            "source": "merchant-sim",
            "catalogue_revision": store.revision,
            "sample_size": len(store.all_skus()),
            "anomalies": anomalies,
        }
        return payload, f"read inventory anomalies ({len(anomalies)})"

    def _checkout_metrics(self) -> tuple[dict[str, Any], str]:
        # Counts by state from committed rows. No amount is summed here: a revenue figure
        # is the evidence endpoint's to derive (``/evidence/retained-revenue``), row by row.
        tenant_id, merchant_id = self._ctx.tenant_id, self._ctx.merchant_id
        by_status = self._session.execute(
            select(Checkout.status, func.count())
            .where(Checkout.tenant_id == tenant_id, Checkout.merchant_id == merchant_id)
            .group_by(Checkout.status)
        ).all()
        orders = self._session.execute(
            select(func.count()).where(
                Order.tenant_id == tenant_id, Order.merchant_id == merchant_id
            )
        ).scalar_one()
        counts = {str(status): int(count) for status, count in by_status}
        payload = {
            "synthetic": True,
            "source": "postgresql",
            "window": "all_time",
            "sample_size": sum(counts.values()),
            "checkouts_by_state": counts,
            "orders": int(orders),
        }
        return payload, f"read checkout metrics ({payload['sample_size']} checkouts)"


def _reason_for_status(status: int) -> str:
    if status == 404:
        return "not_found"
    if status in (401, 403):
        return "not_permitted"
    if status == 422:
        return "invalid_argument"
    return "tool_unavailable"


# --------------------------------------------------------------------------- routing


@dataclass(frozen=True, slots=True)
class TurnInput:
    """Everything a runner is told about the turn. Identity is not in it by design: the
    runner receives a bound :class:`ToolExecutor`, never the session."""

    copilot: Copilot
    message: str
    language: Language
    basket_id: uuid.UUID | None = None
    checkout_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Route:
    """The specialist chosen for a turn, and the deterministic reason it was chosen."""

    specialist: Specialist
    reason: str


_WORDS: Final[re.Pattern[str]] = re.compile(r"[\wऀ-ॿ]+")
_SKU: Final[re.Pattern[str]] = re.compile(r"\b[A-Z]{2,6}-[A-Z]{2,8}-\d{2,4}\b")
_QUANTITY: Final[re.Pattern[str]] = re.compile(r"\b(\d{1,2})\b")

#: Number words, because nobody speaks digits.
#:
#: A typed request says "add 2"; a spoken one says "add two", "do" or "दो", and the digit
#: pattern above finds nothing in any of them. Until this existed every spoken request for
#: more than one of something silently became a request for exactly one -- silently being
#: the problem, since the buyer hears a confirmation naming the product they asked for and
#: has no reason to re-count.
#:
#: The fix belongs here rather than in the voice layer. A transcriber that rewrote "two"
#: to "2" before the agent saw it would be editing the buyer's words on the way to the
#: thing that acts on them, and the transcript would then no longer be evidence of what
#: was actually said.
#:
#: One to ten in three languages, and no further: past ten a buyer says a digit or is
#: asked. Hinglish is spelled the way people type it, with the common variants, because a
#: table that only accepts one spelling of "paanch" is a table that fails on half of them.
_NUMBER_WORDS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "one": 1, "a": 1, "an": 1, "ek": 1, "एक": 1,
        "two": 2, "do": 2, "दो": 2, "couple": 2,
        "three": 3, "teen": 3, "तीन": 3,
        "four": 4, "char": 4, "chaar": 4, "चार": 4,
        "five": 5, "panch": 5, "paanch": 5, "पांच": 5, "पाँच": 5,
        "six": 6, "chah": 6, "chhah": 6, "che": 6, "छह": 6,
        "seven": 7, "saat": 7, "सात": 7,
        "eight": 8, "aath": 8, "आठ": 8,
        "nine": 9, "nau": 9, "नौ": 9,
        "ten": 10, "das": 10, "dus": 10, "दस": 10,
    }
)  # fmt: skip


def quantity_in(message: str) -> int:
    """How many the buyer asked for. Digits first, then number words, then one.

    Digits win when both appear, because a message carrying both is far more likely to be
    "add 2 of the three-pack" than a contradiction, and the digit is the one the buyer
    reached for deliberately.

    ``a`` and ``an`` map to one so that "add a milk" is not read as a request with no
    quantity at all -- it has one, and it is one.
    """
    digits = _QUANTITY.search(message)
    if digits is not None:
        return int(digits.group(1))
    for token in _tokens(message):
        spoken = _NUMBER_WORDS.get(token)
        if spoken is not None:
            return spoken
    return 1


_SUPPORT_CUES: Final[frozenset[str]] = frozenset(
    {
        "order", "orders", "refund", "refunds", "return", "returned", "cancel", "cancelled",
        "delivery", "delivered", "track", "tracking", "complaint", "escalate", "case",
        "wapas", "raddi", "ऑर्डर", "रिफंड", "वापस", "रद्द", "डिलीवरी",
    }
)  # fmt: skip
_CHECKOUT_CUES: Final[frozenset[str]] = frozenset(
    {
        "checkout", "approve", "approval", "approved", "reapprove", "pay", "payment",
        "submit", "version", "total", "bhugtan", "भुगतान", "चेकआउट", "मंज़ूर", "मंजूर",
    }
)  # fmt: skip
_CASE_CUES: Final[frozenset[str]] = frozenset(
    {"case", "cases", "escalation", "escalations", "review", "queue", "ticket", "tickets"}
)
_ADD_CUES: Final[frozenset[str]] = frozenset(
    {"add", "put", "want", "need", "buy", "chahiye", "dalo", "daalo", "kharido", "चाहिए", "डालो"}
)
_REFUND_CUES: Final[frozenset[str]] = frozenset({"refund", "refunds", "रिफंड", "wapas", "वापस"})
_CANCEL_CUES: Final[frozenset[str]] = frozenset({"cancel", "cancelled", "रद्द", "raddi"})


def _tokens(message: str) -> list[str]:
    return [token.casefold() for token in _WORDS.findall(message)]


def route(turn: TurnInput) -> Route:
    """Choose the specialist from structured intent first, then a lexicon. No model.

    Precedence is what the caller is looking at, then what they said: an order in
    context is a support conversation even if the message mentions a checkout, because
    the identifier is a fact and the word is a guess. A misroute costs one turn and
    changes no state; a model deciding this would cost a call and could not be tested
    for certainty.
    """
    words = set(_tokens(turn.message))
    if turn.copilot is Copilot.MERCHANT:
        hit = sorted(words & _CASE_CUES)
        if hit:
            return Route(Specialist.CASE, f"case_cue:{hit[0]}")
        return Route(Specialist.GROWTH, "default_growth")

    if turn.order_id is not None:
        return Route(Specialist.SUPPORT, "order_in_context")
    hit = sorted(words & _SUPPORT_CUES)
    if hit:
        return Route(Specialist.SUPPORT, f"support_cue:{hit[0]}")
    if turn.checkout_id is not None:
        return Route(Specialist.CHECKOUT, "checkout_in_context")
    hit = sorted(words & _CHECKOUT_CUES)
    if hit:
        return Route(Specialist.CHECKOUT, f"checkout_cue:{hit[0]}")
    return Route(Specialist.SHOPPING, "default_shopping")


def language_for(message: str, locale: str | None) -> Language:
    """The turn's language: the caller's explicit locale, else detected from the message.

    Language is a harness decision, never the model's (ADR 0004 gate 21). An unsupported
    locale is a 422 rather than a silent English: the panel asked for a script and would
    otherwise show the wrong one without learning why.
    """
    if locale is None or not locale.strip():
        return detect_language(message)
    wanted = locale.strip()
    for language in Language:
        if wanted in (language.value, language.locale.value):
            return language
    raise ProblemError(
        422,
        "Unsupported locale",
        "locale must be one of "
        + ", ".join(f"{lang.value} ({lang.locale.value})" for lang in Language)
        + ".",
        field="locale",
    )


# ------------------------------------------------------------------------- runners


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """What a runner produces: the sentence, and a structured block built from tool
    results. The ledger of calls and denials lives on the executor, not here, so a runner
    cannot report a tool call it did not make."""

    reply: str
    structured: dict[str, Any] | None = None


class TurnRunner(Protocol):
    """The seam a model-backed specialist plugs into.

    ``agent_runtime``'s ADK adapter satisfies this by running its ``LlmAgent`` inside
    ``run`` and calling back through the executor for every tool. The HTTP layer does
    not care which; what it guarantees either way is that the runner never sees the
    session, the tenant or a database handle.
    """

    def run(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome: ...


# Three-language templates. Amounts and names are substituted from structured results;
# no template computes anything.
_T: Final[Mapping[str, Mapping[Language, str]]] = MappingProxyType(
    {
        "results": {
            Language.EN: "I found {count} products for “{query}”: {names}. Prices and stock "
            "are live from the store; tell me which one and how many to add.",
            Language.HI: "“{query}” के लिए {count} उत्पाद मिले: {names}। दाम और स्टॉक दुकान से "
            "अभी के हैं; बताइए कौन-सा और कितना जोड़ना है।",
            Language.HI_LATN: "“{query}” ke liye {count} products mile: {names}. Daam aur stock "
            "dukaan se abhi ke hain; bataiye kaunsa aur kitna add karna hai.",
        },
        "no_results": {
            Language.EN: "I could not find anything for “{query}” in this store.",
            Language.HI: "इस दुकान में “{query}” के लिए कुछ नहीं मिला।",
            Language.HI_LATN: "Is dukaan mein “{query}” ke liye kuch nahi mila.",
        },
        "product": {
            Language.EN: "{name} ({sku}) is {price} {currency}; {stock} in stock.",
            Language.HI: "{name} ({sku}) {price} {currency} का है; स्टॉक में {stock} हैं।",
            Language.HI_LATN: "{name} ({sku}) {price} {currency} ka hai; stock mein {stock} hain.",
        },
        "product_unavailable": {
            Language.EN: "{name} ({sku}) is not available right now.",
            Language.HI: "{name} ({sku}) अभी उपलब्ध नहीं है।",
            Language.HI_LATN: "{name} ({sku}) abhi available nahi hai.",
        },
        "proposal_line": {
            Language.EN: " I have prepared adding {quantity} × {name}; confirm it on the "
            "basket and the store will re-quote.",
            Language.HI: " मैंने {quantity} × {name} जोड़ने का प्रस्ताव तैयार किया है; बास्केट पर "
            "पुष्टि कीजिए और दुकान दोबारा दाम बताएगी।",
            Language.HI_LATN: " Maine {quantity} × {name} add karne ka prastav taiyar kiya hai; "
            "basket par confirm kijiye aur dukaan dobara quote degi.",
        },
        "basket": {
            Language.EN: "Your basket has {count} lines and the store quotes {total} {currency} "
            "right now{stale}.",
            Language.HI: "आपकी बास्केट में {count} लाइनें हैं और दुकान अभी {total} {currency} बता "
            "रही है{stale}।",
            Language.HI_LATN: "Aapki basket mein {count} lines hain aur dukaan abhi {total} "
            "{currency} bata rahi hai{stale}.",
        },
        "stale": {
            Language.EN: " (the catalogue has moved since it was last priced)",
            Language.HI: " (पिछली बार दाम लगने के बाद कैटलॉग बदल गया है)",
            Language.HI_LATN: " (pichhli baar daam lagne ke baad catalogue badal gaya hai)",
        },
        "basket_unpriced": {
            Language.EN: "Your basket has {count} lines; the store cannot price it right now "
            "({code}).",
            Language.HI: "आपकी बास्केट में {count} लाइनें हैं; दुकान अभी इसका दाम नहीं लगा सकती ({code})।",
            Language.HI_LATN: "Aapki basket mein {count} lines hain; dukaan abhi iska daam nahi "
            "laga sakti ({code}).",
        },
        "checkout": {
            Language.EN: "Checkout version {version} is {state}. The total on the card is "
            "{total} {currency}. Approval happens on the trusted surface, not here.",
            Language.HI: "चेकआउट संस्करण {version} {state} है। कार्ड पर कुल {total} {currency} "
            "है। मंज़ूरी भरोसेमंद सतह पर होती है, यहाँ नहीं।",
            Language.HI_LATN: "Checkout version {version} {state} hai. Card par total {total} "
            "{currency} hai. Approval trusted surface par hoti hai, yahan nahi.",
        },
        "checkout_no_card": {
            Language.EN: "Checkout version {version} is {state}; there is no approval card "
            "open on it.",
            Language.HI: "चेकआउट संस्करण {version} {state} है; इस पर कोई मंज़ूरी कार्ड खुला नहीं है।",
            Language.HI_LATN: "Checkout version {version} {state} hai; is par koi approval card "
            "khula nahi hai.",
        },
        "need_checkout": {
            Language.EN: "Open a checkout from your basket first; I can then explain each "
            "version and what the store says now.",
            Language.HI: "पहले अपनी बास्केट से चेकआउट खोलिए; फिर मैं हर संस्करण और दुकान की "
            "मौजूदा स्थिति समझा सकता हूँ।",
            Language.HI_LATN: "Pehle apni basket se checkout kholiye; phir main har version aur "
            "dukaan ki maujooda sthiti samjha sakta hoon.",
        },
        "order": {
            Language.EN: "Order {order_id} is {state}; captured amount {amount} {currency}, "
            "evidence {evidence}. Razorpay's record decides payment state, not this chat.",
            Language.HI: "ऑर्डर {order_id} {state} है; वसूली गई राशि {amount} {currency}, "
            "प्रमाण {evidence}। भुगतान की स्थिति Razorpay का रिकॉर्ड तय करता है, यह चैट नहीं।",
            Language.HI_LATN: "Order {order_id} {state} hai; captured amount {amount} "
            "{currency}, evidence {evidence}. Payment state Razorpay ka record tay karta hai, "
            "yeh chat nahi.",
        },
        "need_order": {
            Language.EN: "Tell me which order — its identifier — and I will look it up "
            "against the platform's own records.",
            Language.HI: "बताइए कौन-सा ऑर्डर — उसका पहचान-चिह्न — और मैं उसे मंच के अपने रिकॉर्ड में देखूँगा।",
            Language.HI_LATN: "Bataiye kaunsa order — uska identifier — aur main use platform "
            "ke apne records mein dekhunga.",
        },
        "proposal_remedy": {
            Language.EN: " I have prepared a {remedy} request for you to confirm on the trusted "
            "surface. The amount is decided by the platform when you confirm; I cannot name "
            "it.",
            Language.HI: " मैंने {remedy} का अनुरोध तैयार किया है जिसे आप भरोसेमंद सतह पर पुष्टि "
            "कर सकते हैं। राशि पुष्टि के समय मंच तय करता है; मैं उसे नहीं बता सकता।",
            Language.HI_LATN: " Maine {remedy} ka request taiyar kiya hai jise aap trusted surface "
            "par confirm kar sakte hain. Amount confirm hone par platform tay karta hai; main "
            "use nahi bata sakta.",
        },
        "unavailable": {
            Language.EN: "That part of the platform is not available on this surface yet, so "
            "I have nothing verified to tell you.",
            Language.HI: "मंच का वह हिस्सा अभी इस सतह पर उपलब्ध नहीं है, इसलिए मेरे पास बताने को "
            "कुछ सत्यापित नहीं है।",
            Language.HI_LATN: "Platform ka woh hissa abhi is surface par available nahi hai, "
            "isliye mere paas batane ko kuch verified nahi hai.",
        },
        "not_found": {
            Language.EN: "I could not find that in this session's records.",
            Language.HI: "वह इस सत्र के रिकॉर्ड में नहीं मिला।",
            Language.HI_LATN: "Woh is session ke records mein nahi mila.",
        },
        "metrics": {
            Language.EN: "Synthetic data, all-time window, {sample} checkouts: {states}; "
            "{orders} orders. These are counts from committed rows; revenue figures come "
            "from the evidence endpoint.",
            Language.HI: "सिंथेटिक डेटा, पूरी अवधि, {sample} चेकआउट: {states}; {orders} ऑर्डर। "
            "ये कमिट की गई पंक्तियों की गिनती हैं; राजस्व के आँकड़े साक्ष्य एंडपॉइंट से आते हैं।",
            Language.HI_LATN: "Synthetic data, poori avadhi, {sample} checkouts: {states}; "
            "{orders} orders. Yeh committed rows ki ginti hai; revenue figures evidence "
            "endpoint se aate hain.",
        },
        "health": {
            Language.EN: "Synthetic catalogue at revision {revision}: {products} products, "
            "{listed} listed, {delisted} delisted, {out} out of stock, {low} low on stock.",
            Language.HI: "सिंथेटिक कैटलॉग, संशोधन {revision}: {products} उत्पाद, {listed} सूचीबद्ध, "
            "{delisted} हटाए गए, {out} स्टॉक से बाहर, {low} कम स्टॉक।",
            Language.HI_LATN: "Synthetic catalogue, revision {revision}: {products} products, "
            "{listed} listed, {delisted} delisted, {out} out of stock, {low} low stock.",
        },
        "anomalies": {
            Language.EN: "{count} inventory anomalies in the synthetic catalogue: {items}. "
            "Any change to stock or price is a proposal for you to apply, never mine.",
            Language.HI: "सिंथेटिक कैटलॉग में {count} इन्वेंटरी विसंगतियाँ: {items}। स्टॉक या दाम "
            "में कोई भी बदलाव आपके लागू करने का प्रस्ताव है, मेरा नहीं।",
            Language.HI_LATN: "Synthetic catalogue mein {count} inventory anomalies: {items}. "
            "Stock ya daam mein koi bhi badlaav aapke apply karne ka prastav hai, mera nahi.",
        },
        "no_anomalies": {
            Language.EN: "No inventory anomalies in the synthetic catalogue right now.",
            Language.HI: "सिंथेटिक कैटलॉग में अभी कोई इन्वेंटरी विसंगति नहीं है।",
            Language.HI_LATN: "Synthetic catalogue mein abhi koi inventory anomaly nahi hai.",
        },
    }
)


def _t(key: str, language: Language, **values: Any) -> str:
    return _T[key][language].format(**values)


def _decision_card_from(checkout: Mapping[str, Any]) -> dict[str, Any] | None:
    """The decision card for a checkout whose earlier approval was superseded, or None.

    A turn never submits -- the agent proposes and a human submits on the trusted surface --
    so no ``KernelDecision`` passes through here and one must not be manufactured. What a
    checkout read does know is stronger than a guess: a live version carrying a
    ``previous_version`` and a non-empty delta list is, by construction, a version that
    replaced an approval which no longer matched the merchant's state.

    So the fields the checkout genuinely holds are filled in, and the three that belong to
    the kernel's own decision record are ``None`` rather than invented. ``decision_id`` is
    null because no admission ran in this turn; ``explanation`` is null because the reason
    key is the kernel's word and this is not the kernel. ``code`` is stated, and stated
    only under the three conditions above, because it describes the checkout's own state
    rather than reporting what some earlier call returned.

    ``source`` says which of those two things this is, so a consumer speaking it aloud can
    tell a card derived from a read apart from one carried back from an admission. A
    renderer that could not tell them apart would eventually speak the second as though it
    were the first.
    """
    deltas = checkout.get("deltas") or []
    card = checkout.get("approval_card")
    if not deltas or card is None or card.get("previous_version") is None:
        return None
    return {
        "kind": "decision",
        "source": "checkout_state",
        "decision_id": None,
        "allowed": False,
        "code": "REAPPROVAL_REQUIRED",
        "explanation": None,
        # ``items``, not ``deltas``, because ``agent_runtime.rendering.cards`` already
        # emits a card of this kind and puts its rows there. Two producers of one card
        # kind disagreeing about a key name is how a consumer comes to read zero deltas
        # and say "something changed" -- which is the summary specification 19.10 exists
        # to prevent, and is exactly what happened to the voice renderer before this.
        # A test asserts the two keep agreeing.
        "items": list(deltas),
        "count": len(deltas),
        "previous_version": card["previous_version"],
        "next_version": card["version"],
        "checkout_id": checkout["checkout_id"],
        "current_version": checkout["current_version"],
        "state": checkout["state"],
        "total": card.get("total"),
        "where": "trusted_surface",
    }


class DeterministicRunner:
    """The model-free specialist: every sentence comes from a tool result.

    Used when no model runner is configured on the app, and the reference point for what
    a model-backed turn must never do worse than. It grounds first (the read the message
    calls for), answers from that result through a template, and turns any write the
    buyer asked for into a ``proposal`` in ``structured`` naming only identifiers a tool
    returned this turn. It performs no arithmetic on money: every figure is a
    ``display`` string copied from a structured result.
    """

    def run(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome:
        denied = _refuse_absent_verbs(turn, tools.ledger)
        handler = self._handlers()[chosen.specialist]
        outcome = handler(self, turn, tools)
        if denied is None:
            return outcome
        # The refusal leads, in the buyer's language; the grounded answer follows so the
        # turn is still useful. A denial that reads as an error is a denial that gets
        # worked around.
        return TurnOutcome(reply=f"{denied} {outcome.reply}".strip(), structured=outcome.structured)

    @staticmethod
    def _handlers() -> Mapping[
        Specialist, Callable[[DeterministicRunner, TurnInput, ToolExecutor], TurnOutcome]
    ]:
        return {
            Specialist.SHOPPING: DeterministicRunner._shopping,
            Specialist.CHECKOUT: DeterministicRunner._checkout,
            Specialist.SUPPORT: DeterministicRunner._support,
            Specialist.GROWTH: DeterministicRunner._growth,
            Specialist.CASE: DeterministicRunner._case,
        }

    # --- buyer specialists ------------------------------------------------------------

    def _shopping(self, turn: TurnInput, tools: ToolExecutor) -> TurnOutcome:
        language = turn.language
        skus = _SKU.findall(turn.message)
        if skus:
            result = tools.call("catalog.get_product", sku=skus[0])
            if not result.ok:
                return self._after_failure(result, language)
            product = result.payload
            key = "product" if product["is_available"] else "product_unavailable"
            reply = _t(
                key,
                language,
                name=product["display_name"],
                sku=product["sku"],
                price=product["unit_price"]["display"],
                currency=product["unit_price"]["currency"],
                stock=product["stock_units"],
            )
            structured: dict[str, Any] = {"kind": "product", "product": product}
            proposal = self._line_proposal(turn, product, tools.ledger)
            if proposal is not None:
                structured["proposal"] = proposal
                reply += _t("proposal_line", language, **proposal["display"])
            return TurnOutcome(reply=reply, structured=structured)

        query = turn.message.strip()
        result = tools.call("catalog.search", query=query, limit=_SEARCH_LIMIT)
        if not result.ok:
            return self._after_failure(result, language)
        hits = result.payload["hits"]
        if not hits:
            return TurnOutcome(
                reply=_t("no_results", language, query=query),
                structured={"kind": "products", **result.payload},
            )
        names = ", ".join(
            f"{hit['display_name']} "
            f"({hit['unit_price']['display']} {hit['unit_price']['currency']})"
            for hit in hits
        )
        reply = _t("results", language, count=len(hits), query=query, names=names)
        structured = {"kind": "products", **result.payload}
        proposal = self._line_proposal(turn, hits[0], tools.ledger) if len(hits) == 1 else None
        if proposal is not None:
            structured["proposal"] = proposal
            reply += _t("proposal_line", language, **proposal["display"])
        return TurnOutcome(reply=reply, structured=structured)

    def _checkout(self, turn: TurnInput, tools: ToolExecutor) -> TurnOutcome:
        language = turn.language
        if turn.checkout_id is None:
            if turn.basket_id is not None:
                return self._basket_view(turn, tools, propose_checkout=True)
            return TurnOutcome(reply=_t("need_checkout", language))
        result = tools.call("checkout.read", checkout_id=turn.checkout_id)
        if not result.ok:
            return self._after_failure(result, language)
        checkout = result.payload
        card = checkout.get("approval_card")
        if card is None:
            reply = _t(
                "checkout_no_card",
                language,
                version=checkout["current_version"],
                state=checkout["state"],
            )
        else:
            reply = _t(
                "checkout",
                language,
                version=card["version"],
                state=checkout["state"],
                total=card["total"]["display"],
                currency=card["total"]["currency"],
            )
        structured: dict[str, Any] = {"kind": "checkout", "checkout": checkout}
        decision = _decision_card_from(checkout)
        if decision is not None:
            # A superseded approval is a decision, and the voice pipeline speaks decisions
            # from versioned templates rather than from model prose (specification 19.10).
            # Emitting it as `kind: "decision"` is what lets it do that; the checkout block
            # stays alongside so the panel renders unchanged.
            structured = {**decision, "checkout": checkout}
        return TurnOutcome(reply=reply, structured=structured)

    def _support(self, turn: TurnInput, tools: ToolExecutor) -> TurnOutcome:
        language = turn.language
        if turn.order_id is None:
            if turn.checkout_id is not None:
                return self._checkout(turn, tools)
            return TurnOutcome(reply=_t("need_order", language))
        result = tools.call("order.track", order_id=turn.order_id)
        if not result.ok:
            return self._after_failure(result, language)
        order = result.payload
        evidence = (order["payment"].get("capture_evidence") or {}).get("kind", "none")
        reply = _t(
            "order",
            language,
            order_id=order["order_id"],
            state=order["state"],
            amount=order["amount"]["display"],
            currency=order["amount"]["currency"],
            evidence=evidence,
        )
        structured: dict[str, Any] = {"kind": "order", "order": order}
        words = set(_tokens(turn.message))
        remedy = None
        if words & _REFUND_CUES:
            remedy = "refund"
        elif words & _CANCEL_CUES:
            remedy = "cancel"
        if remedy is not None:
            # A proposal, never an execution. It names the order and the remedy and no
            # amount: what is owed is the Resolution Service's to decide when the buyer
            # confirms on the trusted surface (roster, Support Specialist hard rules).
            structured["proposal"] = {
                "action": "refund.request" if remedy == "refund" else "order.propose_cancel",
                "order_id": order["order_id"],
                "amount_minor": None,
                "executes_on": "trusted_surface",
            }
            reply += _t("proposal_remedy", language, remedy=remedy)
        return TurnOutcome(reply=reply, structured=structured)

    # --- merchant specialists ---------------------------------------------------------

    def _growth(self, turn: TurnInput, tools: ToolExecutor) -> TurnOutcome:
        language = turn.language
        words = set(_tokens(turn.message))
        if words & {"inventory", "stock", "anomaly", "anomalies", "स्टॉक"}:
            result = tools.call("merchant.inventory_anomalies.read")
            if not result.ok:
                return self._after_failure(result, language)
            anomalies = result.payload["anomalies"]
            if not anomalies:
                reply = _t("no_anomalies", language)
            else:
                items = "; ".join(f"{a['sku']} {a['anomaly']}" for a in anomalies)
                reply = _t("anomalies", language, count=len(anomalies), items=items)
            return TurnOutcome(
                reply=reply, structured={"kind": "inventory_anomalies", **result.payload}
            )
        if words & {"catalogue", "catalog", "health", "listing", "listings", "कैटलॉग"}:
            result = tools.call("merchant.catalogue_health.read")
            if not result.ok:
                return self._after_failure(result, language)
            p = result.payload
            reply = _t(
                "health",
                language,
                revision=p["catalogue_revision"],
                products=p["products"],
                listed=p["listed"],
                delisted=p["delisted"],
                out=p["out_of_stock"],
                low=p["low_stock"],
            )
            return TurnOutcome(reply=reply, structured={"kind": "catalogue_health", **p})
        result = tools.call("merchant.checkout_metrics.read")
        if not result.ok:
            return self._after_failure(result, language)
        p = result.payload
        states = ", ".join(f"{k} {v}" for k, v in sorted(p["checkouts_by_state"].items())) or "none"
        reply = _t("metrics", language, sample=p["sample_size"], states=states, orders=p["orders"])
        return TurnOutcome(reply=reply, structured={"kind": "checkout_metrics", **p})

    def _case(self, turn: TurnInput, tools: ToolExecutor) -> TurnOutcome:
        result = tools.call("support.case.read")
        if not result.ok:
            return self._after_failure(result, turn.language)
        return TurnOutcome(reply=_t("unavailable", turn.language), structured=result.payload)

    # --- shared ------------------------------------------------------------------------

    def _basket_view(
        self, turn: TurnInput, tools: ToolExecutor, *, propose_checkout: bool
    ) -> TurnOutcome:
        language = turn.language
        assert turn.basket_id is not None
        result = tools.call("basket.read", basket_id=turn.basket_id)
        if not result.ok:
            return self._after_failure(result, language)
        basket = result.payload
        quote = basket.get("quote")
        count = len(basket.get("lines", ()))
        if quote is None:
            reply = _t("basket_unpriced", language, count=count, code=basket["code"])
        else:
            reply = _t(
                "basket",
                language,
                count=count,
                total=quote["total"]["display"],
                currency=quote["total"]["currency"],
                stale=_t("stale", language) if basket.get("stale") else "",
            )
        structured: dict[str, Any] = {"kind": "basket", "basket": basket}
        if propose_checkout and count and quote is not None:
            structured["proposal"] = {
                "action": "checkout.create",
                "basket_id": basket["basket_id"],
                "executes_on": "trusted_surface",
            }
        return TurnOutcome(reply=reply, structured=structured)

    @staticmethod
    def _line_proposal(
        turn: TurnInput, product: Mapping[str, Any], ledger: TurnLedger
    ) -> dict[str, Any] | None:
        """A ``basket.update`` proposal for a product a tool returned this turn, or None.

        The provenance check is the whole function: ``product`` came from the ledger's
        own tool results, and the assertion below makes the dependency explicit rather
        than incidental. A quantity is parsed only from the buyer's message, capped at
        the basket service's own limit, and defaults to one.
        """
        sku = product["sku"]
        if sku not in ledger.seen_skus or not product.get("is_available"):
            return None
        words = set(_tokens(turn.message))
        if not (words & _ADD_CUES):
            return None
        quantity = max(1, min(quantity_in(turn.message), basket_service.MAX_LINE_QUANTITY))
        return {
            "action": "basket.update",
            "sku": sku,
            "quantity": quantity,
            "basket_id": None if turn.basket_id is None else str(turn.basket_id),
            "executes_on": "trusted_surface",
            "display": {"quantity": quantity, "name": product["display_name"]},
        }

    @staticmethod
    def _after_failure(result: ToolResult, language: Language) -> TurnOutcome:
        if result.denied:
            return TurnOutcome(reply=render_denial(result.reason_key or "", language))
        if result.reason_key == "not_found":
            return TurnOutcome(reply=_t("not_found", language))
        if result.reason_key in ("tool_budget_exhausted", "tool_not_registered", "tool_failed"):
            return TurnOutcome(reply=render_denial(result.reason_key, language))
        return TurnOutcome(reply=_t("unavailable", language))


def _refuse_absent_verbs(turn: TurnInput, ledger: TurnLedger) -> str | None:
    """Record a denial for each consent verb in the message; return the sentence to lead
    with, or None when the message asked for nothing an agent may not do.

    ``reason_key`` distinguishes two facts a reviewer cares about: the session does not
    hold the capability at all (``capability_missing``), or it does -- a BUYER session
    holds ``checkout.approve`` -- and an agent still may not exercise it because consent
    is not delegable (``not_on_agent_surface``). Either way no tool exists to run.
    """
    if turn.copilot is not Copilot.BUYER:
        return None
    words = _tokens(turn.message)
    seen: set[str] = set()
    for word in words:
        capability = ABSENT_VERBS.get(word)
        if capability is None or capability in seen:
            continue
        seen.add(capability)
        ledger.deny(
            capability,
            "not_on_agent_surface",
            tool=None,
            summary=f"refused: {capability} is buyer consent and not on the agent surface",
        )
    if not seen:
        return None
    return render_denial("capability_missing", turn.language)


# ------------------------------------------------------------------------- the turn


@dataclass(frozen=True, slots=True)
class TurnResult:
    """One turn, complete, in the shape the router serialises."""

    reply: str
    language: Language
    specialist: Specialist
    routing_reason: str
    tool_calls: tuple[ToolCall, ...]
    denials: tuple[Denial, ...]
    structured: dict[str, Any] | None
    principal_id: str


def run_turn(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    copilot: Copilot,
    message: str,
    locale: str | None,
    basket_id: uuid.UUID | None,
    checkout_id: uuid.UUID | None,
    order_id: uuid.UUID | None,
    runner: TurnRunner | None = None,
) -> TurnResult:
    """Bind, route, run, and record. The harness, in one function.

    Order matters and is the harness's whole job: language is fixed before anything
    reads the message, the principal is bound before any tool exists, the specialist is
    chosen before the runner is consulted, and the ledger the response is built from is
    the executor's own, so the response reports what happened and not what the runner
    says happened.
    """
    language = language_for(message, locale)
    binding = bind(ctx, copilot)
    turn = TurnInput(
        copilot=copilot,
        message=message,
        language=language,
        basket_id=basket_id,
        checkout_id=checkout_id,
        order_id=order_id,
    )
    chosen = route(turn)
    ledger = TurnLedger()
    tools = ToolExecutor(
        session=session,
        ctx=ctx,
        registry=registry,
        principal=binding.principal_for(chosen.specialist),
        specialist=chosen.specialist,
        language=language,
        ledger=ledger,
    )
    active: TurnRunner = runner if runner is not None else DeterministicRunner()
    outcome = active.run(turn, chosen, tools)
    _log.info(
        "agent turn session=%s copilot=%s specialist=%s reason=%s tools=%d denials=%d",
        session_tag(ctx.session_id),
        copilot.value,
        chosen.specialist.value,
        chosen.reason,
        len(ledger.tool_calls),
        len(ledger.denials),
    )
    return TurnResult(
        reply=outcome.reply,
        language=language,
        specialist=chosen.specialist,
        routing_reason=chosen.reason,
        tool_calls=tuple(ledger.tool_calls),
        denials=tuple(ledger.denials),
        structured=outcome.structured,
        principal_id=tools.principal.principal_id,
    )


def copilot_for(ctx: RequestContext, wanted: Copilot) -> Copilot:
    """Refuse a session on the wrong harness. 403: authenticated, not entitled.

    A buyer session on the merchant endpoint would, at best, bind to an empty capability
    set and answer nothing useful; at worst a future capability overlap would let a
    buyer read merchant metrics. Refusing by actor type closes that before binding.
    """
    actor = ctx.actor_type
    if wanted is Copilot.MERCHANT and actor is not ActorType.OPERATOR:
        raise ProblemError(
            403,
            "Merchant session required",
            "The merchant copilot serves OPERATOR sessions only.",
            actor_type=actor.value,
        )
    if wanted is Copilot.BUYER and actor not in (ActorType.BUYER, ActorType.AGENT):
        raise ProblemError(
            403,
            "Buyer session required",
            "The RazorAI serves BUYER and AGENT sessions only.",
            actor_type=actor.value,
        )
    return wanted

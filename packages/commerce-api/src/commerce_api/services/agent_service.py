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
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol

from agent_runtime.harness import BUYER_SPECIALISTS, Specialist
from agent_runtime.harness import session_tag as _harness_session_tag
from agent_runtime.language import Language, detect_language
from agent_runtime.rendering import render_denial, render_reasoning_unavailable
from sqlalchemy.orm import Session
from transaction_kernel import ActorType, AgentPrincipal

from ..deps import (
    AGENT_CAPABILITIES,
    SUPPORT_AGENT_CAPABILITIES,
    RequestContext,
    assert_owner,
)
from ..errors import ProblemError
from ..merchants import MerchantRegistry
from ..schemas import FreshnessOut, ProductOut, SearchHitOut
from . import cart_service, catalogue_service, checkout_service
from .refund_service import load_order, order_payload

__all__ = [
    "ABSENT_VERBS",
    "AGENT_SURFACE",
    "COPILOT_SPECIALISTS",
    "MAX_TOOL_CALLS",
    "SPECIALIST_ALLOWLIST",
    "TOOLS",
    "Binding",
    "Copilot",
    "DeterministicRunner",
    "Route",
    "ScenarioFaultClaimer",
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
    "line_proposal_record",
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

#: At or below this many units a listed product is reported as low on stock.
# ------------------------------------------------------------------------ vocabulary


class Copilot(StrEnum):
    """The harnesses. Neither is a model.

    One member, and an enum rather than a constant: the harness a turn runs under is
    written into every delegated principal id, so it has to be a named thing an auditor
    can read back. A merchant harness that returns adds a member here.
    """

    BUYER = "buyer"

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


#: The specialists of ``docs/briefs/AGENT_ROSTER.md``, and the one enumeration of
#: them: the harness's ``Specialist`` is the registry's ``AgentRole``, re-exported here so
#: the API, the harness, the tool factory and the specialist specs cannot disagree about
#: what "shopping" is. Only these may be models.


#: Every capability an agent principal may ever hold, in the vocabulary ``api_sessions``
#: stores. ``checkout.approve``, ``checkout.reject``, ``checkout.cancel``,
#: ``payment.verify`` and ``refund.request`` are Registry B, buyer consent, and are not
#: here -- which is what makes the absence of an approve or refund tool structural rather
#: than a matter of which tools happen to be registered.
AGENT_SURFACE: Final[frozenset[str]] = AGENT_CAPABILITIES | SUPPORT_AGENT_CAPABILITIES

#: Per-specialist allowlist (specification 5.4, intersection input 1), from the roster's
#: tool lists translated into the session vocabulary.
SPECIALIST_ALLOWLIST: Final[Mapping[Specialist, frozenset[str]]] = MappingProxyType(
    {
        Specialist.SHOPPING: frozenset({"catalogue.read", "basket.write"}),
        Specialist.CHECKOUT: frozenset(
            {"catalogue.read", "checkout.create", "checkout.submit_approved", "order.read"}
        ),
        Specialist.SUPPORT: frozenset({"order.read"}) | SUPPORT_AGENT_CAPABILITIES,
    }
)

#: Which specialists each harness may route to. A buyer session can never reach the
#: merchant specialists and the reverse, whatever the message says.
COPILOT_SPECIALISTS: Final[Mapping[Copilot, tuple[Specialist, ...]]] = MappingProxyType(
    {
        Copilot.BUYER: (Specialist.SHOPPING, Specialist.CHECKOUT, Specialist.SUPPORT),
    }
)

# The harness owns which specialists each copilot may reach; this table only fixes their
# order for the capabilities view. A disagreement is a bug, so it fails at import.
if frozenset(COPILOT_SPECIALISTS[Copilot.BUYER]) != BUYER_SPECIALISTS:
    raise RuntimeError("RazorAI specialists disagree with agent_runtime.harness")

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
            _spec("cart.read", "catalogue.read", Specialist.SHOPPING, Specialist.CHECKOUT),
            _spec("checkout.read", "order.read", Specialist.CHECKOUT, Specialist.SUPPORT),
            _spec("order.track", "order.read", Specialist.CHECKOUT, Specialist.SUPPORT),
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
            "cart.read": self._basket,
            "checkout.read": self._checkout,
            "order.track": self._order,
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

    def _basket(self, *, cart_id: uuid.UUID) -> tuple[dict[str, Any], str]:
        body = cart_service.read_cart(self._session, self._ctx, self._registry, cart_id)
        for line in body.get("lines", ()):
            sku = line.get("sku")
            if isinstance(sku, str):
                self._ledger.seen_skus.add(sku)
        return body, f"read cart ({len(body.get('lines', ()))} lines)"

    def _checkout(self, *, checkout_id: uuid.UUID) -> tuple[dict[str, Any], str]:
        body = checkout_service.read_checkout(self._session, self._ctx, checkout_id)
        return body, f"read checkout v{body['current_version']} {body['state']}"

    def _order(self, *, order_id: uuid.UUID) -> tuple[dict[str, Any], str]:
        order = load_order(self._session, self._ctx, order_id=order_id)
        assert_owner(self._session, self._ctx, order.checkout_id)
        body = order_payload(self._session, self._ctx, order).model_dump(mode="json")
        return body, f"tracked order {body['state']}"


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
    cart_id: uuid.UUID | None = None
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
        "checkout", "proceed", "approve", "approval", "approved", "reapprove", "pay", "payment",
        "submit", "version", "total", "bhugtan", "भुगतान", "चेकआउट", "मंज़ूर", "मंजूर",
    }
)  # fmt: skip
_ADD_CUES: Final[frozenset[str]] = frozenset(
    {"add", "put", "want", "need", "buy", "chahiye", "dalo", "daalo", "kharido", "चाहिए", "डालो"}
)
_REFUND_CUES: Final[frozenset[str]] = frozenset({"refund", "refunds", "रिफंड", "wapas", "वापस"})
_CANCEL_CUES: Final[frozenset[str]] = frozenset({"cancel", "cancelled", "रद्द", "raddi"})


def _tokens(message: str) -> list[str]:
    """The message as comparable words: NFKC first, then casefold.

    Casefolding alone is not a defence in this market. A Hindi IME emits the precomposed
    nukta letters (``ज़`` U+095B); the tables in this module are written with the
    decomposed pair (``ज`` + U+093C) that NFKC canonicalises to, and the two are
    different strings. Fullwidth Latin (``ｐａｙ``) folds to ASCII for the same reason.
    Without this, "मंज़ूर करो" typed on one keyboard recorded a denial and on another
    recorded nothing, and "ｐａｙ" asked for consent without ever being refused.
    """
    normalized = unicodedata.normalize("NFKC", message)
    return [token.casefold() for token in _WORDS.findall(normalized)]


def route(turn: TurnInput) -> Route:
    """Choose the specialist from structured intent first, then a lexicon. No model.

    Precedence is what the caller is looking at, then what they said: an order in
    context is a support conversation even if the message mentions a checkout, because
    the identifier is a fact and the word is a guess. A misroute costs one turn and
    changes no state; a model deciding this would cost a call and could not be tested
    for certainty.
    """
    words = set(_tokens(turn.message))
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

    ``agent_runtime``'s ADK adapter does **not** satisfy this, and this docstring said it
    did until a live turn answered ``AttributeError: 'AdkSpecialistRunner' object has no
    attribute 'run'``. The adapter implements the harness's own protocol -- ``async
    __call__(bound, message, turn, session) -> SpecialistReply`` -- and the two contracts
    are joined by :class:`~commerce_api.services.agent_bridge.SpecialistBridge`, which
    implements this one and awaits that one inside :meth:`run`.

    The HTTP layer does not care which runner it holds; what it guarantees either way is
    that the runner never sees the session, the tenant or a database handle. Its only
    handle on the platform is the :class:`ToolExecutor` it is passed, so every read it
    makes is gated and recorded whether a model or a template asked for it.
    """

    def run(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome: ...


#: The two demo faults an agent turn consults. The vocabulary belongs to
#: ``scenario_service.FaultKind``; it is spelled again here so that this module -- which
#: is the live turn path -- imports no demo apparatus at all and stays free of the
#: scenario controller's dependencies. ``test_capi_scenario`` asserts the two spellings
#: agree, because a rename on one side would otherwise leave a lever that arms cleanly
#: and never fires, which is the failure this whole build unit exists to remove.
REASONING_FAULT: Final[str] = "LLM_FAILURE"
SPEECH_FAULT: Final[str] = "TTS_FAILURE"


class ScenarioFaultClaimer(Protocol):
    """The demo controller's hook into a turn, and ``None`` everywhere else.

    A turn asks this object, once, which armed failures it has just consumed; the object
    owns the kernel transaction that disarms them and records that they fired. The
    Protocol exists so that this module never imports the scenario service, and so that
    the production gate is the *absence* of an object rather than a flag inside one:
    ``routers.agent`` passes ``None`` unless ``scenario_routes_enabled``, and with ``None``
    there is no query, no branch and nothing to reach.

    Naming a claimer that is not the demo controller would be a way to make a buyer's
    reasoning layer fail on purpose, which is why nothing but the router constructs one.
    """

    def claim_for_turn(
        self, ctx: RequestContext, *, context: Mapping[str, str]
    ) -> frozenset[str]: ...


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
            Language.EN: " Adding {quantity} × {name} at {price} {currency} each to your cart. "
            "Would you like to add anything else?",
            Language.HI: " {quantity} × {name}, {price} {currency} प्रति नग, आपकी बास्केट में "
            "जोड़ रहा हूँ। क्या आप कुछ और जोड़ना चाहेंगे?",
            Language.HI_LATN: " {quantity} × {name}, {price} {currency} prati nag, aapki cart "
            "mein add kar raha hoon. Kya aap kuch aur add karna chahenge?",
        },
        "proposal_line_more": {
            Language.EN: " Your cart already holds {current} of {name}; adding {quantity} more "
            "to take that line to {absolute}, at {price} {currency} each. "
            "Would you like to add anything else?",
            Language.HI: " आपकी बास्केट में {name} पहले से {current} हैं; {quantity} और जोड़कर उस "
            "लाइन को {absolute} कर रहा हूँ, {price} {currency} प्रति नग। क्या आप कुछ और जोड़ना चाहेंगे?",
            Language.HI_LATN: " Aapki cart mein {name} pehle se {current} hain; {quantity} aur "
            "add karke us line ko {absolute} kar raha hoon, {price} {currency} prati nag. "
            "Kya aap kuch aur add karna chahenge?",
        },
        "proposal_line_no_basket": {
            Language.EN: " Adding {quantity} × {name} at {price} {currency} each — no cart "
            "open yet, so opening one for you. Would you like to add anything else?",
            Language.HI: " {quantity} × {name}, {price} {currency} प्रति नग — कोई बास्केट नहीं "
            "थी, तो आपके लिए खोल रहा हूँ। क्या आप कुछ और जोड़ना चाहेंगे?",
            Language.HI_LATN: " {quantity} × {name}, {price} {currency} prati nag — koi "
            "cart khuli nahi thi, to open kar raha hoon. Kya aap kuch aur add karna chahenge?",
        },
        "proposal_line_unbound": {
            Language.EN: " Adding {quantity} × {name} at {price} {currency} each; I could not "
            "read your cart this turn, so the cart page has the exact total.",
            Language.HI: " {quantity} × {name}, {price} {currency} प्रति नग, जोड़ रहा हूँ; इस बार "
            "आपकी बास्केट नहीं पढ़ पाया, सटीक कुल बास्केट पेज पर दिखेगा।",
            Language.HI_LATN: " {quantity} × {name}, {price} {currency} prati nag, add kar raha "
            "hoon; is baar aapki cart nahi padh paya, sahi total cart page par dikhega.",
        },
        "proposal_clamped": {
            Language.EN: " You asked for {asked}; one line holds at most {cap}, so the proposal "
            "is for {cap} and I have not quietly rounded it for you.",
            Language.HI: " आपने {asked} माँगे; एक लाइन में अधिकतम {cap} ही आते हैं, इसलिए प्रस्ताव "
            "{cap} का है — मैंने इसे चुपचाप नहीं बदला।",
            Language.HI_LATN: " Aapne {asked} mange; ek line mein zyada se zyada {cap} hi aate "
            "hain, is liye prastav {cap} ka hai — maine ise chupchaap badla nahi.",
        },
        "proposal_low_stock": {
            Language.EN: " The store lists {stock} on the shelf, fewer than the {absolute} "
            "proposed. Nothing is held for you either way; stock is held only at checkout.",
            Language.HI: " दुकान शेल्फ़ पर {stock} बताती है, प्रस्तावित {absolute} से कम। किसी भी "
            "हाल में आपके लिए कुछ रोका नहीं गया है; स्टॉक केवल चेकआउट पर रुकता है।",
            Language.HI_LATN: " Dukaan shelf par {stock} batati hai, prastavit {absolute} se kam. "
            "Kisi bhi haal mein aapke liye kuch roka nahi gaya hai; stock sirf checkout par "
            "rukta hai.",
        },
        "disambiguate": {
            Language.EN: " I will not guess which of these {count} you meant. Pick one and I "
            "will prepare {quantity} of it with the store's own price on it.",
            Language.HI: " इन {count} में से आपका मतलब कौन-सा था, यह मैं अंदाज़े से तय नहीं करूँगा। "
            "एक चुनिए और मैं उसके {quantity} का प्रस्ताव दुकान के अपने दाम के साथ तैयार करूँगा।",
            Language.HI_LATN: " In {count} mein se aapka matlab kaunsa tha, yeh main andaaze se "
            "tay nahi karunga. Ek chuniye aur main uske {quantity} ka prastav dukaan ke apne "
            "daam ke saath taiyar karunga.",
        },
        "cart": {
            Language.EN: "Your cart has {count} lines and the store quotes {total} {currency} "
            "right now{stale}.",
            Language.HI: "आपकी बास्केट में {count} लाइनें हैं और दुकान अभी {total} {currency} बता "
            "रही है{stale}।",
            Language.HI_LATN: "Aapki cart mein {count} lines hain aur dukaan abhi {total} "
            "{currency} bata rahi hai{stale}.",
        },
        "stale": {
            Language.EN: " (the catalogue has moved since it was last priced)",
            Language.HI: " (पिछली बार दाम लगने के बाद कैटलॉग बदल गया है)",
            Language.HI_LATN: " (pichhli baar daam lagne ke baad catalogue badal gaya hai)",
        },
        "basket_unpriced": {
            Language.EN: "Your cart has {count} lines; the store cannot price it right now "
            "({code}).",
            Language.HI: "आपकी बास्केट में {count} लाइनें हैं; दुकान अभी इसका दाम नहीं लगा सकती ({code})।",
            Language.HI_LATN: "Aapki cart mein {count} lines hain; dukaan abhi iska daam nahi "
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
            Language.EN: "Confirm a checkout from your cart first; I can then explain each "
            "version and what the store says now.",
            Language.HI: "पहले अपनी बास्केट से चेकआउट खोलिए; फिर मैं हर संस्करण और दुकान की "
            "मौजूदा स्थिति समझा सकता हूँ।",
            Language.HI_LATN: "Pehle apni cart se checkout kholiye; phir main har version aur "
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
    }
)


def _t(key: str, language: Language, **values: Any) -> str:
    return _T[key][language].format(**values)


def _decision_card_from(checkout: Mapping[str, Any]) -> dict[str, Any] | None:
    """The decision card for a checkout whose earlier approval was superseded, or None.

    A turn never submits -- the agent proposes and a human submits on the trusted surface --
    so no ``AdmissionDecision`` passes through here and one must not be manufactured. What a
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


def line_proposal_record(
    product: Mapping[str, Any],
    delta: int,
    cart_id: uuid.UUID | None,
    tools: ToolExecutor,
) -> dict[str, Any]:
    """The ``basket.update`` proposal record for ``delta`` more units of one SKU.

    Shared by the deterministic runner, which parses the delta out of the buyer's
    sentence, and by the model bridge, whose ``basket_propose_line`` tool receives it as
    an argument. Both produce the same record, so the panel acts on one shape whichever
    half answered. The record never claims a write has happened: the buyer's own
    instruction -- not the agent's sentence -- is what the panel turns into a cart
    write, and this record describes that write precisely (which cart, which SKU, what
    absolute quantity, and the binding the mutation endpoint re-checks under the cart's
    lock).

    The quantity rule, the clamp and the binding are documented on
    :meth:`DeterministicRunner._line_proposal`; nothing here multiplies, adds or rounds a
    price. Every figure in ``display`` is copied from a tool result.
    """
    sku = product["sku"]
    display: dict[str, Any] = {
        "quantity": delta,
        "name": product["display_name"],
        "unit_label": product["unit_label"],
        "unit_price": product["unit_price"],
        "stock_units": product["stock_units"],
        "basket_total": None,
    }
    proposal: dict[str, Any] = {
        "action": "basket.update",
        "sku": sku,
        "cart_id": None if cart_id is None else str(cart_id),
        "delta": delta,
        "current_quantity": None,
        "quantity": None,
        "clamped_from": None,
        "exceeds_stock": False,
        "blocked_by": None,
        "executes_on": "trusted_surface",
        "binding": None,
        "display": display,
    }

    if cart_id is None:
        # No cart yet, so there is no line to make absolute against. The panel's direct
        # add creates the cart first; on an empty cart the absolute quantity equals
        # the delta, so ``blocked_by`` here is a request to create rather than a refusal.
        proposal["blocked_by"] = "no_basket"
        return proposal

    read = tools.call(
        "cart.read",
        cart_id=cart_id if isinstance(cart_id, uuid.UUID) else uuid.UUID(str(cart_id)),
    )
    if not read.ok:
        proposal["blocked_by"] = "basket_unreadable"
        return proposal

    cart = read.payload
    current = next(
        (int(line["quantity"]) for line in cart.get("lines", ()) if line.get("sku") == sku),
        0,
    )
    absolute = current + delta
    capped = min(absolute, cart_service.MAX_LINE_QUANTITY)
    proposal["current_quantity"] = current
    proposal["quantity"] = capped
    # A clamp the buyer did not ask for is a figure the platform substituted, so it is
    # reported rather than applied in silence.
    proposal["clamped_from"] = None if capped == absolute else absolute
    # Reported, never clamped to. Trimming the request to the shelf count would look
    # like a hold, and nothing is held: stock is reserved at checkout, and an agent
    # that could hold it could deny another buyer a product on the strength of a chat.
    proposal["exceeds_stock"] = capped > int(product["stock_units"])

    quote = cart.get("quote")
    display["basket_total"] = None if quote is None else quote["total"]
    proposal["binding"] = {
        "basket_content_hash": None if quote is None else quote["content_hash"],
        "unit_price_minor": int(product["unit_price_minor"]),
        "catalogue_revision": int(cart["freshness"]["catalogue_revision"]),
    }
    return proposal


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
            proposal = self._line_proposal(turn, product, tools)
            if proposal is not None:
                structured["proposal"] = proposal
                reply += self._proposal_sentence(proposal, language)
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
        # One hit is a proposal; several are a question. The `len(hits) == 1` guard was
        # always right -- a buyer who says "add 2 amul milk" and gets five products has not
        # named one, and picking the top-scoring row for them is the storefront deciding
        # what they meant. What was missing is the other half: until this branch existed
        # that turn ended in a sentence with nothing to act on, which is how "RazorAI fills
        # the cart" came to be printed on a homepage above a cart that stayed empty.
        if len(hits) == 1:
            proposal = self._line_proposal(turn, hits[0], tools)
            if proposal is not None:
                structured["proposal"] = proposal
                reply += self._proposal_sentence(proposal, language)
        else:
            choice = self._disambiguation(turn, hits, tools.ledger)
            if choice is not None:
                structured["proposal"] = choice
                reply += _t(
                    "disambiguate",
                    language,
                    count=len(choice["candidates"]),
                    quantity=choice["quantity"],
                )
        return TurnOutcome(reply=reply, structured=structured)

    def _checkout(self, turn: TurnInput, tools: ToolExecutor) -> TurnOutcome:
        language = turn.language
        if turn.checkout_id is None:
            if turn.cart_id is not None:
                return self._cart_view(turn, tools, propose_checkout=True)
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

    # --- shared ------------------------------------------------------------------------

    def _cart_view(
        self, turn: TurnInput, tools: ToolExecutor, *, propose_checkout: bool
    ) -> TurnOutcome:
        language = turn.language
        assert turn.cart_id is not None
        result = tools.call("cart.read", cart_id=turn.cart_id)
        if not result.ok:
            return self._after_failure(result, language)
        cart = result.payload
        quote = cart.get("quote")
        count = len(cart.get("lines", ()))
        if quote is None:
            reply = _t("basket_unpriced", language, count=count, code=cart["code"])
        else:
            reply = _t(
                "cart",
                language,
                count=count,
                total=quote["total"]["display"],
                currency=quote["total"]["currency"],
                stale=_t("stale", language) if cart.get("stale") else "",
            )
        structured: dict[str, Any] = {"kind": "cart", "cart": cart}
        if propose_checkout and count and quote is not None:
            structured["proposal"] = {
                "action": "checkout.create",
                "cart_id": cart["cart_id"],
                "executes_on": "trusted_surface",
            }
        return TurnOutcome(reply=reply, structured=structured)

    @staticmethod
    def _line_proposal(
        turn: TurnInput, product: Mapping[str, Any], tools: ToolExecutor
    ) -> dict[str, Any] | None:
        """A ``basket.update`` record for a product a tool returned this turn, or None.

        Provenance first: ``product`` must have come from this turn's own tool results, and
        the check below makes that dependency explicit rather than incidental.

        Then the correction this method exists for. ``quantity_in`` returns a **delta** --
        "add 2" is two more -- and ``PUT /v1/carts/{id}/lines/{sku}`` takes an
        **absolute** quantity, deliberately, because an absolute quantity is the only kind
        that survives a retry unchanged. Until the cart was read the delta went straight
        into the field the route reads as absolute, so "add 2 milk" against a cart
        already holding three would have silently *reduced* the line to two.

        The record itself -- current quantity read out of the cart, the absolute the
        write will send, the clamp, the binding -- is built by
        :func:`~commerce_api.services.agent_service.line_proposal_record`, the same
        function the model bridge's ``basket_propose_line`` tool uses, so the panel acts
        on one shape whichever half of the platform answered. The buyer's instruction, not
        this record, is what the panel turns into the write; nothing here moves a cart.
        """
        sku = product["sku"]
        if sku not in tools.ledger.seen_skus or not product.get("is_available"):
            return None
        if not (set(_tokens(turn.message)) & _ADD_CUES):
            return None
        delta = max(1, quantity_in(turn.message))
        return line_proposal_record(product, delta, turn.cart_id, tools)

    @staticmethod
    def _proposal_sentence(proposal: Mapping[str, Any], language: Language) -> str:
        """The reply's tail for a line proposal. Every substitution is a tool's figure."""
        display = proposal["display"]
        common = {
            "quantity": proposal["delta"],
            "name": display["name"],
            "price": display["unit_price"]["display"],
            "currency": display["unit_price"]["currency"],
        }
        blocked = proposal["blocked_by"]
        if blocked == "no_basket":
            return _t("proposal_line_no_basket", language, **common)
        if blocked == "basket_unreadable":
            return _t("proposal_line_unbound", language, **common)

        absolute = proposal["quantity"]
        current = proposal["current_quantity"]
        if current:
            sentence = _t(
                "proposal_line_more", language, current=current, absolute=absolute, **common
            )
        else:
            sentence = _t("proposal_line", language, **common)
        if proposal["clamped_from"] is not None:
            sentence += _t(
                "proposal_clamped",
                language,
                asked=proposal["clamped_from"],
                cap=cart_service.MAX_LINE_QUANTITY,
            )
        if proposal["exceeds_stock"]:
            sentence += _t(
                "proposal_low_stock", language, stock=display["stock_units"], absolute=absolute
            )
        return sentence

    @staticmethod
    def _disambiguation(
        turn: TurnInput, hits: Sequence[Mapping[str, Any]], ledger: TurnLedger
    ) -> dict[str, Any] | None:
        """The other half of "which one did you mean", or None when it does not apply.

        This action commits nothing and carries no binding token, because there is nothing
        to bind: pressing a row sends another turn naming the SKU, and *that* turn produces
        the ``basket.update`` proposal with the price on it. Two presses, and two is the
        right number -- collapsing them would have the buyer consenting to a price they
        glimpsed in a list of five while choosing on the name.

        ``matched_terms`` rides along because it is the honest reason each row is on the
        list. It is also what makes a Hinglish query auditable: a reviewer can see that
        "doodh" hit a real index term rather than a model's guess at one.
        """
        if not (set(_tokens(turn.message)) & _ADD_CUES):
            return None
        candidates = [
            {
                "sku": hit["sku"],
                "display_name": hit["display_name"],
                "unit_label": hit["unit_label"],
                "unit_price": hit["unit_price"],
                "stock_units": hit["stock_units"],
                "is_available": hit["is_available"],
                "matched_terms": list(hit.get("matched_terms", ())),
            }
            for hit in hits
            if hit["sku"] in ledger.seen_skus
        ]
        if len(candidates) < 2:
            return None
        return {
            "action": "cart.disambiguate",
            "cart_id": None if turn.cart_id is None else str(turn.cart_id),
            "quantity": max(1, quantity_in(turn.message)),
            "candidates": candidates,
            # Not ``trusted_surface``: nothing executes anywhere. The press asks the
            # question again with one product named, and the answer to that is what a
            # trusted surface would later execute.
            "executes_on": "conversation",
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

    #: Demo faults this turn consumed, as their wire names. Always empty outside the
    #: demonstration profile, where no claimer exists to consume anything. Kept out of
    #: the response body on purpose -- see ``routers.agent``, which carries it in a
    #: header so injected apparatus never mixes with organic data (specification 31.3).
    scenario_faults: frozenset[str] = frozenset()


def run_turn(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    copilot: Copilot,
    message: str,
    locale: str | None,
    cart_id: uuid.UUID | None,
    checkout_id: uuid.UUID | None,
    order_id: uuid.UUID | None,
    runner: TurnRunner | None = None,
    scenario: ScenarioFaultClaimer | None = None,
) -> TurnResult:
    """Bind, route, run, and record. The harness, in one function.

    Order matters and is the harness's whole job: language is fixed before anything
    reads the message, the principal is bound before any tool exists, the specialist is
    chosen before the runner is consulted, and the ledger the response is built from is
    the executor's own, so the response reports what happened and not what the runner
    says happened.

    ``scenario`` is the demonstration's failure lever and is ``None`` everywhere else. It
    is consulted after routing -- so the audit can say which specialist was about to run
    -- and before the runner, so that a reasoning failure fires *instead of* the model
    call and never alongside it. That is the same discipline the worker-side faults keep,
    and it is what makes the demonstration honest: there is no half-run turn to explain
    away, because the model was never asked.
    """
    language = language_for(message, locale)
    binding = bind(ctx, copilot)
    turn = TurnInput(
        copilot=copilot,
        message=message,
        language=language,
        cart_id=cart_id,
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
    fired: frozenset[str] = (
        frozenset()
        if scenario is None
        else scenario.claim_for_turn(
            ctx,
            context={
                "session_tag": session_tag(ctx.session_id),
                "copilot": copilot.value,
                "specialist": chosen.specialist.value,
                "routing_reason": chosen.reason,
                "fallback": "deterministic_runner",
            },
        )
    )
    if REASONING_FAULT in fired:
        # The model is not called at all, and the answer is still correct. A fresh
        # DeterministicRunner runs over the *same* executor and the same ledger, so the
        # tool calls the panel shows are the ones that really happened on this turn; the
        # leading sentence says which layer went missing. Reusing ``render_fallback``
        # here would tell the buyer that something was removed from an answer, which is a
        # different event and did not happen.
        outcome = DeterministicRunner().run(turn, chosen, tools)
        outcome = TurnOutcome(
            reply=f"{render_reasoning_unavailable(language)} {outcome.reply}",
            structured=outcome.structured,
        )
    elif runner is None:
        outcome = DeterministicRunner().run(turn, chosen, tools)
    else:
        # Imported here, not at module top, because agent_bridge imports from this module
        # to name its runner's inputs -- pulling the exception up to the import block would
        # close that loop into a cycle. The reference is only needed on the failure path, so
        # binding the name inside the branch that uses it costs nothing a healthy turn pays.
        from .agent_bridge import BridgeUnavailableError, SpecialistBridge

        try:
            outcome = runner.run(turn, chosen, tools)
            # A bridged specialist's call to the model returned. Recorded on the runner
            # itself -- not here in a local -- so `GET /v1/config` can read the same fact
            # this branch just proved, without re-deriving it from configuration the way
            # `bridged` alone would have to.
            if isinstance(runner, SpecialistBridge):
                runner.model_reached = True
        except BridgeUnavailableError as exc:
            # Distinct from the outage below, and logged so: an empty toolset is not the
            # model going missing, it is the roster and the capability table disagreeing
            # about what this specialist can hold, and no retry heals a disagreement. Left at
            # WARNING alongside a Vertex blip it read as one, and the wiring defect it names
            # -- which specialist bound to nothing, and against which tools -- stayed
            # invisible for the nine hours it took to find by hand. ERROR is the level that
            # pages someone; the exception's own message already carries the mismatch, so it
            # is passed through whole rather than re-summarised and drifting from the source.
            #
            # The buyer-facing answer is deliberately identical to the outage path: the same
            # deterministic reply with the same render_reasoning_unavailable prefix, because
            # specification 30's contract to the buyer does not change with the cause. Only
            # the operator's signal changes, which is the whole of this branch.
            _log.error(
                "agent specialist bound to an empty toolset -- capability/roster wiring "
                "defect, not a model failure; this will not self-heal: %s: %s",
                type(exc).__name__,
                exc,
            )
            outcome = DeterministicRunner().run(turn, chosen, tools)
            outcome = TurnOutcome(
                reply=f"{render_reasoning_unavailable(language)} {outcome.reply}",
                structured=outcome.structured,
            )
        except Exception as exc:  # noqa: BLE001 - specification 30 answers every model failure
            # A model that raises is the same event as a model that was never configured,
            # and specification 30 gives it one answer: preserve state, fall back to
            # deterministic text. Until the runner was attached this branch could not be
            # reached, and the call sat unguarded -- so the first real Vertex outage would
            # have turned every turn into a 500 on the surface whose entire claim is that
            # the deterministic layer does not depend on the model behaving.
            #
            # BridgeUnavailableError is peeled off above this line, so what reaches here is a
            # genuine model or transport failure -- expected, transient, and self-healing --
            # which is why it stays at WARNING while the wiring defect is raised to ERROR.
            #
            # The reply is built exactly as the armed LLM_FAILURE fault builds it: a fresh
            # DeterministicRunner over the *same* executor and the same ledger, so the tool
            # calls the panel shows are the ones that really happened, led by the sentence
            # that says which layer went missing. A real outage and the demonstration of one
            # therefore look identical to the buyer, which is the point of demonstrating it.
            _log.warning(
                "agent turn fell back to the deterministic runner: %s: %s",
                type(exc).__name__,
                exc,
            )
            # Recorded here and not in the ERROR branch above: a wiring defect says nothing
            # about whether the model would answer if it were asked correctly, so it must
            # not report `model_reached: false` -- that claim belongs only to a call that
            # actually reached the model and failed.
            if isinstance(runner, SpecialistBridge):
                runner.model_reached = False
            outcome = DeterministicRunner().run(turn, chosen, tools)
            outcome = TurnOutcome(
                reply=f"{render_reasoning_unavailable(language)} {outcome.reply}",
                structured=outcome.structured,
            )
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
        scenario_faults=fired,
    )


def copilot_for(ctx: RequestContext, wanted: Copilot) -> Copilot:
    """Refuse a session on the wrong harness. 403: authenticated, not entitled."""
    actor = ctx.actor_type
    if wanted is Copilot.BUYER and actor not in (ActorType.BUYER, ActorType.AGENT):
        raise ProblemError(
            403,
            "Buyer session required",
            "The RazorAI serves BUYER and AGENT sessions only.",
            actor_type=actor.value,
        )
    return wanted

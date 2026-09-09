"""The copilot harness: ordinary Python that owns everything a specialist must not.

A harness calls no model and has no prompt. One turn through :meth:`Harness.run` is:

1. Refuse a principal this harness does not serve (:meth:`Harness.accepts`), and refuse a
   session that belongs to another tenant. Both raise; neither reaches a specialist.
2. Refuse an interim speech transcript before routing. Specification 6.1 and 19.5: only
   a final transcript enters intent processing. This is a rule in code, not a prompt.
3. Detect the language from the buyer's words (:mod:`agent_runtime.language`).
4. Route to exactly one specialist, deterministically (:mod:`.routing`).
5. **Bind** the harness principal to the specialist: capabilities are
   ``harness ∩ role allowlist`` through :meth:`AgentPrincipal.subset_for`, which itself
   refuses widening. This is the moment authority is granted, and it is here, in code.
6. Build the specialist's tools through the factory, with the bound principal and the
   turn's :class:`~agent_runtime.turn.TurnContext` captured in closures.
7. Run the grounding hook (Unit A's rules) *before* the specialist, then the specialist
   under a timeout, then the reply post-check (:func:`verify_reply`) and the
   conversational rules of specification 6.1 *after*.
8. Persist session facts from structured tool records, write the transcript, and return
   a :class:`TurnResult` whose every field came from a record, never from prose.

The two seams -- :class:`ToolsetBuilder` and :class:`SpecialistRunner` -- exist so that
this module imports nothing from ``google.adk`` and a test can drive a whole turn with a
scripted specialist. The defaults resolve lazily to the factory and the ADK runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from commerce_domain import ActorType, AdmissionDecision, AgentPrincipal, RecoveryCode

from ..backends.base import CommerceBackend
from ..capabilities.registry import AGENT_ALLOWLIST, ALL_CAPABILITIES
from ..capabilities.tools import (
    STATE_CART_ID,
    STATE_CHECKOUT_ID,
    STATE_CHECKOUT_VERSION,
    BoundToolset,
    build_toolset,
)
from ..core.grounding_rules import DEFAULT_LEXICON, GroundingState, first_rule, rules_for
from ..core.provenance import PROVENANCE_STATE_KEY, SessionProvenance
from ..grounding.postcheck import extract_amounts_minor, verify_reply
from ..language import Language, detect_language
from ..rendering.messages import recovery_text, render_decision, render_fallback
from ..rendering.money import display_delta_value, is_money_field
from ..turn import Denial, ToolCallRecord, TurnContext
from .routing import Clarification, Route, Specialist
from .session import (
    CopilotSession,
    InMemorySessionStore,
    Modality,
    SessionRefusedError,
    SessionStore,
)
from .transcript import AgentEvent, Transcript, TranscriptTurn, events_for_calls

__all__ = [
    "REGISTRY_A_CAPABILITIES",
    "ROLE_CAPABILITIES",
    "Binding",
    "BindingError",
    "BoundSpecialist",
    "GroundingHook",
    "HandBack",
    "Harness",
    "HarnessConfigurationError",
    "PrincipalRefusedError",
    "SpecialistInput",
    "SpecialistReply",
    "SpecialistRunner",
    "ToolsetBuilder",
    "TurnResult",
    "bind",
    "enforce_conversational_rules",
    "factory_toolset",
    "is_buyer_principal",
    "is_merchant_principal",
    "prefetch_grounding",
]

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ capabilities

#: Specification 5.3 Registry A, as the registry enumerates it. A harness principal
#: carrying any other string is malformed and refused: there is no Registry B, C or D
#: capability an agent can hold, so there is nothing a harness could bind it to.
REGISTRY_A_CAPABILITIES: Final[frozenset[str]] = ALL_CAPABILITIES

#: The roster's allowlist per specialist (docs/briefs/AGENT_ROSTER.md), read from the one
#: table the tool factory also reads. This is intersection input 1 of specification 5.4;
#: the harness principal is input 4. Derived rather than restated so the set a specialist
#: is *bound* to and the set its *tools* are built from cannot drift apart.
ROLE_CAPABILITIES: Final[Mapping[Specialist, frozenset[str]]] = {
    Specialist(role.value): frozenset(capability.value for capability in capabilities)
    for role, capabilities in AGENT_ALLOWLIST.items()
}


class PrincipalRefusedError(Exception):
    """This harness does not serve this principal. Raised before routing; nothing ran."""


class BindingError(Exception):
    """Binding would have granted a specialist something the harness does not hold."""


class HarnessConfigurationError(Exception):
    """A seam has no implementation: no tool factory or no model runtime is available."""


@dataclass(frozen=True, slots=True)
class Binding:
    """A specialist principal derived from a harness principal. Immutable evidence."""

    specialist: Specialist
    harness_principal: AgentPrincipal
    principal: AgentPrincipal

    @property
    def capabilities(self) -> frozenset[str]:
        return self.principal.capabilities


def bind(
    principal: AgentPrincipal,
    specialist: Specialist,
    *,
    allowlist: Mapping[Specialist, frozenset[str]] = ROLE_CAPABILITIES,
) -> Binding:
    """Derive the specialist's principal: ``harness ∩ allowlist``, never wider.

    ``subset_for`` refuses widening on its own; the check after it is a second, local
    proof of the same invariant, because this is the invariant a prompt-injection attack
    would most like to break and two independent checks are cheap.
    """
    allowed = allowlist[specialist] & principal.capabilities
    bound = principal.subset_for(specialist.value, allowed)
    if not bound.capabilities <= principal.capabilities:  # pragma: no cover - subset_for raises
        raise BindingError(
            f"{specialist.value} would hold {sorted(bound.capabilities - principal.capabilities)}"
        )
    if bound.tenant_id != principal.tenant_id:  # pragma: no cover - subset_for copies it
        raise BindingError("binding changed the tenant")
    return Binding(specialist=specialist, harness_principal=principal, principal=bound)


# ------------------------------------------------------------------------- seams


@dataclass(frozen=True, slots=True)
class BoundSpecialist:
    """What a runner receives: the role, its bound principal, and its factory-built tools."""

    specialist: Specialist
    binding: Binding
    tools: Sequence[object] | BoundToolset

    @property
    def principal(self) -> AgentPrincipal:
        return self.binding.principal

    @property
    def agent_name(self) -> str:
        return self.specialist.value


@dataclass(frozen=True, slots=True)
class SpecialistInput:
    """The message a specialist gets. ``preamble`` is a fenced prefetch result from the
    grounding hook; ``facts`` are session facts for the dynamic prompt block (ids and a
    clock only, never product text). Neither ever enters the static instruction."""

    text: str
    language: Language
    preamble: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HandBack:
    """A specialist's typed statement that this is not its job. Routing reads this,
    never model prose (ADR 0004 gate 22)."""

    to: Specialist
    reason: str


@dataclass(frozen=True, slots=True)
class SpecialistReply:
    """What a runner returns. ``text`` is unverified until the harness post-checks it."""

    text: str
    structured: Mapping[str, Any] = field(default_factory=dict)
    handback: HandBack | None = None


class ToolsetBuilder(Protocol):
    """The factory seam. The only source of tools for any specialist."""

    def __call__(
        self, bound: Binding, backend: CommerceBackend, turn: TurnContext, session: CopilotSession
    ) -> Sequence[object] | BoundToolset: ...


class SpecialistRunner(Protocol):
    """The model seam. Runs one specialist for one turn; the harness never calls a model."""

    def __call__(
        self,
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> Awaitable[SpecialistReply]: ...


GroundingHook = Callable[
    [str, CopilotSession, TurnContext, "Sequence[object] | BoundToolset"], Awaitable[str | None]
]
"""Runs BEFORE the specialist: Unit A's grounding rules. Returns a fenced preamble or None."""


def factory_toolset(
    bound: Binding, backend: CommerceBackend, turn: TurnContext, session: CopilotSession
) -> BoundToolset:
    """The default :class:`ToolsetBuilder`: the factory in ``capabilities/tools.py``.

    The specialist principal is the *bound* one, so the factory builds only tools that
    principal may hold and the gate it returns is keyed to the same principal. The session
    id is the harness's, never a model argument.
    """
    return build_toolset(
        bound,
        backend,
        turn,
        session_id=session.session_id,
        agent_name=bound.specialist.value,
    )


class _PrefetchContext:
    """The tool context a harness-side read runs under: the session's own state."""

    def __init__(self, state: MutableMapping[str, Any], call_id: str) -> None:
        self._state = state
        self._call_id = call_id

    @property
    def state(self) -> MutableMapping[str, Any]:
        return self._state

    @property
    def function_call_id(self) -> str | None:
        return self._call_id


async def prefetch_grounding(
    text: str,
    session: CopilotSession,
    turn: TurnContext,
    tools: Sequence[object] | BoundToolset,
) -> str | None:
    """The default :data:`GroundingHook`: Unit A's rules, enforced by prefetch.

    The first rule that fires names the read the turn must start with. Where the input is
    already known to the harness (a SKU token, the session's checkout, an order id) the
    harness runs that read itself -- through the same factory tool and the same gate the
    model would go through, so the call is budgeted, recorded and provenance-tracked like
    any other -- and hands the result to the specialist above the message. A rule whose
    input is the model's to write has no prefetch form and is left to the adapter to force.
    A failed prefetch is logged and the turn goes on without it, as in the reference.
    """
    if not isinstance(tools, BoundToolset):
        return None
    rules = rules_for(turn.agent_name or "")
    if not rules:
        return None
    # `session.state` is the outer, tool-visible dict; the provenance record lives inside
    # it under PROVENANCE_STATE_KEY, which is how `_load` in the tool factory reads it.
    # Passing the outer dict gave `from_state` a mapping with no "skus" key, and its
    # documented fail-safe -- anything malformed yields an *empty* record -- turned that
    # into "this session has seen nothing". Every rule conditioned on a SKU already having
    # been seen therefore judged against an empty set on every turn.
    provenance = SessionProvenance.from_state(session.state.get(PROVENANCE_STATE_KEY))
    state = GroundingState(
        seen_skus=provenance.seen_skus(),
        cart_id=session.cart_id,
        checkout_id=session.checkout_id,
        order_id=session.order_id,
    )
    found = first_rule(rules, DEFAULT_LEXICON, text, state)
    if found is None:
        return None
    rule, args = found
    if rule.prefetch_intro is None or rule.tool not in tools.names:
        return None
    tool = tools.get(rule.tool)
    # Ids the tool reads from session state are not schema parameters; pass only what is.
    call_args = {key: value for key, value in args.items() if key in tool.parameters}
    context = _PrefetchContext(session.state, f"prefetch:{rule.name}")
    if tools.gate(tool, call_args, context) is not None:
        return None  # the gate recorded the denial on the turn; nothing ran
    try:
        result = await tool.func(**call_args, tool_context=context)
    except Exception:
        logger.warning(
            "prefetch %s failed and the turn continues without it session=%s",
            rule.tool,
            session.tag,
            exc_info=True,
        )
        return None
    body = json.dumps(result, ensure_ascii=False, default=str, sort_keys=True)
    return f"{rule.prefetch_intro(args)}\n{body}"


async def _no_runtime(
    bound: BoundSpecialist, message: SpecialistInput, turn: TurnContext, session: CopilotSession
) -> SpecialistReply:
    del bound, message, turn, session
    raise HarnessConfigurationError("no specialist runtime is configured; pass runner=")


# ------------------------------------------------------------------------ results


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Everything the API needs from one turn. Every field is from a record, not prose."""

    reply_text: str
    structured: dict[str, Any]
    tool_calls: tuple[ToolCallRecord, ...]
    denials: tuple[Denial, ...]
    language: Language
    specialist: str | None
    routing_reason: str
    session_id: str
    tenant_id: uuid.UUID
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None
    turn_id: uuid.UUID
    events: tuple[AgentEvent, ...]
    corrections: tuple[str, ...]
    refused: bool
    stop_reason: str


# ---------------------------------------------------------- specification 6.1 rules

_INTERIM_REFUSED: Final[Mapping[Language, str]] = {
    Language.EN: "Still listening — I will act once your sentence is complete.",
    Language.HI: "सुन रहा हूँ — आपका वाक्य पूरा होते ही मैं आगे बढ़ूँगा।",
    Language.HI_LATN: "Sun raha hoon — aapka vaakya poora hote hi main aage badhunga.",
}

_UNAVAILABLE: Final[Mapping[Language, str]] = {
    Language.EN: "Not available right now: {skus}.",
    Language.HI: "अभी उपलब्ध नहीं: {skus}।",
    Language.HI_LATN: "Abhi available nahi: {skus}.",
}

CORRECTION_INTERIM = "interim_transcript_refused"
CORRECTION_UNGROUNDED = "ungrounded_sentences_dropped"
CORRECTION_DECISION = "decision_deltas_restored"
CORRECTION_RECOVERY = "recovery_code_restored"
CORRECTION_UNAVAILABLE = "unavailable_items_restored"
CORRECTION_FALLBACK = "fallback_rendered"
#: A scarcity or popularity claim was taken out. Recorded apart from the general
#: ungrounded-sentence correction because it answers a different question about the run --
#: not "did the model get a number wrong" but "did it try to pressure the buyer" -- and a
#: reviewer reading the trace should be able to see that on its own.
CORRECTION_PRESSURE = "sales_pressure_removed"


def _mentions_value(reply: str, field_path: str, value: Any, currency: str) -> bool:
    """Whether ``reply`` states ``value`` in any currency spelling the post-check accepts."""
    if value is None:
        return True
    if is_money_field(field_path) and isinstance(value, int) and not isinstance(value, bool):
        return value in extract_amounts_minor(reply, currency)
    return display_delta_value(field_path, value, currency) in reply


def _decision_is_told(
    reply: str, decision: AdmissionDecision, language: Language, currency: str
) -> bool:
    if decision.allowed:
        return True
    if not decision.deltas:
        return recovery_text(decision.code, language) in reply
    return all(
        _mentions_value(reply, delta.field_path, delta.approved, currency)
        and _mentions_value(reply, delta.field_path, delta.current, currency)
        for delta in decision.deltas
    )


def enforce_conversational_rules(
    reply: str,
    turns: Sequence[TurnContext],
    language: Language,
    *,
    currency: str = "INR",
) -> tuple[str, tuple[str, ...]]:
    """Specification 6.1 as code: a material change a tool reported is never summarised away.

    Three sources of material change, all structured, none from prose:

    * A kernel decision with deltas. Every approved and current value must appear in the
      reply; if one is missing the deterministic :func:`render_decision` block is appended
      whole, because the hero moment of a refusal is the buyer seeing every number.
    * A non-OK recovery code on a cart or checkout tool result. The code's template
      sentence must appear.
    * A line the merchant reported unavailable. The SKU must be named.

    The reply is corrected by appending, never by editing the model's sentences: the
    template is the authoritative text and the prose around it is the model's to own.
    """
    corrections: list[str] = []
    additions: list[str] = []
    decided_codes = {decision.code for turn in turns for decision in turn.decisions}

    for turn in turns:
        for decision in turn.decisions:
            if not _decision_is_told(reply, decision, language, currency):
                additions.append(render_decision(decision, language, currency=currency))
                corrections.append(CORRECTION_DECISION)

        for record in turn.tool_calls:
            code = record.summary.get("code")
            if not record.ok or not isinstance(code, str) or code == RecoveryCode.OK.value:
                continue
            try:
                recovery = RecoveryCode(code)
            except ValueError:
                continue
            if recovery in decided_codes:
                continue  # the decision block above already carries this code's text
            sentence = recovery_text(recovery, language)
            if sentence not in reply and sentence not in additions:
                additions.append(sentence)
                corrections.append(CORRECTION_RECOVERY)

        unavailable = sorted(
            sku
            for sku, product in turn.ledger.products.items()
            if not product.is_available and product.name == sku and sku not in reply
        )
        if unavailable:
            additions.append(_UNAVAILABLE[language].format(skus=", ".join(unavailable)))
            corrections.append(CORRECTION_UNAVAILABLE)

    if not additions:
        return reply, ()
    parts = [reply.strip()] if reply.strip() else []
    return "\n\n".join([*parts, *additions]), tuple(dict.fromkeys(corrections))


# ------------------------------------------------------------------------ harness

_SESSION_FACT_TOOLS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    # tool -> ((summary key, session attribute), ...)
    "basket_create": (("cart_id", "cart_id"),),
    "checkout_create": (("checkout_id", "checkout_id"), ("version", "checkout_version")),
    "checkout_get": (("current_version", "checkout_version"),),
    "order_track": (("order_id", "order_id"),),
    "support_escalate": (("case_id", "case_id"),),
}


#: Tool-state keys the factory's tools maintain -> session attributes.
_STATE_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (STATE_CART_ID, "cart_id"),
    (STATE_CHECKOUT_ID, "checkout_id"),
    (STATE_CHECKOUT_VERSION, "checkout_version"),
)


class Harness:
    """Base of the two copilots. Subclasses name their specialists, router and acceptance."""

    specialists: frozenset[Specialist] = frozenset()

    def __init__(
        self,
        *,
        runner: SpecialistRunner | None = None,
        tools: ToolsetBuilder | None = None,
        grounding: GroundingHook | None = None,
        store: SessionStore | None = None,
        turn_timeout_s: float = 30.0,
        currency: str = "INR",
    ) -> None:
        self._runner: SpecialistRunner = runner if runner is not None else _no_runtime
        self._tools: ToolsetBuilder = tools if tools is not None else factory_toolset
        self._grounding: GroundingHook = grounding if grounding is not None else prefetch_grounding
        self._store: SessionStore = store if store is not None else InMemorySessionStore()
        self._transcripts: dict[str, Transcript] = {}
        self._turn_timeout_s = turn_timeout_s
        self._currency = currency

    # ---- what subclasses define ------------------------------------------

    def accepts(self, principal: AgentPrincipal) -> bool:
        """Whether this harness serves ``principal``. Subclasses decide; the base refuses."""
        del principal
        return False

    def route(
        self,
        text: str,
        language: Language,
        context: Mapping[str, Any],
        session: CopilotSession,
    ) -> Route | Clarification:
        raise NotImplementedError

    # ---- session ----------------------------------------------------------

    def transcript(self, session_id: str) -> Transcript:
        return self._transcripts.setdefault(session_id, Transcript(session_id))

    def session(self, session_id: str) -> CopilotSession | None:
        return self._store.get(session_id)

    def _open_session(
        self, session_id: str, principal: AgentPrincipal, context: Mapping[str, Any]
    ) -> CopilotSession:
        existing = self._store.get(session_id)
        if existing is not None:
            if existing.tenant_id != principal.tenant_id:
                raise SessionRefusedError("session belongs to another tenant")
            if existing.principal_id != principal.principal_id:
                raise SessionRefusedError("session belongs to another principal")
            return existing
        correlation = principal.correlation_id
        if correlation is None:
            raw = context.get("correlation_id")
            correlation = raw if isinstance(raw, uuid.UUID) else uuid.uuid4()
        session = CopilotSession(
            session_id=session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            correlation_id=correlation,
            buyer_ref=principal.buyer_ref,
            merchant_id=principal.merchant_id,
        )
        self._store.put(session)
        return session

    def _refuse_malformed(self, principal: AgentPrincipal) -> None:
        if not self.accepts(principal):
            raise PrincipalRefusedError(
                f"{type(self).__name__} does not serve principal {principal.principal_id}"
            )
        foreign = principal.capabilities - REGISTRY_A_CAPABILITIES
        if foreign:
            raise PrincipalRefusedError(
                f"principal holds non-Registry-A capabilities: {sorted(foreign)}"
            )

    # ---- the turn ---------------------------------------------------------

    async def run(
        self,
        session_id: str,
        principal: AgentPrincipal,
        text: str,
        backend: CommerceBackend,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> TurnResult:
        """One turn. See the module docstring for the eight steps."""
        ctx: dict[str, Any] = dict(context or {})
        self._refuse_malformed(principal)
        session = self._open_session(session_id, principal, ctx)
        started = time.monotonic()
        turn_id, causation = session.begin_turn()
        session.modality = _modality(ctx, session.modality)
        language = detect_language(text)
        events: list[AgentEvent] = [AgentEvent.user_message(text, modality=session.modality.value)]
        self._absorb_context(session, ctx)

        def finish(
            reply: str,
            *,
            specialist: Specialist | None,
            routing_reason: str,
            turns: Sequence[TurnContext] = (),
            corrections: Sequence[str] = (),
            structured: Mapping[str, Any] | None = None,
            refused: bool = False,
            stop_reason: str = "end_turn",
        ) -> TurnResult:
            return self._finish(
                session=session,
                turn_id=turn_id,
                causation=causation,
                started=started,
                language=language,
                text=text,
                reply=reply,
                specialist=specialist,
                routing_reason=routing_reason,
                turns=turns,
                corrections=corrections,
                structured=dict(structured or {}),
                refused=refused,
                stop_reason=stop_reason,
                events=events,
            )

        # Specification 6.1 / 19.5: an interim transcript is never confirmed intent.
        if ctx.get("transcript") == "interim":
            return finish(
                _INTERIM_REFUSED[language],
                specialist=None,
                routing_reason="refused:interim_transcript",
                corrections=(CORRECTION_INTERIM,),
                refused=True,
                stop_reason="refused",
            )

        routed = self.route(text, language, ctx, session)
        if isinstance(routed, Clarification):
            return finish(
                routed.question,
                specialist=None,
                routing_reason=routed.reason,
                stop_reason="clarify",
            )

        turns: list[TurnContext] = []
        reply, specialist, reason, structured, stop = await self._run_specialist(
            routed, principal, text, language, backend, session, turns
        )
        if stop != "end_turn":
            # Timeout or runtime failure: fallback text, state untouched (spec 6.1, 29.4).
            return finish(
                render_fallback(language),
                specialist=specialist,
                routing_reason=reason,
                turns=turns,
                corrections=(CORRECTION_FALLBACK,),
                structured=structured,
                stop_reason=stop,
            )

        corrections: list[str] = []
        ledger = turns[-1].ledger
        check = verify_reply(reply, ledger, currency=self._currency)
        reply = check.reply
        if check.rewritten:
            corrections.append(CORRECTION_UNGROUNDED)
            structured["dropped_sentences"] = len(check.dropped_sentences)
        if check.pressure_removed or check.ungrounded_stock_counts:
            corrections.append(CORRECTION_PRESSURE)
        reply, restored = enforce_conversational_rules(
            reply, turns, language, currency=self._currency
        )
        corrections.extend(restored)
        if not reply.strip():
            reply = render_fallback(language)
            corrections.append(CORRECTION_FALLBACK)
        return finish(
            reply,
            specialist=specialist,
            routing_reason=reason,
            turns=turns,
            corrections=corrections,
            structured=structured,
        )

    async def _run_specialist(
        self,
        route: Route,
        principal: AgentPrincipal,
        text: str,
        language: Language,
        backend: CommerceBackend,
        session: CopilotSession,
        turns: list[TurnContext],
    ) -> tuple[str, Specialist, str, dict[str, Any], str]:
        """Bind, build tools, ground, run. At most one typed hand-back per turn."""
        specialist = route.specialist
        reason = route.reason
        structured: dict[str, Any] = {}
        for hop in range(2):
            if specialist not in self.specialists:
                raise HarnessConfigurationError(
                    f"{type(self).__name__} has no specialist {specialist.value}"
                )
            binding = bind(principal, specialist)
            turn = TurnContext(
                language=language, principal=binding.principal, agent_name=specialist.value
            )
            turns.append(turn)
            tools = self._tools(binding, backend, turn, session)
            bound = BoundSpecialist(specialist=specialist, binding=binding, tools=tools)
            try:
                async with asyncio.timeout(self._turn_timeout_s):
                    preamble = await self._grounding(text, session, turn, tools)
                    message = SpecialistInput(
                        text=text,
                        language=language,
                        preamble=preamble,
                        facts=_facts(session, language),
                    )
                    reply = await self._runner(bound, message, turn, session)
            except TimeoutError:
                logger.warning(
                    "turn timeout session=%s specialist=%s", session.tag, specialist.value
                )
                return "", specialist, reason, structured, "timeout"
            except HarnessConfigurationError:
                raise
            except Exception:
                logger.exception(
                    "specialist failed session=%s specialist=%s", session.tag, specialist.value
                )
                return "", specialist, reason, structured, "error"

            structured = dict(reply.structured)
            if reply.handback is None or hop == 1:
                if reply.handback is not None:
                    structured["handback_ignored"] = reply.handback.to.value
                return reply.text, specialist, reason, structured, "end_turn"
            handback = reply.handback
            if handback.to not in self.specialists or handback.to is specialist:
                structured["handback_ignored"] = handback.to.value
                return reply.text, specialist, reason, structured, "end_turn"
            reason = f"handback:{specialist.value}->{handback.to.value}:{handback.reason}"
            specialist = handback.to
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- after the turn ----------------------------------------------------

    @staticmethod
    def _absorb_context(session: CopilotSession, ctx: Mapping[str, Any]) -> None:
        """Server-supplied ids in the request context become session facts."""
        for key in ("checkout_id", "order_id", "case_id", "cart_id"):
            value = ctx.get(key)
            if isinstance(value, str) and value.strip():
                setattr(session, key, value.strip())

    @staticmethod
    def _absorb_records(session: CopilotSession, turns: Sequence[TurnContext]) -> None:
        """Ids from structured tool records and tool state become session facts; prose never."""
        for state_key, attribute in _STATE_FACTS:
            value = session.state.get(state_key)
            if value is not None and value != "":
                setattr(session, attribute, value)
        for turn in turns:
            for record in turn.tool_calls:
                if not record.ok:
                    continue
                for summary_key, attribute in _SESSION_FACT_TOOLS.get(record.tool, ()):
                    value = record.summary.get(summary_key)
                    if value is not None:
                        setattr(session, attribute, value)

    def _finish(
        self,
        *,
        session: CopilotSession,
        turn_id: uuid.UUID,
        causation: uuid.UUID | None,
        started: float,
        language: Language,
        text: str,
        reply: str,
        specialist: Specialist | None,
        routing_reason: str,
        turns: Sequence[TurnContext],
        corrections: Sequence[str],
        structured: dict[str, Any],
        refused: bool,
        stop_reason: str,
        events: list[AgentEvent],
    ) -> TurnResult:
        self._absorb_records(session, turns)
        tool_calls = tuple(record for turn in turns for record in turn.tool_calls)
        denials = tuple(denial for turn in turns for denial in turn.denials)
        decisions = [decision for turn in turns for decision in turn.decisions]
        events.extend(events_for_calls(tool_calls, turn_id))
        if reply:
            events.append(AgentEvent.text_delta(reply))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        name = None if specialist is None else specialist.value
        events.append(
            AgentEvent.turn_complete(
                stop_reason=stop_reason,
                specialist=name,
                routing_reason=routing_reason,
                elapsed_ms=elapsed_ms,
                corrections=corrections,
            )
        )
        structured.update(
            {
                "routing": {"specialist": name, "reason": routing_reason},
                "session": {
                    "cart_id": session.cart_id,
                    "checkout_id": session.checkout_id,
                    "checkout_version": session.checkout_version,
                    "order_id": session.order_id,
                    "case_id": session.case_id,
                    "modality": session.modality.value,
                },
                "decisions": [
                    {
                        "decision_id": str(d.decision_id),
                        "allowed": d.allowed,
                        "code": d.code.value,
                        "next_version": d.next_version,
                        "deltas": [
                            {
                                "field_path": delta.field_path,
                                "approved": delta.approved,
                                "current": delta.current,
                                "reason": delta.reason,
                            }
                            for delta in d.deltas
                        ],
                    }
                    for d in decisions
                ],
                "injection_flags": sum(len(turn.injection_flags) for turn in turns),
                "corrections": list(corrections),
            }
        )
        session.end_turn(turn_id, name, language)
        self._store.put(session)
        self.transcript(session.session_id).append(
            TranscriptTurn(
                turn_id=turn_id,
                correlation_id=session.correlation_id,
                causation_id=causation,
                specialist=name,
                routing_reason=routing_reason,
                user_text=text,
                reply_text=reply,
                events=tuple(events),
            )
        )
        logger.info(
            "turn session=%s turn=%d specialist=%s reason=%s calls=%d denials=%d "
            "corrections=%s stop=%s elapsed_ms=%d",
            session.tag,
            session.turns,
            name,
            routing_reason,
            len(tool_calls),
            len(denials),
            ",".join(corrections) or "-",
            stop_reason,
            elapsed_ms,
        )
        return TurnResult(
            reply_text=reply,
            structured=structured,
            tool_calls=tool_calls,
            denials=denials,
            language=language,
            specialist=name,
            routing_reason=routing_reason,
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            correlation_id=session.correlation_id,
            causation_id=causation,
            turn_id=turn_id,
            events=tuple(events),
            corrections=tuple(corrections),
            refused=refused,
            stop_reason=stop_reason,
        )


def _modality(ctx: Mapping[str, Any], current: Modality) -> Modality:
    raw = ctx.get("modality")
    if isinstance(raw, Modality):
        return raw
    if isinstance(raw, str):
        try:
            return Modality(raw.casefold())
        except ValueError:
            return current
    return current


def _facts(session: CopilotSession, language: Language) -> dict[str, Any]:
    """Session facts for the dynamic prompt block: ids and an hour-rounded clock.

    The clock is rounded so the block is byte-stable within an hour (ADR 0004 §1.5).
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return {
        "language": language.label,
        "modality": session.modality.value,
        "cart_id": session.cart_id,
        "checkout_id": session.checkout_id,
        "checkout_version": session.checkout_version,
        "order_id": session.order_id,
        "case_id": session.case_id,
        "clock_hour_utc": now.isoformat(),
    }


def is_buyer_principal(principal: AgentPrincipal) -> bool:
    """A principal minted for a buyer session: it carries a buyer scope or is the buyer.

    A MERCHANT actor is never one, whatever else the row says. That exception is not
    defensive tidiness: ``api_sessions.buyer_ref`` was NOT NULL until merchant sessions
    existed, and every session that lacked a real buyer got a minted placeholder. A
    merchant session carrying one would have satisfied this test, which would have made
    :func:`is_merchant_principal` false, which would have routed a merchant to the buyer's
    copilot. The column is nullable now and merchant sessions carry no reference, so this
    clause should never be the one that decides -- and it is here because "should never"
    is how that bug would come back.
    """
    if principal.actor_type is ActorType.MERCHANT:
        return False
    return principal.buyer_ref is not None or principal.actor_type is ActorType.BUYER


def is_merchant_principal(principal: AgentPrincipal) -> bool:
    """A principal from a merchant's authenticated session: merchant scope, no buyer.

    Two ways to be one, and both are kept. The actor type is the direct answer now that
    MERCHANT exists. The scope test is the older one and still carries a case the first
    does not: a session scoped to a merchant with no buyer at all.
    """
    if principal.actor_type is ActorType.MERCHANT:
        return True
    return principal.merchant_id is not None and not is_buyer_principal(principal)

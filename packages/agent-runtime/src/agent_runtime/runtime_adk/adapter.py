"""The one place a :class:`SpecialistSpec` becomes a google-adk ``LlmAgent``.

Everything about a specialist that matters is decided before this module runs: its
roster (``specialists/``), its principal (``harness/base.py::bind``), the gate on every
tool and the tool closures (``capabilities/tools.py::build_toolset``), the money
templates (``rendering/``). This adapter only binds them to ADK's shapes, and it is thin
on purpose so that another runtime is a second adapter beside it, not a fork (ADR 0004
section 1.4).

Four things are enforced here and nowhere else in ADK terms:

* **Factory-only tools.** Every ``FunctionTool`` on the agent wraps a closure the
  factory's :class:`BoundToolset` returned, under the name the registry row carries. A
  closure the factory offers that the specialist's roster does not list is a loud
  error, never filtered quietly.
* **The gate is one callable.** ``before_tool_callback`` is the toolset's own gate,
  registered as a single callable rather than a list, so no later callback can reset a
  denial to ``None``. A denial is a non-empty dict (``capabilities/broker.py``).
* **Text only.** No live-session API, no audio modality, no per-call confirmation
  prompt, no sub-agents, no transfer: a specialist cannot route to another specialist,
  because routing is the harness's and it is deterministic. A source test greps this
  package for the ADK names of those features.
* **The model is Gemini on Vertex, or a test double.** Configured by environment
  (``GOOGLE_GENAI_USE_VERTEXAI``, ``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``); no
  API key exists. Building on a string model without that environment raises rather
  than silently trying a key path. Tests pass a scripted ``BaseLlm``.

:class:`AdkSpecialistRunner` is the harness's ``SpecialistRunner`` seam: it receives the
bound specialist and the factory's toolset from the harness, builds the agent for this
turn, seeds the model session from the harness's session state, runs one text turn, and
writes the model session's state (ids and the provenance record) back. The harness owns
the timeout, the post-check and the transcript; this runner owns nothing but the call.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from commerce_domain import AgentPrincipal
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.run_config import RunConfig, StreamingMode  # type: ignore[attr-defined]
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService, InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

from ..backends.base import CommerceBackend
from ..capabilities.broker import ToolErrorGate, ToolGate
from ..capabilities.tools import (
    STATE_CART_ID,
    STATE_CHECKOUT_ID,
    STATE_CHECKOUT_VERSION,
    BoundToolset,
    build_toolset,
)
from ..harness.base import (
    Binding,
    BoundSpecialist,
    HarnessConfigurationError,
    SpecialistInput,
    SpecialistReply,
    bind,
)
from ..harness.routing import Specialist
from ..harness.session import CopilotSession
from ..language import Language
from ..specialists import SpecialistSpec, spec_for
from ..turn import TurnContext
from .prompts_loader import LoadedPrompt, load_prompt

__all__ = [
    "APP_NAME",
    "DEFAULT_MODEL",
    "DEFAULT_TEMPERATURE",
    "MODEL_ENV",
    "TEXT_ONLY_MODALITIES",
    "AdkSpecialistRunner",
    "BuiltSpecialist",
    "SpecialistToolingError",
    "VertexNotConfiguredError",
    "build_agent",
    "build_specialist",
    "model_name",
    "seed_state",
    "text_generation_config",
    "text_run_config",
    "unbuilt_tools",
    "user_content",
    "vertex_configured",
]

logger = logging.getLogger(__name__)

APP_NAME: Final[str] = "acr-agent-runtime"
DEFAULT_MODEL: Final[str] = "gemini-3.8-flash"
MODEL_ENV: Final[str] = "AGENT_RUNTIME_MODEL"
#: The only response modality any specialist is ever given.
TEXT_ONLY_MODALITIES: Final[tuple[str, ...]] = ("TEXT",)
#: Low, not zero: a money explanation should be steady, and 0 makes some models loop.
DEFAULT_TEMPERATURE: Final[float] = 0.2

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
#: ADK-scoped state prefixes that are not the harness's to keep.
_ADK_STATE_PREFIXES: Final[tuple[str, ...]] = ("app:", "user:", "temp:")
#: Session facts the factory's tools read from state, seeded from the harness session.
_SEEDED_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (STATE_CART_ID, "cart_id"),
    (STATE_CHECKOUT_ID, "checkout_id"),
    (STATE_CHECKOUT_VERSION, "checkout_version"),
)

BeforeToolCallback = Callable[[BaseTool, dict[str, Any], Context], dict[str, Any] | None]
OnToolErrorCallback = Callable[
    [BaseTool, dict[str, Any], Context, Exception], dict[str, Any] | None
]


class VertexNotConfiguredError(RuntimeError):
    """A real model was requested without the Vertex environment. Refuse, do not guess."""


class SpecialistToolingError(RuntimeError):
    """The factory's toolset and the specialist's roster disagree, or a name drifted."""


# ------------------------------------------------------------------------- environment


def vertex_configured(env: Mapping[str, str] | None = None) -> bool:
    """True when google-genai will route to Vertex with a project and location."""
    source = os.environ if env is None else env
    return (
        source.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in _TRUTHY
        and bool(source.get("GOOGLE_CLOUD_PROJECT", "").strip())
        and bool(source.get("GOOGLE_CLOUD_LOCATION", "").strip())
    )


def model_name(env: Mapping[str, str] | None = None) -> str:
    """The Gemini model id, overridable by ``AGENT_RUNTIME_MODEL``."""
    source = os.environ if env is None else env
    return source.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _resolve_model(model: str | BaseLlm | None, require_vertex: bool) -> str | BaseLlm:
    if isinstance(model, BaseLlm):
        return model
    if require_vertex and not vertex_configured():
        raise VertexNotConfiguredError(
            "GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must "
            "be set; this runtime never uses an API key"
        )
    return model or model_name()


def text_generation_config(
    *, temperature: float = DEFAULT_TEMPERATURE, shopping_model: str | None = None
) -> types.GenerateContentConfig:
    """Plain text, steady temperature. The only generation config a specialist gets."""
    return types.GenerateContentConfig(
        temperature=temperature,
        response_modalities=list(TEXT_ONLY_MODALITIES),
        thinking_config=(
            types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
            if shopping_model
            and shopping_model.startswith("gemini-3")
            and "flash" in shopping_model
            else None
        ),
    )


def text_run_config(*, max_llm_calls: int = 12) -> RunConfig:
    """A non-streaming text run with a hard cap on model calls; never the live API."""
    return RunConfig(streaming_mode=StreamingMode.NONE, max_llm_calls=max_llm_calls)


# ------------------------------------------------------------------------- building


@dataclass(frozen=True, slots=True)
class BuiltSpecialist:
    """An agent and everything the harness needs to hold beside it for one turn."""

    spec: SpecialistSpec
    binding: Binding
    toolset: BoundToolset
    agent: LlmAgent
    turn: TurnContext
    tools: tuple[FunctionTool, ...]
    prompt: LoadedPrompt
    unbuilt: tuple[str, ...]

    @property
    def principal(self) -> AgentPrincipal:
        return self.binding.principal


def unbuilt_tools(spec: SpecialistSpec, toolset: BoundToolset) -> tuple[str, ...]:
    """Roster tools this toolset does not carry. A coverage report, not a failure."""
    offered = set(toolset.names)
    return tuple(name for name in spec.tool_names if name not in offered)


def _adk_gate(gate: ToolGate) -> BeforeToolCallback:
    # ADK's ``Context`` satisfies ``ToolContextLike`` in behaviour (``state`` is a mapping
    # with ``get``/``[]``, ``function_call_id`` is a property) but its ``State`` class does
    # not subclass ``MutableMapping``, so the structural check needs this one cast.
    return cast(BeforeToolCallback, gate)


def _adk_error_gate(gate: ToolErrorGate) -> OnToolErrorCallback:
    return cast(OnToolErrorCallback, gate)


def _wrap(spec: SpecialistSpec, toolset: BoundToolset) -> tuple[FunctionTool, ...]:
    """One ``FunctionTool`` per factory closure, under the registry's name, roster-checked."""
    offered = frozenset(spec.tool_names)
    tools: list[FunctionTool] = []
    for bound in toolset:
        if bound.name not in offered:
            raise SpecialistToolingError(
                f"the factory offered {bound.name!r}, which {spec.name} does not list"
            )
        tool = FunctionTool(bound.func)
        if tool.name != bound.name:
            raise SpecialistToolingError(
                f"closure for {bound.name!r} presents as {tool.name!r}; the gate checks "
                "the registry name, so the model would see a tool the gate does not"
            )
        tools.append(tool)
    return tuple(tools)


def build_specialist(
    spec: SpecialistSpec,
    principal: AgentPrincipal | Binding,
    backend: CommerceBackend | None = None,
    *,
    toolset: BoundToolset | None = None,
    turn: TurnContext | None = None,
    session_id: str | None = None,
    language: Language = Language.EN,
    model: str | BaseLlm | None = None,
    prompts_dir: Path | None = None,
    require_vertex: bool = True,
) -> BuiltSpecialist:
    """Bind, build and wire one specialist for one turn.

    ``principal`` is the harness principal (bound here through ``harness.base.bind``) or
    a :class:`Binding` the harness already made. Tools come from ``toolset`` when the
    harness built it, else from the factory over ``backend``. A turn is the unit because
    the tool closures capture the turn's ledger and budget; a new agent per message costs
    microseconds and guarantees no evidence from an earlier turn leaks into this one.
    """
    resolved_model = _resolve_model(model, require_vertex)
    binding = (
        principal if isinstance(principal, Binding) else bind(principal, Specialist(spec.role))
    )
    if binding.specialist.value != spec.role:
        raise SpecialistToolingError(
            f"binding is for {binding.specialist.value!r}, spec is {spec.role!r}"
        )
    if turn is None:
        turn = TurnContext(
            language=language,
            principal=binding.principal,
            max_tool_calls=spec.max_tool_calls,
            agent_name=spec.role,
        )
    if toolset is None:
        if backend is None:
            raise SpecialistToolingError("a backend or a prebuilt toolset is required")
        toolset = build_toolset(binding, backend, turn, session_id=session_id, agent_name=spec.role)
    elif toolset.principal != binding.principal:
        raise SpecialistToolingError("toolset was built for a different principal")

    tools = _wrap(spec, toolset)
    prompt = load_prompt(spec, prompts_dir=prompts_dir)
    agent = LlmAgent(
        name=spec.name,
        model=resolved_model,
        description=spec.description,
        # Static, byte-stable, no templating: ADK substitutes ``{state}`` placeholders in
        # ``instruction`` but sends ``static_instruction`` as-is, and a prompt Gemini wrote
        # may legitimately contain braces.
        static_instruction=prompt.instruction,
        tools=list(tools),
        generate_content_config=text_generation_config(
            shopping_model=resolved_model
            if spec.role == "shopping" and isinstance(resolved_model, str)
            else None,
        ),
        before_tool_callback=_adk_gate(toolset.gate),
        on_tool_error_callback=_adk_error_gate(toolset.error_gate),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    return BuiltSpecialist(
        spec=spec,
        binding=binding,
        toolset=toolset,
        agent=agent,
        turn=turn,
        tools=tools,
        prompt=prompt,
        unbuilt=unbuilt_tools(spec, toolset),
    )


def build_agent(
    spec: SpecialistSpec,
    principal: AgentPrincipal | Binding,
    backend: CommerceBackend | None = None,
    *,
    toolset: BoundToolset | None = None,
    turn: TurnContext | None = None,
    session_id: str | None = None,
    language: Language = Language.EN,
    model: str | BaseLlm | None = None,
    prompts_dir: Path | None = None,
    require_vertex: bool = True,
) -> LlmAgent:
    """``build_specialist`` for callers that want only the agent."""
    return build_specialist(
        spec,
        principal,
        backend,
        toolset=toolset,
        turn=turn,
        session_id=session_id,
        language=language,
        model=model,
        prompts_dir=prompts_dir,
        require_vertex=require_vertex,
    ).agent


# ------------------------------------------------------------------------- running


def seed_state(session: CopilotSession) -> dict[str, Any]:
    """The model session's state for this turn: the harness's tool state, ids filled in.

    ``session.state`` is authoritative (the prefetch wrote provenance there this turn);
    an id the harness learned from the request context or an earlier record fills a gap
    so the factory's tools find their cart and checkout without a model argument.
    """
    state = dict(session.state)
    for key, attribute in _SEEDED_FACTS:
        value = getattr(session, attribute)
        if state.get(key) in (None, "") and value is not None:
            state[key] = value
    return state


def _sync_back(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if not key.startswith(_ADK_STATE_PREFIXES):
            target[key] = value


def user_content(message: SpecialistInput) -> types.Content:
    """The user turn: prefetch preamble first, the message, then the platform's facts.

    The facts are ids and a clock, never third-party text, so they need no fence; the
    preamble is already fenced by the harness. None of it touches the static instruction.
    """
    blocks: list[str] = []
    if message.preamble:
        blocks.append(message.preamble)
    blocks.append(message.text)
    if message.facts:
        facts = "; ".join(f"{key}={value}" for key, value in message.facts.items())
        blocks.append(f"Session facts from the platform, not from the buyer: {facts}")
    return types.Content(role="user", parts=[types.Part(text="\n\n".join(blocks))])


#: Where the ADK keeps conversations, as an environment name.
#:
#: Unset means the in-memory store, which is right for a test and for a laptop and wrong
#: for anything that restarts. See :func:`build_session_service`.
SESSION_DB_URL_ENV: Final[str] = "ADK_SESSION_DATABASE_URL"


def build_session_service(db_url: str | None) -> BaseSessionService:
    """The store the ADK keeps conversations in.

    WHY THIS IS A CHOICE AND NOT A CONSTANT
    ---------------------------------------
    ``InMemorySessionService`` is a plain nested dict with no TTL, no cap and no eviction,
    and nothing here calls ``delete_session``. So it does two things: it loses every
    conversation when the process ends, and it grows for the life of the one that is
    running -- an entry per bearer session per specialist, forever.

    Losing them is the visible half. What survives a restart is only what the client
    re-sends or the database already holds: the cart id arrives on each request and the
    checkout is in Postgres, so "what is in my cart" still answers and "add the milk we
    just talked about" does not.

    WHY THE URL POINTS SOMEWHERE ELSE
    ---------------------------------
    The ADK owns its schema and creates it: ``sessions``, ``events``, ``app_states``,
    ``user_states``, ``adk_internal_metadata``. None carries a ``tenant_id`` and none
    carries RLS, while every tenant table in the commerce database carries both, and
    alembic owns that schema. So the URL names a database of its own -- ``commerce_dev_adk``
    in development -- and the separation is the point rather than tidiness: un-tenanted
    tables inside an RLS-governed schema are a boundary this platform spends real effort
    keeping, and a migration tool that meets five tables it did not create is a second
    hazard on top of the first.

    Unset keeps the in-memory store, so a unit test, an offline run and a fresh checkout
    all behave exactly as they did.
    """
    if db_url is None or not db_url.strip():
        return InMemorySessionService()
    return DatabaseSessionService(db_url.strip())


class AdkSpecialistRunner:
    """The harness's ``SpecialistRunner`` on google-adk. One per process, sessions inside.

    The session service is held here rather than made per call because the provenance
    record and the cart and checkout ids live in ADK session state between turns; the
    harness's ``CopilotSession.state`` is written back after every turn so the two never
    disagree and a prefetch on the next turn reads what this turn's tools recorded.

    Where those sessions are kept is :func:`build_session_service`'s decision, read from
    the environment once here rather than per turn: a store built per call would be a new
    connection pool per turn and, for the in-memory one, a new empty dict.
    """

    def __init__(
        self,
        *,
        model: str | BaseLlm | None = None,
        prompts_dir: Path | None = None,
        require_vertex: bool = True,
        app_name: str = APP_NAME,
        session_db_url: str | None = None,
    ) -> None:
        self._model = model
        self._prompts_dir = prompts_dir
        self._require_vertex = require_vertex
        self._app_name = app_name
        self._sessions = build_session_service(
            session_db_url if session_db_url is not None else os.environ.get(SESSION_DB_URL_ENV)
        )

    async def plan_shopping(
        self, message: str, previous: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One bounded model pass; backend validates every plan and executes only reads."""
        import json

        from google import genai

        from .shopping_plan import INSTRUCTION, ShoppingPlan

        model = _resolve_model(self._model, self._require_vertex)
        if not isinstance(model, str):
            raise ValueError("Structured planning requires a configured model")
        config = text_generation_config(shopping_model=model)
        config.system_instruction = INSTRUCTION
        config.response_mime_type = "application/json"
        config.response_schema = ShoppingPlan
        client = genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=json.dumps(
                    {"request": message, "previous_plan": previous}, ensure_ascii=False
                ),
                config=config,
            )
            return ShoppingPlan.model_validate_json(response.text or "").model_dump()
        finally:
            await client.aio.aclose()

    @property
    def sessions(self) -> BaseSessionService:
        """The store this runner keeps conversations in. Read-only, and read by tests."""
        return self._sessions

    @property
    def app_name(self) -> str:
        return self._app_name

    async def __call__(
        self,
        bound: BoundSpecialist,
        message: SpecialistInput,
        turn: TurnContext,
        session: CopilotSession,
    ) -> SpecialistReply:
        toolset = bound.tools
        if not isinstance(toolset, BoundToolset):
            raise HarnessConfigurationError(
                "AdkSpecialistRunner needs the factory's BoundToolset; another tools= "
                "builder was configured"
            )
        spec = spec_for(bound.specialist.value)
        built = build_specialist(
            spec,
            bound.binding,
            toolset=toolset,
            turn=turn,
            language=message.language,
            model=self._model,
            prompts_dir=self._prompts_dir,
            require_vertex=self._require_vertex,
        )
        user_id = session.buyer_ref or session.principal_id
        seed = seed_state(session)
        existing = await self._sessions.get_session(
            app_name=self._app_name, user_id=user_id, session_id=session.session_id
        )
        if existing is None:
            await self._sessions.create_session(
                app_name=self._app_name,
                user_id=user_id,
                session_id=session.session_id,
                state=seed,
            )
        runner = Runner(app_name=self._app_name, agent=built.agent, session_service=self._sessions)
        texts: list[str] = []
        model_calls = 0
        started = time.monotonic()
        first_event_ms: int | None = None
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session.session_id,
                new_message=user_content(message),
                state_delta=seed,
                run_config=text_run_config(max_llm_calls=spec.max_rounds),
            ):
                if event.author != built.agent.name or event.content is None:
                    continue
                model_calls += 1
                if first_event_ms is None:
                    first_event_ms = round((time.monotonic() - started) * 1000)
                texts.extend(part.text for part in event.content.parts or [] if part.text)
        finally:
            close = getattr(runner, "close", None)
            if close is not None:
                await close()
            refreshed = await self._sessions.get_session(
                app_name=self._app_name, user_id=user_id, session_id=session.session_id
            )
            if refreshed is not None:
                _sync_back(session.state, refreshed.state)
        # One line per model turn, with the session tag and never the session id.
        logger.info(
            "specialist turn session=%s specialist=%s prompt=%s tools=%d events=%d "
            "first_event_ms=%s elapsed_ms=%d",
            session.tag,
            spec.role,
            built.prompt.source,
            len(built.tools),
            model_calls,
            first_event_ms,
            round((time.monotonic() - started) * 1000),
        )
        return SpecialistReply(
            text="\n".join(texts).strip(),
            structured={
                "runtime": "google-adk",
                "model": built.agent.model if isinstance(built.agent.model, str) else "test",
                "prompt_source": built.prompt.source,
                "tools_offered": list(built.toolset.names),
                "tools_unbuilt": list(built.unbuilt),
            },
        )

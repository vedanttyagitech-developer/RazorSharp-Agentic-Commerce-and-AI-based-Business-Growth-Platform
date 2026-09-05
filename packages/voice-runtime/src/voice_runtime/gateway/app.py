"""The Voice Gateway: an ASGI app with one HTTP route that mints and one socket that speaks.

    POST /v1/voice/tickets    authenticated; mints a single-use ticket
    WS   /v1/voice/stream     redeems it once, then runs the split pipeline
    GET  /v1/voice/metrics    the counters of 19.13
    GET  /healthz

WHY A TICKET AND NOT THE BEARER
-------------------------------
A browser cannot set an ``Authorization`` header on a WebSocket handshake. The alternatives
are putting the bearer in the query string -- where it lands in access logs, proxy logs and
``Referer`` headers -- or holding it server-side behind an opaque single-use handle. This
does the second (:mod:`voice_runtime.wire.tickets`).

WHAT THE GATEWAY IS NOT ALLOWED TO BE
--------------------------------------
It mints no principal, holds no capability and performs no money operation. It learns who
a session is by asking the trusted server (``GET /v1/agent/capabilities``) and it acts by
asking the trusted server (``POST /v1/agent/turn``), both with the buyer's own bearer. A
spoken "yes" therefore cannot approve anything, because there is no code here that could
carry it to an approval: approvals bind to a checkout version and content hash on the
trusted surface and are single-use, so replayed audio cannot replay one either (19.11).

Origin is checked on the handshake. Browsers send it on every WebSocket upgrade and page
script cannot forge it, so an exact allow-list is the cross-site defence; a missing or
``null`` Origin is refused, because this is a browser surface.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
from fastapi import APIRouter, FastAPI, Header, Query, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from ..clock import Clock, MonotonicClock
from ..constants import TRANSCRIBE_MODEL
from ..pipeline import VoicePipeline
from ..stt.events import LiveSttFactory
from ..stt.gemini import GeminiTranscribeLiveFactory
from ..tts.gemini_tts import CONVERSATIONAL_MODEL_FALLBACK, FallbackSynthesizer, GeminiSynthesizer
from ..tts.synth import SpeechSynthesizer
from ..wire.origin import OriginPolicy
from ..wire.tickets import TicketError, TicketIssuer
from .agent_client import AgentUnavailableError, HttpTurnHandler, resolve_identity
from .scenario import OneShotFailingSynthesizer
from .settings import GatewaySettings
from .transport import WebSocketTransport

__all__ = ["VoiceGateway", "create_app"]

log = logging.getLogger(__name__)

#: WebSocket close codes. 1008 is "policy violation", which is what a refused origin, a
#: spent ticket and an unusable session all are.
_CLOSE_POLICY: Final[int] = status.WS_1008_POLICY_VIOLATION
_HTTP_TIMEOUT_S: Final[float] = 30.0


class TicketOut(BaseModel):
    """What the storefront receives. The bearer is not in it and never will be."""

    model_config = ConfigDict(extra="forbid")

    ticket: str
    expires_in_s: float
    session_id: str
    #: So the client can show the right surface before the socket is even open.
    speech_available: bool


class VoiceGateway:
    """One gateway process: its settings, its ticket store and its outbound HTTP client."""

    def __init__(
        self,
        settings: GatewaySettings | None = None,
        *,
        clock: Clock | None = None,
        http_client: httpx.AsyncClient | None = None,
        stt_factory: LiveSttFactory | None = None,
        synthesizer: SpeechSynthesizer | None = None,
    ) -> None:
        self.settings = settings if settings is not None else GatewaySettings.from_env()
        self.clock = clock if clock is not None else MonotonicClock()
        self.tickets = TicketIssuer(clock=self.clock)
        self.origins = OriginPolicy(self.settings.allowed_origins)
        self._http_client = http_client
        self._stt_factory = stt_factory
        self._synthesizer = synthesizer
        self._owns_client = http_client is None
        # Metrics (19.13). Counters that are never surfaced are not observability, so
        # ``GET /v1/voice/metrics`` reads exactly these.
        self.sockets_opened = 0
        self.sockets_refused = 0

    # ---- lazily built collaborators ---------------------------------------------------

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.settings.api_base_url, timeout=_HTTP_TIMEOUT_S
            )
        return self._http_client

    def stt_factory(self) -> LiveSttFactory | None:
        """``None`` when speech is not configured: the socket then opens in text mode and
        says so, rather than listening to a microphone it cannot transcribe (19.12)."""
        if self._stt_factory is not None:
            return self._stt_factory
        if not self.settings.speech_configured:
            return None
        assert self.settings.project is not None
        return GeminiTranscribeLiveFactory(project=self.settings.project, model=TRANSCRIBE_MODEL)

    def synthesizer(self) -> SpeechSynthesizer:
        """Chirp 3 HD for transactional speech where it is reachable, Gemini TTS behind it.

        Cloud TTS needs its own API enabled on the project; when it is not, the chain
        falls through to the conversational voice and the substitution is surfaced.
        """
        if self._synthesizer is not None:
            return self._synthesizer
        if not self.settings.speech_configured:
            raise AgentUnavailableError("speech is not configured")
        assert self.settings.project is not None
        chain: list[SpeechSynthesizer] = []
        names: list[str] = []
        if self.settings.chirp_available:
            from ..tts.chirp import ChirpSynthesizer

            chain.append(ChirpSynthesizer())
            names.append("chirp3-hd")
        chain.append(GeminiSynthesizer(project=self.settings.project))
        names.append("gemini-tts")
        chain.append(
            GeminiSynthesizer(project=self.settings.project, model=CONVERSATIONAL_MODEL_FALLBACK)
        )
        names.append("gemini-tts-fallback")
        return FallbackSynthesizer(*chain, names=tuple(names))

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ---- routes -----------------------------------------------------------------------

    async def mint_ticket(self, authorization: str | None) -> TicketOut:
        """Exchange a buyer bearer for a single-use socket ticket."""
        bearer = _bearer_from(authorization)
        if bearer is None:
            raise _problem(status.HTTP_401_UNAUTHORIZED, "a bearer token is required")
        try:
            identity = await resolve_identity(self.http, bearer=bearer)
        except AgentUnavailableError as exc:
            log.info("voice ticket refused: %s", exc)
            raise _problem(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        issued = self.tickets.issue(
            session_id=identity.session_id,
            principal_id=identity.principal_id,
            bearer=bearer,
            tenant_id=identity.tenant_id,
        )
        return TicketOut(
            ticket=issued.token,
            expires_in_s=issued.expires_in_s,
            session_id=identity.session_id,
            speech_available=self.settings.speech_configured,
        )

    async def serve_socket(self, socket: WebSocket, ticket: str | None, origin: str | None) -> None:
        """Redeem the ticket, then run one pipeline until the socket closes."""
        verdict = self.origins.check(origin)
        if not verdict.allowed:
            self.sockets_refused += 1
            log.info("voice socket refused: %s (origin %r)", verdict.reason, origin)
            await socket.close(code=_CLOSE_POLICY, reason=verdict.reason)
            return
        try:
            claims = self.tickets.redeem(ticket)
        except TicketError as exc:
            self.sockets_refused += 1
            log.info("voice socket refused: ticket %s", exc.reason)
            await socket.close(code=_CLOSE_POLICY, reason=f"ticket_{exc.reason}")
            return
        try:
            # Re-resolve identity on the socket rather than trusting the minted copy. The
            # ticket may be up to a minute old and a session can be revoked inside a
            # minute; the answer that matters is the one the server gives now.
            identity = await resolve_identity(self.http, bearer=claims.bearer)
        except AgentUnavailableError as exc:
            self.sockets_refused += 1
            log.info("voice socket refused: %s", exc)
            await socket.close(code=_CLOSE_POLICY, reason="session_unusable")
            return

        await socket.accept()
        self.sockets_opened += 1
        transport = WebSocketTransport(socket)
        try:
            synthesizer = self.synthesizer()
        except AgentUnavailableError:
            synthesizer = _MuteSynthesizer()
        # Demo apparatus, and inert unless the trusted server dispenses a fault on a turn
        # this socket ran. Wrapping unconditionally rather than behind a gateway setting
        # keeps the gateway's ignorance intact: it does not know which profile the API is
        # in, it is simply never told to fire outside the demonstration one.
        failing = OneShotFailingSynthesizer(synthesizer)
        pipeline = VoicePipeline(
            transport=transport,
            stt_factory=self.stt_factory(),
            synthesizer=failing,
            turn_handler=HttpTurnHandler(
                self.http, bearer=claims.bearer, on_scenario_fault=failing.arm_for
            ),
            identity=identity,
            clock=self.clock,
        )
        try:
            await pipeline.run()
        finally:
            await transport.close()

    def metrics(self) -> dict[str, int]:
        """Counters surfaced for 19.13. One correlation id is the session id."""
        return {
            "sockets_opened": self.sockets_opened,
            "sockets_refused": self.sockets_refused,
            "tickets_issued": self.tickets.issued,
            "tickets_redeemed": self.tickets.redeemed,
            "tickets_rejected": self.tickets.rejected,
            "tickets_outstanding": self.tickets.outstanding,
        }


class _MuteSynthesizer:
    """Speaks nothing, loudly.

    Used when speech is unconfigured, so the pipeline still runs its text path and the
    buyer is told what is unavailable -- instead of the socket refusing to open, which
    would take typed input away too.
    """

    async def synthesize(self, text: str, voice: Any) -> bytes:
        raise RuntimeError(
            f"speech synthesis is not configured; {len(text)} characters left unspoken "
            f"for {getattr(voice, 'name', 'the default voice')}"
        )


def _bearer_from(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().casefold() != "bearer" or not token.strip():
        return None
    return token.strip()


def _problem(code: int, detail: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=code, detail=detail)


def create_app(gateway: VoiceGateway | None = None) -> FastAPI:
    """The ASGI app. ``gateway`` is injectable so a test drives it with fakes."""
    resolved = gateway if gateway is not None else VoiceGateway()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await resolved.aclose()

    app = FastAPI(title="Voice Gateway", version="0.1.0", lifespan=lifespan)
    app.state.gateway = resolved
    # The storefront mints its ticket cross-origin in development -- the gateway is a
    # separate process on another port, and the buyer-web CSP names this origin rather
    # than proxying the socket through a Next route. A POST carrying an Authorization
    # header is preflighted, so without this the browser never sends the request at all
    # and the failure looks like the gateway being down.
    #
    # The allowlist is the SAME one the websocket handshake checks, so a browser cannot
    # reach either surface from an origin the other would refuse. Credentials are off:
    # the bearer travels in a header the caller sets explicitly, never as a cookie.
    if resolved.settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.settings.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "speech_available": resolved.settings.speech_configured}

    @router.get("/v1/voice/metrics")
    async def metrics() -> dict[str, int]:
        return resolved.metrics()

    @router.post("/v1/voice/tickets", response_model=TicketOut)
    async def mint(authorization: str | None = Header(default=None)) -> TicketOut:
        return await resolved.mint_ticket(authorization)

    @router.websocket("/v1/voice/stream")
    async def stream(
        websocket: WebSocket,
        ticket: str | None = Query(default=None),
        origin: str | None = Header(default=None),
    ) -> None:
        await resolved.serve_socket(websocket, ticket, origin)

    app.include_router(router)
    return app

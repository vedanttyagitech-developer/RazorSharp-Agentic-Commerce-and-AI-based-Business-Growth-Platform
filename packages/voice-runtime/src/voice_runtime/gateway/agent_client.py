"""The seam to RazorAI, over ordinary authenticated HTTP.

The Voice Gateway does not run the harness, mint a principal or narrow a capability set.
It sends a settled transcript to ``POST /v1/agent/turn`` carrying the buyer's own bearer,
and the trusted server does what it already does for typed input: resolve the session,
intersect the session's capabilities with the agent surface and then with the specialist's
allowlist, gate every tool call, ground every sentence, and return an attributable record.

That is the whole point of specification 19.1's split. The speech layer owns two audio
streams and nothing else. It has no authority to leak, no principal to forge and no gate
to forget, because it holds none of them. A voice turn and a typed turn are the same
request to the same endpoint with the same bearer, which is the strongest possible form of
"a transcript is intent evidence, never authority evidence" (19.11): there is no voice
code path for a money action to take.

WHAT IS DELIBERATELY NOT SENT
-----------------------------
Only **final** transcripts reach this client. Interim text never leaves the gateway, so
the "an interim transcript is never confirmed intent" rule of 19.5 is enforced by there
being no call to make rather than by a flag the server has to honour.

GROUNDED AMOUNTS
----------------
The response carries ``structured`` -- the same JSON the REST read endpoints return, whose
every amount is integer minor units. :func:`grounded_amounts` walks it and collects them,
and the outbound speech guard will speak an amount only if it is in that set. The walk
reads integers and never divides, so no float is created anywhere on the path between the
server's number and the spoken one.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx

from ..identity import VoiceIdentity
from ..stt.transcript import TranscriptTurn
from ..tts.templates import Locale
from ..turn import TurnReply

__all__ = [
    "AGENT_TURN_PATH",
    "CAPABILITIES_PATH",
    "AgentUnavailableError",
    "HttpTurnHandler",
    "decision_card_in",
    "grounded_amounts",
    "identity_from_capabilities",
    "locale_for_language",
    "resolve_identity",
    "session_id_from_principal",
]

log = logging.getLogger(__name__)

CAPABILITIES_PATH: Final[str] = "/v1/agent/capabilities"
AGENT_TURN_PATH: Final[str] = "/v1/agent/turn"

#: Longest message the turn endpoint accepts. A transcript longer than this is truncated
#: rather than rejected: the buyer said something, and losing the turn entirely is worse
#: than acting on its first two thousand characters.
MAX_MESSAGE_CHARS: Final[int] = 2000

#: How the server renders an agent principal: ``session:<id>/razorai/<specialist>``.
_PRINCIPAL_PREFIX: Final[str] = "session:"

#: Keys whose integer value is already in minor units, as the API's wire shapes use them.
_MINOR_SUFFIX: Final[str] = "_minor"
#: ``{"minor": 7300, "currency": "INR", "display": "73.00"}`` -- the API's money object.
_MINOR_KEY: Final[str] = "minor"

_LOCALE_FOR_LANGUAGE: Final[dict[str, Locale]] = {
    "en": Locale.EN_IN,
    "hi": Locale.HI_IN,
    # Hinglish is Hindi spoken in Latin script. It is SPOKEN as Hindi -- an Indian
    # English voice reading "chahiye" mispronounces it -- so it maps to the Hindi voice
    # while the text on screen stays in the script the buyer wrote.
    "hi-latn": Locale.HI_IN,
}


class AgentUnavailableError(RuntimeError):
    """The agent layer could not be reached or answered unusably.

    Raised, not swallowed: the pipeline turns it into a visible degradation, and the
    buyer is told that search and checkout still work from the screen and that no
    approval, payment or refund state changed (19.12).
    """


def locale_for_language(language: str) -> Locale:
    """Map the server's language tag to a speech locale. Unknown tags speak English."""
    return _LOCALE_FOR_LANGUAGE.get(language.strip().casefold(), Locale.EN_IN)


def grounded_amounts(payload: object) -> frozenset[int]:
    """Every integer minor-unit amount anywhere in a server payload.

    Walks dicts and lists to any depth. Collects ``*_minor`` integers and the ``minor``
    field of the API's money object. Booleans are excluded: ``True`` is an ``int`` in
    Python and a stray flag must not become a spendable amount.
    """
    found: set[int] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(key, str)
                    and (key.endswith(_MINOR_SUFFIX) or key == _MINOR_KEY)
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                ):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return frozenset(found)


def session_id_from_principal(principal_id: str) -> str:
    """``session:<id>/razorai/<specialist>`` -> ``<id>``.

    The server already names the session inside every principal it renders, so the gateway
    reads it there rather than accepting one from the caller. An id the client supplied
    would be an identity claim, and the gateway believes no identity claim from a client.
    """
    if not principal_id.startswith(_PRINCIPAL_PREFIX):
        raise AgentUnavailableError(f"unrecognised principal shape: {principal_id!r}")
    return principal_id.removeprefix(_PRINCIPAL_PREFIX).split("/", 1)[0]


def decision_card_in(structured: object) -> dict[str, Any] | None:
    """A ``decision`` card inside a turn's structured payload, if there is one.

    The platform's ``present_decision`` tool produces exactly this shape
    (``agent_runtime.rendering.cards.decision_card``). When a card is present the
    transactional sentence is rendered from templates and spoken with
    ``deterministic=True``, which is what specification 19.10 asks for.

    Today ``POST /v1/agent/turn`` never returns one -- its structured payload is a product,
    a search page, a basket, a checkout or an order -- so this reads ``None`` on every live
    turn and the guard carries the whole weight. This half is built and tested against the
    real card shape so that closing the gap is a change on one side only.
    """
    if not isinstance(structured, dict):
        return None
    for candidate in (structured, structured.get("card"), structured.get("decision")):
        if isinstance(candidate, dict) and candidate.get("kind") == "decision":
            return candidate
    return None


def identity_from_capabilities(payload: dict[str, Any]) -> VoiceIdentity:
    """Build the identity from the server's own capabilities answer, and nothing else.

    Refuses anything that is not a buyer copilot: the merchant console has no voice
    surface in P0, and a merchant session speaking into the buyer harness would attribute
    a basket to a buyer who never asked for one.
    """
    copilot = str(payload.get("copilot", ""))
    actor_type = str(payload.get("actor_type", ""))
    if copilot != "buyer" or actor_type != "BUYER":
        raise AgentUnavailableError(
            f"voice serves the buyer copilot only; this session is {copilot or '?'}/{actor_type}"
        )
    specialists = payload.get("specialists")
    principal_id = ""
    if isinstance(specialists, list) and specialists:
        first = specialists[0]
        if isinstance(first, dict):
            principal_id = str(first.get("principal_id", ""))
    if not principal_id:
        raise AgentUnavailableError("capabilities answer named no principal")
    capabilities = payload.get("agent_capabilities")
    return VoiceIdentity(
        tenant_id=None,
        session_id=session_id_from_principal(principal_id),
        principal_id=principal_id,
        copilot=copilot,
        agent_capabilities=frozenset(str(entry) for entry in capabilities if isinstance(entry, str))
        if isinstance(capabilities, list)
        else frozenset(),
    )


class HttpTurnHandler:
    """``TurnHandler`` over ``POST /v1/agent/turn``, carrying the buyer's own bearer."""

    def __init__(self, client: httpx.AsyncClient, *, bearer: str) -> None:
        self._client = client
        self._bearer = bearer

    async def handle_turn(self, transcript: TranscriptTurn, identity: VoiceIdentity) -> TurnReply:
        """One settled turn. Only finals arrive here; interim text never leaves the gateway."""
        if not transcript.is_final:  # pragma: no cover - the pipeline never schedules one
            raise AgentUnavailableError("only a settled transcript may enter intent processing")
        message = transcript.text.strip()[:MAX_MESSAGE_CHARS]
        if not message:
            return TurnReply()
        # One correlation id reconstructs the whole conversation, across every recognizer
        # rotation, from one place (19.13). The transcript itself is never logged.
        log.info(
            "voice turn %d for %s (%d chars)",
            transcript.turn_id,
            identity.principal_id,
            len(message),
        )
        try:
            response = await self._client.post(
                AGENT_TURN_PATH,
                json={"message": message},
                headers={"Authorization": f"Bearer {self._bearer}"},
            )
        except httpx.HTTPError as exc:
            raise AgentUnavailableError(f"agent turn failed: {exc}") from exc
        if response.status_code >= 400:
            # A denial is not an error: it arrives inside a 200 as a structured refusal.
            # A 4xx or 5xx here really is the agent layer being unavailable.
            raise AgentUnavailableError(f"agent turn returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentUnavailableError("agent turn returned a body that is not JSON") from exc
        if not isinstance(payload, dict):
            raise AgentUnavailableError("agent turn returned a body that is not an object")
        return self._to_reply(payload)

    @staticmethod
    def _to_reply(payload: dict[str, Any]) -> TurnReply:
        reply = payload.get("reply")
        structured = payload.get("structured")
        return TurnReply(
            text=reply if isinstance(reply, str) else "",
            locale=locale_for_language(str(payload.get("language", "en"))),
            grounded_amounts_minor=grounded_amounts(structured),
            decision_card=decision_card_in(structured),
        )


async def resolve_identity(client: httpx.AsyncClient, *, bearer: str) -> VoiceIdentity:
    """Ask the trusted server who this bearer is. The client's own claims are never read."""
    try:
        response = await client.get(
            CAPABILITIES_PATH, headers={"Authorization": f"Bearer {bearer}"}
        )
    except httpx.HTTPError as exc:
        raise AgentUnavailableError(f"capabilities lookup failed: {exc}") from exc
    if response.status_code >= 400:
        raise AgentUnavailableError(f"capabilities lookup returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AgentUnavailableError("capabilities lookup returned a body that is not JSON") from exc
    if not isinstance(payload, dict):
        raise AgentUnavailableError("capabilities lookup returned a body that is not an object")
    return identity_from_capabilities(payload)

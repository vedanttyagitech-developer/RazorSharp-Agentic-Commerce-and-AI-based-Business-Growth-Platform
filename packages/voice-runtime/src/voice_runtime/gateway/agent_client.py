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
from collections.abc import Callable
from typing import Any, Final

import httpx

from ..consent import ApprovalCardFacts, CardUnavailableError
from ..identity import VoiceIdentity
from ..stt.transcript import TranscriptTurn
from ..tts.templates import Locale
from ..turn import TurnReply

__all__ = [
    "AGENT_TURN_PATH",
    "CAPABILITIES_PATH",
    "CHECKOUT_PATH",
    "SCENARIO_FAULT_HEADER",
    "AgentUnavailableError",
    "HttpCardReader",
    "HttpTurnHandler",
    "card_facts_from",
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
#: The read model the storefront renders the approval card from. The consent path reads
#: the card from here, never from the client that asked for it to be read.
CHECKOUT_PATH: Final[str] = "/v1/checkouts/{checkout_id}"

#: Set by the trusted server, and only in the demonstration profile, to name the scenario
#: faults it consumed while running this turn. It is a response header rather than a body
#: field because the body is the buyer panel's contract and specification 31.3 forbids
#: mixing injected apparatus with organic data. Absent on every organic turn.
SCENARIO_FAULT_HEADER: Final[str] = "X-Scenario-Fault-Fired"

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


def offer_in(structured: object, text: str = "") -> dict[str, Any] | None:
    """The one product a turn put forward, or None.

    A ``basket.update`` proposal names a SKU and a quantity; a ``product`` card names one
    product; a ``products`` page names several, of which the first is what the sentence
    led with. Anything else is not an offer, and a spoken "yes" after it refers to nothing.
    """
    if not isinstance(structured, dict):
        return None
    proposal = structured.get("proposal")
    if isinstance(proposal, dict) and proposal.get("action") == "basket.update":
        shown = proposal.get("display")
        display: dict[str, Any] = shown if isinstance(shown, dict) else {}
        return {
            "sku": str(proposal.get("sku", "")),
            "name": str(display.get("name", "")),
            "quantity": int(proposal.get("delta") or 1),
            "unit_price": display.get("unit_price"),
        }
    # The bridged runner spreads the card flat -- ``{"kind": "product", "sku": ...}`` --
    # while the deterministic one nests it under ``product``. Both are one product.
    if structured.get("kind") == "product" and structured.get("sku"):
        return _offer_of(structured)
    product = structured.get("product")
    if isinstance(product, dict) and product.get("sku"):
        return _offer_of(product)
    hits = structured.get("hits")
    if isinstance(hits, list) and hits:
        rows = [row for row in hits if isinstance(row, dict) and row.get("sku")]
        # The product the sentence leads with, when the reply names any of them; the
        # first hit otherwise. A model that recommends the second result and then hears
        # "yes" must be taken at its word, not at the search engine's.
        lowered = text.lower()
        named = sorted(
            (
                (lowered.index(str(row.get("display_name", "")).lower()), index)
                for index, row in enumerate(rows)
                if row.get("display_name") and str(row["display_name"]).lower() in lowered
            ),
        )
        chosen = rows[named[0][1]] if named else (rows[0] if rows else None)
        return None if chosen is None else _offer_of(chosen)
    return None


def _offer_of(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("is_available") is False or row.get("stock_units") == 0:
        return None
    return {
        "sku": str(row["sku"]),
        "name": str(row.get("display_name") or row.get("name") or row["sku"]),
        "quantity": 1,
        "unit_price": row.get("unit_price"),
    }


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

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        bearer: str,
        on_scenario_fault: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._bearer = bearer
        self._on_scenario_fault = on_scenario_fault

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
        self._note_scenario_faults(response)
        return self._to_reply(payload)

    def _note_scenario_faults(self, response: httpx.Response) -> None:
        """Pass on what the server said it injected, if anything and if anyone is listening.

        Deliberately after the body has been accepted: a turn that did not produce a reply
        has nothing to speak, so arming a speech failure for it would strand the fault on
        a turn the buyer never hears. The callback is handed each name separately and
        decides for itself which it recognises, so a kind meant for a different consumer
        -- or a malformed header -- reaches nothing.
        """
        if self._on_scenario_fault is None:
            return
        header = response.headers.get(SCENARIO_FAULT_HEADER)
        if not header:
            return
        for name in header.split(","):
            fault = name.strip()
            if fault:
                log.info("scenario fault dispensed by the server: %s", fault)
                self._on_scenario_fault(fault)

    @staticmethod
    def _to_reply(payload: dict[str, Any]) -> TurnReply:
        reply = payload.get("reply")
        structured = payload.get("structured")
        return TurnReply(
            text=reply if isinstance(reply, str) else "",
            locale=locale_for_language(str(payload.get("language", "en"))),
            grounded_amounts_minor=grounded_amounts(structured),
            decision_card=decision_card_in(structured),
            offer=offer_in(structured, reply if isinstance(reply, str) else ""),
        )


class HttpCardReader:
    """``CardReader`` over ``GET /v1/checkouts/{id}``, carrying the buyer's own bearer.

    The one read the consent path adds to the gateway's outbound calls, and the only new
    one: the gateway still sends nothing to ``/approve``. It asks the trusted server for
    the card exactly as the storefront does, through a route that answers 404 for a
    checkout this buyer does not own, and it accepts the card only when the checkout is
    awaiting approval at the version the screen named. Anything else is
    :class:`~voice_runtime.consent.CardUnavailableError`, which the pipeline turns into a
    visible degradation with nothing read aloud.
    """

    def __init__(self, client: httpx.AsyncClient, *, bearer: str) -> None:
        self._client = client
        self._bearer = bearer

    async def read_card(self, checkout_id: str, version: int) -> ApprovalCardFacts:
        try:
            response = await self._client.get(
                CHECKOUT_PATH.format(checkout_id=checkout_id),
                headers={"Authorization": f"Bearer {self._bearer}"},
            )
        except httpx.HTTPError as exc:
            raise CardUnavailableError(f"checkout read failed: {exc}") from exc
        if response.status_code >= 400:
            raise CardUnavailableError(f"checkout read returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CardUnavailableError("checkout read returned a body that is not JSON") from exc
        if not isinstance(payload, dict):
            raise CardUnavailableError("checkout read returned a body that is not an object")
        return card_facts_from(payload, checkout_id=checkout_id, version=version)


def card_facts_from(
    payload: dict[str, Any], *, checkout_id: str, version: int
) -> ApprovalCardFacts:
    """The five binding fields out of a ``GET /v1/checkouts/{id}`` body, or a refusal.

    Refuses rather than adapts. A checkout that is not ``APPROVAL_REQUIRED`` has no card
    to consent to; a card at a different version from the one the screen asked for is a
    different document, and reading it aloud would ask the buyer to consent to bytes they
    are not looking at. ``amount_minor`` must be an actual integer -- a boolean is an
    ``int`` in Python and must never become an amount.
    """
    state = str(payload.get("state", ""))
    if state != "APPROVAL_REQUIRED":
        raise CardUnavailableError(f"this checkout is not awaiting approval (state {state or '?'})")
    card = payload.get("approval_card")
    if not isinstance(card, dict):
        raise CardUnavailableError("the server sent no approval card for this checkout")
    held_version = card.get("version")
    if not isinstance(held_version, int) or isinstance(held_version, bool):
        raise CardUnavailableError("the approval card names no version")
    if held_version != version:
        raise CardUnavailableError(
            f"the screen asked for version {version}; the server holds version {held_version}"
        )
    amount = card.get("amount_minor")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise CardUnavailableError("the approval card carries no integer amount")
    content_hash = card.get("content_hash")
    currency = card.get("currency")
    if not isinstance(content_hash, str) or not content_hash:
        raise CardUnavailableError("the approval card carries no content hash")
    if not isinstance(currency, str) or len(currency) != 3:
        raise CardUnavailableError("the approval card carries no currency")
    if str(card.get("checkout_id", checkout_id)) != checkout_id:
        raise CardUnavailableError("the approval card names a different checkout")
    return ApprovalCardFacts(
        checkout_id=checkout_id,
        version=held_version,
        content_hash=content_hash,
        amount_minor=amount,
        currency=currency,
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

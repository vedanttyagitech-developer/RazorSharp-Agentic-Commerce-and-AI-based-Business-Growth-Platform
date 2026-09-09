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
    "AGENT_TURN_BUDGET_S",
    "AGENT_TURN_PATH",
    "AGENT_TURN_TIMEOUT_S",
    "CAPABILITIES_PATH",
    "CHECKOUT_PATH",
    "MAX_ITEMS",
    "SCENARIO_FAULT_HEADER",
    "AgentUnavailableError",
    "HttpCardReader",
    "HttpTurnHandler",
    "card_facts_from",
    "decision_card_in",
    "grounded_amounts",
    "identity_from_capabilities",
    "items_in",
    "locale_for_language",
    "offer_in",
    "resolve_identity",
    "session_id_from_principal",
]

log = logging.getLogger(__name__)

CAPABILITIES_PATH: Final[str] = "/v1/agent/capabilities"
AGENT_TURN_PATH: Final[str] = "/v1/agent/turn"
#: The read model the storefront renders the approval card from. The consent path reads
#: the card from here, never from the client that asked for it to be read.
CHECKOUT_PATH: Final[str] = "/v1/checkouts/{checkout_id}"

#: The API's own budget for one agent turn: ``agent_runtime.harness.base.RazorAI``'s
#: ``turn_timeout_s`` default. Written here as a number rather than imported, because
#: voice-runtime does not depend on agent-runtime and should not start -- this package
#: reaches the agent over HTTP precisely so it does not have to hold the model runtime.
#:
#: ``test_voice_agent_turn_outlives_the_harness`` reads the real default out of
#: ``RazorAI`` and fails if this number stops matching it, so the copy cannot go stale
#: quietly. That is the same shape as the wire-contract test next to it.
AGENT_TURN_BUDGET_S: Final[float] = 30.0

#: What the rest of an agent turn costs, outside the model call the budget above covers:
#: routing, capability binding, the tool executor's own reads, the session write, the
#: transcript, and two network hops. Generous on purpose -- being wrong low here reopens
#: the bug this constant exists to close, and being wrong high costs one slow turn.
_TURN_OVERHEAD_S: Final[float] = 20.0

#: How long this gateway waits for ``POST /v1/agent/turn``.
#:
#: It MUST exceed :data:`AGENT_TURN_BUDGET_S`, and the reason is not tuning. When the two
#: were both 30.0 they expired at the same instant, so the harness's graceful answer --
#: it catches its own ``TimeoutError`` and returns ``reason="timeout"``, which is the
#: platform's designed degradation -- could never be observed here. The gateway gave up
#: first, every time, and the buyer heard "the agent is unavailable" instead of the
#: sentence the platform had prepared for exactly this.
#:
#: It reproduced only under load: one voice turn alone finishes well inside 30s, so every
#: unit test passed and the live-audio suite failed the same test on every run.
#:
#: Applied per request rather than to the client, because the same client also resolves
#: identity and reads approval cards. Those are indexed reads with no model in them and
#: they must keep failing fast: the spoken-consent window depends on a card arriving
#: promptly, and a card read that hung for fifty seconds would be worse than one that
#: gave up in thirty.
AGENT_TURN_TIMEOUT_S: Final[float] = AGENT_TURN_BUDGET_S + _TURN_OVERHEAD_S

#: Set by the trusted server, and only in the demonstration profile, to name the scenario
#: faults it consumed while running this turn. It is a response header rather than a body
#: field because the body is the buyer panel's contract and specification 31.3 forbids
#: mixing injected apparatus with organic data. Absent on every organic turn.
SCENARIO_FAULT_HEADER: Final[str] = "X-Scenario-Fault-Fired"

#: Longest message the turn endpoint accepts. A transcript longer than this is truncated
#: rather than rejected: the buyer said something, and losing the turn entirely is worse
#: than acting on its first two thousand characters.
MAX_MESSAGE_CHARS: Final[int] = 2000

#: How many products a reply may put on the page. A search page can carry more; the
#: sentence names a few at most, and a shelf longer than this is the catalogue, not the
#: reply. The product the sentence leads with is always first, so the cut never loses it.
MAX_ITEMS: Final[int] = 5

#: How the server renders an agent principal: ``session:<id>/razorai/<specialist>``.
_PRINCIPAL_PREFIX: Final[str] = "session:"

#: Keys whose integer value is already in minor units, as the API's wire shapes use them.
_MINOR_SUFFIX: Final[str] = "_minor"
#: ``{"minor": 7300, "currency": "INR", "display": "73.00"}`` -- the API's money object.
_MINOR_KEY: Final[str] = "minor"
#: The server's own grounding ledger for the turn, as the API names it on the wire.
_LEDGER_KEY: Final[str] = "grounded_amounts_minor"

_LOCALE_FOR_LANGUAGE: Final[dict[str, Locale]] = {
    "en": Locale.EN_IN,
    "hi": Locale.HI_IN,
    # Romanised Hindi is answered in English (``agent_runtime.language._LOCALE_FOR``), so it
    # is spoken in English too: the sentence being read aloud is English, and reading it in
    # the Hindi voice would mispronounce the English rather than the other way round.
    #
    # This used to map to Hindi, and that was right while the reply was itself romanised
    # Hindi -- an Indian English voice reading "chahiye" mispronounces it. The reply
    # language changed; this follows it, because a voice chosen for the language the buyer
    # wrote rather than the language being spoken is a voice reading the wrong text.
    "hi-latn": Locale.EN_IN,
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
    """Every minor-unit amount this turn is entitled to say out loud.

    TWO SOURCES, AND WHY THE SECOND HAD TO BE ADDED
    -----------------------------------------------
    The walk below reads figures out of the payload's own shapes -- ``*_minor`` integers and
    the ``minor`` field of the API's money object -- and that is all it used to do. But
    ``structured`` carries only the LAST tool result of a turn, so a turn that read two
    products offered one price, and the speech guard refused, as ungrounded, a sentence
    naming the other. The reply was on screen and mostly unspoken, which reads as broken
    synthesis and is not.

    ``grounded_amounts_minor`` is the server's own grounding ledger for the whole turn --
    every figure any tool returned -- and it is what the *server* already checked this reply
    against before sending it. The gateway may be stricter than the model; it must not be
    stricter than the platform's own proof.

    Booleans are excluded throughout: ``True`` is an ``int`` in Python and a stray flag must
    not become a spendable amount.
    """
    found: set[int] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == _LEDGER_KEY:
                    # Read explicitly below, and never by the generic rule: this key ends in
                    # ``_minor``, so a malformed scalar here would otherwise be swallowed as
                    # one grounded amount instead of rejected as an unusable ledger.
                    continue
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
    # Evidence, not instruction: it arrives over HTTP from another service, so anything that
    # is not a list of plain integers grounds nothing rather than grounding everything. A
    # guard that failed open on a malformed field would be worse than no guard at all.
    if isinstance(payload, dict):
        ledger = payload.get(_LEDGER_KEY)
        if isinstance(ledger, list):
            found.update(
                value for value in ledger if isinstance(value, int) and not isinstance(value, bool)
            )
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
    proposal = _line_proposal(structured)
    if proposal is not None:
        display = _display_of(proposal)
        return {
            "sku": str(proposal.get("sku", "")),
            "name": str(display.get("name", "")),
            "quantity": int(proposal.get("delta") or 1),
            "unit_price": display.get("unit_price"),
        }
    rows = _product_rows(structured, text)
    return _offer_of(rows[0]) if rows else None


def items_in(structured: object, text: str = "") -> list[dict[str, Any]]:
    """Every product a turn put on the page, up to :data:`MAX_ITEMS`, the offer first.

    The same rows :func:`offer_in` reads, in the same order, so the first item is the
    product a spoken "yes" would take. Unlike the offer, a sold-out product stays in the
    list with ``available`` False: the buyer asked to see it, and a shelf that quietly
    drops what cannot be bought has hidden the answer to "why can't I add it". A
    ``basket.update`` proposal narrows the shelf to the product it proposes. Empty when
    the turn showed no product -- a basket, a checkout, a refusal.

    Each item is ``{"sku", "name", "unit_price", "stock_units", "available"}``.
    ``unit_price`` is the API's money object exactly as it arrived --
    ``{"minor", "currency", "display"}`` -- or None; nothing here computes, converts or
    formats an amount.
    """
    if not isinstance(structured, dict):
        return []
    rows = _product_rows(structured, text)
    proposal = _line_proposal(structured)
    if proposal is not None and (sku := str(proposal.get("sku", ""))):
        # The card the proposal was prepared from usually travels with it and carries the
        # shelf count; when it did not, the proposal's own display is the product.
        rows = [row for row in rows if str(row["sku"]) == sku] or [_row_of_proposal(proposal)]
    return [_item_of(row) for row in rows[:MAX_ITEMS]]


def _product_rows(structured: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """The product rows a turn's payload carries, the one the sentence leads with first.

    The bridged runner spreads the card flat -- ``{"kind": "product", "sku": ...}`` --
    while the deterministic one nests it under ``product``. Both are one product. A
    ``products`` page carries ``hits`` in the search engine's order; when the reply names
    any of them, the one it names first moves to the front and the rest keep their order.
    A model that recommends the second result and then hears "yes" must be taken at its
    word, not at the search engine's. Every row returned has a ``sku``.
    """
    if structured.get("kind") == "product" and structured.get("sku"):
        return [structured]
    product = structured.get("product")
    if isinstance(product, dict) and product.get("sku"):
        return [product]
    hits = structured.get("hits")
    if not isinstance(hits, list):
        return []
    return _led_by(text, [row for row in hits if isinstance(row, dict) and row.get("sku")])


def _led_by(text: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` with the one ``text`` names first moved to the front; unchanged otherwise."""
    lowered = text.lower()
    named = sorted(
        (lowered.index(str(row["display_name"]).lower()), index)
        for index, row in enumerate(rows)
        if row.get("display_name") and str(row["display_name"]).lower() in lowered
    )
    if not named:
        return rows
    lead = named[0][1]
    return [rows[lead], *rows[:lead], *rows[lead + 1 :]]


def _line_proposal(structured: dict[str, Any]) -> dict[str, Any] | None:
    proposal = structured.get("proposal")
    if isinstance(proposal, dict) and proposal.get("action") == "basket.update":
        return proposal
    return None


def _display_of(proposal: dict[str, Any]) -> dict[str, Any]:
    shown = proposal.get("display")
    return shown if isinstance(shown, dict) else {}


def _row_of_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """A product row for a proposal whose product card did not travel with it.

    The server only proposes a product it read this turn and found available, and every
    figure in ``display`` is copied from that read, so this is the same product with
    fewer fields rather than a guess at one.
    """
    display = _display_of(proposal)
    return {
        "sku": str(proposal.get("sku", "")),
        "display_name": display.get("name", ""),
        "unit_price": display.get("unit_price"),
        "stock_units": display.get("stock_units"),
    }


def _available(row: dict[str, Any]) -> bool:
    """Sold out is ``is_available`` False or a shelf count of zero; anything else is for sale."""
    return row.get("is_available") is not False and row.get("stock_units") != 0


def _name_of(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("name") or row["sku"])


def _offer_of(row: dict[str, Any]) -> dict[str, Any] | None:
    if not _available(row):
        return None
    return {
        "sku": str(row["sku"]),
        "name": _name_of(row),
        "quantity": 1,
        "unit_price": row.get("unit_price"),
    }


def _item_of(row: dict[str, Any]) -> dict[str, Any]:
    units = row.get("stock_units")
    price = row.get("unit_price")
    return {
        "sku": str(row["sku"]),
        "name": _name_of(row),
        "unit_price": price if isinstance(price, dict) else None,
        # A count, never an amount; a boolean is an ``int`` in Python and is not a count.
        "stock_units": units if isinstance(units, int) and not isinstance(units, bool) else None,
        "available": _available(row),
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
                # Longer than the client's default, and only here. See
                # :data:`AGENT_TURN_TIMEOUT_S`: this is the one call on this client with a
                # model behind it, and the one that must outlive the harness's own budget.
                timeout=AGENT_TURN_TIMEOUT_S,
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
        text = reply if isinstance(reply, str) else ""
        return TurnReply(
            text=text,
            locale=locale_for_language(str(payload.get("language", "en"))),
            # Read from the server rather than inferred. Absent defaults to False, which is
            # the safe direction: an unknown author is guarded as though a model wrote it.
            server_authored=payload.get("server_authored") is True,
            # The WHOLE payload, not just `structured`: the server's ledger for the turn is
            # a top-level field, and `structured` alone is one tool result. See
            # `grounded_amounts` -- passing the fragment is what silenced priced sentences.
            grounded_amounts_minor=grounded_amounts(payload),
            decision_card=decision_card_in(structured),
            offer=offer_in(structured, text),
            # The gateway already reads the proposal to build the offer; saying so costs
            # nothing and is the only way the surface can tell "add this" from "here it is".
            offer_is_proposal=(
                isinstance(structured, dict) and _line_proposal(structured) is not None
            ),
            items=items_in(structured, text),
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

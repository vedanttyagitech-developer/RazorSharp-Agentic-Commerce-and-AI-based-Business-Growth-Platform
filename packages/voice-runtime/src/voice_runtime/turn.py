"""The seam to the text agents: a final transcript in, a structured reply out.

The voice layer never sees a model and never runs one. It hands a settled transcript to a
:class:`TurnHandler` -- in production the gateway's HTTP client against the platform's own
``POST /v1/agent/turn``, which runs the RazorAI harness in text mode -- and receives text
back. That text exists before any speech is synthesised, which is the entire reason the
pipeline is split (specification 19.1).

The transcript it carries is **intent evidence, never authority evidence** (19.11).
Nothing in this contract can approve, pay, refund or cancel; there is no field that could
express it. A money action reaches the buyer as a proposal they act on through the trusted
surface, exactly as for typed input.

GROUNDED AMOUNTS, AND WHY THEY TRAVEL WITH THE REPLY
---------------------------------------------------
``grounded_amounts_minor`` is every amount, in integer minor units, that a tool result of
*this turn* actually returned. The outbound speech guard speaks an amount only if it is in
that set (:mod:`voice_runtime.tts.guard`). Without it the guard has two options and both
are wrong: refuse every sentence containing money, which gags an ordinary "the 1 litre is
₹73" product conversation and makes the voice surface useless, or allow them all, which
lets a model say a price nothing returned. Carrying the set makes the rule the one that
matters -- a spoken amount is one the server computed -- rather than a blanket ban.

The set is built by walking the server's own structured payload for integer minor-unit
fields. No float is ever produced, parsed or compared anywhere on this path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from commerce_domain import AdmissionDecision, Money

from .identity import VoiceIdentity
from .stt.transcript import TranscriptTurn
from .tts.templates import Locale

__all__ = ["FakeTurnHandler", "TurnHandler", "TurnReply"]


@dataclass(frozen=True, slots=True)
class TurnReply:
    """What the agent wants said. ``text`` is conversational; ``decision`` is transactional.

    Both may be present. When they are, the deterministic sentence is spoken first: a
    buyer hears the settled fact before any commentary about it.
    """

    text: str = ""
    locale: Locale = Locale.EN_IN
    #: True when the platform wrote ``text`` rather than a model -- an outage sentence, a
    #: deterministic answer, a fallback. Reported by ``POST /v1/agent/turn``.
    #:
    #: The speech guard checks model sentences one at a time and passes server-authored
    #: text through whole, because there is nothing in it to second-guess. This is how it
    #: is told which it has. It was inferred from ``decision_card`` before, and that card
    #: never arrives on a live turn, so the platform's own outage sentence was checked as
    #: though a model had written it. In English it passed; in Hindi the guard refused it
    #: for naming a transaction outcome outside a template, and the buyer got an answer on
    #: screen with nothing spoken at all.
    server_authored: bool = False
    decision: AdmissionDecision | None = None
    #: A ``decision`` card as ``agent_runtime.rendering.cards.decision_card`` produces it.
    #: Carries the same facts as ``decision`` and is what actually arrives over HTTP;
    #: either one makes the reply's transactional sentence server-authored.
    decision_card: Mapping[str, Any] | None = None
    #: The one product this reply put in front of the buyer, when the structured payload
    #: named exactly that: a product card, a line proposal, or the first hit of a search.
    #: ``{"sku", "name", "quantity", "unit_price"}``. What a spoken "yes" refers to.
    offer: Mapping[str, Any] | None = None
    #: True when ``offer`` came from a ``basket.update`` proposal -- the buyer asked for it
    #: to be added -- rather than from a product the reply merely showed. Without it a
    #: surface cannot tell the two apart, because ``offer`` is set for both.
    offer_is_proposal: bool = False
    support_order_id: str | None = None
    #: Every product this reply put on the page, up to five, the offer first: each one
    #: ``{"sku", "name", "unit_price", "stock_units", "available"}``. A sold-out product
    #: is listed with ``available`` False rather than dropped. None when the handler said
    #: nothing about products, which the pipeline puts on the wire as an empty shelf.
    items: Sequence[Mapping[str, Any]] | None = None
    amount: Money | None = None
    previous_amount: Money | None = None
    #: Integer minor units the server returned this turn. See the module docstring.
    grounded_amounts_minor: frozenset[int] = field(default_factory=frozenset)
    #: Set when the agent layer itself failed and the reply is a visible fallback, so the
    #: pipeline can surface the degradation rather than speak the fallback as if it were
    #: an answer (19.12).
    degraded: bool = False


class TurnHandler(Protocol):
    async def handle_turn(self, transcript: TranscriptTurn, identity: VoiceIdentity) -> TurnReply:
        """Process one settled turn. Raising is reported as a visible reasoning failure."""
        ...


class FakeTurnHandler:
    """Scripted replies in order; an echo reply when the script is exhausted."""

    def __init__(
        self,
        *,
        replies: Sequence[TurnReply] = (),
        fail: bool = False,
        locale: Locale = Locale.EN_IN,
    ) -> None:
        self.calls: list[tuple[TranscriptTurn, VoiceIdentity]] = []
        self._replies = list(replies)
        self.fail = fail
        self._locale = locale

    async def handle_turn(self, transcript: TranscriptTurn, identity: VoiceIdentity) -> TurnReply:
        self.calls.append((transcript, identity))
        if self.fail:
            raise RuntimeError("fake reasoning failure")
        if self._replies:
            return self._replies.pop(0)
        return TurnReply(text=f"You said {transcript.text}.", locale=self._locale)

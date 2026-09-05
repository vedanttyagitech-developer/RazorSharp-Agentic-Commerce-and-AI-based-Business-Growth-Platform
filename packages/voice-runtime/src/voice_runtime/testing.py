"""Test doubles and captured API samples, shipped with the package on purpose.

``stt/fakes.py`` already ships a fake recognizer and ``tts/synth.py`` a fake synthesizer,
for the same reason these are here: the pipeline is defined by Protocols, and a caller
integrating against it -- the storefront's own tests, an end-to-end harness, another
session's service -- needs the same doubles this package's tests use. Duplicating them
outside is how two versions of "what the wire looks like" come to exist.

:data:`CAPABILITIES` and :data:`STRUCTURED` are **captured**, not invented: they were
copied out of live responses from the running commerce API. When the API changes shape
these stop matching and a test fails, which is the point -- six frontend contract bugs on
this project were found by driving the real API rather than reading the OpenAPI document.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Final

from .identity import VoiceIdentity
from .pipeline import Incoming, TransportClosedError
from .wire.frames import ServerFrame, dump_server_frame

__all__ = [
    "CAPABILITIES",
    "CHECKOUT_STATE_DECISION_CARD",
    "SAMPLE_SESSION_ID",
    "STRUCTURED",
    "MemoryTransport",
    "an_identity",
]

SAMPLE_SESSION_ID: Final[str] = "01a06f9e-6d53-7196-a222-008ea1992868"

#: Captured verbatim from ``GET /v1/agent/capabilities`` on the running API. Note that
#: ``agent_capabilities`` is strictly smaller than ``session_capabilities``: approve, pay
#: and refund are absent by construction, not filtered later.
CAPABILITIES: Final[dict[str, Any]] = {
    "copilot": "buyer",
    "actor_type": "BUYER",
    "session_capabilities": ["basket.write", "catalogue.read", "checkout.approve"],
    "agent_capabilities": ["basket.write", "catalogue.read", "checkout.create"],
    "specialists": [
        {
            "specialist": "shopping",
            "principal_id": f"session:{SAMPLE_SESSION_ID}/razorai/shopping",
            "capabilities": ["basket.write", "catalogue.read"],
            "tools": ["basket.read", "catalog.search"],
        }
    ],
    "absent_by_construction": ["checkout.approve", "payment.verify", "refund.request"],
}

#: The money shapes ``POST /v1/agent/turn`` returns, in both spellings the API uses:
#: a bare ``*_minor`` integer and the money object with a ``minor`` field.
STRUCTURED: Final[dict[str, Any]] = {
    "kind": "products",
    "hits": [
        {
            "sku": "AMUL-DAIRY-002",
            "unit_price_minor": 7300,
            "unit_price": {"minor": 7300, "currency": "INR", "display": "73.00"},
            "stock_units": 30,
            "is_available": True,
        },
        {
            "sku": "AMUL-DAIRY-003",
            "unit_price_minor": 2500,
            "unit_price": {"minor": 2500, "currency": "INR", "display": "25.00"},
            "stock_units": 12,
            "is_available": True,
        },
    ],
    "quote": {"total_minor": 9800, "delivery_fee_minor": 2500, "free_delivery_applied": False},
}


def an_identity(session_id: str = SAMPLE_SESSION_ID) -> VoiceIdentity:
    """A buyer identity shaped exactly as the server's capabilities answer produces one."""
    return VoiceIdentity(
        tenant_id=uuid.UUID("01a06dc7-09f7-7f2c-bd05-df1c773ddb22"),
        session_id=session_id,
        principal_id=f"session:{session_id}/razorai/shopping",
        agent_capabilities=frozenset({"catalogue.read", "basket.write"}),
    )


class MemoryTransport:
    """A :class:`~voice_runtime.pipeline.VoiceTransport` that records and replays.

    ``sent`` preserves the exact interleaving of JSON frames and binary audio, which is
    the only way to assert that a ``speech_chunk`` header is immediately followed by the
    binary frame it announced -- the client sizes its next read from that header.
    """

    def __init__(self, *, suspend_on_write: bool = False) -> None:
        self.sent: list[tuple[str, Any]] = []
        self._inbox: asyncio.Queue[Incoming | None] = asyncio.Queue()
        self.closed = False
        #: Make every write yield to the event loop, the way a real socket does.
        #:
        #: With this False the transport never suspends, so no other task can interleave
        #: and any ordering invariant holds trivially. That is how the speech_chunk
        #: header/binary pairing passed a green test while being violated on a real
        #: socket. A test that asserts ordering MUST set this.
        self.suspend_on_write = suspend_on_write

    # ---- VoiceTransport ---------------------------------------------------------------

    async def send_frame(self, frame: ServerFrame) -> None:
        if self.suspend_on_write:
            await asyncio.sleep(0)
        self.sent.append(("json", json.loads(dump_server_frame(frame))))

    async def send_audio(self, pcm: bytes) -> None:
        if self.suspend_on_write:
            # An audio frame is orders of magnitude larger than a JSON one, so on a real
            # socket it takes longer to write. Modelling that is what makes the
            # header/binary race reachable in a test: with both writes yielding exactly
            # once, whoever suspended first always resumes first and nothing interleaves.
            await asyncio.sleep(0.001)
        self.sent.append(("audio", pcm))

    async def receive(self) -> Incoming:
        item = await self._inbox.get()
        if item is None:
            self.closed = True
            raise TransportClosedError("scripted end of stream")
        return item

    # ---- driving it -------------------------------------------------------------------

    def push_audio(self, pcm: bytes) -> None:
        self._inbox.put_nowait(Incoming(data=pcm))

    def push_text(self, payload: dict[str, Any]) -> None:
        self._inbox.put_nowait(Incoming(text=json.dumps(payload)))

    def push_raw(self, raw: str) -> None:
        self._inbox.put_nowait(Incoming(text=raw))

    def end(self) -> None:
        self._inbox.put_nowait(None)

    # ---- reading it back ---------------------------------------------------------------

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return [body for tag, body in self.sent if tag == "json" and body.get("type") == kind]

    def frame_types(self) -> list[str]:
        return [body["type"] for tag, body in self.sent if tag == "json"]

    def audio_chunks(self) -> list[bytes]:
        return [body for tag, body in self.sent if tag == "audio"]

    def one(self, kind: str) -> dict[str, Any]:
        found = self.frames(kind)
        if len(found) != 1:
            raise AssertionError(f"expected exactly one {kind!r} frame, got {len(found)}")
        return found[0]


#: The ``decision`` card ``commerce_api.services.agent_service._decision_card_from``
#: emits, captured from its source and confirmed against a live refusal (version 1
#: superseded by 2, total 8550 -> 11984).
#:
#: It differs from ``agent_runtime.rendering.cards.decision_card`` in two ways that both
#: matter to a renderer: ``decision_id`` and ``explanation`` are ``None``, because no
#: admission ran in the turn and the reason key is the kernel's word; and it carries
#: ``previous_version`` and ``source``.
#:
#: There used to be a third, and it is worth remembering rather than deleting: this card
#: put its rows under ``deltas`` while the other used ``items``, and the renderer read one
#: key and spoke none of them -- a refusal that said "something changed" and named neither
#: figure, which is the summary specification 19.10 exists to prevent. The two producers
#: were unified onto ``items`` and a test now holds them together.
CHECKOUT_STATE_DECISION_CARD: Final[dict[str, Any]] = {
    "kind": "decision",
    "source": "checkout_state",
    "decision_id": None,
    "allowed": False,
    "code": "REAPPROVAL_REQUIRED",
    "explanation": None,
    # ``items``, with ``count`` beside it, because that is the key the card vocabulary in
    # ``agent_runtime.rendering.cards`` uses and ``agent_service`` was unified onto it. The
    # renderer accepts either, but a fixture carrying the older name would slowly stop
    # resembling the thing it claims to be captured from.
    "items": [
        {"field_path": "total", "approved": 8550, "current": 11984, "reason": "total_changed"}
    ],
    "count": 1,
    "previous_version": 1,
    "next_version": 2,
    "checkout_id": "01a06ffb-fbd5-7cea-bb50-f7e0c2790b36",
    "current_version": 2,
    "state": "AWAITING_APPROVAL",
    "total": {"minor": 11984, "currency": "INR", "display": "119.84"},
    "where": "trusted_surface",
}

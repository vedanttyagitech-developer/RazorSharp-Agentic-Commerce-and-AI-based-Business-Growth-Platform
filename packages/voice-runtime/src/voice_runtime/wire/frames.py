"""The transcript wire contract as pydantic models (19.5, 19.12).

For every streamed field the contract states three things explicitly: cumulative or
delta, revisable or monotonic, and what an empty value means. Shape is not semantics, so
the semantics are fields of the frame itself, typed as literals the client can assert on:

    transcript_partial   cumulative, revisable, empty keeps the held text
    transcript_final     cumulative, monotonic, empty keeps the held text; closes the turn

Binary WebSocket frames are audio: client to server, PCM16 LE 16 kHz mono microphone
frames; server to client, PCM16 LE 24 kHz mono speech, each preceded by exactly one
``speech_chunk`` JSON frame announcing its length, text and generation.

Degradation is a first-class frame (19.12) so a degraded path is always visible.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..constants import (
    BARGE_IN_LEVEL_RMS,
    BARGE_IN_SUSTAIN_S,
    ECHO_TAIL_S,
    INPUT_SAMPLE_RATE_HZ,
    MIC_FRAME_MS,
    OUTPUT_SAMPLE_RATE_HZ,
    PLAYBACK_LEAD_S,
)
from ..intents import IntentOutcome

DegradationKind = Literal[
    "stt_connection_lost",
    "stt_rotation_failed",
    "stt_unavailable",
    "tts_failed",
    "reasoning_failed",
    "echo_gate_uncertain",
    # The outbound guard refused a model sentence. The text is on screen; it is simply
    # not spoken. Visible, because silence the buyer cannot explain is worse than a
    # sentence they can read (19.12).
    "speech_guard_refused",
    # A settled turn aged past the end-to-end budget while queued behind a longer one.
    # The buyer is told rather than left waiting for an answer that will never come.
    "stale_turn_dropped",
    # The approval card a ``read_card`` frame named could not be read from the trusted
    # server as asked: unreachable, not this buyer's, not awaiting approval, or not the
    # version on the screen. Nothing was read aloud and no consent window opened; the
    # button works.
    "card_unavailable",
]


class _Frame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- server -> client ------------------------------------------------------------------


class AudioContract(_Frame):
    sample_rate_hz: int
    encoding: Literal["pcm16le"] = "pcm16le"
    channels: Literal[1] = 1


class SessionReady(_Frame):
    """First frame on every stream: the constants the client must honour (19.15)."""

    type: Literal["session_ready"] = "session_ready"
    session_id: str
    protocol_version: Literal[2] = 2
    input: AudioContract = AudioContract(sample_rate_hz=INPUT_SAMPLE_RATE_HZ)
    output: AudioContract = AudioContract(sample_rate_hz=OUTPUT_SAMPLE_RATE_HZ)
    mic_frame_ms: int = MIC_FRAME_MS
    echo_tail_s: float = ECHO_TAIL_S
    barge_in_level_rms: float = BARGE_IN_LEVEL_RMS
    barge_in_sustain_s: float = BARGE_IN_SUSTAIN_S
    playback_lead_s: float = PLAYBACK_LEAD_S
    voice_is_authority: Literal[False] = False


class RecognitionState(_Frame):
    """Recognition readiness is independent of the socket handshake."""

    type: Literal["recognition_state"] = "recognition_state"
    state: Literal["connecting", "ready", "unavailable"]


class TranscriptPartial(_Frame):
    """``interim_input_transcription``: replace held text; never confirmed intent."""

    type: Literal["transcript_partial"] = "transcript_partial"
    text: str
    turn_id: int
    stt_generation: int
    age_ms: int
    cumulative: Literal[True] = True
    revisable: Literal[True] = True
    empty_means: Literal["keep_held"] = "keep_held"


class TranscriptFinal(_Frame):
    """``input_transcription``: replace, then close the turn. ``stale`` marks a final that
    aged past the freshness window and was NOT given to the agent."""

    type: Literal["transcript_final"] = "transcript_final"
    text: str
    turn_id: int
    stt_generation: int
    age_ms: int
    stale: bool
    source: Literal["voice", "text"] = "voice"
    cumulative: Literal[True] = True
    revisable: Literal[False] = False
    empty_means: Literal["keep_held"] = "keep_held"


class AgentReply(_Frame):
    """Text exists before speech. Deterministic replies carry their template audit facts."""

    type: Literal["agent_reply"] = "agent_reply"
    intent_id: int | None = None
    unspoken: bool = False
    text: str
    deterministic: bool
    locale: str
    turn_id: int
    speech_generation: int
    template_id: str | None = None
    template_version: int | None = None
    fields: dict[str, str] | None = None
    #: The one product this reply put forward, for the page to act on when the buyer says
    #: yes. Never set on a deterministic (money) utterance.
    offer: dict[str, Any] | None = None
    #: Whether ``offer`` is a proposal to ADD it, or merely the product being shown.
    #:
    #: The distinction the surface cannot make for itself, and the reason it is here.
    #: ``offer`` is set for both -- a ``basket.update`` proposal names a SKU and a delta,
    #: and a product page names its first row -- so a page acting on ``offer`` alone puts
    #: something in the buyer's basket every time they ask to *see* a product. The gateway
    #: knows which it built (``agent_client._line_proposal``); until now it did not say.
    #:
    #: True means the buyer asked for it and the trusted surface may add it: that is their
    #: own instruction, and adding to a basket is not consent to buy. False means show it
    #: and wait.
    offer_is_proposal: bool = False
    #: Every product this reply put on the page, up to five, the offer first: each one
    #: ``{sku, name, unit_price, stock_units, available}``, ``unit_price`` the API's own
    #: money object or null. A list -- empty when the reply showed no product -- on every
    #: conversational reply, so the page redraws its shelf from the frame alone. None on a
    #: deterministic (money) utterance, which shows nothing and clears nothing.
    items: list[dict[str, Any]] | None = None


class TurnOpened(_Frame):
    type: Literal["turn_opened"] = "turn_opened"
    intent_id: int
    turn_id: int
    source: str


class TurnReasoning(_Frame):
    type: Literal["turn_reasoning"] = "turn_reasoning"
    intent_id: int


class TurnClosed(_Frame):
    """Completion is independent of reply text or whether audio was available."""

    type: Literal["turn_closed"] = "turn_closed"
    intent_id: int
    turn_id: int
    outcome: IntentOutcome


class SpeechStart(_Frame):
    type: Literal["speech_start"] = "speech_start"
    utterance_id: int = Field(ge=1)
    speech_generation: int


class SpeechChunkHeader(_Frame):
    """Announces exactly one following binary frame of ``byte_length`` bytes."""

    type: Literal["speech_chunk"] = "speech_chunk"
    seq: int
    utterance_id: int = Field(ge=1)
    speech_generation: int
    text: str
    sample_rate_hz: int
    byte_length: int
    deterministic: bool
    encoding: Literal["pcm16le"] = "pcm16le"


class SpeechEnd(_Frame):
    type: Literal["speech_end"] = "speech_end"
    utterance_id: int = Field(ge=1)
    speech_generation: int
    chunks: int
    cancelled: bool


class Interrupted(_Frame):
    """Server acknowledgement of a client barge-in: the generation that is now current."""

    type: Literal["interrupted"] = "interrupted"
    speech_generation: int


class Degradation(_Frame):
    """A degraded path, made visible (19.12). Money invariants are stated on the wire."""

    type: Literal["degradation"] = "degradation"
    kind: DegradationKind
    message: str
    text_input_available: Literal[True] = True
    transaction_state_changed: Literal[False] = False


class ErrorFrame(_Frame):
    type: Literal["error"] = "error"
    code: str
    message: str


# ---- spoken consent (19.11) --------------------------------------------------------------
#
# Voice is a second way to press the Approve button, and these frames are how the browser
# sees which way fired. Every one of them is descriptive: the gateway reads a card, speaks
# it, listens for a word, and reports. It records nothing. ``recorded: False`` travels on
# ``consent_recognised`` the way ``transaction_state_changed: False`` travels on
# ``Degradation`` -- as a literal the client asserts on, not a comment it has to trust.


class CardRead(_Frame):
    """The gateway read this card from the trusted server and is about to speak it.

    Sent right after the deterministic ``agent_reply`` that carries the reading's text
    (text before speech, always). The five binding fields are the ones the storefront
    compares against the card on screen before it presses the button on the buyer's behalf.
    """

    type: Literal["card_read"] = "card_read"
    checkout_id: str
    version: int
    content_hash: str
    amount_minor: int
    currency: str
    locale: str
    template_id: str
    template_version: int
    speech_generation: int


class ConsentListening(_Frame):
    """The reading has been fully SENT; a yes or no now counts for ``closes_in_s`` seconds.

    Anchored on the server's send-completion, never on a client report. The client may
    still be playing the last phrases; the buyer answering over them is a barge-in, and a
    barge-in closes the window.
    """

    type: Literal["consent_listening"] = "consent_listening"
    consent_id: str
    closes_in_s: float
    speech_generation: int


class ConsentRecognised(_Frame):
    """A lexicon yes landed inside the window. Recognised, not recorded.

    The card fields are the ones that were READ, carried so the storefront can refuse to
    act on them if the card on screen has since moved. ``heard`` is the settled transcript
    and ``offset_ms`` is how far into the window it arrived.
    """

    type: Literal["consent_recognised"] = "consent_recognised"
    consent_id: str
    checkout_id: str
    version: int
    content_hash: str
    amount_minor: int
    currency: str
    heard: str
    turn_id: int
    stt_generation: int
    offset_ms: int
    recorded: Literal[False] = False
    voice_is_authority: Literal[False] = False


class ConsentDeclined(_Frame):
    """A lexicon no landed inside the window. Nothing was sent anywhere: a spoken no
    closes the window and leaves the version open, because Reject releases the reservation
    at once and a misheard no would cost the buyer their hold."""

    type: Literal["consent_declined"] = "consent_declined"
    consent_id: str
    heard: str


class ConsentUnrecognised(_Frame):
    """Something was heard inside the window and it was not a yes or a no.

    The window stays open. ``began_before_reading_ended`` names an utterance that was
    already in flight when the reading finished: whatever it says, the buyer had not
    heard the whole amount when they started it.
    """

    type: Literal["consent_unrecognised"] = "consent_unrecognised"
    consent_id: str
    text: str
    reason: Literal["not_in_lexicon", "began_before_reading_ended"]


ConsentClosedReason = Literal["recognised", "declined", "expired", "barge_in", "superseded"]


class ConsentClosed(_Frame):
    """Every window ends with exactly one of these, so silence is never the last frame."""

    type: Literal["consent_closed"] = "consent_closed"
    consent_id: str
    reason: ConsentClosedReason


class Pong(_Frame):
    """Answers a client ``Ping``, and is also sent unprompted as a keepalive.

    It exists because of a proxy, not because of the protocol. A session here is
    deliberately unbounded -- the STT rotation margin sits under the provider's stream
    limit precisely so a buyer's conversation outlives Google's -- but an intermediary
    between the buyer and this gateway does not know that. Cloudflare closes a proxied
    WebSocket after 100 seconds with no traffic in either direction on its Free and Pro
    plans, and a buyer who is simply thinking sends nothing at all.

    So the server speaks first rather than trusting the client to. A frame every
    ``VOICE_GATEWAY_KEEPALIVE_SECONDS`` is enough to keep the socket alive, and it carries
    no state: a dropped or duplicated pong means nothing, which is the property that lets
    it be sent from a background task without touching the turn machinery.
    """

    type: Literal["pong"] = "pong"


ServerFrame = Annotated[
    SessionReady
    | RecognitionState
    | TranscriptPartial
    | TranscriptFinal
    | AgentReply
    | SpeechStart
    | SpeechChunkHeader
    | SpeechEnd
    | Interrupted
    | Degradation
    | ErrorFrame
    | CardRead
    | ConsentListening
    | ConsentRecognised
    | ConsentDeclined
    | ConsentUnrecognised
    | ConsentClosed
    | TurnOpened
    | TurnReasoning
    | TurnClosed
    | Pong,
    Field(discriminator="type"),
]

_server_adapter: TypeAdapter[ServerFrame] = TypeAdapter(ServerFrame)


def dump_server_frame(frame: ServerFrame) -> str:
    return _server_adapter.dump_json(frame).decode()


# ---- client -> server ------------------------------------------------------------------


class TextInput(_Frame):
    """Typed input. Always available, including while STT is degraded (19.12)."""

    type: Literal["text_input"] = "text_input"
    text: str = Field(min_length=1, max_length=4000)


class BargeIn(_Frame):
    """The client already flushed local playback (19.7); the server reconciles."""

    type: Literal["barge_in"] = "barge_in"
    reason: Literal["speak", "cancel"] = "speak"


class PlaybackStarted(_Frame):
    """Acknowledges actual browser playback, not receipt of PCM."""

    type: Literal["playback_started"] = "playback_started"
    utterance_id: int = Field(ge=1)
    speech_generation: int


class PlaybackEnded(_Frame):
    """The client finished playing the speech of ``speech_generation``; starts the echo
    tail (19.6). The server never assumes this."""

    type: Literal["playback_ended"] = "playback_ended"
    utterance_id: int = Field(ge=1)
    speech_generation: int


class Ping(_Frame):
    type: Literal["ping"] = "ping"


class ReadCard(_Frame):
    """Ask the gateway to read an approval card aloud and listen for a yes or no.

    The client NAMES a card; it does not describe one. There is no hash and no amount in
    this frame, because a client-supplied hash would be a client-supplied consent surface.
    The gateway reads the card from the trusted server with the buyer's own bearer, refuses
    if the checkout is not awaiting approval or holds a different version, and speaks only
    what the server returned. A checkout id is data, not authority: ownership is enforced
    where it always is.
    """

    type: Literal["read_card"] = "read_card"
    checkout_id: uuid.UUID
    version: int = Field(ge=1)
    locale: Literal["en-IN", "hi-IN"] = "en-IN"


class CheckoutGuidance(_Frame):
    """Read-only guidance; the server reads amounts/outcomes with the buyer credential."""

    type: Literal["checkout_guidance"] = "checkout_guidance"
    checkout_id: uuid.UUID | None = None
    version: int | None = Field(default=None, ge=1)
    stage: Literal[
        "review", "reserve-review", "manual", "verifying", "success", "failed", "bill-details"
    ] = "review"


class ScreenContext(_Frame):
    """Routing fence while a trusted checkout has not produced a bill yet."""

    type: Literal["screen_context"] = "screen_context"
    scope: Literal["shopping", "checkout"]


class CartUpdated(_Frame):
    """Name a saved cart mutation; the authenticated API verifies it before speaking."""

    type: Literal["cart_updated"] = "cart_updated"
    cart_id: uuid.UUID
    event_id: uuid.UUID


ClientFrame = Annotated[
    TextInput
    | BargeIn
    | PlaybackStarted
    | PlaybackEnded
    | Ping
    | ReadCard
    | CheckoutGuidance
    | CartUpdated
    | ScreenContext,
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


def parse_client_frame(raw: str | bytes) -> ClientFrame:
    """Strict parse; unknown types and extra fields are rejected."""
    return _client_adapter.validate_json(raw)

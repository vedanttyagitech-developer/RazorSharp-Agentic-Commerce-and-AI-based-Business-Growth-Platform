"""Who a voice connection speaks for, and where that answer came from.

Every field here is an answer the **trusted server** gave, never something the client
said. The gateway learns a session's identity by presenting the buyer's bearer to
``GET /v1/agent/capabilities`` and believing only the reply; a websocket that arrives
with a ticket carries no claims of its own beyond the ticket itself.

This type deliberately is not an :class:`commerce_domain.AgentPrincipal`. A principal
is an authority object -- it says what may be done -- and minting one belongs to the
server that owns the session row, not to a speech layer. The voice gateway holds an
identity so it can attribute a transcript and address the right session, and it holds no
authority at all: every consequential act goes back through the trusted surface with the
buyer's own bearer, exactly as for typed input (specification 19.11).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

__all__ = ["VoiceIdentity"]


@dataclass(frozen=True, slots=True)
class VoiceIdentity:
    """The server's answer about one commerce session, carried for the socket's lifetime."""

    #: Known where the minting path learned it. The trusted server enforces tenancy on
    #: every call the gateway makes, so this is for binding and audit and never decides
    #: anything; ``None`` means "the server knows, and this layer did not need to".
    tenant_id: uuid.UUID | None
    session_id: str
    #: ``session:<id>/razorai/<specialist>`` as the server renders it, for the audit trail.
    principal_id: str
    #: ``buyer``. A merchant session is refused before a ticket is ever minted: the buyer
    #: copilot is the only harness with a voice surface in P0.
    buyer_ref: str | None = None
    copilot: str = "buyer"
    #: What the server said this session's agent may do, for display and audit only. The
    #: gate that matters runs server-side on every tool call; this copy never decides.
    agent_capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def correlation_id(self) -> str:
        """One id that reconstructs the conversation across every STT rotation (19.13)."""
        return self.session_id

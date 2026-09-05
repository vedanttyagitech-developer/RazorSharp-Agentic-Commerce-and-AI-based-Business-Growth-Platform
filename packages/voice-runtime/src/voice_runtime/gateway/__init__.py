"""The Voice Gateway: the only part of this package that knows about HTTP and sockets.

Everything below it -- the recognizer session, the echo gate, the guard, the templates --
is transport-free and driven by Protocols, so the pipeline a test runs against fakes is
byte for byte the pipeline that serves a browser.
"""

from .agent_client import AgentUnavailableError, HttpTurnHandler, grounded_amounts, resolve_identity
from .app import VoiceGateway, create_app
from .settings import GatewaySettings
from .transport import WebSocketTransport

__all__ = [
    "AgentUnavailableError",
    "GatewaySettings",
    "HttpTurnHandler",
    "VoiceGateway",
    "WebSocketTransport",
    "create_app",
    "grounded_amounts",
    "resolve_identity",
]

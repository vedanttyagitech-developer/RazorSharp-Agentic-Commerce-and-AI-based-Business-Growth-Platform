"""Run the Voice Gateway: ``uv run --no-sync python -m voice_runtime.gateway``.

Configuration is environment only, so the same command serves development and a container:

    VOICE_GATEWAY_API_BASE_URL   the trusted commerce API   (default http://127.0.0.1:8000)
    VOICE_GATEWAY_ALLOWED_ORIGINS  comma-separated exact origins for the WebSocket
    VOICE_GATEWAY_HOST / _PORT   where to listen            (default 127.0.0.1:8100)
    GOOGLE_CLOUD_PROJECT         Vertex project for speech
    GOOGLE_GENAI_USE_VERTEXAI    must be truthy for speech to be enabled

Speech being unconfigured is not a startup failure. The gateway serves, the socket opens,
and its first frames say that recognition is unavailable and typing still works -- which
is the visible degradation of specification 19.12, applied to its own configuration.
"""

from __future__ import annotations

import logging
import os

from .app import VoiceGateway, create_app
from .settings import GatewaySettings

HOST_ENV = "VOICE_GATEWAY_HOST"
PORT_ENV = "VOICE_GATEWAY_PORT"


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("VOICE_GATEWAY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = GatewaySettings.from_env()
    log = logging.getLogger("voice_runtime.gateway")
    log.info("commerce API: %s", settings.api_base_url)
    log.info("allowed origins: %s", ", ".join(settings.allowed_origins) or "(none: no browser)")
    if not settings.speech_configured:
        log.warning(
            "speech is NOT configured (GOOGLE_CLOUD_PROJECT / GOOGLE_GENAI_USE_VERTEXAI); "
            "sockets will open in text mode and say so"
        )
    uvicorn.run(
        create_app(VoiceGateway(settings)),
        host=os.environ.get(HOST_ENV, "127.0.0.1"),
        port=int(os.environ.get(PORT_ENV, "8100")),
        log_level=os.environ.get("VOICE_GATEWAY_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()

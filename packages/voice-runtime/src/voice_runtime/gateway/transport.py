"""The FastAPI WebSocket as a :class:`~voice_runtime.pipeline.VoiceTransport`.

The pipeline is written against a Protocol with two send methods and one receive, so this
module is the only place in the package that knows what a Starlette WebSocket is. The
tests drive the same pipeline through an in-memory transport and exercise the identical
code path -- which is what makes the real-audio test meaningful rather than a mock of
itself.

One rule the type system cannot state: a ``speech_chunk`` JSON frame and the binary frame
it announces must arrive in that order with nothing between them, because the client sizes
its next read from the header. The pipeline sends them back to back and this transport
does not reorder, buffer or coalesce.
"""

from __future__ import annotations

import logging

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from ..pipeline import Incoming, TransportClosedError
from ..wire.frames import ServerFrame, dump_server_frame

__all__ = ["WebSocketTransport"]

log = logging.getLogger(__name__)


class WebSocketTransport:
    """``VoiceTransport`` over one accepted Starlette/FastAPI WebSocket."""

    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket

    async def send_frame(self, frame: ServerFrame) -> None:
        try:
            await self._socket.send_text(dump_server_frame(frame))
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise TransportClosedError(str(exc)) from exc

    async def send_audio(self, pcm: bytes) -> None:
        try:
            await self._socket.send_bytes(pcm)
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise TransportClosedError(str(exc)) from exc

    async def receive(self) -> Incoming:
        """Next message. Binary is microphone audio; text is a control frame."""
        try:
            message = await self._socket.receive()
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise TransportClosedError(str(exc)) from exc
        kind = message.get("type")
        if kind == "websocket.disconnect":
            raise TransportClosedError("client disconnected")
        data = message.get("bytes")
        if data is not None:
            return Incoming(data=data)
        text = message.get("text")
        if text is not None:
            return Incoming(text=text)
        # A frame that is neither text nor binary carries nothing the pipeline can act
        # on. Returning an empty text frame would be parsed and rejected as invalid;
        # returning an empty Incoming lets the pipeline ignore it without counting a
        # client error against a client that did not make one.
        return Incoming()

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._socket.client_state is WebSocketState.CONNECTED:
            try:
                await self._socket.close(code=code, reason=reason)
            except RuntimeError as exc:  # pragma: no cover - already closing
                log.debug("closing voice socket raised: %s", exc)

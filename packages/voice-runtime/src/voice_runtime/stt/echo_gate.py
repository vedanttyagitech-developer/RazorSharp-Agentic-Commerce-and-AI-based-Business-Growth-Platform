"""The echo gate: substitute silence, never withhold frames (19.6).

Browser echo cancellation does not reliably cancel Web Audio playback, so the assistant's
own speech comes back through the microphone. Stopping mic frames while speaking is the
wrong fix: withholding frames starves the recognizer and the stream times out. This gate
replaces each frame with digital silence of the same length, so the cadence of frames the
recognizer sees is unchanged and the session and its voice-activity detection stay alive.

The tail starts when the **client** reports playback completion, not when the server
finished sending: a deeply buffered client otherwise leaks its last sentence back into the
microphone. If the client never reports, the gate releases after ``ECHO_GATE_MAX_HOLD_S``
and the caller surfaces the uncertainty (19.12: prefer suppression; the buyer can type).
"""

from __future__ import annotations

from ..clock import Clock
from ..constants import (
    BYTES_PER_SAMPLE,
    ECHO_GATE_MAX_HOLD_S,
    ECHO_TAIL_S,
    INPUT_SAMPLE_RATE_HZ,
    SILENCE_BYTE,
)


class EchoGate:
    """Half-duplex gate over the microphone frame stream."""

    def __init__(
        self,
        *,
        clock: Clock,
        tail_s: float = ECHO_TAIL_S,
        max_hold_s: float = ECHO_GATE_MAX_HOLD_S,
    ) -> None:
        self._clock = clock
        self._tail_s = tail_s
        self._max_hold_s = max_hold_s
        self._speaking = False
        self._echo_until = 0.0
        self._server_done_at: float | None = None
        self._hold_expired = False
        # Metrics (19.13)
        self.gated_frames = 0
        self.gated_bytes = 0
        self.passed_frames = 0

    # ---- state transitions -----------------------------------------------------------

    def start_speaking(self) -> None:
        """Assistant audio is about to be sent. Engage."""
        self._speaking = True
        self._server_done_at = None
        self._hold_expired = False

    def on_server_send_complete(self) -> None:
        """The server finished sending bytes. The gate stays engaged: the client may still be
        playing. Only the client's report (or the bounded hold) releases it."""
        self._server_done_at = self._clock.now()

    def on_client_playback_ended(self) -> None:
        """The client finished playing. Start the tail from here (19.6)."""
        self._release_with_tail()

    def on_barge_in(self) -> None:
        """The client flushed playback locally (19.7); release with the tail."""
        self._release_with_tail()

    def _release_with_tail(self) -> None:
        self._speaking = False
        self._server_done_at = None
        self._echo_until = self._clock.now() + self._tail_s

    # ---- frame path ------------------------------------------------------------------

    @property
    def engaged(self) -> bool:
        now = self._clock.now()
        if self._speaking:
            done = self._server_done_at
            if done is not None and now - done > self._max_hold_s:
                # Bounded hold: the client never reported playback end. Release with the
                # tail and remember that we did, so the caller can make it visible.
                self._hold_expired = True
                self._release_with_tail()
            else:
                return True
        return now < self._echo_until

    @property
    def speaking(self) -> bool:
        return self._speaking

    def consume_hold_expired(self) -> bool:
        """True once if the bounded hold released the gate without a client report."""
        expired, self._hold_expired = self._hold_expired, False
        return expired

    def gate(self, frame: bytes) -> bytes:
        """Return the frame to send: the original, or digital silence of equal length.

        A frame is ALWAYS returned; the gate never withholds (19.6).
        """
        if self.engaged:
            self.gated_frames += 1
            self.gated_bytes += len(frame)
            return bytes([SILENCE_BYTE]) * len(frame)
        self.passed_frames += 1
        return frame

    @property
    def engaged_seconds(self) -> float:
        """Echo-gate engagement time, derived from gated audio at the input contract."""
        return self.gated_bytes / (INPUT_SAMPLE_RATE_HZ * BYTES_PER_SAMPLE)

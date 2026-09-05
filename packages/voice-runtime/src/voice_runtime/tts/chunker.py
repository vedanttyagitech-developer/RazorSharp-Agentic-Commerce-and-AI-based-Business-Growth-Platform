"""Sentence-boundary chunking for speech (19.9).

Speaking sentence by sentence as tokens arrive removes seconds of dead air. The chunker
uses the same tokenizer as the guard, by import, not by copy.
"""

from __future__ import annotations

from .tokenizer import split_complete_sentences, split_sentences

#: The chunker's tokenizer IS the guard's tokenizer. ``tests/test_voice_tokenizer.py``
#: asserts identity, not equality, so a copy-paste divergence fails loudly.
chunk_for_speech = split_sentences


class SentenceChunker:
    """Streaming chunker: feed text deltas, receive complete sentences as they close."""

    tokenizer = staticmethod(split_sentences)

    def __init__(self) -> None:
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        """Absorb a delta; return every sentence whose boundary is now confirmed."""
        self._buffer += delta
        complete, remainder = split_complete_sentences(self._buffer)
        self._buffer = remainder
        return complete

    def flush(self) -> list[str]:
        """End of stream: the remainder is a sentence now."""
        tail, self._buffer = self._buffer, ""
        return split_sentences(tail)

    @property
    def pending(self) -> str:
        return self._buffer

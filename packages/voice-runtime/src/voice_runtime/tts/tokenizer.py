"""The one sentence tokenizer (19.9).

Both the outbound content guard and the TTS chunker import ``split_sentences`` from here.
If the guard and the thing it guards tokenize differently, that difference is the bypass.

The trailing-whitespace requirement after terminal punctuation is load-bearing: without it
``₹1,299.50``, ``3.3`` and ``razorpay.com`` split mid-token, and a split amount is both
mispronounced and able to slip past a guard that matched on the whole string. The
Devanagari danda (``।``) is a terminal punctuation mark for Hindi.
"""

from __future__ import annotations

import re
from typing import Final

#: Terminal punctuation (Latin and the danda), optional closing quotes/brackets, then
#: mandatory whitespace; or one or more newlines.
SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"([.!?।]+[\"')\]]*\s+|\n+)")


def split_sentences(text: str) -> list[str]:
    """Split complete text into sentences. The unterminated tail is its own sentence."""
    sentences: list[str] = []
    position = 0
    for match in SENTENCE_END.finditer(text):
        sentence = text[position : match.end()].strip()
        if sentence:
            sentences.append(sentence)
        position = match.end()
    tail = text[position:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def split_complete_sentences(text: str) -> tuple[list[str], str]:
    """Split off every sentence with a confirmed boundary; return the remainder unsplit.

    Used by the streaming chunker: the remainder may still grow (``₹1,299`` may become
    ``₹1,299.50``), so it is never emitted until a boundary or a flush confirms it.
    """
    sentences: list[str] = []
    position = 0
    for match in SENTENCE_END.finditer(text):
        sentence = text[position : match.end()].strip()
        if sentence:
            sentences.append(sentence)
        position = match.end()
    return sentences, text[position:]

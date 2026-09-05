"""The one sentence tokenizer (19.9).

Both the outbound content guard and the TTS chunker import ``split_sentences`` from here.
If the guard and the thing it guards tokenize differently, that difference is the bypass.

The trailing-whitespace requirement after terminal punctuation is load-bearing: without it
``₹1,299.50``, ``3.3`` and ``razorpay.com`` split mid-token, and a split amount is both
mispronounced and able to slip past a guard that matched on the whole string. The
Devanagari danda (``।``) is a terminal punctuation mark for Hindi.

SPLITTING FURTHER, FOR SYNTHESIS ONLY
-------------------------------------
:func:`split_for_synthesis` cuts an **already-approved** sentence into shorter phrases at
commas and semicolons. It exists because a single sentence can be very long -- RazorAI
answers a product search with five products and their prices in one sentence -- and
synthesis latency scales with length. Measured on the running system: a 350-character
product listing took **21 seconds to synthesise** before one sample could play. Split at
its commas, the buyer hears the first phrase in about three.

This does not weaken 19.9's one-tokenizer rule, because it only ever makes the SPEAKER's
unit smaller than the GUARD's, never larger. The guard always evaluates the whole
sentence; a phrase reaching the synthesiser was part of a sentence that was approved
entire. The dangerous direction -- a guard checking smaller units than are spoken, so a
claim spanning a boundary slips through -- is not reachable from here.

The split point is a comma or semicolon **followed by whitespace**. Indian digit grouping
never has a space after its comma, so ``₹1,29,999`` cannot be cut, and a test says so.
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


#: A comma or semicolon followed by whitespace. Digit grouping (``1,299``) has no space
#: after the comma, so an amount cannot be cut here.
PHRASE_END: Final[re.Pattern[str]] = re.compile(r"([,;،؛]\s+)")


def split_for_synthesis(sentence: str, max_chars: int) -> list[str]:
    """Cut an approved sentence into phrases no longer than ``max_chars`` where it can.

    Returns ``[sentence]`` unchanged when it is short enough or has no safe cut point: a
    phrase that cannot be shortened safely is spoken long rather than spoken wrong.
    """
    if max_chars <= 0 or len(sentence) <= max_chars:
        return [sentence]
    segments: list[str] = []
    position = 0
    for match in PHRASE_END.finditer(sentence):
        segments.append(sentence[position : match.end()])
        position = match.end()
    tail = sentence[position:]
    if tail:
        segments.append(tail)
    if len(segments) < 2:
        return [sentence]  # no safe cut point exists
    pieces: list[str] = []
    current = ""
    for segment in segments:
        # A single segment longer than the budget is still kept whole: the alternative is
        # cutting mid-phrase, and a mispronounced amount is worse than a long one.
        if current and len(current) + len(segment) > max_chars:
            pieces.append(current.strip())
            current = segment
        else:
            current += segment
    if current.strip():
        pieces.append(current.strip())
    return pieces or [sentence]

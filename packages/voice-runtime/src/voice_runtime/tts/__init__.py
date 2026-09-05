"""Text-to-speech side of the split pipeline (specification 19.8 to 19.10).

Text exists before speech, always. Transactional facts are rendered from versioned locale
templates with no model involvement; conversational text passes an outbound guard that
tokenizes exactly as the chunker does; synthesis is cancellable between chunks and after
every await by a generation counter.
"""

from .chunker import SentenceChunker, chunk_for_speech
from .guard import GuardVerdict, Refusal, SpeechGuard
from .synth import (
    FakeSynthesizer,
    Speaker,
    SpeakResult,
    SpeechChunk,
    SpeechGeneration,
    SpeechSink,
    SpeechSynthesizer,
    VoiceSpec,
    voice_for,
)
from .templates import (
    Locale,
    RenderedSpeech,
    format_money_digits,
    money_to_words,
    render_decision,
)
from .tokenizer import SENTENCE_END, split_sentences

__all__ = [
    "SENTENCE_END",
    "FakeSynthesizer",
    "GuardVerdict",
    "Locale",
    "Refusal",
    "RenderedSpeech",
    "SentenceChunker",
    "SpeakResult",
    "Speaker",
    "SpeechChunk",
    "SpeechGeneration",
    "SpeechGuard",
    "SpeechSink",
    "SpeechSynthesizer",
    "VoiceSpec",
    "chunk_for_speech",
    "format_money_digits",
    "money_to_words",
    "render_decision",
    "split_sentences",
    "voice_for",
]

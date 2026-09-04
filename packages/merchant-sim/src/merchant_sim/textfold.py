"""Deterministic text normalization, tokenization and Hinglish folding.

This module is the reason ``doodh``, ``dodh``, ``dudh`` and ``दूध`` all find milk without
a model in the loop. Every function here is pure: same input, same output, forever. That
matters because search grounding is evidence -- a demo replayed next month must return the
same product IDs in the same order.

THREE LAYERS
------------
1. :func:`normalize` -- Unicode hygiene. NFKC, casefold, and removal of format characters
   (zero-width joiners, soft hyphens, byte-order marks). This is also a grounding defense:
   specification 20.3 requires catalogue input to be scanned for hidden Unicode, and a
   product name carrying an invisible character must not become unsearchable or
   un-comparable because of it.

2. :func:`tokenize` -- word splitting that understands Devanagari. Devanagari vowel signs
   are combining marks (Unicode category ``Mn``/``Mc``) and ``str.isalnum()`` is False for
   them, so a naive "split on non-alphanumeric" shatters ``दूध`` into ``द`` and ``ध``. The
   splitter treats combining marks as word-forming.

3. :func:`fold_hinglish` -- collapses romanization variance. Hindi written in Latin script
   has no spelling standard: the same word is typed ``doodh``/``dudh``/``duudh``,
   ``sabzi``/``sabji``, ``chawal``/``chaval``, ``cheeni``/``chini``. The fold maps all of
   them onto one key. Devanagari is returned unchanged -- it is already unambiguous, and
   the fold's vowel rules are romanization rules that would be nonsense applied to it.

The fold is deliberately lossy. It is applied to BOTH the index and the query, so a
collision widens a result set rather than corrupting one; the scorer in ``search`` keeps
exact matches ranked above folded ones so the extra hits never displace the right answer.
"""

from __future__ import annotations

import unicodedata
from typing import Final

__all__ = [
    "STOPWORDS",
    "fold_hinglish",
    "has_devanagari",
    "normalize",
    "tokenize",
    "within_edit_distance_one",
]

# Devanagari and Devanagari Extended. A token containing any of these is treated as
# native script and exempted from the romanization fold.
_DEVANAGARI_RANGES: Final[tuple[tuple[int, int], ...]] = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))

# Apostrophes are removed rather than treated as separators: "Haldiram's" must tokenize to
# one term, not to "haldiram" plus a stray "s" that matches every other possessive brand.
_APOSTROPHES: Final[str] = "'’ʼ՚"

# Function words carry no product signal. Removing them lets a spoken sentence
# ("mujhe do litre doodh chahiye") reduce to the terms that actually identify a product.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # Hinglish / Hindi function words
        "ka",
        "ki",
        "ke",
        "ko",
        "se",
        "me",
        "mein",
        "par",
        "aur",
        "hai",
        "hain",
        "chahiye",
        "mujhe",
        "mera",
        "meri",
        "de",
        "do",
        "dena",
        "kuch",
        "thoda",
        "bhi",
        "ek",
        # Devanagari function words
        "का",
        "की",
        "के",
        "को",
        "से",
        "में",
        "और",
        "है",
        "हैं",
        "चाहिए",
        "मुझे",
        "मेरा",
        "मेरी",
        "कुछ",
        "थोड़ा",
        "भी",
        # English function words
        "a",
        "an",
        "the",
        "of",
        "and",
        "for",
        "with",
        "some",
        "please",
        "want",
        "need",
        "buy",
        "get",
        "add",
    }
)

# Marker standing in for "ch" while a bare "c" is rewritten to "k". Without it, "chini"
# would become "khini" and stop matching "cheeni". U+0001 cannot collide with real text:
# :func:`normalize` deletes every Cc control character, and the fold normalizes its input
# before the marker is introduced.
_CH_MARKER: Final[str] = "\x01"

# Consonant equivalences observed in Indian romanization. Each entry is a spelling
# variant, never a phonetic claim: "z"/"j" (sabzi/sabji), "w"/"v" (chawal/chaval),
# "ph"/"f" (phal/fal), "q"/"k" (qeema/keema).
_CONSONANT_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("z", "j"),
    ("w", "v"),
    ("q", "k"),
    ("x", "ks"),
)


def has_devanagari(text: str) -> bool:
    """True when any character lies in a Devanagari block."""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _DEVANAGARI_RANGES) for ch in text)


def normalize(text: str) -> str:
    """Unicode-normalize, casefold and strip invisible characters.

    Guarantees: the result contains no format characters (category ``Cf``), no apostrophe
    variants, no runs of whitespace, and is in NFKC. Compatibility normalization is what
    turns a non-breaking space into a space and ``Nescafé``'s composed and decomposed
    spellings into one string.

    Refuses nothing -- normalization never fails; an empty or all-invisible input
    normalizes to the empty string, which downstream code treats as "no query".
    """
    normalized = unicodedata.normalize("NFKC", text)
    # Cf covers ZWSP, ZWNJ, ZWJ, soft hyphen, BOM and the bidi overrides; Cc covers the
    # C0/C1 control characters. Neither may survive into an index key or a comparison:
    # invisible characters are exactly how a poisoned catalogue record hides a second
    # spelling of itself (specification 20.3). Control characters that are whitespace
    # become a space so they still separate words.
    stripped = "".join(
        ch if unicodedata.category(ch) not in ("Cf", "Cc") else (" " if ch.isspace() else "")
        for ch in normalized
    )
    for apostrophe in _APOSTROPHES:
        stripped = stripped.replace(apostrophe, "")
    # Casefolding can denormalize, so normalize once more afterwards.
    folded = unicodedata.normalize("NFKC", stripped.casefold())
    return " ".join(folded.split())


def _is_word_char(ch: str) -> bool:
    # Mn/Mc are Devanagari matras and the virama; they belong to the word they attach to.
    return ch.isalnum() or unicodedata.category(ch) in ("Mn", "Mc")


def tokenize(text: str, *, drop_stopwords: bool = True) -> tuple[str, ...]:
    """Split normalized text into search tokens.

    Guarantees: tokens contain only word characters, single-character Latin fragments are
    discarded (they are punctuation debris such as the ``s`` in ``50-50 g``, never a
    product signal), and digits are preserved because pack sizes like ``500`` and brand
    names like ``50-50`` are real query terms.
    """
    tokens: list[str] = []
    current: list[str] = []
    for ch in normalize(text):
        if _is_word_char(ch):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))

    kept = [
        tok
        for tok in tokens
        # A one-character Latin token is debris; a one-character Devanagari token can be a
        # real syllable, so length is only a disqualifier for Latin script.
        if not (len(tok) == 1 and tok.isalpha() and not has_devanagari(tok))
    ]
    if drop_stopwords:
        kept = [tok for tok in kept if tok not in STOPWORDS]
    return tuple(kept)


def _collapse_runs(text: str) -> str:
    """``duudh`` -> ``dudh``. Doubled letters in romanized Hindi mark vowel length, which
    typists apply inconsistently; they never distinguish two products."""
    out: list[str] = []
    for ch in text:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def fold_hinglish(token: str) -> str:
    """Collapse one romanized token onto a spelling-variance-free key.

    Guarantees:

    * A token containing Devanagari is returned normalized but otherwise untouched.
    * Latin tokens lose diacritics (``nescafé`` -> ``nescafe``), then have ``ph`` -> ``f``,
      ``c`` -> ``k`` (except inside ``ch``), ``z`` -> ``j``, ``w`` -> ``v``, ``q`` -> ``k``,
      ``x`` -> ``ks``, a trailing ``y`` -> ``i``, then ``e`` -> ``i`` and ``o`` -> ``u``,
      then runs of one character collapsed.
    * The result is idempotent: ``fold(fold(t)) == fold(t)``.

    The vowel rule is the load-bearing one. Devanagari's romanization ambiguity is
    overwhelmingly between e/i and o/u -- ``cheeni``/``chini``, ``doodh``/``dudh``,
    ``ande``/``anda``, ``lehsun``/``lahsun`` -- so folding to a three-vowel system removes
    the variance without touching the consonant skeleton that actually identifies a word.
    """
    token = normalize(token)
    if has_devanagari(token):
        return token

    # Strip diacritics from Latin only: the same pass over Devanagari would delete the
    # vowel signs that make a word a word.
    decomposed = unicodedata.normalize("NFD", token)
    text = unicodedata.normalize(
        "NFC", "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    )

    text = text.replace("ph", "f")
    text = text.replace("ch", _CH_MARKER)
    text = text.replace("c", "k")
    for src, dst in _CONSONANT_MAP:
        text = text.replace(src, dst)
    if text.endswith("y"):
        text = text[:-1] + "i"
    text = text.replace("e", "i").replace("o", "u")
    text = text.replace(_CH_MARKER, "ch")
    return _collapse_runs(text)


def within_edit_distance_one(left: str, right: str) -> bool:
    """True when one insertion, deletion, substitution or adjacent transposition separates
    the two strings.

    Bounded at one on purpose. It is computed directly rather than by a full
    Damerau-Levenshtein table, so it is O(n) and cannot be accidentally loosened by a
    threshold constant drifting upward. Distance two would start merging genuinely
    different products (``dal`` and ``dahi`` are distance two apart after folding), and a
    search that quietly substitutes one grocery item for another is worse than one that
    finds nothing.
    """
    if left == right:
        return True
    len_left, len_right = len(left), len(right)
    if abs(len_left - len_right) > 1:
        return False

    if len_left == len_right:
        diffs = [i for i in range(len_left) if left[i] != right[i]]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            first, second = diffs
            return left[first] == right[second] and left[second] == right[first]
        return False

    longer, shorter = (left, right) if len_left > len_right else (right, left)
    i = j = 0
    skipped = False
    while i < len(longer) and j < len(shorter):
        if longer[i] == shorter[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            i += 1
    return True

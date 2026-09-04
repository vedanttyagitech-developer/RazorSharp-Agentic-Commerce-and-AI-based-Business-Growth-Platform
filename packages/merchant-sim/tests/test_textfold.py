"""Tokenizer and Hinglish fold.

Every test here pins a behaviour that a real query depends on. If the fold is loosened,
the collision tests fail; if it is tightened, the equivalence tests fail.
"""

from __future__ import annotations

import pytest
from merchant_sim.textfold import (
    fold_hinglish,
    has_devanagari,
    normalize,
    tokenize,
    within_edit_distance_one,
)


class TestNormalize:
    def test_strips_zero_width_and_soft_hyphen(self) -> None:
        # These are invisible. If normalization stops removing them, a catalogue row
        # carrying one silently stops matching its own name while looking correct.
        assert normalize("Nirma Washing​ Powder­") == "nirma washing powder"

    def test_strips_control_characters(self) -> None:
        assert normalize("milk\x00\x01\x02") == "milk"
        assert normalize("brown\tbread") == "brown bread"

    def test_nfkc_folds_compatibility_forms(self) -> None:
        # U+FB01 LATIN SMALL LIGATURE FI, and a full-width digit.
        assert normalize("coﬁ") == "cofi"
        assert normalize("５００ ml") == "500 ml"

    def test_removes_apostrophes_rather_than_splitting_on_them(self) -> None:
        # "haldiram s" would leave a bare "s" matching every possessive brand.
        assert normalize("Haldiram's") == "haldirams"
        assert normalize("Mother’s Recipe") == "mothers recipe"

    def test_collapses_whitespace_runs_and_strips_the_edges(self) -> None:
        # A documented guarantee of normalize(). It is load bearing for the phrase bonus
        # in search, which asks whether the whole normalized query occurs inside an index
        # phrase: a leading space or a doubled space makes that substring test miss a
        # product the buyer plainly named.
        assert normalize("  Amul   Gold  Milk  ") == "amul gold milk"
        assert normalize("\tdoodh\n") == "doodh"

    def test_is_idempotent(self) -> None:
        once = normalize("Nestlé A+ Slim Milk")
        assert normalize(once) == once


class TestTokenize:
    def test_keeps_devanagari_words_whole(self) -> None:
        # Devanagari vowel signs are combining marks and str.isalnum() is False for them.
        # A naive splitter shatters this word into its consonants.
        assert tokenize("दूध") == ("दूध",)
        assert tokenize("हरी मिर्च") == ("हरी", "मिर्च")

    def test_splits_awkward_product_names(self) -> None:
        assert tokenize("Maggi 2-Minute Masala Noodles (Pack of 4)") == (
            "maggi",
            "2",
            "minute",
            "masala",
            "noodles",
            "pack",
            "4",
        )
        assert tokenize("Tata Salt — Iodised, 1 kg") == ("tata", "salt", "iodised", "1", "kg")
        assert tokenize("Dettol Original Soap — 4 × 125 g") == (
            "dettol",
            "original",
            "soap",
            "4",
            "125",
        )

    def test_drops_single_letter_latin_debris_but_keeps_digits(self) -> None:
        # The trailing "g" of a pack size is noise; the "4" of "Pack of 4" is not.
        assert "g" not in tokenize("Chocos 375 g")
        assert "375" in tokenize("Chocos 375 g")

    def test_drops_function_words_so_a_spoken_sentence_reduces_to_products(self) -> None:
        assert tokenize("mujhe do litre doodh chahiye") == ("litre", "doodh")
        assert tokenize("मुझे दूध चाहिए") == ("दूध",)

    def test_all_stopword_query_yields_no_tokens(self) -> None:
        # This is what stops "please add some of the" from matching the whole catalogue.
        assert tokenize("please add some of the") == ()


class TestFoldHinglish:
    @pytest.mark.parametrize(
        ("variants", "label"),
        [
            (("doodh", "dodh", "dudh", "duudh"), "milk"),
            (("cheeni", "chini", "chiini"), "sugar"),
            (("aloo", "alu", "aaloo"), "potato"),
            (("pyaz", "pyaaz", "pyaj"), "onion"),
            (("chawal", "chaval", "chawwal"), "rice"),
            (("paneer", "panir", "paniir"), "paneer"),
            (("makkhan", "makhan"), "butter"),
            (("sabzi", "sabji"), "vegetable"),
            (("phal", "fal"), "fruit"),
            (("chai", "chay"), "tea"),
            (("pani", "paani"), "water"),
            (("coffee", "cofee"), "coffee"),
            (("atta", "aata"), "flour"),
            (("haldi", "haldii"), "turmeric"),
        ],
    )
    def test_romanization_variants_collapse_to_one_key(
        self, variants: tuple[str, ...], label: str
    ) -> None:
        folded = {fold_hinglish(v) for v in variants}
        assert len(folded) == 1, f"{label}: {variants} folded to {folded}"

    def test_devanagari_is_returned_unchanged(self) -> None:
        # The fold's vowel rules are romanization rules. Applied to Devanagari they would
        # be nonsense, and stripping combining marks would delete the vowels outright.
        assert fold_hinglish("दूध") == "दूध"
        assert fold_hinglish("चीनी") == "चीनी"

    def test_diacritics_are_stripped_from_latin_only(self) -> None:
        assert fold_hinglish("Nescafé") == fold_hinglish("nescafe")
        assert has_devanagari("दूध") and not has_devanagari("doodh")

    def test_is_idempotent(self) -> None:
        for word in ("doodh", "cheeni", "nescafé", "50-50", "दूध", "chawal"):
            once = fold_hinglish(word)
            assert fold_hinglish(once) == once, word

    def test_does_not_merge_different_groceries(self) -> None:
        # A fold aggressive enough to merge these would make search substitute one
        # grocery item for another, which is worse than finding nothing.
        distinct = ["dal", "dahi", "atta", "achar", "namak", "chawal", "maggi", "haldi"]
        assert len({fold_hinglish(w) for w in distinct}) == len(distinct)


class TestEditDistanceOne:
    def test_accepts_the_four_single_edits(self) -> None:
        assert within_edit_distance_one("dudh", "dudh")  # identical
        assert within_edit_distance_one("dudh", "duhh")  # substitution
        assert within_edit_distance_one("dudh", "dudhh")  # insertion
        assert within_edit_distance_one("dudh", "duh")  # deletion
        assert within_edit_distance_one("dudh", "duhd")  # adjacent transposition

    def test_refuses_two_edits(self) -> None:
        assert not within_edit_distance_one("dudh", "dahi")
        assert not within_edit_distance_one("dudh", "duu")
        assert not within_edit_distance_one("chana", "chawal")

    def test_refuses_non_adjacent_transposition(self) -> None:
        # "abcd" -> "dbca" swaps characters two apart; that is two edits, not one.
        assert not within_edit_distance_one("abcd", "dbca")

    def test_is_symmetric(self) -> None:
        pairs = [("dudh", "duh"), ("chana", "chan"), ("atta", "atat"), ("a", "abc")]
        for left, right in pairs:
            assert within_edit_distance_one(left, right) == within_edit_distance_one(right, left)

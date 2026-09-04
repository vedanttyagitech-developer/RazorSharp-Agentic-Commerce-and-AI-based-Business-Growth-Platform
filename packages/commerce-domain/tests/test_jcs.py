"""RFC 8785 canonicalization.

A wrong byte here produces a signature that verifies locally and fails at a counterparty,
so these tests target the specific places an implementation plausibly goes wrong.
"""

import pytest
from commerce_domain import CanonicalizationError, canonicalize, canonicalize_str
from hypothesis import given
from hypothesis import strategies as st


class TestKeyOrdering:
    def test_keys_are_sorted(self):
        assert canonicalize_str({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_nested_objects_are_sorted(self):
        assert canonicalize_str({"z": {"y": 1, "x": 2}, "a": 3}) == '{"a":3,"z":{"x":2,"y":1}}'

    def test_utf16_order_not_codepoint_order(self):
        """The case separating a correct implementation from a plausible one.

        U+FB00 is in the BMP and U+1F600 is supplementary, so U+FB00 has the lower code
        point and Python's default sort puts it first. In UTF-16, U+1F600 encodes as the
        surrogate pair D83D DE00, and 0xD83D < 0xFB00, so RFC 8785 requires the emoji
        first. An implementation that sorts by code point gets this backwards.
        """
        emoji = "\U0001f600"
        ligature = "ﬀ"
        out = canonicalize_str({emoji: 1, ligature: 2})
        assert out.index(emoji) < out.index(ligature), out
        # Confirm the two orderings genuinely disagree, so this test has teeth.
        assert sorted([emoji, ligature]) == [ligature, emoji]

    def test_array_order_is_preserved(self):
        assert canonicalize_str([3, 1, 2]) == "[3,1,2]"


class TestEscaping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('say "hi"', '"say \\"hi\\""'),
            ("back\\slash", '"back\\\\slash"'),
            ("tab\there", '"tab\\there"'),
            ("line\nbreak", '"line\\nbreak"'),
            ("ret\rurn", '"ret\\rurn"'),
            ("back\bspace", '"back\\bspace"'),
            ("form\ffeed", '"form\\ffeed"'),
        ],
    )
    def test_two_character_escapes(self, raw, expected):
        assert canonicalize_str(raw) == expected

    @pytest.mark.parametrize("codepoint", [0x00, 0x01, 0x07, 0x0B, 0x0E, 0x1F])
    def test_other_control_chars_use_lowercase_u_form(self, codepoint):
        assert canonicalize_str(chr(codepoint)) == f'"\\u{codepoint:04x}"'

    def test_hex_escape_is_lowercase_not_uppercase(self):
        assert canonicalize_str(chr(0x1F)) == '"\\u001f"'

    def test_non_ascii_is_literal_utf8_not_escaped(self):
        # RFC 8785 emits UTF-8 directly; only control chars, quote and backslash escape.
        assert canonicalize({"k": "₹395"}) == '{"k":"₹395"}'.encode()

    def test_del_char_is_not_escaped(self):
        # 0x7F is not a C0 control; RFC 8785 leaves it literal.
        assert canonicalize_str(chr(0x7F)) == f'"{chr(0x7F)}"'


class TestNumberProfile:
    def test_float_is_refused_with_actionable_message(self):
        with pytest.raises(CanonicalizationError, match="integers only"):
            canonicalize({"total": 395.00})

    def test_float_nested_in_array_reports_its_path(self):
        with pytest.raises(CanonicalizationError, match=r"\$\.items\[1\]"):
            canonicalize({"items": [1, 2.5]})

    def test_booleans_are_not_integers(self):
        assert canonicalize_str({"a": True, "b": False, "c": 1}) == '{"a":true,"b":false,"c":1}'

    def test_null(self):
        assert canonicalize_str({"a": None}) == '{"a":null}'

    def test_large_integers_are_exact(self):
        big = 9_007_199_254_740_993  # beyond float64 integer precision
        assert canonicalize_str({"n": big}) == '{"n":9007199254740993}'

    def test_negative_integer(self):
        assert canonicalize_str({"delta": -5500}) == '{"delta":-5500}'


class TestDeterminism:
    def test_insertion_order_does_not_matter(self):
        a = {"total": 39500, "currency": "INR", "version": 7}
        b = {"version": 7, "currency": "INR", "total": 39500}
        assert canonicalize(a) == canonicalize(b)

    def test_output_is_utf8_bytes(self):
        assert isinstance(canonicalize({"a": 1}), bytes)

    def test_no_insignificant_whitespace(self):
        assert b" " not in canonicalize({"a": 1, "b": [1, 2]})

    @given(
        st.dictionaries(
            st.text(min_size=1, max_size=8),
            st.one_of(st.integers(), st.text(max_size=8), st.booleans(), st.none()),
            max_size=6,
        )
    )
    def test_property_stable_under_reordering(self, data):
        shuffled = dict(reversed(list(data.items())))
        assert canonicalize(data) == canonicalize(shuffled)


class TestRejections:
    def test_non_string_key_is_refused(self):
        with pytest.raises(CanonicalizationError, match="non-string object key"):
            canonicalize({1: "a"})

    def test_unknown_type_is_refused(self):
        with pytest.raises(CanonicalizationError, match="cannot canonicalize"):
            canonicalize({"a": object()})

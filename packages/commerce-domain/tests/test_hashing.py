"""The canonical hash is a frozen contract: stored approvals depend on it never moving."""

from commerce_domain import b64url, b64url_decode, canonical_hash, canonicalize, sha256_b64url


class TestBase64Url:
    def test_roundtrip(self):
        raw = bytes(range(256))
        assert b64url_decode(b64url(raw)) == raw

    def test_no_padding_and_url_safe(self):
        out = b64url(b"\xff\xfe\xfd")
        assert "=" not in out
        assert "+" not in out
        assert "/" not in out


class TestCanonicalHash:
    def test_stable_across_key_order(self):
        a = {"checkout_id": "c1", "version": 7, "total_minor": 39500, "currency": "INR"}
        b = {"currency": "INR", "total_minor": 39500, "version": 7, "checkout_id": "c1"}
        assert canonical_hash(a) == canonical_hash(b)

    def test_one_paisa_changes_the_hash(self):
        """The invariant behind 'a one-paisa rounding difference fails admission'."""
        base = {"total_minor": 39500, "currency": "INR"}
        drift = {"total_minor": 39501, "currency": "INR"}
        assert canonical_hash(base) != canonical_hash(drift)

    def test_version_bump_changes_the_hash(self):
        """Version N and version N+1 can never share an approval."""
        v7 = {"checkout_id": "c1", "version": 7, "total_minor": 34000}
        v8 = {"checkout_id": "c1", "version": 8, "total_minor": 34000}
        assert canonical_hash(v7) != canonical_hash(v8)

    def test_hash_is_sha256_over_jcs_bytes(self):
        payload = {"b": 2, "a": 1}
        assert canonical_hash(payload) == sha256_b64url(canonicalize(payload))

    def test_frozen_regression_vector(self):
        """Regression anchor. If this changes, every stored approval becomes unverifiable."""
        payload = {"currency": "INR", "total_minor": 39500, "version": 7}
        expected_bytes = b'{"currency":"INR","total_minor":39500,"version":7}'
        assert canonicalize(payload) == expected_bytes
        assert canonical_hash(payload) == sha256_b64url(expected_bytes)

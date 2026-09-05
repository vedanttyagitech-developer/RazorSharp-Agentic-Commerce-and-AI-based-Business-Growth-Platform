"""Redaction is structural: the secret cannot be logged, not merely is not.

Every test here passes the secret **deliberately**, because that is the only interesting
case. A redactor that works when nobody tries to defeat it is a coding convention. The
question is what happens when a developer at two in the morning writes exactly the line
the rule forbids -- and the answer must be that the value does not appear, without anybody
having remembered anything.
"""

from __future__ import annotations

import pytest
from platform_observability import REDACTED, Secret, redact_fields, redact_value, scrub_text
from platform_observability.redaction import (
    MAX_VALUE_LENGTH,
    digest_of,
    is_denied_name,
)

#: Luhn-valid and famous. A real test card, not a real card.
TEST_PAN = "4111111111111111"


class TestDeniedByName:
    """A field is refused by what it is called, whatever it happens to hold."""

    @pytest.mark.parametrize(
        "name",
        [
            "token",
            "lease_token",
            "session_token",
            "authorization",
            "razorpay_signature",
            "x_razorpay_signature",
            "webhook_signature",
            "hmac",
            "key_secret",
            "razorpay_key_secret",
            "api_key",
            "password",
            "card_number",
            "pan",
            "cvv",
            "raw_body",
            "request_body",
            "payload",
            "buyer_email",
            "phone_number",
            "delivery_address",
            "upi_vpa",
            "cookie",
            "private_key",
        ],
    )
    def test_a_denied_name_yields_the_marker(self, name: str) -> None:
        assert redact_value(name, "definitely-the-secret-value") == REDACTED
        assert redact_value(name, 42) == REDACTED

    @pytest.mark.parametrize(
        "name",
        [
            "body_digest",
            "self_hash",
            "prev_hash",
            "payload_hash",
            "receipt_hash",
            "policy_receipt_hash",
            "checkout_hash",
            "idempotency_key",
            "dedup_key",
            "signing_key_id",
        ],
    )
    def test_hashes_and_references_survive(self, name: str) -> None:
        """A hash chain that cannot be logged cannot be investigated (spec 26.2). The
        digest of a body is not the body, and this is where that distinction is kept."""
        digest = "a" * 64
        assert redact_value(name, digest) == digest
        assert not is_denied_name(name)

    def test_a_denied_name_is_matched_case_insensitively(self) -> None:
        assert redact_value("Authorization", "Bearer abc") == REDACTED
        assert redact_value("X-Razorpay-Signature", "deadbeef") == REDACTED

    def test_provider_references_survive_because_the_proof_chain_needs_them(self) -> None:
        """``pay_...`` and ``order_...`` are links in the Money Action Proof Chain (spec
        26.4) and are exactly what an operator reconstructs a journey from."""
        assert redact_value("razorpay_payment_id", "pay_MkL9xQ2vRt3Zc1") == "pay_MkL9xQ2vRt3Zc1"
        assert redact_value("razorpay_order_id", "order_MkL9xQ2vRt3Zc1") == "order_MkL9xQ2vRt3Zc1"


class TestDeniedByShape:
    """The layer that covers code which has never heard of this package."""

    def test_a_card_number_is_removed_from_a_harmless_field(self) -> None:
        assert TEST_PAN not in str(redact_value("note", f"buyer said {TEST_PAN} is declined"))
        assert "[redacted:pan]" in str(redact_value("note", f"buyer said {TEST_PAN}"))

    @pytest.mark.parametrize("separator", ["", " ", "-"])
    def test_a_grouped_card_number_is_removed(self, separator: str) -> None:
        grouped = separator.join(("4111", "1111", "1111", "1111"))
        assert "4111" not in scrub_text(f"pan={grouped}")

    def test_a_luhn_invalid_digit_run_survives(self) -> None:
        """Not everything long and numeric is a card. An amount in minor units, a
        millisecond timestamp and an order reference all have to stay readable."""
        assert scrub_text("amount_minor=129950") == "amount_minor=129950"
        assert scrub_text("occurred_at_ms=1757068800000") == "occurred_at_ms=1757068800000"
        assert scrub_text("seq=1234567890123456") == "seq=1234567890123456"

    def test_a_uuid_survives(self) -> None:
        identifier = "01a06faa-eb72-72a8-b989-8abdc6662355"
        assert scrub_text(identifier) == identifier

    @pytest.mark.parametrize(
        "text",
        [
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefgh",
            "Basic dXNlcjpwYXNzd29yZA==",
            "Token abcdef1234567890",
        ],
    )
    def test_an_authorization_scheme_is_stripped_of_its_credential(self, text: str) -> None:
        scrubbed = scrub_text(text)
        assert REDACTED in scrubbed
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in scrubbed
        assert "dXNlcjpwYXNzd29yZA==" not in scrubbed

    def test_razorpay_credentials_are_removed_in_both_halves(self) -> None:
        """The key id is not secret and is also worthless in a log; telling it from the
        secret by eye is the mistake this exists to make impossible."""
        assert "rzp_test_" not in scrub_text("using rzp_test_1DP5mmOlF5G5ag")
        assert "rzp_live_" not in scrub_text("using rzp_live_1DP5mmOlF5G5ag")

    def test_a_pem_private_key_is_removed_whole(self) -> None:
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAx4fm7dngEmOULNmAs1IGZ9Apfzh+BGPQTSbTP1Q=\n"
            "-----END RSA PRIVATE KEY-----"
        )
        scrubbed = scrub_text(f"loaded key {pem} ok")
        assert "MIIEowIBAAKCAQEA" not in scrubbed
        assert scrubbed == "loaded key [redacted:private-key] ok"

    def test_a_sha256_hash_is_not_mistaken_for_a_signature(self) -> None:
        """Both are 64 hex characters. Scrubbing by shape would take the audit chain's own
        links with it, so signatures are excluded by name and hashes are left alone."""
        digest = digest_of(b"a webhook body")
        assert len(digest) == 64
        assert scrub_text(digest) == digest


class TestUnrepresentableStructures:
    """A whole webhook body is not a thing a log line can contain."""

    def test_a_mapping_becomes_a_marker_with_a_digest(self) -> None:
        body = {"event": "payment.captured", "payload": {"payment": {"id": "pay_x", "amount": 1}}}
        rendered = str(redact_value("evidence", body))
        assert "payment.captured" not in rendered
        assert rendered.startswith("[redacted:dict sha256=")

    def test_bytes_become_a_marker_with_a_length_and_a_digest(self) -> None:
        raw = b'{"event":"payment.captured"}'
        rendered = str(redact_value("evidence", raw))
        assert "payment.captured" not in rendered
        assert f"len={len(raw)}" in rendered
        assert digest_of(raw) in rendered

    def test_the_digest_is_the_join_back_to_the_evidence(self) -> None:
        """``webhook_inbox.body_digest`` holds the same SHA-256 over the same bytes, so an
        operator can still get from a log line to the row holding the body."""
        raw = b'{"event":"payment.captured"}'
        marker = str(redact_value("evidence", raw))
        assert digest_of(raw) in marker

    def test_an_oversize_string_is_replaced_rather_than_truncated(self) -> None:
        """Truncation is the wrong instinct: the first 256 characters of a card-on-file
        blob is still a card number."""
        long_value = f"{TEST_PAN} " * 200
        rendered = str(redact_value("note", long_value))
        assert TEST_PAN not in rendered
        assert rendered.startswith("[redacted:oversize chars=")
        assert len(rendered) < MAX_VALUE_LENGTH + 100

    def test_an_arbitrary_object_never_becomes_its_repr(self) -> None:
        class Row:
            def __repr__(self) -> str:
                return "Row(card_number='4111111111111111')"

        rendered = str(redact_value("row", Row()))
        assert TEST_PAN not in rendered
        assert rendered.startswith("[redacted:Row sha256=")


class TestSecret:
    """A value that can be carried through code and never rendered."""

    def test_every_rendering_route_yields_the_marker(self) -> None:
        secret = Secret("rzp_live_supersecret")
        assert str(secret) == REDACTED
        assert repr(secret) == REDACTED
        assert f"{secret}" == REDACTED
        assert f"{secret:>40}" == REDACTED
        # Percent formatting on purpose: it is the interpolation stdlib logging uses,
        # and it reaches __str__ by a different route than an f-string does.
        assert "%s" % (secret,) == REDACTED  # noqa: UP031
        assert "".join([f"{secret!r}", f"{secret!s}"]) == REDACTED * 2

    def test_reveal_is_the_only_way_out(self) -> None:
        secret = Secret("the-value")
        assert secret.reveal() == "the-value"

    def test_a_secret_in_a_log_field_is_the_marker(self) -> None:
        assert redact_value("anything", Secret("the-value")) == REDACTED

    def test_secrets_compare_and_hash_by_their_value(self) -> None:
        assert Secret("a") == Secret("a")
        assert Secret("a") != Secret("b")
        assert len({Secret("a"), Secret("a")}) == 1
        assert Secret("a") != "a"


class TestFieldMapping:
    def test_keys_are_scrubbed_as_well_as_values(self) -> None:
        """A caller building a key from data would otherwise smuggle the data into the
        half of the pair nobody thought to redact."""
        fields = redact_fields({f"header_{TEST_PAN}": "x"})
        assert not any(TEST_PAN in key for key in fields)

    def test_scalars_pass_through_intact(self) -> None:
        fields = redact_fields(
            {"attempt": 2, "elapsed": 0.5, "completed": True, "detail": None, "code": "OK"}
        )
        assert fields == {
            "attempt": 2,
            "elapsed": 0.5,
            "completed": True,
            "detail": None,
            "code": "OK",
        }

    def test_a_bool_stays_a_bool(self) -> None:
        """``isinstance(True, int)`` is true, and a JSON ``1`` where a reader expects
        ``true`` is the kind of thing a dashboard query gets wrong silently."""
        assert redact_fields({"allowed": True})["allowed"] is True

    def test_redaction_never_raises(self) -> None:
        class Hostile:
            def __repr__(self) -> str:
                raise RuntimeError("no repr for you")

        assert redact_value("x", Hostile()) == REDACTED
